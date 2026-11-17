#!/usr/bin/env python3
"""
Checks for remeasure.py's many-run mode: medians, spread, and the verdict on
whether a change is bigger than the run-to-run noise.

The runs here are hand-written CSVs with chosen numbers. They pin down the
arithmetic and the verdict rules. Nothing in this file is a measurement.

    python3 test_remeasure.py
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "perfetto-triage", "scripts", "remeasure.py")
spec = importlib.util.spec_from_file_location("remeasure", SCRIPT)
rm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rm)

PKG = "com.example.app"


def run_dir(parent, name, ttid, janky_pct=10.0, section_ms=None, verdict="trusted",
            build="release", extra_finding=None):
    d = os.path.join(parent, name)
    os.makedirs(os.path.join(d, "csv"))

    def write(stem, text):
        with open(os.path.join(d, "csv", stem + ".csv"), "w") as fh:
            fh.write(text)
    write("10_startup_summary", "startup_id,package,startup_type,startup_ms,ttid_ms,ttfd_ms\n"
                                f"1,{PKG},cold,{ttid + 5},{ttid},[NULL]\n")
    write("12_startup_main_thread_state", "startup_id,main_thread_state,ms\n"
                                          "1,Running,60\n1,Sleeping,40\n")
    write("14_startup_class_loading", f"startup_id,package,classes_loaded,ms\n1,{PKG},3000,30\n")
    write("20_jank_summary",
          "process_name,frames,janky,janky_pct,big_jank,huge_jank,worst_overrun_ms,app_jank,sf_jank\n"
          f"com.android.systemui,50,0,0,0,0,-3,0,0\n{PKG},100,10,{janky_pct},1,0,40,8,2\n")
    findings = [{"severity": "high", "area": "startup", "title": f"Cold start TTID is {ttid} ms",
                 "detail": "", "action": ""}]
    if extra_finding:
        findings.append({"severity": "medium", "area": "jank", "title": extra_finding,
                         "detail": "", "action": ""})
    data = {"trace": name, "pkg": PKG, "findings": findings,
            "health_status": {"verdict": verdict, "build_type": build}}
    if section_ms is not None:
        write("13_startup_top_slices", "startup_id,slice_name,n,total_ms,max_ms\n"
                                       f"1,checkout.stage.pay,1,{section_ms},{section_ms}\n"
                                       "1,traversal,3,90,40\n")
        data["code_map"] = {"slices": [
            {"name": "checkout.stage.pay", "how": "trace section", "owner": "app"},
            {"name": "traversal", "how": "not in your source", "owner": "other"}]}
        write("24_jank_by_app_section",
              "section,times_run,total_ms,late_frames,all_late_frames,late_frames_in_any_section,worst_overrun_ms\n"
              f"checkout.stage.pay,1,{section_ms},{int(section_ms // 10)},10,8,40\n")
    with open(os.path.join(d, "findings.json"), "w") as fh:
        json.dump(data, fh)
    return d


def side(label, ttids, **kw):
    parent = tempfile.mkdtemp(prefix=f"rm-{label}-")
    return [run_dir(parent, f"{label}-{i:02d}", t, **kw) for i, t in enumerate(ttids)]


def row(rows, metric):
    return next(r for r in rows if r["metric"] == metric)


def test_median_range_and_change():
    before = side("b", [300, 320, 310, 305, 400])          # one slow outlier
    after = side("a", [250, 255, 260, 245, 252])
    r = row(rm.summarise(before, after, PKG), "cold start TTID (ms)")
    assert r["before"] == {"n": 5, "median": 310, "min": 300, "max": 400}, r["before"]
    assert r["after"]["median"] == 252 and r["delta"] == -58, r
    assert round(r["delta_pct"], 1) == -18.7, r["delta_pct"]
    assert r["verdict"] == "clear", "no after run is as slow as the fastest before run"


def test_overlapping_runs_are_noise():
    before = side("b", [300, 320, 310, 305, 330, 315, 298, 325, 312, 308])
    after = side("a", [304, 318, 309, 301, 327, 311, 299, 322, 314, 306])
    r = row(rm.summarise(before, after, PKG), "cold start TTID (ms)")
    assert r["verdict"] == "noise", r


def test_shifted_but_overlapping_is_likely():
    before = side("b", [300, 302, 304, 306, 308, 310, 312, 314, 316, 318])
    after = side("a", [286, 288, 290, 292, 294, 296, 298, 300, 302, 304])
    r = row(rm.summarise(before, after, PKG), "cold start TTID (ms)")
    assert r["verdict"] == "likely", r
    assert rm.judge([1, 2], [5, 6]) == "too few runs"
    assert rm.judge([5, 5, 5], [5, 5, 5]) == "no change"


def test_picks_the_apps_row_not_the_first_row():
    before, after = side("b", [300, 301, 302], janky_pct=20.0), side("a", [300, 301, 302], janky_pct=5.0)
    rows = rm.summarise(before, after, PKG)
    assert row(rows, "late frames (%)")["before"]["median"] == 20.0
    assert row(rows, "late frames (%)")["verdict"] == "clear"
    assert row(rows, "frames")["before"]["median"] == 100, "systemui is listed first and must be skipped"
    assert row(rows, "main thread running in startup (%)")["after"]["median"] == 60.0
    assert not any(r["metric"] == "TTFD (ms)" for r in rows), "[NULL] is not a number"


def test_the_apps_own_sections_get_a_row():
    before, after = side("b", [300, 301, 302], section_ms=80), side("a", [300, 301, 302], section_ms=35)
    rows = rm.summarise(before, after, PKG)
    r = row(rows, "your code: checkout.stage.pay (ms)")
    assert r["before"]["median"] == 80 and r["after"]["median"] == 35 and r["verdict"] == "clear", r
    assert not any("traversal" in x["metric"] for x in rows), "framework slices get no row"
    late = row(rows, "late frames inside checkout.stage.pay")
    assert late["before"]["median"] == 8 and late["after"]["median"] == 3 and late["verdict"] == "clear"


def test_a_folder_of_runs_expands():
    runs = side("b", [300, 310, 320])
    assert rm.expand_runs([os.path.dirname(runs[0])]) == runs
    assert rm.expand_runs(runs[:2]) == runs[:2]


def report(before, after):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rm.compare_many(before, after, PKG)
    return buf.getvalue()


def test_report_says_what_moved_and_what_did_not():
    before = side("b", [300, 320, 310, 305, 330, 315, 298, 325, 312, 308], janky_pct=20.0)
    after = side("a", [250, 262, 255, 249, 270, 258, 247, 266, 256, 252], janky_pct=20.0)
    text = report(before, after)
    assert "10 before runs against 10 after runs" in text
    assert "- cold start TTID (ms): 311.0 to 255.5 (clear)" in text, text
    assert "fewer than 10 runs" not in text
    assert "late frames (%)" in text and "no change" in text
    assert "Cold start TTID is # ms" not in text, "a finding on both sides is not news"
    assert "The same findings show up on both sides." in text


def test_report_warns_on_untrusted_mixed_builds_and_few_runs():
    before = side("b", [300, 310, 320], verdict="directional_only", build="debuggable")
    after = side("a", [250, 260, 270], extra_finding="Shader compile during frames (199 ms)")
    text = report(before, after)
    assert "WARNING: build types differ (debuggable vs release)" in text
    assert "WARNING: 3 before run(s) are not trusted" in text
    assert "NOTE: fewer than 10 runs on a side" in text
    assert "Shader compile during frames (# ms)  (0/3 before, 3/3 after)" in text, text


def test_one_run_each_still_works_from_the_command_line():
    before, after = side("b", [300]), side("a", [250])
    out = subprocess.run([sys.executable, SCRIPT, before[0], after[0]],
                         capture_output=True, text=True)
    assert out.returncode == 0 and "## Trust" in out.stdout, out.stderr
    assert "compare them with --before ... --after ..." in out.stdout
    bad = subprocess.run([sys.executable, SCRIPT, "--before", before[0]],
                         capture_output=True, text=True)
    assert bad.returncode != 0 and "give both --before and --after" in bad.stderr




def test_frames_with_no_deadlines_are_not_compared():
    """A phone older than Android 12 records no frame deadlines. Every run then
    reads 0% late, and comparing them would report 'no change' for something
    that was never measured."""
    parent = tempfile.mkdtemp(prefix="rm-old-")
    d = run_dir(parent, "old", 700, janky_pct=0.0)
    with open(os.path.join(d, "csv", "01_coverage.csv"), "w") as fh:
        fh.write("slices,sched_rows,frametimeline_rows,blocked_reason_rows,binder_slices\n9000,500,0,0,30\n")
    with open(os.path.join(d, "csv", "40_slice_hotspots.csv"), "w") as fh:
        fh.write("name,thread_name,n,total_ms,self_ms\nCompiling,Jit thread pool,600,1844,1844\n"
                 "draw,main,500,2000,2000\n")
    m = rm.run_metrics(d, PKG)
    assert "late frames (%)" not in m and "worst overrun (ms)" not in m, m
    assert m["frames"] == 100 and m["cold start TTID (ms)"] == 700
    assert m["JIT compile (ms)"] == 1844
    new = run_dir(parent, "new", 700)
    assert "late frames (%)" in rm.run_metrics(new, PKG), "no coverage file means an older triage: keep it"


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
    print(f"\n{len(CASES) - failed} passed, {failed} failed (hand-written runs, not captures)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
