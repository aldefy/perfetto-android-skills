#!/usr/bin/env python3
"""
Logic checks for triage.py's trust layer, on SYNTHETIC rows.

This is deliberately separate from run_eval.py. run_eval.py only uses real
fixtures from real incidents. These are hand-written rows that pin down the
decision table in health_status() and analyse(), including branches no real
fixture covers yet. A pass here proves the logic, not that a real trace
produces these rows. Nothing in this file is a measurement.

    python3 test_logic.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TRIAGE_PY = os.path.join(os.path.dirname(HERE), "perfetto-triage", "scripts", "triage.py")
spec = importlib.util.spec_from_file_location("triage", TRIAGE_PY)
triage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(triage)

PKG = "com.example.app"
COV_H = ["slices", "sched_rows", "frametimeline_rows", "blocked_reason_rows",
         "binder_slices", "processes", "trace_span_ms"]
STARTUP_H = ["startup_id", "package", "startup_type", "startup_ms", "ttid_ms", "ttfd_ms"]
BUILD_H = ["package_name", "version_code", "debuggable"]


def base(debuggable="0", startups=True, cov=None, health=None):
    """A healthy release startup capture, with knobs."""
    r = {
        "00_health": (["name", "value"], health or []),
        "01_coverage": (COV_H, [cov or ["1000", "500", "90", "40", "30", "800", "12000"]]),
        "10_startup_summary": (STARTUP_H, [["1", PKG, "cold", "900", "880", "950"]]
                               if startups else []),
        "20_jank_summary": (["process_name", "frames", "janky", "janky_pct", "app_jank",
                             "sf_jank", "worst_overrun_ms"],
                            [[PKG, "100", "20", "20.0", "18", "2", "40"]]),
    }
    if debuggable is not None:
        r["03_build_type"] = (BUILD_H, [[PKG, "42", debuggable]])
    return r


def run(results, pkg=PKG, mode="startup"):
    hs = triage.health_status(results, pkg, mode)
    return hs, triage.analyse(results, pkg, mode, hs)


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def release_build_is_trusted():
    hs, f = run(base())
    assert hs["build_type"] == "release", hs
    assert hs["verdict"] == "trusted", hs
    assert hs["trace_health"] == "pass" and hs["startup_capture"] == "ok", hs
    assert not any(x["directional_only"] for x in f), f
    assert not any("Debuggable" in x["title"] or "not verified" in x["title"] for x in f)


@case
def debuggable_build_is_a_blocker_and_taints_every_finding():
    hs, f = run(base(debuggable="1"))
    assert hs["build_type"] == "debuggable" and hs["verdict"] == "directional_only", hs
    assert f[0]["severity"] == "blocker" and "Debuggable build" in f[0]["title"], f[0]
    rest = [x for x in f if x["area"] != "trace"]
    assert rest and all(x["directional_only"] for x in rest), rest
    assert not f[0]["directional_only"]


@case
def debuggable_true_as_text_also_counts():
    hs, _ = run(base(debuggable="true"))
    assert hs["build_type"] == "debuggable", hs


@case
def missing_build_query_is_unknown_never_release():
    hs, f = run(base(debuggable=None))
    assert hs["build_type"] == "unknown" and hs["verdict"] == "directional_only", hs
    assert any(x["title"] == "Build type not verified" for x in f)


@case
def build_query_with_no_rows_is_unknown():
    r = base()
    r["03_build_type"] = (BUILD_H, [])
    hs, _ = run(r)
    assert hs["build_type"] == "unknown", hs


@case
def wildcard_package_cannot_decide_build_type():
    hs, _ = run(base(), pkg="*")
    assert hs["build_type"] == "unknown", hs


@case
def startup_mode_with_no_launch_is_missed():
    hs, f = run(base(startups=False), mode="startup")
    assert hs["startup_capture"] == "missed" and hs["verdict"] == "directional_only", hs
    hit = [x for x in f if x["title"] == "Startup capture missed the app launch"]
    assert hit and hit[0]["severity"] == "blocker", f


@case
def interactive_mode_with_no_launch_is_expected_and_quiet():
    hs, f = run(base(startups=False), mode="interactive")
    assert hs["startup_capture"] == "not_expected" and hs["verdict"] == "trusted", hs
    assert not any(x["area"] == "startup" for x in f), f


@case
def unknown_mode_with_no_launch_says_so_without_calling_it_a_failure():
    hs, f = run(base(startups=False), mode="unknown")
    assert hs["startup_capture"] == "unknown", hs
    hit = [x for x in f if x["title"] == "No app launch found in this trace"]
    assert hit and hit[0]["severity"] == "info", f


@case
def health_rows_mean_fail_and_empty_means_pass():
    hs, _ = run(base(health=[["traced_buf_chunks_overwritten", "12"]]))
    assert hs["trace_health"] == "fail" and hs["verdict"] == "not_trustworthy", hs
    hs, _ = run(base())
    assert hs["trace_health"] == "pass", hs


@case
def health_query_not_run_is_unknown_not_pass():
    r = base()
    del r["00_health"]
    hs, _ = run(r)
    assert hs["trace_health"] == "unknown" and hs["verdict"] == "directional_only", hs


@case
def coverage_zero_is_missing_and_sched_decides_the_verdict():
    hs, _ = run(base(cov=["1000", "500", "0", "40", "30", "800", "12000"]))
    assert hs["coverage"]["frametimeline"] == "missing", hs
    assert hs["coverage_gaps"] == ["frametimeline"] and hs["verdict"] == "trusted", hs
    hs, _ = run(base(cov=["1000", "0", "90", "40", "30", "800", "12000"]))
    assert hs["coverage"]["sched"] == "missing" and hs["verdict"] == "not_trustworthy", hs


@case
def mode_is_read_from_capture_sh_filenames_only():
    assert triage.infer_mode("/x/com.example.app-startup-171407.pftrace") == "startup"
    assert triage.infer_mode("traces/com.example.app-interactive-101010.pftrace") == "interactive"
    assert triage.infer_mode("startup.pftrace") == "unknown"
    assert triage.infer_mode("") == "unknown"


JANK_H = ["process_name", "frames", "janky", "janky_pct", "app_jank", "sf_jank",
          "worst_overrun_ms"]
SPLIT_H = ["process_name", "ui_thread_ms", "render_thread_ms", "vsync_delay_ms"]
HOT_H = ["name", "total_ms", "max_ms"]


def jank_case(frames, janky, pct, ui, rt):
    r = base()
    r["20_jank_summary"] = (JANK_H, [[PKG, str(frames), str(janky), str(pct), "2", "1", "7"]])
    r["22_jank_cpu_split"] = (SPLIT_H, [[PKG, str(ui), str(rt), "3"]])
    r["23_jank_hot_slices"] = (HOT_H, [["measure", "57", "9"],
                                        ["CreateGraphicsPipeline", "30", "12"]])
    return r


@case
def low_jank_rate_caps_follow_on_findings_at_info():
    # 9 of 3,000 frames janky: "cost is on the RenderThread" must not be rated
    # medium, nor shader compilation high, on a trace that is 99.7% smooth
    _, f = run(jank_case(3000, 9, 0.3, 40, 60), mode="interactive")
    jank = [x for x in f if x["area"] == "jank"]
    assert jank and all(x["severity"] == "info" for x in jank), [(x["severity"], x["title"]) for x in jank]
    assert any("9 of 3000 frames (0.3%)" in x["detail"] for x in jank), jank


@case
def high_jank_rate_keeps_follow_on_severity():
    _, f = run(jank_case(100, 40, 40.0, 45, 90), mode="interactive")
    sev = {x["title"].split(":")[-1].strip()[:28]: x["severity"] for x in f if x["area"] == "jank"}
    assert "high" in sev.values() and "medium" in sev.values(), sev
    assert any(x["severity"] == "high" and "Shader" in x["title"] for x in f), sev


@case
def no_janky_frames_means_no_medium_jank_findings():
    _, f = run(jank_case(500, 0, 0.0, 45, 90), mode="interactive")
    assert all(x["severity"] == "info" for x in f if x["area"] == "jank"), f


@case
def near_equal_thread_cost_is_called_a_split():
    # 50 ms vs 52 ms is a coin toss, not "the RenderThread"
    _, f = run(jank_case(100, 40, 40.0, 50, 52), mode="interactive")
    assert any("split between the UI thread and RenderThread" in x["title"] for x in f), [x["title"] for x in f]
    _, f = run(jank_case(100, 40, 40.0, 45, 90), mode="interactive")
    assert any(x["title"].endswith("cost is on the RenderThread") for x in f)


@case
def every_finding_names_the_query_it_came_from():
    for r, mode in ((jank_case(100, 40, 40.0, 45, 90), "interactive"),
                    (base(debuggable="1"), "startup"),
                    (base(startups=False), "startup")):
        _, f = run(r, mode=mode)
        assert f and all(x.get("source") for x in f), [(x["title"], x.get("source")) for x in f]
    _, f = run(base(startups=False), mode="startup")
    missed = [x for x in f if x["title"] == "Startup capture missed the app launch"][0]
    assert missed["source"] == ["10_startup_summary", "01_coverage"], missed["source"]


def main():
    failed = 0
    for fn in CASES:
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"[FAIL] {fn.__name__}: {str(e)[:300]}")
    print(f"\n{len(CASES) - failed} passed, {failed} failed (synthetic rows, not captures)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
