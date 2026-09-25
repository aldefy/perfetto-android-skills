#!/usr/bin/env python3
"""
remeasure.py — Skill 2, step 3: diff two triage-out directories and print
a real before/after report.

Usage:
    python3 remeasure.py <before-out-dir> <after-out-dir>

Reads findings.json and csv/20_jank_summary.csv, csv/40_slice_hotspots.csv
from each directory (whichever exist — a startup-only capture won't have
jank CSVs, and that's fine, this script only reports what both sides have).
Does not run triage.py itself and does not capture anything: run
triage.py twice, on a real before-trace and a real after-trace, then point
this at both output directories.

This intentionally does not compute a single "% improvement" verdict. It
prints what changed, in each dimension, and leaves the read to you — a
single number invites picking the flattering one out of ten runs, which is
exactly what "measure, change one thing, measure again" exists to prevent.
"""
import argparse
import csv
import json
import os
import sys


def load_findings(out_dir):
    path = os.path.join(out_dir, "findings.json")
    if not os.path.exists(path):
        print(f"error: {path} not found — did you run triage.py with --out {out_dir}?", file=sys.stderr)
        sys.exit(1)
    with open(path) as fh:
        return json.load(fh)


def load_csv(out_dir, name):
    path = os.path.join(out_dir, "csv", name)
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return list(csv.DictReader(fh))


def diff_findings(before, after):
    before_titles = {f["title"]: f for f in before["findings"]}
    after_titles = {f["title"]: f for f in after["findings"]}

    resolved = [t for t in before_titles if t not in after_titles]
    new = [t for t in after_titles if t not in before_titles]
    persisted = [t for t in before_titles if t in after_titles]

    print("## Findings\n")
    print("(matched by exact title, which embeds live numbers — a finding whose")
    print(" count or ms changed will show up as both resolved and new. That's")
    print(" expected, not a bug: read it alongside the Jank/hotspots sections below.)\n")
    if resolved:
        print(f"Resolved ({len(resolved)}):")
        for t in resolved:
            print(f"  - [{before_titles[t]['severity']}] {t}")
        print()
    if new:
        print(f"New ({len(new)}) — did the fix introduce these, or were they always there and just outranked?")
        for t in new:
            print(f"  - [{after_titles[t]['severity']}] {t}")
        print()
    if persisted:
        print(f"Still present ({len(persisted)}):")
        for t in persisted:
            print(f"  - [{after_titles[t]['severity']}] {t}")
        print()
    if not resolved and not new and not persisted:
        print("(no findings in either run)\n")


def diff_jank(before_dir, after_dir, pkg):
    b = load_csv(before_dir, "20_jank_summary.csv")
    a = load_csv(after_dir, "20_jank_summary.csv")
    if b is None or a is None:
        return
    b_row = next((r for r in b if r["process_name"] == pkg), None)
    a_row = next((r for r in a if r["process_name"] == pkg), None)
    if not b_row or not a_row:
        return
    print("## Jank\n")
    print(f"  {'':28} {'before':>10} {'after':>10}")
    for key, label in [
        ("janky_pct", "janky %"),
        ("worst_overrun_ms", "worst overrun (ms)"),
        ("app_jank", "app-attributed"),
        ("sf_jank", "SurfaceFlinger-attributed"),
    ]:
        b_val = float(b_row.get(key, 0) or 0)
        a_val = float(a_row.get(key, 0) or 0)
        print(f"  {label:28} {b_val:>10.1f} {a_val:>10.1f}")
    print()


def diff_hotspots(before_dir, after_dir, top_n=5):
    b = load_csv(before_dir, "40_slice_hotspots.csv")
    a = load_csv(after_dir, "40_slice_hotspots.csv")
    if b is None or a is None:
        return
    print(f"## Top {top_n} self-time hotspots\n")
    print("before:")
    for row in b[:top_n]:
        print(f"  {row['self_ms']:>10} ms  {row['name']} ({row['thread_name']})")
    print("\nafter:")
    for row in a[:top_n]:
        print(f"  {row['self_ms']:>10} ms  {row['name']} ({row['thread_name']})")
    print()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("before_dir", help="triage-out directory from the baseline capture")
    p.add_argument("after_dir", help="triage-out directory from the post-fix capture")
    p.add_argument("--pkg", help="package name, for the jank-summary row lookup (defaults to the pkg in before's findings.json)")
    args = p.parse_args()

    before = load_findings(args.before_dir)
    after = load_findings(args.after_dir)
    pkg = args.pkg or before.get("pkg") or after.get("pkg")

    print(f"# Remeasure: {before.get('pkg', '?')}")
    print(f"before: {before.get('trace', '?')}")
    print(f"after:  {after.get('trace', '?')}\n")

    diff_findings(before, after)
    if pkg:
        diff_jank(args.before_dir, args.after_dir, pkg)
    diff_hotspots(args.before_dir, args.after_dir)

    print("Reminder: this is one run each unless you generated before_dir and")
    print("after_dir from a median of >=10 captures. Say that plainly if you")
    print("report these numbers anywhere.")


if __name__ == "__main__":
    main()
