#!/usr/bin/env python3
"""
remeasure.py: compare a before and an after, from triage-out directories.

One run each:
    python3 remeasure.py <before-out-dir> <after-out-dir>

Many runs each, which is what a quotable number needs:
    python3 remeasure.py --before runs/before-* --after runs/after-*
    python3 remeasure.py --before runs/before --after runs/after   # folders of triage-out dirs

With many runs it prints, per metric, the median, the range, the change in
the median, and whether that change is bigger than the run-to-run spread:

    clear   every after run is on one side of every before run
    likely  each side's median sits outside the other side's middle half
    noise   the two sets of runs overlap too much to tell apart

It does not run triage.py and does not capture anything. It also does not
boil the comparison down to one "% improvement": a single number invites
picking the flattering run, which is what "measure, change one thing,
measure again" exists to prevent.
"""
import argparse
import csv
import fnmatch
import json
import os
import re
import statistics
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


def trust_line(label, data):
    """One line per side from findings.json's health_status block. Reports
    written before that block existed have none, and say so."""
    hs = data.get("health_status")
    if not hs:
        return f"{label}: no health_status in findings.json (older triage.py). Trust not checked."
    why = f" ({'; '.join(hs.get('reasons', []))})" if hs.get("reasons") else ""
    return (f"{label}: {hs.get('verdict', '?')}, build {hs.get('build_type', '?')}, "
            f"startup capture {hs.get('startup_capture', '?')}{why}")


def diff_trust(before, after):
    print("## Trust\n")
    print("  " + trust_line("before", before))
    print("  " + trust_line("after ", after))
    b, a = before.get("health_status") or {}, after.get("health_status") or {}
    warnings = []
    if b and a and b.get("build_type") != a.get("build_type"):
        warnings.append(f"build types differ ({b.get('build_type')} vs {a.get('build_type')}): "
                        "this is not a like-for-like comparison")
    for label, hs in (("before", b), ("after", a)):
        if hs and hs.get("verdict") != "trusted":
            warnings.append(f"{label} is {hs.get('verdict')}: do not quote its numbers")
    for w in warnings:
        print(f"  WARNING: {w}")
    print()


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


# --------------------------------------------------------------------------
# many runs per side
# --------------------------------------------------------------------------

NULL = "[NULL]"
NAMED = ("trace section", "source file", "class.method")


def _num(v):
    try:
        return None if v in (None, "", NULL) else float(v)
    except (TypeError, ValueError):
        return None


def _matches(name, pkg):
    if not pkg or pkg == "*":
        return True
    return fnmatch.fnmatchcase(name or "", pkg) or (name or "") == pkg.rstrip("*")


def expand_runs(paths):
    """Each path is a triage-out directory, or a folder that holds several."""
    runs = []
    for path in paths:
        if os.path.exists(os.path.join(path, "findings.json")):
            runs.append(path)
            continue
        if os.path.isdir(path):
            inner = sorted(os.path.join(path, d) for d in os.listdir(path)
                           if os.path.exists(os.path.join(path, d, "findings.json")))
            if inner:
                runs.extend(inner)
                continue
        print(f"error: {path} is not a triage-out directory and holds none", file=sys.stderr)
        sys.exit(1)
    return runs


def run_metrics(out_dir, pkg):
    """The numbers worth comparing from one run: {metric label: value}."""
    m = {}

    def row_for(rows, key):
        if not rows:
            return None
        return next((r for r in rows if _matches(r.get(key, ""), pkg)), rows[0])

    s = row_for(load_csv(out_dir, "10_startup_summary.csv"), "package")
    if s:
        for key, label in (("ttid_ms", "cold start TTID (ms)"),
                           ("startup_ms", "startup total (ms)"), ("ttfd_ms", "TTFD (ms)")):
            v = _num(s.get(key))
            if v is not None:
                m[label] = v
    states = load_csv(out_dir, "12_startup_main_thread_state.csv") or []
    total = sum(_num(r.get("ms")) or 0 for r in states)
    if total:
        running = sum(_num(r.get("ms")) or 0 for r in states
                      if r.get("main_thread_state") == "Running")
        m["main thread running in startup (%)"] = 100.0 * running / total
    c = row_for(load_csv(out_dir, "14_startup_class_loading.csv"), "package")
    if c:
        for key, label in (("classes_loaded", "classes loaded in startup"),
                           ("ms", "class loading (ms)")):
            v = _num(c.get(key))
            if v is not None:
                m[label] = v
    jank = load_csv(out_dir, "20_jank_summary.csv") or []
    j = next((r for r in jank if _matches(r.get("process_name", ""), pkg)), None) \
        if pkg and pkg != "*" else (jank[0] if jank else None)
    if j:
        for key, label in (("frames", "frames"), ("janky_pct", "late frames (%)"),
                           ("worst_overrun_ms", "worst overrun (ms)"),
                           ("app_jank", "late frames blamed on the app")):
            v = _num(j.get(key))
            if v is not None:
                m[label] = v
    # the app's own trace sections, when triage was run with --app-repo
    try:
        with open(os.path.join(out_dir, "findings.json")) as fh:
            owned = {x["name"] for x in (json.load(fh).get("code_map") or {}).get("slices", [])
                     if x.get("how") in NAMED}
    except (OSError, ValueError):
        owned = set()
    if owned:
        startup = {r.get("slice_name"): _num(r.get("total_ms"))
                   for r in load_csv(out_dir, "13_startup_top_slices.csv") or []}
        hot = {r.get("name"): _num(r.get("self_ms"))
               for r in load_csv(out_dir, "40_slice_hotspots.csv") or []}
        for name in sorted(owned):
            v = startup.get(name)
            if v is None:
                v = hot.get(name)
            if v is not None:
                m[f"your code: {name} (ms)"] = v
    for r in load_csv(out_dir, "24_jank_by_app_section.csv") or []:
        v = _num(r.get("late_frames"))
        if v is not None and r.get("section"):
            m[f"late frames inside {r['section']}"] = v
    return m


