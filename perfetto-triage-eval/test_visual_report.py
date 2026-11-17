#!/usr/bin/env python3
"""
Checks for visual_report.py.

Three kinds, kept apart on purpose:

  * theme discovery and the contrast guard, on small hand-written source trees
  * rendering, on the two real fixtures replayed through today's triage logic
  * safety: nothing from a trace or a theme file reaches the page unescaped

A pass proves the page is well formed and says what the data says. It does not
prove the page looks right. Open it for that.

    python3 test_visual_report.py
"""
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_eval  # noqa: E402

VR_PY = os.path.join(os.path.dirname(HERE), "perfetto-triage", "scripts", "visual_report.py")
spec = importlib.util.spec_from_file_location("visual_report", VR_PY)
vr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vr)


def tree(files: dict) -> str:
    root = tempfile.mkdtemp(prefix="vr-test-")
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
    return root


def fixture_out(name: str) -> str:
    """A triage-out directory for a fixture, with findings from today's code."""
    r = run_eval.rederive(run_eval.load_triage(), name)
    out = tempfile.mkdtemp(prefix="vr-out-")
    shutil.copytree(os.path.join(run_eval.FIXTURES, name, "csv"), os.path.join(out, "csv"))
    with open(os.path.join(out, "findings.json"), "w") as fh:
        json.dump({"trace": r["frozen"].get("trace"), "pkg": r["pkg"], "mode": r["mode"],
                   "health_status": r["status"], "findings": r["findings"]}, fh)
    return out


def well_formed(page: str):
    assert page.startswith("<!doctype html>"), page[:40]
    assert "<script" not in page.lower(), "the page must not carry scripts"
    assert not re.search(r'(src|href)="https?://', page), "the page must not load anything"
    for svg in re.findall(r"<svg.*?</svg>", page, flags=re.S):
        ET.fromstring(svg)          # raises if a chart is malformed


# ---- theme discovery -------------------------------------------------------

def test_hex_parsing():
    assert vr.parse_hex("0xFF6650A4") == (0x66, 0x50, 0xA4)
    assert vr.parse_hex("#6650a4") == (0x66, 0x50, 0xA4)
    assert vr.parse_hex("#80112233") == (0x11, 0x22, 0x33)      # Android puts alpha first
    assert vr.parse_hex("#abc") == (0xAA, 0xBB, 0xCC)
    assert vr.parse_hex("red") is None and vr.parse_hex("") is None
    assert round(vr.contrast("#000000", "#ffffff")) == 21


def test_compose_colour_scheme():
    repo = tree({
        "app/src/main/java/ui/theme/Color.kt":
            "val Purple40 = Color(0xFF6650A4)\nval Ink = Color(0xFF1C1B1F)\n"
            "val Paper = Color(0xFFFFFBFE)\nval Purple80 = Color(0xFFD0BCFF)\n",
        "app/src/main/java/ui/theme/Theme.kt":
            "private val Light = lightColorScheme(primary = Purple40, background = Paper,\n"
            "    onBackground = Ink, surface = Paper, onSurface = Ink)\n"
            "private val Dark = darkColorScheme(primary = Purple80,\n"
            "    background = Color(0xFF1C1B1F), onBackground = Color(0xFFE6E1E5))\n",
        "app/src/main/java/ui/theme/Shape.kt":
            "val Shapes = Shapes(small = RoundedCornerShape(4.dp), medium = RoundedCornerShape(16.dp))\n",
        "app/build/generated/Color.kt": "val Primary = Color(0xFF00FF00)\n",   # must be skipped
    })
    t = vr.discover_theme(repo)
    assert t["light"]["primary"] == "#6650a4", t
    assert t["light"]["background"] == "#fffbfe" and t["light"]["onbackground"] == "#1c1b1f", t
    assert t["dark"]["primary"] == "#d0bcff" and t["dark"]["background"] == "#1c1b1f", t
    assert t["radius"] == 16, t
    assert not t.get("inferred"), t
    v, notes = vr.resolve_theme(t)
    assert v["themed"] and v["light"]["accent"] == "#6650a4" and v["dark"]["accent"] == "#d0bcff", v
    assert v["light"]["bg"] == "#fffbfe" and v["radius"] == 16 and not notes, (v, notes)


def test_xml_colours_and_night():
    repo = tree({
        "app/src/main/res/values/colors.xml":
            '<resources><color name="brand">#0B57D0</color>'
            '<color name="colorPrimary">@color/brand</color>'
            '<color name="colorBackground">#FFFFFFFF</color></resources>',
        "app/src/main/res/values-night/colors.xml":
            '<resources><color name="colorPrimary">#A8C7FA</color></resources>',
    })
    t = vr.discover_theme(repo)
    assert t["light"]["primary"] == "#0b57d0", t
    assert t["dark"]["primary"] == "#a8c7fa", t
    assert t["light"]["background"] == "#ffffff", t


