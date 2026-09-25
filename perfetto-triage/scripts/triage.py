#!/usr/bin/env python3
"""
Run the Perfetto triage query pack against a trace and emit a ranked report.

This is the machine half of the trace-reading checklist: it answers, in order,
the questions a senior engineer asks when a trace lands on their desk.

  ./triage.py trace.perfetto-trace --pkg com.example.app

Outputs into --out (default ./triage-out):
  report.md      human-readable report, one section per question
  findings.json  machine-readable verdicts, for an agent to act on
  csv/*.csv      raw result of every query

No third-party dependencies. Downloads trace_processor on first run.
"""

import argparse
import csv
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid

TP_VERSION = "v57.2"
TP_BASE = "https://commondatastorage.googleapis.com/perfetto-luci-artifacts"
HERE = os.path.dirname(os.path.abspath(__file__))
QUERY_DIR = os.path.join(os.path.dirname(HERE), "queries")
NULL = "[NULL]"


# --------------------------------------------------------------------------
# trace_processor plumbing
# --------------------------------------------------------------------------

def tp_arch() -> str:
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if sysname == "darwin":
        return "mac-arm64" if arm else "mac-amd64"
    if sysname == "linux":
        return "linux-arm64" if arm else "linux-amd64"
    raise SystemExit(f"unsupported platform: {sysname}/{machine}")


def ensure_tp(explicit: str | None) -> str:
    if explicit:
        return explicit
    found = shutil.which("trace_processor_shell") or shutil.which("trace_processor")
    if found:
        return found
    cache = os.path.expanduser(f"~/.cache/perfetto-triage/{TP_VERSION}")
    os.makedirs(cache, exist_ok=True)
    dest = os.path.join(cache, "trace_processor_shell")
    if not os.path.exists(dest):
        url = f"{TP_BASE}/{TP_VERSION}/{tp_arch()}/trace_processor_shell"
        sys.stderr.write(f"[triage] downloading trace_processor {TP_VERSION} ({tp_arch()})\n")
        urllib.request.urlretrieve(url, dest)
        os.chmod(dest, 0o755)
    return dest


class Session:
    """A warm trace_processor session: parse the trace once, query it many times."""

    def __init__(self, tp: str, trace: str):
        self.tp = tp
        self.trace = trace
        self.name = f"triage-{uuid.uuid4().hex[:8]}"
        self.remote = True

    def __enter__(self):
        try:
            subprocess.run(
                [self.tp, "server", "unix", "--name", self.name,
                 "--daemonize", "--idle-timeout", "30m", self.trace],
                check=True, capture_output=True, timeout=900,
            )
            # Wait for the socket to accept queries.
            for _ in range(120):
                probe = subprocess.run(
                    [self.tp, "query", "--remote", self.name, "SELECT 1 AS ok"],
                    capture_output=True, text=True,
                )
                if probe.returncode == 0 and "ok" in probe.stdout:
                    return self
                time.sleep(1)
            raise RuntimeError("session server never became ready")
        except Exception as exc:  # noqa: BLE001 - fall back to cold queries
            sys.stderr.write(f"[triage] warm session unavailable ({exc}); "
                             f"falling back to re-parsing per query\n")
            self.remote = False
            return self

    def __exit__(self, *_):
        if self.remote:
            subprocess.run([self.tp, "server", "kill", self.name],
                           capture_output=True)

    def query_file(self, path: str) -> tuple[list[str], list[list[str]], str]:
        target = ["--remote", self.name] if self.remote else [self.trace]
        proc = subprocess.run(
            [self.tp, "query", *target, "-f", path],
            capture_output=True, text=True, timeout=600,
        )
        stdout, stderr = proc.stdout, proc.stderr
        if proc.returncode != 0:
            return [], [], (stderr or stdout).strip()
        # trace_processor prints one CSV result set per statement, separated by
        # blank lines. INCLUDE statements produce empty result sets, so the real
        # table is the last non-empty block.
        blocks, cur = [], []
        for line in stdout.splitlines():
            if line.strip():
                cur.append(line)
            elif cur:
                blocks.append(cur)
                cur = []
        if cur:
            blocks.append(cur)
        if not blocks:
            return [], [], ""
        rows = list(csv.reader(io.StringIO("\n".join(blocks[-1]))))
        if not rows:
            return [], [], ""
        return rows[0], rows[1:], ""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def num(v):
    if v is None or v == NULL or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def as_dicts(header, rows):
    return [dict(zip(header, r)) for r in rows]


