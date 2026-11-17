#!/usr/bin/env python3
"""
Checks for fix_plan.py and the live status files.

The plan is advice, so the tests are about its shape and its restraint: the
capture comes first, measuring comes before fixing, findings that are only
information do not get promoted to work, and the same suggestion is not made
three times. Nothing in this file is a measurement.

    python3 test_fix_plan.py
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "perfetto-triage", "scripts")


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fp, triage, vr = load("fix_plan"), load("triage"), load("visual_report")


def finding(kind, severity="medium", title=None, **extra):
    return dict({"kind": kind, "severity": severity, "area": "x",
                 "title": title or f"finding {kind}", "detail": "d", "action": "a"}, **extra)


def kinds(plan, priority=None):
    return [i["kind"] for i in plan["items"] if priority is None or i["priority"] == priority]


def test_every_finding_kind_triage_can_emit_is_known():
    emitted = {k for _, k in triage.KINDS}
    for kind in fp.FIXES:
        assert kind in emitted or kind in (
            "query_unfinished", "no_trace_sections", "sections_not_ranked",
            "late_frames_in_section", "handoff_compose-expert"), kind
    assert triage.kind_of("Startup is waiting-bound: main thread waits 55% of the time") == "startup_waiting"
    assert triage.kind_of("com.example.app: 31/32 frames janky (96.9%)") == "jank_rate"
    assert triage.kind_of("something new") == "other"


def test_a_bad_capture_comes_first_and_blocks_the_rest():
    plan = fp.build([finding("shader_compile", "high"), finding("debuggable_build", "blocker"),
                     finding("no_ttfd", "info")], {"verdict": "directional_only"})
    assert kinds(plan)[0] == "debuggable_build" and plan["items"][0]["priority"] == "first"
    assert all(i.get("blocked_by_capture") for i in plan["items"][1:]), plan["items"]
    assert plan["summary"].startswith("Fix the capture first (1 item)")
    assert [i["n"] for i in plan["items"]] == [1, 2, 3]
    soft = fp.build([finding("shader_compile", "high"), finding("build_unknown", "info")],
                    {"verdict": "directional_only"})
    assert kinds(soft)[0] == "build_unknown" and not soft["items"][1].get("blocked_by_capture")
    assert soft["summary"].startswith("Settle the capture question first (1 item)")


def test_measuring_comes_before_fixing():
    plan = fp.build([finding("shader_compile", "high"), finding("no_trace_sections", "medium"),
                     finding("no_ttfd", "info")], {"verdict": "trusted"})
    assert kinds(plan, "next") == ["no_trace_sections", "no_ttfd", "shader_compile"], kinds(plan)
    assert "sections in the before build too" in " ".join(plan["items"][0]["change"])


def test_information_only_findings_can_wait():
    plan = fp.build([finding("class_loading", "info"), finding("startup_waiting", "info"),
                     finding("jank_rate", "info")], {"verdict": "trusted"})
    assert kinds(plan, "next") == [] and kinds(plan, "later") == ["class_loading", "startup_waiting"]
    assert plan["summary"].startswith("Nothing in this trace needs fixing now. 2 optional items")
    assert all(i["why_later"] for i in plan["items"])
    assert fp.build([finding("jank_rate", "high")])["summary"] == "No fix to suggest from this trace."


def test_compose_suggestions_are_made_once():
    handoffs = [{"skill": "compose-expert"}]
    plan = fp.build([finding("jank_ui", "medium", "cost is on the UI thread"),
                     finding("layout_cost", "medium", "Measure/layout cost (945 ms)"),
                     finding("handoff_compose-expert", "info", "Compose work ranks")],
                    {"verdict": "trusted"}, None, handoffs)
    assert kinds(plan) == ["handoff_compose-expert"], kinds(plan)
    item = plan["items"][0]
    assert item["skill"] == "compose-expert"
    assert item["also_because"] == ["cost is on the UI thread", "Measure/layout cost (945 ms)"]
    without = fp.build([finding("jank_ui", "medium"), finding("layout_cost", "medium")])
    assert kinds(without) == ["jank_ui", "layout_cost"], "without Compose work they stay separate"


def test_where_and_check_come_from_the_evidence():
    code_map = {"slices": [{"name": "checkout.pay", "evidence": "app/Checkout.kt:41"}]}
    plan = fp.build([
        finding("late_frames_in_section", "medium", "22 of 40 late frames overlap checkout.pay",
                section="checkout.pay"),
        finding("main_binder", "medium",
                "Main-thread binder: AIDL::java::android.app.IFoo::bar::server costs 34 ms"),
        finding("no_ttfd", "info")], {"verdict": "trusted"}, code_map)
    by = {i["kind"]: i for i in plan["items"]}
    section = by["late_frames_in_section"]
    assert section["where"] == ["app/Checkout.kt:41"] and "checkout.pay" in section["title"]
    assert section["check"][0] == "late frames inside checkout.pay"
    assert by["main_binder"]["where"] == ["AIDL::java::android.app.IFoo::bar::server"]
    assert by["no_ttfd"]["where"] == [] and "first screen" in by["no_ttfd"]["where_hint"]


def test_checks_name_metrics_remeasure_actually_prints():
    rm = load("remeasure")
    src = open(os.path.join(SCRIPTS, "remeasure.py")).read()
    for kind, fix in fp.FIXES.items():
        if fix["stage"] != "fix":
            continue
        for check in fix["check"]:
            if check.endswith(")") and ("(ms)" in check or "(%)" in check):
                assert f'"{check}"' in src, f"{kind}: remeasure.py has no metric called {check!r}"
    assert hasattr(rm, "run_metrics")


def test_markdown_and_html_carry_the_review_steps_and_escape():
    plan = fp.build([finding("main_binder", "medium",
                             "Main-thread binder: <b>IFoo</b>::bar costs 34 ms"),
                     finding("class_loading", "info")], {"verdict": "trusted"})
    md = fp.to_markdown(plan)
    assert md.startswith("## Suggested fixes, in order") and "### Review next" in md and "### Can wait" in md
    assert "not instructions" in md and "clear or likely" in md and "`<b>IFoo</b>::bar`" in md
    page = vr.fix_section(plan)
    assert "Before acting on any of these" in page and "<b>IFoo</b>" not in page
    assert "&lt;b&gt;IFoo&lt;/b&gt;::bar" in page and vr.fix_section(None) == ""
    assert fp.to_markdown(fp.build([])).strip().endswith("No fix to suggest from this trace.")


def test_status_files_say_what_is_running_and_then_stop_refreshing():
    out = tempfile.mkdtemp(prefix="status-")
    status = triage.Status(out, "/x/app-startup-1.pftrace", html=True)
    triage.STATUS = status
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            triage.log("[13/20] 23_jank_hot_slices: still running, 40s so far")
        line = open(os.path.join(out, "progress.txt")).read()
        assert line.startswith("running: [13/20] 23_jank_hot_slices: still running") and "elapsed" in line
        page = open(os.path.join(out, "report.html")).read()
        assert "Triage is running" in page and 'http-equiv="refresh"' in page
        assert "[13/20] 23_jank_hot_slices" in page and "app-startup-1.pftrace" in page
        status.update("stopped: RuntimeError: <boom>", state="failed")
        page = open(os.path.join(out, "report.html")).read()
        assert "Triage failed" in page and "refresh" not in page and "&lt;boom&gt;" in page
        assert open(os.path.join(out, "progress.txt")).read().startswith("failed: stopped")
        # once done, the status must not overwrite the real report
        with open(os.path.join(out, "report.html"), "w") as fh:
            fh.write("REAL REPORT")
        status.state = "done"
        with contextlib.redirect_stderr(io.StringIO()):
            triage.log("done in 3s")
        assert open(os.path.join(out, "report.html")).read() == "REAL REPORT"
        assert open(os.path.join(out, "progress.txt")).read().startswith("done: done in 3s")
    finally:
        triage.STATUS = None


def test_status_without_html_only_writes_the_one_line():
    out = tempfile.mkdtemp(prefix="status-")
    triage.Status(out, "t.pftrace", html=False).update("loading the trace")
    assert os.path.exists(os.path.join(out, "progress.txt"))
    assert not os.path.exists(os.path.join(out, "report.html"))


def test_old_phone_frame_gap_follows_its_severity():
    """On a cold-start capture the gap is information, so it must not jump the
    queue the way other measuring steps do. On a jank capture it is next."""
    info = fp.build([finding("frames_unsupported", "info")], {"verdict": "trusted"})
    assert kinds(info, "later") == ["frames_unsupported"], info["items"]
    assert "Nothing in this trace needs fixing now" in info["summary"]
    medium = fp.build([finding("frames_unsupported", "medium")], {"verdict": "trusted"})
    assert kinds(medium, "next") == ["frames_unsupported"]
    assert "Android 12" in medium["items"][0]["title"]


def test_heavy_jit_suggests_compiling_the_build_before_measuring():
    plan = fp.build([finding("jit_compile")], {"verdict": "trusted"})
    item = plan["items"][0]
    assert item["priority"] == "next" and "JIT compile (ms)" in item["check"]
    assert any("speed-profile" in c for c in item["change"])


CASES = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


def main():
    failed = 0
    for fn in CASES:
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {fn.__name__}: {type(e).__name__}: {str(e)[:400]}")
    print(f"\n{len(CASES) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