def test_inline_colours_when_no_tokens():
    screen = ("Box(Modifier.background(Color(0xFFF2F2F7)))\n" * 3
              + "Text(color = Color(0xFF2D2D2D))\n" * 4
              + "Icon(tint = Color(0xFF5B5FE6))\nFab(contentColor = Color(0xFF5B5FE6))\n"
              + "Text(color = Color(0xFFFF6B6B))\n")
    t = vr.discover_theme(tree({"app/src/main/java/Home.kt": screen}))
    assert t.get("inferred") is True, t
    assert t["light"] == {"background": "#f2f2f7", "onbackground": "#2d2d2d",
                          "primary": "#5b5fe6"}, t


def test_unreadable_tokens_fall_back():
    """A primary that cannot be told apart from the page is not used."""
    v, notes = vr.resolve_theme({"light": {"primary": "#fefefe", "background": "#ffffff",
                                           "onbackground": "#f0f0f0"}, "dark": {}})
    assert v["light"]["accent"] == vr.DEFAULTS["light"]["accent"], v["light"]
    assert v["light"]["bg"] == vr.DEFAULTS["light"]["bg"], v["light"]
    assert len(notes) >= 2 and "below" in notes[0], notes
    for mode in ("light", "dark"):
        assert vr.contrast(v[mode]["text"], v[mode]["bg"]) >= 4.5
        assert vr.contrast(v[mode]["on_accent"], v[mode]["accent"]) >= 4.5


def test_no_repo_is_fine():
    assert vr.discover_theme("/nonexistent/path")["light"] == {}
    v, _ = vr.resolve_theme(None)
    assert not v["themed"]


# ---- rendering -------------------------------------------------------------

def test_renders_debug_build_fixture():
    out = fixture_out("debug-build-out")
    page = vr.render(out)
    well_formed(page)
    for needle in ("Can this trace be trusted?", "Directional only",
                   "Was the main thread running or waiting?", "Running 66%",
                   "Which frames were late", "frame 26045026",
                   "Where did the time go?", "Compiling baseline"):
        assert needle in page, needle
    data = json.load(open(os.path.join(out, "findings.json")))
    for f in data["findings"]:
        assert vr.esc(f["title"]) in page, f["title"]


def test_renders_missed_launch_fixture():
    page = vr.render(fixture_out("startup-race-out"))
    well_formed(page)
    assert "Startup capture missed the app launch" in page
    assert "failed measurement, not a fast startup" in page
    assert "time to first frame (TTID)" not in page, "no startup tiles without a launch"


def test_renders_with_nothing_but_findings():
    out = tempfile.mkdtemp(prefix="vr-empty-")
    json.dump({"trace": "x.pftrace", "pkg": "*", "findings": []},
              open(os.path.join(out, "findings.json"), "w"))
    page = vr.render(out)
    well_formed(page)
    assert "older triage.py" in page and "Nothing crossed a threshold" in page


def test_write_puts_report_next_to_findings():
    out = fixture_out("debug-build-out")
    dest = vr.write(out)
    assert dest == os.path.join(out, "report.html") and os.path.getsize(dest) > 5000


def test_readings_a_real_run_showed_were_wrong():
    """Three things a real capture exposed. Each was a confident, wrong sentence."""
    out = tempfile.mkdtemp(prefix="vr-real-")
    os.makedirs(os.path.join(out, "csv"))
    pkg = "com.example.app"

    def write(stem, text):
        with open(os.path.join(out, "csv", stem + ".csv"), "w") as fh:
            fh.write(text)
    # an interactive capture: the main thread mostly sleeps between frames
    write("33_main_thread_blocking", "process_name,state,blocked_function,n,total_ms\n"
          f"{pkg},Sleeping,-,100,99000\n{pkg},Running,-,100,18000\n"
          f"{pkg},Uninterruptible Sleep (IO),folio_wait_bit_common,3,6\n")
    # 56 of 8527 frames late
    write("20_jank_summary",
          "process_name,frames,janky,janky_pct,big_jank,huge_jank,worst_overrun_ms,app_jank,sf_jank\n"
          f"{pkg},8527,56,0.66,12,0,109,27,15\n")
    # a frame that overran but carries no jank type
    write("21_worst_frames", "process_name,frame_id,ts,overrun_ms,cpu_ms,ui_ms,jank_type,present_type\n"
          f"{pkg},1,1,109,120,2,App Deadline Missed,Late Present\n"
          f"{pkg},2,2,38,53,15,None,Unspecified Present\n")
    json.dump({"trace": "t.pftrace", "pkg": pkg, "findings": []},
              open(os.path.join(out, "findings.json"), "w"))
    page = vr.render(out)
    well_formed(page)
    assert "expected to sleep between frames" in page, "idle sleep is not being blocked"
    assert "Profiling the app&#x27;s own code will not find that time" not in page
    assert "Kernel functions the main thread was blocked in" not in page, "6 ms is not worth a chart"
    assert "Late, not flagged as jank" in page
    assert ">On time</span></li></ul></div><p class=\"where\">" not in page, \
        "a frame in the worst-frames chart is never labelled on time"
    assert "under 1% of frames, which is a low rate" in page


