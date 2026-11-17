#!/usr/bin/env python3
"""
Run the Perfetto triage query pack against a trace and emit a ranked report.

This is the machine half of the trace-reading checklist: it answers, in order,
the questions a senior engineer asks when a trace lands on their desk.

  ./triage.py trace.perfetto-trace --pkg com.example.app

Outputs into --out (default ./triage-out):
  report.md      human-readable report, one section per question
  findings.json  machine-readable verdicts plus a health_status block, for an
                 agent to act on
  report.html    the same findings as a page for a person: charts in the
                 Perfetto UI's colours, each with a plain explanation
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
import threading
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


def log(msg: str) -> None:
    """One progress line on stderr, flushed, so a caller tailing the output
    always sees what triage is doing right now."""
    sys.stderr.write(f"[triage] {msg}\n")
    sys.stderr.flush()


class Heartbeat:
    """While a slow step runs, say so every `every` seconds. Without this a
    long query on a large trace is indistinguishable from a hang."""

    def __init__(self, label: str, every: float = 20.0):
        self.label, self.every = label, every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.wait(self.every):
            log(f"{self.label}: still running, {int(time.time() - self.t0)}s so far")

    def __enter__(self):
        self.t0 = time.time()
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join(timeout=1)


class Session:
    """A warm trace_processor session: parse the trace once, query it many times."""

    def __init__(self, tp: str, trace: str):
        self.tp = tp
        self.trace = trace
        self.name = f"triage-{uuid.uuid4().hex[:8]}"
        self.remote = True

    def __enter__(self):
        try:
            size_mb = os.path.getsize(self.trace) / 1e6
        except OSError:
            size_mb = 0.0
        log(f"loading the trace into trace_processor ({size_mb:.0f} MB)")
        with Heartbeat("loading the trace"):
            self._start()
        return self

    def _start(self):
        started = time.time()
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
                    log(f"trace loaded in {time.time() - started:.0f}s")
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

    def restart(self):
        """Throw the server away and load the trace again. A timed-out query
        keeps running inside the old server, and every later query would
        queue behind it."""
        self.__exit__()
        self.name = f"triage-{uuid.uuid4().hex[:8]}"
        self.remote = True
        log("restarting trace_processor after a timeout")
        with Heartbeat("loading the trace"):
            self._start()

    def query_file(self, path: str, timeout: float = 600) -> tuple[list[str], list[list[str]], str]:
        target = ["--remote", self.name] if self.remote else [self.trace]
        try:
            proc = subprocess.run(
                [self.tp, "query", *target, "-f", path],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return [], [], f"timed out after {timeout:g}s"
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

COVERAGE_COLUMNS = [
    # (01_coverage column, key in health_status["coverage"])
    ("slices", "slices"),
    ("sched_rows", "sched"),
    ("frametimeline_rows", "frametimeline"),
    ("blocked_reason_rows", "blocked_reasons"),
    ("binder_slices", "binder"),
]


def infer_mode(trace_path: str) -> str:
    """capture.sh names traces <pkg>-<mode>-<stamp>.pftrace. Anything else is
    'unknown': we do not guess what the person meant to capture."""
    base = os.path.basename(trace_path or "")
    if "-startup-" in base:
        return "startup"
    if "-interactive-" in base:
        return "interactive"
    return "unknown"


def health_status(results: dict, pkg: str, mode: str = "unknown") -> dict:
    """Explicit pass/fail for the trust checks, so a reader never has to infer
    'healthy' from the absence of a finding.

    Every value is a closed set. 'unknown' always means the check did not run
    or could not decide. It never means the check passed.

      trace_health     pass | fail | unknown        (00_health: zero rows = pass)
      coverage.<src>   ok | missing | unknown       (01_coverage: zero = missing)
      build_type       release | debuggable | unknown   (03_build_type)
      startup_capture  ok | missed | not_expected | unknown   (10_startup_summary)
      verdict          trusted | directional_only | not_trustworthy
    """
    def table(stem):
        h, r = results.get(stem, ([], []))
        return as_dicts(h, r)

    hs = {"mode": mode}
    reasons = []

    # 00_health: an EMPTY result is the good case.
    if "00_health" not in results:
        hs["trace_health"] = "unknown"
        reasons.append("00_health did not run")
    elif table("00_health"):
        hs["trace_health"] = "fail"
        reasons.append("trace_processor reported errors or dropped data")
    else:
        hs["trace_health"] = "pass"

    # 01_coverage: a ZERO in a column is the bad case.
    cov = table("01_coverage")
    hs["coverage"] = {}
    for col, key in COVERAGE_COLUMNS:
        if not cov or col not in cov[0]:
            hs["coverage"][key] = "unknown"
        else:
            hs["coverage"][key] = "ok" if (num(cov[0].get(col)) or 0) > 0 else "missing"
    if not cov:
        reasons.append("01_coverage did not run")
    # Gaps scope what the trace can answer. Only sched/slices decide the verdict.
    hs["coverage_gaps"] = [k for k, v in hs["coverage"].items() if v == "missing"]

    # 03_build_type: needs a named package. With --pkg '*' every package on the
    # device matches, and "is the app under test debuggable" has no answer.
    hs["build_type"] = "unknown"
    builds = table("03_build_type")
    if "03_build_type" not in results:
        hs["build_type_detail"] = "03_build_type did not run on this trace"
    elif pkg in ("*", ""):
        hs["build_type_detail"] = "no package named; pass --pkg to check the build type"
    elif not builds:
        hs["build_type_detail"] = ("package_list has no row for this package "
                                   "(android.packages_list data source missing?)")
    else:
        dbg = [r.get("package_name", "?") for r in builds
               if str(r.get("debuggable", "")).strip().lower() in ("1", "true")]
        if dbg:
            hs["build_type"] = "debuggable"
            hs["build_type_detail"] = "debuggable=1 for " + ", ".join(dbg[:5])
        else:
            hs["build_type"] = "release"
            hs["build_type_detail"] = "debuggable=0 for " + ", ".join(
                r.get("package_name", "?") for r in builds[:5])
    if hs["build_type"] == "debuggable":
        reasons.append("debuggable build")
    elif hs["build_type"] == "unknown":
        reasons.append("build type not verified")

    # 10_startup_summary: zero rows is only a failure when a launch was expected.
    if "10_startup_summary" not in results:
        hs["startup_capture"] = "unknown"
    else:
        launches = [r for r in table("10_startup_summary")
                    if pkg in ("*", "") or _glob(r.get("package", ""), pkg)]
        if launches:
            hs["startup_capture"] = "ok"
        elif mode == "startup":
            hs["startup_capture"] = "missed"
            reasons.append("startup capture missed the app launch")
        elif mode == "interactive":
            hs["startup_capture"] = "not_expected"
        else:
            hs["startup_capture"] = "unknown"

    core_missing = [k for k in ("sched", "slices") if hs["coverage"].get(k) == "missing"]
    if core_missing:
        reasons.append("no data for: " + ", ".join(core_missing))
    if hs["trace_health"] == "fail" or core_missing:
        hs["verdict"] = "not_trustworthy"
    elif (hs["build_type"] != "release" or hs["trace_health"] == "unknown"
          or not cov or hs["startup_capture"] == "missed"):
        hs["verdict"] = "directional_only"
    else:
        hs["verdict"] = "trusted"
    hs["reasons"] = reasons
    return hs


def analyse(results: dict, pkg: str, mode: str = "unknown",
            status: dict | None = None) -> list[dict]:
    """results: {query_stem: (header, rows)}. Returns ordered findings."""
    f = []
    hs = status if status is not None else health_status(results, pkg, mode)

    src = []  # query stems the next finding is derived from

    def add(severity, area, title, detail, action, source=None):
        f.append({"severity": severity, "area": area, "title": title,
                  "detail": detail, "action": action,
                  "source": list(source if source is not None else src)})

    def table(stem):
        # Reading a table makes it the evidence for the findings that follow,
        # so every finding names the CSV its numbers can be re-checked in.
        src[:] = [stem]
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

    if hs["build_type"] == "debuggable":
        src[:] = ["03_build_type"]
        add("blocker", "trace", "Debuggable build: every timing here is directional only",
            f"package_list reports {hs['build_type_detail']}. A debuggable build "
            "runs 2 to 5x slower than release, with JIT and ART checks that release "
            "users never pay for.",
            "Install a release or release-like (non-debuggable) build and recapture "
            "before reporting any number from this trace. The findings below still "
            "point at real code paths, but their sizes are not real.")
    elif hs["build_type"] == "unknown":
        src[:] = ["03_build_type"]
        add("info", "trace", "Build type not verified",
            f"{hs['build_type_detail']}. This report cannot tell whether the app "
            "was a release build.",
            "Run `adb shell dumpsys package <pkg> | grep -i debuggable` (any output "
            "means debuggable), or recapture with scripts/capture.sh, which refuses "
            "debuggable builds unless told otherwise.")

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

    # --- 0b. Did a startup capture actually contain the launch? -----------
    if hs["startup_capture"] == "missed":
        rest = ("the rest of the trace is populated (scheduler and slice data "
                "present), so the capture window did not line up with the launch"
                if hs["coverage"].get("sched") == "ok" and hs["coverage"].get("slices") == "ok"
                else "core data sources are missing too, so the capture itself is broken")
        src[:] = ["10_startup_summary", "01_coverage"]
        add("blocker", "startup", "Startup capture missed the app launch",
            f"android_startups has no launch for this package, and {rest}. "
            "This is a failed measurement, not a healthy startup.",
            "Recapture. If it repeats, lengthen the delay between starting the "
            "trace and force-stopping the app in scripts/capture.sh. Do not read "
            "the absence of startup findings as 'startup is fine'.")
    elif hs["startup_capture"] == "unknown" and "10_startup_summary" in results:
        src[:] = ["10_startup_summary"]
        add("info", "startup", "No app launch found in this trace",
            "android_startups has no launch for this package, and the capture mode "
            "is not known, so this may or may not be expected.",
            "If this was meant to be a cold-start capture, the launch was missed: "
            "recapture. If you were measuring scrolling or another interaction, "
            "pass --mode interactive to say so.")

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
    # The findings after the summary (thread split, hot slices) describe where
    # janky-frame time went. They can never matter more than the jank itself:
    # "cost is on the RenderThread" at medium is noise when 0.3% of frames are
    # janky. So they are capped at the summary's severity, and say the rate.
    rank = {"blocker": 0, "high": 1, "medium": 2, "info": 3}
    jank_cap = "info" if "20_jank_summary" in results else None
    jank_ctx = ""

    def capped(sev):
        if jank_cap is None:
            return sev
        return sev if rank[sev] >= rank[jank_cap] else jank_cap

    for r in table("20_jank_summary"):
        if pkg not in ("*", "") and not _glob(r.get("process_name", ""), pkg):
            continue
        frames = num(r.get("frames")) or 0
        janky = num(r.get("janky")) or 0
        pct = num(r.get("janky_pct")) or 0
        app_j = num(r.get("app_jank")) or 0
        sf_j = num(r.get("sf_jank")) or 0
        worst = num(r.get("worst_overrun_ms")) or 0
        if frames >= 10 and not jank_ctx:
            jank_ctx = f" Jank rate: {int(janky)} of {int(frames)} frames ({pct:.1f}%)."
        if frames < 10 or janky == 0:
            continue
        sev = "high" if pct > 5 else "medium" if pct > 1 else "info"
        if rank[sev] < rank[jank_cap]:
            jank_cap = sev
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
        numbers = (f"doFrame {ui:.0f} ms vs DrawFrame {rt:.0f} ms "
                   f"(vsync delay {vs:.0f} ms).{jank_ctx}")
        if abs(ui - rt) <= 0.10 * max(ui, rt):
            # Within 10% of each other: naming one thread would be a coin toss.
            add(capped("medium"), "jank",
                f"{r['process_name']}: cost is split between the UI thread and RenderThread",
                numbers,
                "Neither thread dominates. Check both: measure/layout and recomposition "
                "on the UI thread, overdraw and shader work on the RenderThread.")
        elif rt > ui:
            add(capped("medium"), "jank", f"{r['process_name']}: cost is on the RenderThread",
                numbers,
                "Look for overdraw, large/unbounded bitmaps, expensive shaders, and "
                "hardware-layer thrash. Shader compilation on first use is a classic here.")
        else:
            add(capped("medium"), "jank", f"{r['process_name']}: cost is on the UI thread",
                numbers,
                "Look for measure/layout passes, recomposition, main-thread IO or "
                "deserialisation, and work posted onto the main Handler.")
        break

    hot = table("23_jank_hot_slices")
    shader = [r for r in hot if re.search(
        r"CreateGraphicsPipeline|CompileAfterCacheMiss|shader|Program", r["name"], re.I)]
    if shader:
        tot = sum(num(r["total_ms"]) or 0 for r in shader)
        add(capped("high"), "jank",
            f"Shader / pipeline compilation during janky frames ({tot:.0f} ms)",
            "Slices matched: " + ", ".join(r["name"] for r in shader[:3]) + "." + jank_ctx,
            "This is first-run-only jank that every user hits. Warm it up: run the "
            "animation once behind a splash, or ship a Baseline Profile — "
            "ProfileInstaller also primes the shader cache.")
    inflate = [r for r in hot if re.search(r"inflate|measure|layout|traversal", r["name"], re.I)]
    if inflate:
        tot = sum(num(r["total_ms"]) or 0 for r in inflate)
        add(capped("medium"), "jank",
            f"Measure/layout/traversal cost during janky frames ({tot:.0f} ms)",
            "Slices matched: " + ", ".join(r["name"] for r in inflate[:3]) + "." + jank_ctx,
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
    # Every finding says whether its numbers can be quoted. Trust findings
    # (area "trace") describe the capture itself, so they are never directional.
    for x in f:
        x["directional_only"] = hs["verdict"] != "trusted" and x["area"] != "trace"
    return f


def unfinished_findings(errors: dict) -> list[dict]:
    """A query that failed or timed out leaves an empty section, and an empty
    section reads as 'nothing wrong'. Say plainly that it was never answered."""
    out = []
    for stem, err in sorted(errors.items()):
        timed_out = err.startswith("timed out")
        out.append({
            "severity": "blocker", "area": "trace",
            "title": f"Query {stem} did not finish",
            "detail": (f"It {err}. " if timed_out else
                       f"It failed: {err.splitlines()[-1][:200]}. ")
                      + "Its section of this report is empty because the question was "
                        "never answered, not because nothing was found.",
            "action": ("Run triage again with a larger --query-timeout, or capture a "
                       "shorter window." if timed_out else
                       "Run the query by hand with trace_processor to see the full error."),
            "source": [stem], "directional_only": False})
    return out


def trust_table(hs: dict) -> list[str]:
    """The 'Trust check' block at the top of report.md."""
    mark = {"pass": "PASS", "ok": "PASS", "release": "PASS", "trusted": "PASS",
            "fail": "FAIL", "missing": "FAIL", "debuggable": "FAIL", "missed": "FAIL",
            "not_trustworthy": "FAIL", "directional_only": "CAUTION",
            "not_expected": "n/a"}
    rows = [("Trace health (`00_health`)", hs["trace_health"],
             "zero error rows is the pass case")]
    for key, val in hs["coverage"].items():
        rows.append((f"Coverage: {key} (`01_coverage`)", val,
                     "zero rows means that data source was never recorded"))
    rows.append(("Build type (`03_build_type`)", hs["build_type"],
                 hs.get("build_type_detail", "")))
    rows.append(("Startup capture (`10_startup_summary`)", hs["startup_capture"],
                 f"capture mode: {hs['mode']}"))
    out = ["| Check | Result | Status | Note |", "|---|---|---|---|"]
    for name, val, note in rows:
        out.append(f"| {name} | {mark.get(val, 'UNKNOWN')} | `{val}` | {note} |")
    out.append("")
    verdict = {"trusted": "**Verdict: trusted.** Numbers below can be quoted with "
                          "the device, build type and run count.",
               "directional_only": "**Verdict: directional only.** The findings point "
                                   "at real code paths, but do not quote the numbers.",
               "not_trustworthy": "**Verdict: not trustworthy.** Fix the capture "
                                  "before reading anything below."}[hs["verdict"]]
    out.append(verdict)
    if hs["reasons"]:
        out.append("")
        out.append("Why: " + "; ".join(hs["reasons"]) + ".")
    if hs.get("coverage_gaps"):
        out.append("")
        out.append("Not answerable from this trace: " + ", ".join(hs["coverage_gaps"]) + ".")
    return out


def _glob(value: str, pattern: str) -> bool:
    if pattern in ("", "*"):
        return True
    import fnmatch
    return fnmatch.fnmatch(value or "", pattern)


# --------------------------------------------------------------------------

STARTUP_STEMS = {"10_startup_summary", "11_startup_breakdown",
                 "12_startup_main_thread_state", "13_startup_top_slices",
                 "14_startup_class_loading", "15_startup_binder"}

SECTIONS = [
    ("Is this trace trustworthy?",
     ["00_health", "01_coverage", "02_processes", "03_build_type"]),
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
    ap.add_argument("--mode", choices=["auto", "startup", "interactive"], default="auto",
                    help="what the capture was meant to contain. 'auto' reads it from "
                         "a capture.sh filename (<pkg>-startup-*.pftrace). In startup "
                         "mode a trace with no app launch is reported as a failed "
                         "capture instead of silently having no startup findings.")
    ap.add_argument("--app-repo", default=None,
                    help="the app's source tree. report.html takes its colours from the "
                         "app's own theme tokens when it can find them.")
    ap.add_argument("--query-timeout", type=float, default=600,
                    help="seconds one query may run before it is abandoned and "
                         "reported as not finished (default 600)")
    ap.add_argument("--no-html", action="store_true",
                    help="skip report.html (the visual report)")
    args = ap.parse_args()
    mode = infer_mode(args.trace) if args.mode == "auto" else args.mode

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
    started = time.time()
    log(f"{len(stems)} queries to run. Phases: load, query, report.")
    with Session(tp, args.trace) as sess:
        for i, stem in enumerate(stems, 1):
            tag = f"[{i}/{len(stems)}] {stem}"
            src = os.path.join(QUERY_DIR, stem + ".sql")
            sql = open(src).read().replace("__PKG__", args.pkg)
            run_path = os.path.join(tmpdir, stem + ".sql")
            open(run_path, "w").write(sql)
            t0 = time.time()
            with Heartbeat(tag):
                header, rows, err = sess.query_file(run_path, args.query_timeout)
            took = time.time() - t0
            if err:
                errors[stem] = err
                log(f"{tag}: FAILED after {took:.1f}s: {err.splitlines()[-1][:160]}")
                if err.startswith("timed out") and sess.remote:
                    sess.restart()
                continue
            results[stem] = (header, rows)
            with open(os.path.join(args.out, "csv", stem + ".csv"), "w", newline="") as fh:
                w = csv.writer(fh)
                if header:
                    w.writerow(header)
                    w.writerows(rows)
            log(f"{tag}: {len(rows)} rows in {took:.1f}s")

    log("queries done, working out the findings")
    status = health_status(results, args.pkg, mode)
    findings = analyse(results, args.pkg, mode, status)
    findings = unfinished_findings(errors) + findings

    # ---- report.md
    icon = {"blocker": "🛑", "high": "🔴", "medium": "🟠", "info": "🔵"}
    out = [f"# Perfetto triage — `{os.path.basename(args.trace)}`", ""]
    out.append(f"Package filter: `{args.pkg}`  ·  capture mode: `{mode}`  ·  "
               f"trace_processor {TP_VERSION}")
    out.append("")
    out.append("## Trust check")
    out.append("")
    out.extend(trust_table(status))
    out.append("")
    out.append("## Findings, worst first")
    out.append("")
    if not findings:
        out.append("Nothing crossed a threshold. Either the app is healthy in this "
                   "trace, or the trace does not cover the interaction you care about.")
    for i, f in enumerate(findings, 1):
        out.append(f"### {i}. {icon.get(f['severity'], '')} {f['title']}")
        tag = " · directional only" if f.get("directional_only") else ""
        out.append(f"*{f['area']} · {f['severity']}{tag}*")
        out.append("")
        out.append(f["detail"])
        out.append("")
        if f.get("source"):
            out.append("Measured in: " + ", ".join(f"`{x}`" for x in f["source"])
                       + " (see Evidence, and `csv/`).")
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
                if not r and stem in STARTUP_STEMS and status["startup_capture"] == "not_expected":
                    out.append("_Not applicable: this is an interactive capture, so no "
                               "app launch is expected in it._\n")
                elif not r and stem in STARTUP_STEMS and status["startup_capture"] == "missed":
                    out.append("_No rows: the capture missed the app launch. This is a "
                               "failed measurement, see the Trust check._\n")
                else:
                    out.append(md_table(h, r))
            out.append("")

    report = os.path.join(args.out, "report.md")
    log("writing report.md and findings.json")
    open(report, "w").write("\n".join(out))
    with open(os.path.join(args.out, "findings.json"), "w") as fh:
        json.dump({"trace": args.trace, "pkg": args.pkg, "mode": mode,
                   "health_status": status, "findings": findings}, fh, indent=2)

    visual = None
    if not args.no_html:
        log("writing report.html")
        try:  # the visual report is a convenience and must never fail a triage
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "visual_report", os.path.join(HERE, "visual_report.py"))
            vr = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(vr)
            visual = vr.write(args.out, app_repo=args.app_repo)
        except Exception as exc:
            log(f"visual report skipped: {exc}")
    log(f"done in {time.time() - started:.0f}s")

    print(f"\nTrust: {status['verdict']}"
          + (f" ({'; '.join(status['reasons'])})" if status["reasons"] else ""))
    print(f"{len(findings)} finding(s). Report: {report}")
    if visual:
        print(f"Visual report: {visual}")
    for f in findings[:8]:
        print(f"  {icon.get(f['severity'], '')} [{f['area']}] {f['title']}")


if __name__ == "__main__":
    main()