def fmt_cell(v: str) -> str:
    if v == NULL:
        return "–"
    f = num(v)
    if f is not None and "." in v:
        return f"{f:,.2f}"
    return v.replace("|", "\\|")


def md_table(header, rows, limit=20):
    if not header:
        return "_no rows_\n"
    if not rows:
        return "_no rows_\n"
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join(["---"] * len(header)) + "|"]
    for r in rows[:limit]:
        out.append("| " + " | ".join(fmt_cell(c) for c in r) + " |")
    if len(rows) > limit:
        out.append(f"\n_({len(rows) - limit} more rows in csv/)_")
    return "\n".join(out) + "\n"


def leading_comment(sql_path: str) -> str:
    lines = []
    with open(sql_path) as fh:
        for line in fh:
            if line.startswith("--"):
                lines.append(line[2:].strip())
            else:
                break
    return " ".join(lines)


# --------------------------------------------------------------------------
# the checklist: turn tables into verdicts
# --------------------------------------------------------------------------

def analyse(results: dict, pkg: str) -> list[dict]:
    """results: {query_stem: (header, rows)}. Returns ordered findings."""
    f = []

    def add(severity, area, title, detail, action):
        f.append({"severity": severity, "area": area, "title": title,
                  "detail": detail, "action": action})

    def table(stem):
        h, r = results.get(stem, ([], []))
        return as_dicts(h, r)

    # --- 0. Is the trace trustworthy and complete? ------------------------
    health = table("00_health")
    if health:
        names = ", ".join(f"{r['name']}={r['value']}" for r in health[:5])
        add("blocker", "trace", "Trace has errors or dropped data",
            f"trace_processor stats reported: {names}",
            "Re-record with a bigger buffer (-b 256mb) or a shorter duration. "
            "Numbers from a lossy trace are not comparable across runs.")

    cov = table("01_coverage")
    if cov:
        c = cov[0]
        if (num(c.get("frametimeline_rows")) or 0) == 0:
            add("blocker", "jank", "No FrameTimeline data in this trace",
                "actual_frame_timeline_slice is empty, so no jank attribution is possible.",
                "Re-record with the android.surfaceflinger.frametimeline data source "
                "(requires a full TraceConfig and Android 12+). The lightweight "
                "`adb shell perfetto <atrace categories>` form cannot enable it.")
        if (num(c.get("blocked_reason_rows")) or 0) == 0:
            add("info", "trace", "No kernel blocking reasons captured",
                "thread_state.blocked_function is empty everywhere.",
                "Add ftrace event sched/sched_blocked_reason to see *why* the "
                "main thread was in uninterruptible sleep (usually IO).")
        if (num(c.get("sched_rows")) or 0) == 0:
            add("blocker", "trace", "No scheduler data",
                "The sched table is empty, so running-vs-waiting cannot be answered.",
                "Add ftrace events sched/sched_switch and sched/sched_waking.")

    # --- 1. Startup -------------------------------------------------------
    startups = [r for r in table("10_startup_summary")
                if pkg in ("*", "") or _glob(r.get("package", ""), pkg)]
    for s in startups:
        ttid = num(s.get("ttid_ms"))
        kind = s.get("startup_type")
        if ttid and kind == "cold" and ttid > 500:
            add("high", "startup", f"Cold start TTID is {ttid:.0f} ms",
                f"{s.get('package')} startup_id={s.get('startup_id')} "
                f"time-to-initial-display {ttid:.0f} ms.",
                "Google Play flags cold starts over 5 s as bad and over 2 s as "
                "sluggish; a well-tuned app lands under 500 ms. See the breakdown below "
                "for where it goes.")
        elif ttid and ttid > 1000:
            add("medium", "startup", f"{kind} start TTID is {ttid:.0f} ms",
                f"{s.get('package')} startup_id={s.get('startup_id')}.",
                "Check the startup breakdown for the dominant reason.")
        if s.get("ttfd_ms") in (NULL, None, ""):
            add("info", "startup", "App never calls reportFullyDrawn()",
                f"{s.get('package')} has no time-to-full-display. TTID only measures "
                "the first frame, which is often a skeleton or spinner.",
                "Call Activity.reportFullyDrawn() (or ReportDrawn* in Compose) once real "
                "content is on screen, so TTFD becomes measurable and Play Console reports it.")

    bd = table("11_startup_breakdown")
    if bd:
        by_reason = {}
        for r in bd:
            by_reason[r["reason"]] = by_reason.get(r["reason"], 0) + (num(r["ms"]) or 0)
        top = sorted(by_reason.items(), key=lambda kv: -kv[1])[:3]
        detail = ", ".join(f"{k} {v:.0f} ms" for k, v in top)
        hint = {
            "binder": "The app is blocked in IPC during startup. Find which AIDL call "
                      "(see the binder section) and move it off the critical path or make it async.",
            "inflate": "Layout inflation on the critical path. Reduce view hierarchy depth, "
                       "drop unused layouts, or switch the first screen to a lighter placeholder.",
            "activity_start": "Time inside Activity lifecycle callbacks. Audit onCreate for "
                              "work that could be deferred past first frame.",
            "choreographer_do_frame": "First-frame rendering is expensive. Usually composition/"
                                      "measure/layout cost or shader compilation.",
            "launch_delay": "Time before the app process got going: process fork, system_server "
                            "queueing, or a cold page cache. Rarely fixable in app code.",
        }.get(top[0][0], "Dig into the dominant reason below.")
        add("high" if top[0][1] > 100 else "info", "startup",
            f"Startup time is dominated by '{top[0][0]}'", detail, hint)

    mts = table("12_startup_main_thread_state")
    if mts:
        agg = {}
        for r in mts:
            agg[r["main_thread_state"]] = agg.get(r["main_thread_state"], 0) + (num(r["ms"]) or 0)
        running = agg.get("Running", 0.0)
        waiting = sum(v for k, v in agg.items() if k != "Running")
        total = running + waiting
        if total > 0:
            pct_wait = 100.0 * waiting / total
            mix = ", ".join(f"{k} {v:.0f} ms"
                            for k, v in sorted(agg.items(), key=lambda kv: -kv[1]))
            if pct_wait > 55:
                add("high", "startup",
                    f"Startup is WAITING-bound — main thread waits {pct_wait:.0f}% of the time",
                    f"Running {running:.0f} ms vs waiting {waiting:.0f} ms ({mix}).",
                    "Optimising your own code will barely move this. The win is in what the "
                    "thread is waiting ON: binder/IPC, disk IO, or a lock. Go to those sections.")
            elif pct_wait < 45:
                add("high" if running > 400 else "info", "startup",
                    f"Startup is COMPUTE-bound — main thread runs {100 - pct_wait:.0f}% of the time",
                    f"Running {running:.0f} ms vs waiting {waiting:.0f} ms ({mix}).",
                    "Your code, class loading, or JIT is the cost. Baseline Profiles and "
                    "deferring init work off the critical path are the levers.")
            else:
                add("medium", "startup",
                    f"Startup is mixed — {100 - pct_wait:.0f}% running, {pct_wait:.0f}% waiting",
                    f"Running {running:.0f} ms vs waiting {waiting:.0f} ms ({mix}).",
                    "Attack both: shave the biggest running slice AND the biggest blocking "
                    "call. Re-measure after each, separately.")

    cl = table("14_startup_class_loading")
    for r in cl:
        n = num(r.get("classes_loaded")) or 0
        ms = num(r.get("ms")) or 0
        if ms < 10 and n < 300:
            continue
        sev = "high" if ms > 40 else "medium" if ms > 15 else "info"
        add(sev, "startup",
            f"{int(n)} classes loaded during startup, {ms:.0f} ms on the critical path",
            f"startup_id={r.get('startup_id')} ({r.get('package', '')}). Note this "
            "counts explicit class-load slices only; JIT and verification cost sits "
            "in the running time above.",
            "This is the clearest Baseline Profile signal there is. Generate one with "
            "BaselineProfileRule, then prove the win by benchmarking "
            "CompilationMode.None() against "
            "CompilationMode.Partial(BaselineProfileMode.Require).")
        break

    # --- 2. Jank ----------------------------------------------------------
    for r in table("20_jank_summary"):
        if pkg not in ("*", "") and not _glob(r.get("process_name", ""), pkg):
            continue
        frames = num(r.get("frames")) or 0
        janky = num(r.get("janky")) or 0
        pct = num(r.get("janky_pct")) or 0
        app_j = num(r.get("app_jank")) or 0
        sf_j = num(r.get("sf_jank")) or 0
        worst = num(r.get("worst_overrun_ms")) or 0
        if frames < 10 or janky == 0:
            continue
        sev = "high" if pct > 5 else "medium" if pct > 1 else "info"
        blame = ("the app" if app_j >= sf_j else "SurfaceFlinger / the display pipeline")
        add(sev, "jank",
            f"{r['process_name']}: {int(janky)}/{int(frames)} frames janky ({pct:.1f}%)",
            f"Worst frame ran {worst:.0f} ms over its deadline. "
            f"App-attributed jank: {int(app_j)}, SurfaceFlinger-attributed: {int(sf_j)}.",
            ("Blame lands on the app. Look at the UI-thread vs RenderThread split "
             "and the hot slices during janky frames."
             if app_j >= sf_j else
             "Blame lands outside the app. Before chasing app code, confirm the device "
             "was not thermally throttled or contended by another process."))

    split = table("22_jank_cpu_split")
    for r in split:
        if pkg not in ("*", "") and not _glob(r.get("process_name", ""), pkg):
            continue
        ui = num(r.get("ui_thread_ms")) or 0
        rt = num(r.get("render_thread_ms")) or 0
        vs = num(r.get("vsync_delay_ms")) or 0
        if ui + rt == 0:
            continue
        if rt > ui:
            add("medium", "jank", f"{r['process_name']}: cost is on the RenderThread",
                f"doFrame {ui:.0f} ms vs DrawFrame {rt:.0f} ms (vsync delay {vs:.0f} ms).",
                "Look for overdraw, large/unbounded bitmaps, expensive shaders, and "
                "hardware-layer thrash. Shader compilation on first use is a classic here.")
        else:
            add("medium", "jank", f"{r['process_name']}: cost is on the UI thread",
                f"doFrame {ui:.0f} ms vs DrawFrame {rt:.0f} ms (vsync delay {vs:.0f} ms).",
                "Look for measure/layout passes, recomposition, main-thread IO or "
                "deserialisation, and work posted onto the main Handler.")
        break

    hot = table("23_jank_hot_slices")
    shader = [r for r in hot if re.search(
        r"CreateGraphicsPipeline|CompileAfterCacheMiss|shader|Program", r["name"], re.I)]
    if shader:
        tot = sum(num(r["total_ms"]) or 0 for r in shader)
        add("high", "jank", f"Shader / pipeline compilation during janky frames ({tot:.0f} ms)",
            "Slices matched: " + ", ".join(r["name"] for r in shader[:3]),
            "This is first-run-only jank that every user hits. Warm it up: run the "
            "animation once behind a splash, or ship a Baseline Profile — "
            "ProfileInstaller also primes the shader cache.")
    inflate = [r for r in hot if re.search(r"inflate|measure|layout|traversal", r["name"], re.I)]
    if inflate:
        tot = sum(num(r["total_ms"]) or 0 for r in inflate)
        add("medium", "jank", f"Measure/layout/traversal cost during janky frames ({tot:.0f} ms)",
            "Slices matched: " + ", ".join(r["name"] for r in inflate[:3]),
            "Flatten the hierarchy, avoid nested weights / double measure, and hoist "
            "state so recomposition scopes stay small.")

    # --- 3. Binder / IPC --------------------------------------------------
    binder = table("30_binder_main_thread")
    if binder:
        worst = binder[0]
        tot = num(worst.get("total_ms")) or 0
        if tot > 20:
            add("high" if tot > 100 else "medium", "binder",
                f"Main-thread binder: {worst.get('aidl_name')} costs {tot:.0f} ms",
                f"{worst.get('n')} synchronous calls from {worst.get('client_process')} "
                f"to {worst.get('server_process')}, worst single call "
                f"{num(worst.get('max_ms')) or 0:.1f} ms.",
                "Synchronous binder on the main thread is a frozen UI. Cache the result, "
                "batch the calls, or move them to a background dispatcher. System APIs that "
                "look local (PackageManager, ConnectivityManager, DisplayManager, "
                "SharedPreferences-backed system settings) are binder calls underneath.")
    why = table("31_binder_why_slow")
    if why:
        top = max(why, key=lambda r: num(r["total_ms"]) or 0)
        if top["reason"] != "Running":
            add("info", "binder", "Slow binder calls are queueing, not computing",
                f"Dominant client-side reason is '{top['reason']}' "
                f"({num(top['total_ms']) or 0:.0f} ms).",
                "The server side is not the bottleneck; the client is descheduled or "
                "waiting for a free binder thread. Reducing call *count* helps more "
                "than making the server faster.")

    mc = table("32_monitor_contention")
    if mc:
        top = mc[0]
        tot = num(top.get("total_ms")) or 0
        if tot > 10:
            add("high" if tot > 50 else "medium", "locks",
                f"Main thread blocked on a lock for {tot:.0f} ms",
                f"blocked in {top.get('short_blocked_method')} waiting on "
                f"{top.get('short_blocking_method')} ({top.get('n')} times).",
                "Classic invisible stall: nothing is 'running' so CPU profilers show "
                "nothing. Shrink the critical section, or move the shared state behind "
                "an actor / single-threaded dispatcher.")

    # --- 4. ANR -----------------------------------------------------------
    anrs = table("41_anrs")
    if anrs:
        add("blocker", "anr", f"{len(anrs)} ANR(s) recorded in this trace",
            "; ".join(f"{r.get('process_name')}: {r.get('subject')}" for r in anrs[:3]),
            "Work backwards from the ANR timestamp: what was the main thread's "
            "thread_state in the 5 s before it, and what was it blocked on?")

    order = {"blocker": 0, "high": 1, "medium": 2, "info": 3}
    f.sort(key=lambda x: order.get(x["severity"], 9))
    return f