def test_old_phone_report_does_not_claim_frames_were_on_time():
    """A real run on an Android 10 phone: no frame deadlines exist there, and
    the page said 528 frames, 0 missed, 100% on time under a Trusted verdict
    that 'passed every check' beside two Fail rows."""
    out = tempfile.mkdtemp(prefix="vr-old-")
    os.makedirs(os.path.join(out, "csv"))
    pkg = "com.example.app"

    def write(stem, text):
        with open(os.path.join(out, "csv", stem + ".csv"), "w") as fh:
            fh.write(text)
    write("20_jank_summary",
          "process_name,frames,janky,janky_pct,big_jank,huge_jank,worst_overrun_ms,app_jank,sf_jank\n"
          f"{pkg},528,0,0,0,0,[NULL],0,0\n")
    write("10_startup_summary", "startup_id,package,startup_type,startup_ms,ttid_ms,ttfd_ms\n"
                                f"0,{pkg},[NULL],690,681,[NULL]\n")
    write("30_binder_main_thread", "client_process,aidl_name,server_process,n,total_ms,max_ms\n"
                                   f"{pkg},unknown,system_server,318,79,5.6\n")
    hs = {"mode": "startup", "trace_health": "pass", "verdict": "trusted", "reasons": [],
          "coverage": {"slices": "ok", "sched": "ok", "frametimeline": "missing",
                       "blocked_reasons": "missing", "binder": "ok"},
          "coverage_gaps": ["frametimeline", "blocked_reasons"], "build_type": "release",
          "startup_capture": "ok", "frames": "unsupported", "device": {"sdk": 29, "android": "10"}}
    json.dump({"trace": "t.pftrace", "pkg": pkg, "findings": [], "health_status": hs},
              open(os.path.join(out, "findings.json"), "w"))
    page = vr.render(out)
    well_formed(page)
    assert "passed every check" not in page
    assert "It cannot answer: which frames were late (this phone runs Android 10" in page
    assert ">Fail<" not in page, "a gap that does not decide the verdict is not a failed check"
    assert ">Missing<" in page and "Android version does not record it" in page
    assert "not measured" in page and "it does not mean the frames were on time" in page
    assert "Every recorded frame met its deadline" not in page and "On time" not in page
    assert "[NULL] start" not in page and "start type not recorded" in page
    assert "to system_server" in page and ">unknown<" not in page


def test_a_named_package_never_shows_another_apps_rows():
    rows = [{"process_name": "com.other.launcher", "frames": "60"}]
    assert vr.pick_process(rows, "process_name", "com.example.app") is None
    assert vr.pick_process(rows, "process_name", "*") == "com.other.launcher"
    assert vr.pick_process(rows, "process_name", "com.other.*") == "com.other.launcher"


# ---- safety ----------------------------------------------------------------

def test_trace_strings_are_escaped():
    out = tempfile.mkdtemp(prefix="vr-xss-")
    os.makedirs(os.path.join(out, "csv"))
    evil = '<img src=x onerror=alert(1)>"\'</style><script>alert(2)</script>'
    with open(os.path.join(out, "csv", "40_slice_hotspots.csv"), "w") as fh:
        fh.write('name,thread_name,n,total_ms,self_ms\n"' + evil.replace('"', '""') + '",main,1,10,10\n')
    json.dump({"trace": evil, "pkg": evil, "mode": evil,
               "health_status": {"verdict": evil, "trace_health": evil, "coverage": {evil: evil},
                                 "build_type": evil, "startup_capture": evil, "reasons": [evil],
                                 "mode": evil},
               "findings": [{"severity": evil, "area": "jank", "title": evil, "detail": evil,
                             "action": evil, "source": [evil]}]},
              open(os.path.join(out, "findings.json"), "w"))
    page = vr.render(out)
    well_formed(page)
    assert "<img" not in page and "onerror=alert(1)>" not in page, "raw markup leaked"
    assert page.count("</style>") == 1, "a trace string closed the style block"


def test_theme_file_cannot_inject_css():
    v, _ = vr.resolve_theme({"light": {"primary": "red;}body{display:none",
                                       "background": "url(http://x)"},
                             "dark": {}, "radius": "9px;}", "font": "x';}*{color:red"})
    css = vr.css_vars(v)
    assert "display:none" not in css and "url(" not in css and "color:red" not in css, css
    assert v["radius"] == 10 and v["font"] is None, v


CASES = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


def main():
    failed = 0
    for fn in CASES:
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except Exception as e:  # a crash in the renderer is a failure too
            failed += 1
            print(f"[FAIL] {fn.__name__}: {type(e).__name__}: {str(e)[:300]}")
    print(f"\n{len(CASES) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