def _middle_half(values):
    """(q1, q3) by linear interpolation. Fine for the 5 to 30 runs this sees."""
    xs = sorted(values)

    def q(p):
        k = (len(xs) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
        return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)
    return q(0.25), q(0.75)


def judge(before, after):
    """'clear' | 'likely' | 'noise' | 'too few runs' | 'no change'."""
    if len(before) < 3 or len(after) < 3:
        return "too few runs"
    mb, ma = statistics.median(before), statistics.median(after)
    if mb == ma:
        return "no change"
    if max(after) < min(before) or min(after) > max(before):
        return "clear"
    b1, b3 = _middle_half(before)
    a1, a3 = _middle_half(after)
    if (ma < b1 or ma > b3) and (mb < a1 or mb > a3):
        return "likely"
    return "noise"


def summarise(before_runs, after_runs, pkg):
    """[{metric, before:{n,median,min,max}, after:{...}, delta, delta_pct, verdict}]"""
    sides = {"before": [run_metrics(d, pkg) for d in before_runs],
             "after": [run_metrics(d, pkg) for d in after_runs]}
    order = []
    for runs in sides.values():
        for m in runs:
            for k in m:
                if k not in order:
                    order.append(k)
    rows = []
    for metric in order:
        b = [m[metric] for m in sides["before"] if metric in m]
        a = [m[metric] for m in sides["after"] if metric in m]
        if not b or not a:
            continue

        def stat(xs):
            return {"n": len(xs), "median": statistics.median(xs), "min": min(xs), "max": max(xs)}
        sb, sa = stat(b), stat(a)
        delta = sa["median"] - sb["median"]
        rows.append({"metric": metric, "before": sb, "after": sa, "delta": delta,
                     "delta_pct": (100.0 * delta / sb["median"]) if sb["median"] else None,
                     "verdict": judge(b, a)})
    return rows


def _title_key(title):
    """Finding titles carry live numbers. Blank them so the same finding
    matches across runs."""
    return re.sub(r"\d+(?:[.,]\d+)*", "#", title)


def many_trust(label, runs):
    verdicts, builds = {}, set()
    for d in runs:
        hs = load_findings(d).get("health_status") or {}
        v = hs.get("verdict", "not recorded")
        verdicts[v] = verdicts.get(v, 0) + 1
        builds.add(hs.get("build_type", "not recorded"))
    told = ", ".join(f"{n} {v}" for v, n in sorted(verdicts.items()))
    print(f"  {label}: {len(runs)} runs ({told}), build {', '.join(sorted(builds))}")
    return verdicts, builds


def many_findings(before_runs, after_runs):
    def tally(runs):
        seen = {}
        for d in runs:
            for key in {_title_key(f["title"]) for f in load_findings(d)["findings"]}:
                seen[key] = seen.get(key, 0) + 1
        return seen
    b, a = tally(before_runs), tally(after_runs)
    nb, na = len(before_runs), len(after_runs)
    gone = [k for k in b if b[k] * 2 >= nb and a.get(k, 0) * 2 < na]
    came = [k for k in a if a[k] * 2 >= na and b.get(k, 0) * 2 < nb]
    print("## Findings (numbers in titles blanked to #, so runs can be matched)\n")
    if gone:
        print("In most before runs, in few or no after runs:")
        for k in gone:
            print(f"  - {k}  ({b[k]}/{nb} before, {a.get(k, 0)}/{na} after)")
        print()
    if came:
        print("In most after runs, in few or no before runs. Did the fix cause these?")
        for k in came:
            print(f"  - {k}  ({b.get(k, 0)}/{nb} before, {a[k]}/{na} after)")
        print()
    if not gone and not came:
        print("The same findings show up on both sides.\n")