def _glob(value: str, pattern: str) -> bool:
    if pattern in ("", "*"):
        return True
    import fnmatch
    return fnmatch.fnmatch(value or "", pattern)


# --------------------------------------------------------------------------

SECTIONS = [
    ("Is this trace trustworthy?", ["00_health", "01_coverage", "02_processes"]),
    ("Startup: where does cold start time go?",
     ["10_startup_summary", "11_startup_breakdown", "12_startup_main_thread_state",
      "13_startup_top_slices", "14_startup_class_loading", "15_startup_binder"]),
    ("Jank: which frames missed, and whose fault was it?",
     ["20_jank_summary", "21_worst_frames", "22_jank_cpu_split", "23_jank_hot_slices"]),
    ("Blocking: binder, IPC and locks",
     ["30_binder_main_thread", "31_binder_why_slow", "32_monitor_contention",
      "33_main_thread_blocking"]),
    ("Hotspots and ANRs", ["40_slice_hotspots", "41_anrs"]),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("trace")
    ap.add_argument("--pkg", default="*",
                    help="package / process name glob, e.g. com.example.app or com.example.*")
    ap.add_argument("--out", default="triage-out")
    ap.add_argument("--tp", default=None, help="path to trace_processor_shell")
    ap.add_argument("--only", default=None,
                    help="comma-separated query stems or prefixes, e.g. 10,20")
    args = ap.parse_args()

    if not os.path.exists(args.trace):
        raise SystemExit(f"no such trace: {args.trace}")

    tp = ensure_tp(args.tp)
    os.makedirs(os.path.join(args.out, "csv"), exist_ok=True)
    tmpdir = os.path.join(args.out, ".sql")
    os.makedirs(tmpdir, exist_ok=True)

    stems = sorted(x[:-4] for x in os.listdir(QUERY_DIR) if x.endswith(".sql"))
    if args.only:
        wanted = [w.strip() for w in args.only.split(",")]
        stems = [s for s in stems if any(s.startswith(w) for w in wanted)]

    results, errors = {}, {}
    with Session(tp, args.trace) as sess:
        for stem in stems:
            src = os.path.join(QUERY_DIR, stem + ".sql")
            sql = open(src).read().replace("__PKG__", args.pkg)
            run_path = os.path.join(tmpdir, stem + ".sql")
            open(run_path, "w").write(sql)
            header, rows, err = sess.query_file(run_path)
            if err:
                errors[stem] = err
                sys.stderr.write(f"[triage] {stem}: {err.splitlines()[-1][:160]}\n")
                continue
            results[stem] = (header, rows)
            with open(os.path.join(args.out, "csv", stem + ".csv"), "w", newline="") as fh:
                w = csv.writer(fh)
                if header:
                    w.writerow(header)
                    w.writerows(rows)
            sys.stderr.write(f"[triage] {stem}: {len(rows)} rows\n")

    findings = analyse(results, args.pkg)

    # ---- report.md
    icon = {"blocker": "🛑", "high": "🔴", "medium": "🟠", "info": "🔵"}
    out = [f"# Perfetto triage — `{os.path.basename(args.trace)}`", ""]
    out.append(f"Package filter: `{args.pkg}`  ·  trace_processor {TP_VERSION}")
    out.append("")
    out.append("## Findings, worst first")
    out.append("")
    if not findings:
        out.append("Nothing crossed a threshold. Either the app is healthy in this "
                   "trace, or the trace does not cover the interaction you care about.")
    for i, f in enumerate(findings, 1):
        out.append(f"### {i}. {icon.get(f['severity'], '')} {f['title']}")
        out.append(f"*{f['area']} · {f['severity']}*")
        out.append("")
        out.append(f["detail"])
        out.append("")
        out.append(f"**What to do:** {f['action']}")
        out.append("")
    out.append("---")
    out.append("")
    out.append("## Evidence")
    out.append("")
    for title, group in SECTIONS:
        group = [s for s in group if s in results or s in errors]
        if not group:
            continue
        out.append(f"### {title}")
        out.append("")
        for stem in group:
            out.append(f"#### `{stem}`")
            desc = leading_comment(os.path.join(QUERY_DIR, stem + ".sql"))
            if desc:
                out.append("")
                out.append(f"> {desc}")
            out.append("")
            if stem in errors:
                out.append(f"```\nQUERY FAILED: {errors[stem]}\n```")
            else:
                h, r = results[stem]
                out.append(md_table(h, r))
            out.append("")

    report = os.path.join(args.out, "report.md")
    open(report, "w").write("\n".join(out))
    with open(os.path.join(args.out, "findings.json"), "w") as fh:
        json.dump({"trace": args.trace, "pkg": args.pkg, "findings": findings}, fh, indent=2)

    print(f"\n{len(findings)} finding(s). Report: {report}")
    for f in findings[:8]:
        print(f"  {icon.get(f['severity'], '')} [{f['area']}] {f['title']}")


if __name__ == "__main__":
    main()
