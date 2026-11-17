#!/usr/bin/env python3
"""
perfetto-triage-eval — do the known traps still bite?

Each trap in KNOWN-TRAPS.md is a real incident with a real fixture: the
triage-out directory from the capture that went wrong. A fixture holds two
things, and this script uses them differently:

  findings.json   FROZEN. What triage.py said on the day. This is the incident
                  record and is never regenerated. Shown as "at the incident".
  csv/*.csv       The raw query output from that same real trace. This script
                  feeds it back through the CURRENT triage.py (health_status +
                  analyse) and checks what the tool says today. Shown as "now".

So a PASS means: given the same real data that fooled it before, today's code
says the right thing. No trace file and no device are needed to run this.

Statuses:
  PASS      the current code handles the real incident data correctly
  FAIL      it does not
  UNPROVEN  the code path exists, but no real fixture exercises it yet. This
            is not a pass, and the exit code stays non-zero until it is closed.
  SKIP      fixture files are missing

    python3 run_eval.py
"""
import csv
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
TRIAGE_PY = os.path.join(os.path.dirname(HERE), "perfetto-triage", "scripts", "triage.py")


def load_triage():
    """Import the real triage.py. Importing it downloads nothing and touches
    no device; trace_processor is only fetched inside its main()."""
    spec = importlib.util.spec_from_file_location("triage", TRIAGE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_frozen(name):
    path = os.path.join(FIXTURES, name, "findings.json")
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def load_results(name):
    """csv/*.csv -> {stem: (header, rows)}, the shape triage.analyse() takes.
    A CSV with a header and no rows is a query that ran and returned nothing,
    which is exactly the case two of the three traps are about."""
    cdir = os.path.join(FIXTURES, name, "csv")
    if not os.path.isdir(cdir):
        return None
    results = {}
    for fn in sorted(os.listdir(cdir)):
        if not fn.endswith(".csv"):
            continue
        with open(os.path.join(cdir, fn), newline="") as fh:
            rows = list(csv.reader(fh))
        results[fn[:-4]] = (rows[0] if rows else [], rows[1:])
    return results


def rederive(triage, name):
    """Run today's triage logic over a fixture's real CSVs."""
    frozen = load_frozen(name)
    results = load_results(name)
    if frozen is None or results is None:
        return None
    pkg = frozen.get("pkg", "*")
    mode = triage.infer_mode(frozen.get("trace", ""))
    status = triage.health_status(results, pkg, mode)
    findings = triage.analyse(results, pkg, mode, status)
    return {"frozen": frozen, "results": results, "pkg": pkg, "mode": mode,
            "status": status, "findings": findings}


def titles(findings, area=None, severity=None):
    return [f["title"] for f in findings
            if (area is None or f["area"] == area)
            and (severity is None or f["severity"] == severity)]


# --------------------------------------------------------------------------
# Trap 1: debug-build contamination
# --------------------------------------------------------------------------

def trap_1_debug_build(triage):
    """A debuggable capture must be called out before anything is ranked.

    The original fixture predates 03_build_type.sql, so its CSVs carry no
    package_list data. Two checks follow from that:
      a) on the ORIGINAL data the tool must no longer be silent: it has to say
         the build type is unverified and refuse to call the trace trusted.
      b) on a REGENERATED debuggable fixture (debug-build-after-out, captured
         with the build-type query in place) it must say 'debuggable'.
    (b) is the real proof. Without that fixture this trap is UNPROVEN.
    """
    old = rederive(triage, "debug-build-out")
    if old is None:
        return "SKIP", ["fixtures/debug-build-out not found"]
    lines = [f"at the incident: {len(old['frozen']['findings'])} findings, "
             "no mention of the build type"]

    hs = old["status"]
    said_unverified = (hs["build_type"] == "unknown" and hs["verdict"] != "trusted"
                       and "Build type not verified" in titles(old["findings"], area="trace"))
    if not said_unverified:
        return "FAIL", lines + [
            f"now, same data: build_type={hs['build_type']}, verdict={hs['verdict']}, "
            "and no explicit 'Build type not verified' finding. Still silent."]
    lines.append(f"now, same data: build_type=unknown, verdict={hs['verdict']}, "
                 "explicit 'Build type not verified' finding. No longer silent.")

    new = rederive(triage, "debug-build-after-out")
    if new is None:
        return "UNPROVEN", lines + [
            "no regenerated fixture (fixtures/debug-build-after-out), so the "
            "debuggable=1 detection has not been shown on a real trace.",
            "to close: capture a debuggable build with --allow-debug, triage it "
            "into that directory, rerun. Steps in KNOWN-TRAPS.md."]
    if "03_build_type" not in new["results"]:
        return "FAIL", lines + ["debug-build-after-out has no 03_build_type.csv: "
                                "it was not produced by the current triage.py"]
    nhs = new["status"]
    blockers = titles(new["findings"], area="trace", severity="blocker")
    others = [f for f in new["findings"] if f["area"] != "trace"]
    ok = (nhs["build_type"] == "debuggable" and nhs["verdict"] != "trusted"
          and any("Debuggable build" in t for t in blockers)
          and all(f.get("directional_only") for f in others))
    if ok:
        return "PASS", lines + [
            f"regenerated fixture: build_type=debuggable, blocker finding raised, "
            f"all {len(others)} other findings marked directional_only"]
    return "FAIL", lines + [
        f"regenerated fixture: build_type={nhs['build_type']}, verdict={nhs['verdict']}, "
        f"trace blockers={blockers}"]


# --------------------------------------------------------------------------
# Trap 2: android_startups silent gap
# --------------------------------------------------------------------------

def trap_2_startup_gap(triage):
    """A startup capture with zero android_startups rows but healthy scheduler
    and slice data must produce an explicit 'the capture missed the launch'
    finding. A capture that did contain the launch must not."""
    race = rederive(triage, "startup-race-out")
    control = rederive(triage, "debug-build-out")
    if race is None or control is None:
        return "SKIP", ["required fixtures not found"]

    cov = triage.as_dicts(*race["results"].get("01_coverage", ([], [])))
    startup_rows = race["results"].get("10_startup_summary", ([], []))[1]
    reproduces = (race["mode"] == "startup" and not startup_rows and cov
                  and int(cov[0].get("sched_rows", 0)) > 0
                  and int(cov[0].get("slices", 0)) > 0)
    if not reproduces:
        return "SKIP", ["startup-race-out no longer reproduces the trap"]

    frozen_startup = titles(race["frozen"]["findings"], area="startup")
    lines = [f"at the incident: {len(race['frozen']['findings'])} findings, "
             f"{len(frozen_startup)} about startup. Silence."]

    flagged = (race["status"]["startup_capture"] == "missed"
               and "Startup capture missed the app launch"
               in titles(race["findings"], area="startup", severity="blocker"))
    if not flagged:
        return "FAIL", lines + [
            f"now, same data: startup_capture={race['status']['startup_capture']}, "
            "no blocker finding. Still silent."]
    lines.append("now, same data: startup_capture=missed, blocker finding "
                 "'Startup capture missed the app launch'")

    false_alarm = (control["status"]["startup_capture"] != "ok"
                   or any("missed" in t for t in titles(control["findings"], area="startup")))
    if false_alarm:
        return "FAIL", lines + [
            "control (debug-build-out, launch present) was wrongly flagged: "
            f"startup_capture={control['status']['startup_capture']}"]
    lines.append("control (debug-build-out, launch present): startup_capture=ok, not flagged")
    return "PASS", lines


# --------------------------------------------------------------------------
# Trap 3: health vs coverage
# --------------------------------------------------------------------------

def trap_3_health_vs_coverage(triage):
    """'00_health returned nothing' is the good case. '01_coverage has a zero'
    is the bad case. findings.json must say which is which in a structured
    field, and the field must agree with the fixture's own CSVs."""
    fx = rederive(triage, "debug-build-out")
    if fx is None:
        return "SKIP", ["fixtures/debug-build-out not found"]
    lines = ["at the incident: findings.json keys = "
             + ", ".join(sorted(fx["frozen"].keys())) + ". No health field."]

    hs = fx["status"]
    health_rows = fx["results"].get("00_health", ([], []))[1]
    cov = triage.as_dicts(*fx["results"].get("01_coverage", ([], [])))
    if not cov:
        return "SKIP", ["debug-build-out has no 01_coverage.csv"]

    problems = []
    expect_health = "fail" if health_rows else "pass"
    if hs.get("trace_health") != expect_health:
        problems.append(f"trace_health={hs.get('trace_health')}, CSV says {expect_health}")
    for col, key in triage.COVERAGE_COLUMNS:
        expect = "ok" if float(cov[0].get(col, 0) or 0) > 0 else "missing"
        got = hs.get("coverage", {}).get(key)
        if got != expect:
            problems.append(f"coverage.{key}={got}, CSV column {col} says {expect}")
    allowed = {"pass", "fail", "unknown"}
    if hs.get("trace_health") not in allowed:
        problems.append("trace_health is not one of pass/fail/unknown")
    report = "\n".join(triage.trust_table(hs))
    if "00_health" not in report or "01_coverage" not in report:
        problems.append("report.md trust table does not name both checks")

    if problems:
        return "FAIL", lines + ["now: " + "; ".join(problems)]
    cov_txt = ", ".join(f"{k}={v}" for k, v in hs["coverage"].items())
    return "PASS", lines + [
        f"now, same data: health_status.trace_health={hs['trace_health']} "
        f"(00_health had {len(health_rows)} rows)",
        f"now, same data: health_status.coverage: {cov_txt}",
        "both agree with the fixture CSVs, and report.md prints them as a Trust check table"]


TRAPS = [
    ("Trap 1: debug-build contamination", trap_1_debug_build),
    ("Trap 2: android_startups silent gap", trap_2_startup_gap),
    ("Trap 3: health vs coverage distinction", trap_3_health_vs_coverage),
]


def main():
    print("perfetto-triage-eval — known trap check")
    print("(frozen incident output vs. the current triage.py on the same real CSVs)\n")
    triage = load_triage()
    results = []
    for name, fn in TRAPS:
        status, lines = fn(triage)
        results.append(status)
        print(f"[{status}] {name}")
        for line in lines:
            print(f"       {line}")
        print()

    counts = {s: results.count(s) for s in ("PASS", "FAIL", "UNPROVEN", "SKIP")}
    print(f"{counts['PASS']} passed, {counts['FAIL']} failed, "
          f"{counts['UNPROVEN']} unproven, {counts['SKIP']} skipped, "
          f"out of {len(TRAPS)} traps")
    if counts["FAIL"]:
        print("\nFAIL means perfetto-triage does not guard against a real,")
        print("documented incident. See KNOWN-TRAPS.md.")
    if counts["UNPROVEN"]:
        print("\nUNPROVEN means the guard is written but no real capture has")
        print("exercised it. Do not report this trap as closed until it passes.")
    sys.exit(1 if (counts["FAIL"] or counts["UNPROVEN"]) else 0)


if __name__ == "__main__":
    main()