def compare_many(before_runs, after_runs, pkg, as_json=False):
    rows = summarise(before_runs, after_runs, pkg)
    if as_json:
        print(json.dumps({"pkg": pkg, "before_runs": before_runs, "after_runs": after_runs,
                          "metrics": rows}, indent=2))
        return rows
    print(f"# Remeasure: {pkg or '?'}, {len(before_runs)} before runs against "
          f"{len(after_runs)} after runs\n")
    print("## Trust\n")
    vb, bb = many_trust("before", before_runs)
    va, ba = many_trust("after ", after_runs)
    if bb != ba:
        print(f"  WARNING: build types differ ({', '.join(sorted(bb))} vs "
              f"{', '.join(sorted(ba))}): this is not a like-for-like comparison")
    for label, verdicts in (("before", vb), ("after", va)):
        bad = sum(n for v, n in verdicts.items() if v != "trusted")
        if bad:
            print(f"  WARNING: {bad} {label} run(s) are not trusted: do not quote these numbers")
    if min(len(before_runs), len(after_runs)) < 10:
        print("  NOTE: fewer than 10 runs on a side. Treat the medians as directional.")
    print()

    print("## Medians\n")
    if not rows:
        print("No metric is present on both sides.\n")
    width = max([len(r["metric"]) for r in rows] + [10])
    print(f"  {'':{width}}  {'before':>9} {'(range)':>17}  {'after':>9} {'(range)':>17}  "
          f"{'change':>16}  verdict")
    for r in rows:
        b, a = r["before"], r["after"]
        pct = f" ({r['delta_pct']:+.0f}%)" if r["delta_pct"] is not None else ""
        b_range = "({:.0f} to {:.0f})".format(b["min"], b["max"])
        a_range = "({:.0f} to {:.0f})".format(a["min"], a["max"])
        change = "{:+.1f}{}".format(r["delta"], pct)
        print(f"  {r['metric']:{width}}  {b['median']:>9.1f} {b_range:>17}  "
              f"{a['median']:>9.1f} {a_range:>17}  {change:>16}  {r['verdict']}")
    print()
    moved = [r for r in rows if r["verdict"] in ("clear", "likely")]
    print("## What moved\n")
    if moved:
        for r in moved:
            print(f"  - {r['metric']}: {r['before']['median']:.1f} to {r['after']['median']:.1f} "
                  f"({r['verdict']})")
    else:
        print("  Nothing moved by more than the run-to-run spread.")
    quiet = [r["metric"] for r in rows if r["verdict"] == "noise"]
    if quiet:
        print(f"\n  Inside the noise, so do not claim these: {', '.join(quiet)}.")
    print()
    many_findings(before_runs, after_runs)
    print("Quote a median with its run count, the device and the build type. "
          "\"clear\" and \"likely\" describe these runs, not a guarantee.")
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dirs", nargs="*", metavar="DIR",
                   help="one run each: <before-out-dir> <after-out-dir>")
    p.add_argument("--before", nargs="+", metavar="DIR",
                   help="many runs: the before triage-out directories, or a folder of them")
    p.add_argument("--after", nargs="+", metavar="DIR",
                   help="many runs: the after triage-out directories, or a folder of them")
    p.add_argument("--pkg", help="package name, for the jank-summary row lookup (defaults to the pkg in before's findings.json)")
    p.add_argument("--json", action="store_true", help="many runs: print the medians as JSON")
    args = p.parse_args()

    if args.before or args.after:
        if not (args.before and args.after) or args.dirs:
            p.error("give both --before and --after, and no positional directories")
        before_runs, after_runs = expand_runs(args.before), expand_runs(args.after)
        pkg = args.pkg or load_findings(before_runs[0]).get("pkg")
        compare_many(before_runs, after_runs, pkg, args.json)
        return
    if len(args.dirs) != 2:
        p.error("give <before-out-dir> <after-out-dir>, or --before ... --after ...")
    args.before_dir, args.after_dir = args.dirs

    before = load_findings(args.before_dir)
    after = load_findings(args.after_dir)
    pkg = args.pkg or before.get("pkg") or after.get("pkg")

    print(f"# Remeasure: {before.get('pkg', '?')}")
    print(f"before: {before.get('trace', '?')}")
    print(f"after:  {after.get('trace', '?')}\n")

    diff_trust(before, after)
    diff_findings(before, after)
    if pkg:
        diff_jank(args.before_dir, args.after_dir, pkg)
    diff_hotspots(args.before_dir, args.after_dir)

    print("Reminder: this is one run each. For a number you can quote, capture")
    print("10 or more runs per side and compare them with --before ... --after ...")


if __name__ == "__main__":
    main()
