#!/usr/bin/env python3
"""
run_eval.py — score perfetto-triage against the known traps in
KNOWN-TRAPS.md, using the real fixture captures in fixtures/.

This does not require a device: it reads the fixture triage-out
directories already committed to this repo and checks whether the tool's
output contains the warning each trap requires. As of this commit, all
three traps are expected to FAIL, because none of them are fixed in
perfetto-triage yet — this script exists to make that gap visible and
re-runnable, not to claim a passing score that doesn't exist.

Usage:
    python3 run_eval.py
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")


def load_findings(rel_path):
    path = os.path.join(FIXTURES, rel_path, "findings.json")
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return json.load(fh)


def findings_text_only(findings):
    """Join only the human-readable fields of each finding (title, detail,
    action) — excludes the trace path and pkg name, which are raw file
    paths that can accidentally contain trigger words (e.g. a filename
    ending in '...pftrace' contains the substring 'race')."""
    parts = []
    for f in findings.get("findings", []):
        parts.append(f.get("title", ""))
        parts.append(f.get("detail", ""))
        parts.append(f.get("action", ""))
    return " ".join(parts).lower()


def has_word(text, word):
    """Whole-word match, not substring — 'race' must not match inside
    'trace'."""
    return re.search(r'\b' + re.escape(word) + r'\b', text) is not None


def load_csv_rows(rel_path, name):
    import csv
    path = os.path.join(FIXTURES, rel_path, "csv", name)
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return list(csv.DictReader(fh))


def trap_1_debug_build_warning():
    """A debug-build capture should produce a finding that warns about it.
    It currently does not — nothing in the query pack checks the
    debuggable flag."""
    findings = load_findings("debug-build-out")
    if findings is None:
        return "SKIP", "fixtures/debug-build-out/findings.json not found"

    text = findings_text_only(findings)
    has_warning = has_word(text, "debuggable") or "debug build" in text or "debug-build" in text

    if has_warning:
        return "PASS", "a finding mentions the debug build"
    return "FAIL", (
        f"{len(findings['findings'])} findings emitted, none mention "
        f"debuggable/debug build — the tool has no way to know this "
        f"capture is unreliable"
    )


def trap_2_startup_silence_is_explicit():
    """A trace with zero android_startups rows but healthy coverage
    elsewhere should produce an explicit 'startup measurement failed'
    finding, not silent omission."""
    findings = load_findings("startup-race-out")
    coverage = load_csv_rows("startup-race-out", "01_coverage.csv")
    startup_summary = load_csv_rows("startup-race-out", "10_startup_summary.csv")

    if findings is None or coverage is None or startup_summary is None:
        return "SKIP", "required fixture files not found"

    coverage_healthy = coverage and int(coverage[0].get("sched_rows", 0)) > 0
    startup_empty = len(startup_summary) == 0

    if not (coverage_healthy and startup_empty):
        return "SKIP", "this fixture does not reproduce the trap (startup data present)"

    text = findings_text_only(findings)
    has_explicit_flag = has_word(text, "startup") and (
        has_word(text, "missed") or has_word(text, "race") or "failed to capture" in text
    )

    if has_explicit_flag:
        return "PASS", "an explicit finding flags the missing startup measurement"
    return "FAIL", "startup queries returned zero rows silently, no finding calls this out"


def trap_3_health_vs_coverage_distinct():
    """00_health empty (good) and 01_coverage column=0 (bad) should be
    distinguishable in findings.json without reading the CSVs directly."""
    findings = load_findings("debug-build-out")
    if findings is None:
        return "SKIP", "fixtures/debug-build-out/findings.json not found"

    # look for an actual structural field, not a substring match against
    # finding text (an earlier version of this check false-matched "pass"
    # inside the unrelated phrase "measure/layout passes")
    has_pass_fail_field = "health_status" in findings or any(
        "health_status" in f for f in findings.get("findings", [])
    )
    if has_pass_fail_field:
        return "PASS", "findings include an explicit health pass/fail field"
    return "FAIL", (
        "findings.json has no explicit field distinguishing "
        "'00_health passed' from '01_coverage gap' — a consumer has to "
        "already know the difference to read this correctly"
    )


TRAPS = [
    ("Trap 1: debug-build contamination", trap_1_debug_build_warning),
    ("Trap 2: android_startups silent gap", trap_2_startup_silence_is_explicit),
    ("Trap 3: health vs coverage distinction", trap_3_health_vs_coverage_distinct),
]


def main():
    print("perfetto-triage-eval — known trap check\n")
    results = []
    for name, fn in TRAPS:
        status, detail = fn()
        results.append(status)
        marker = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}[status]
        print(f"[{marker}] {name}")
        print(f"       {detail}\n")

    passed = results.count("PASS")
    failed = results.count("FAIL")
    skipped = results.count("SKIP")
    print(f"{passed} passed, {failed} failed, {skipped} skipped, out of {len(TRAPS)} traps")

    if failed:
        print("\nFAIL here means perfetto-triage does not yet guard against a")
        print("real, documented incident. See KNOWN-TRAPS.md for the full")
        print("writeup of each one and what a real fix would look like.")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
