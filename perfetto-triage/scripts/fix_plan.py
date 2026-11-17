#!/usr/bin/env python3
"""
Turn triage findings into a list of suggested fixes an agent can review.

  ./fix_plan.py triage-out            # prints the plan as Markdown

triage.py runs this itself and writes the plan three ways: `fix_plan` in
findings.json, a "Suggested fixes" section in report.md and report.html, and
fix_plan.md on its own.

Every item says what to change, where if the trace can tell, which number
should move, and how to check that it did. The order is the method's order:

  first   the capture itself is wrong. Nothing else can be trusted until it is
          captured again.
  next    make the app measurable (trace sections, reportFullyDrawn), then the
          fixes whose findings are high or medium.
  later   fixes whose findings are only information in this trace, for example
          late frames at under 1% or a start that is already fast.

These are suggestions drawn from one trace. They are not instructions. The
agent reads the code, drops what does not apply, makes one change, measures
again, and only then raises the fix.
"""

import argparse
import json
import os
import re
import sys

REVIEW = [
    "Read the code each suggestion points at. Drop any that do not apply, and say which and why.",
    "Take the first one left. One fix per change, so the result can be attributed.",
    "Build before and after from the same build type. Trace sections go in both.",
    "Capture 10 or more runs per side and compare them: "
    "remeasure.py --before runs/before-* --after runs/after-*",
    "Raise the fix only if the number under \"check\" comes back clear or likely, and put that "
    "table in the pull request. If it comes back noise, say so and do not ship it as a win.",
]

LATE = ["late frames (%)", "worst overrun (ms)"]
START = ["cold start TTID (ms)"]

# kind -> what to suggest. stage: capture | measure | fix
FIXES = {
    "trace_errors": {
        "stage": "capture", "title": "Record the trace again without errors or dropped data",
        "change": ["Use capture.sh --start and --stop for anything longer than about 20 seconds: "
                   "that mode streams to the file instead of overwriting a ring buffer",
                   "Let the device cool down and close other heavy apps before recording"],
        "check": ["trace health is pass"]},
    "debuggable_build": {
        "stage": "capture", "title": "Capture a build that is not debuggable",
        "change": ["Build a release-like variant: R8 on, debuggable false. The signing key does not matter",
                   "Install it and capture again with capture.sh, which refuses a debuggable build"],
        "check": ["build type is release"]},
    "build_unknown": {
        "stage": "capture", "title": "Confirm the build type before quoting any number",
        "change": ["Run: adb shell dumpsys package <pkg> | grep -i debuggable. Any output means debuggable",
                   "Or capture again with capture.sh, whose config records the package list"],
        "check": ["build type is release"]},
    "no_frametimeline": {
        "stage": "capture", "title": "Record again with the frame timeline data source",
        "change": ["Capture with capture.sh, whose config includes android.surfaceflinger.frametimeline",
                   "Without it late frames cannot be attributed to the app or the compositor"],
        "check": ["coverage: frametimeline is ok"]},
    "no_sched": {
        "stage": "capture", "title": "Record again with scheduler data",
        "change": ["Capture with capture.sh, whose config includes sched and thread state",
                   "Without it nothing can say whether a thread was running or waiting"],
        "check": ["coverage: sched is ok"]},
    "startup_missed": {
        "stage": "capture", "title": "Capture the app launch again",
        "change": ["Run capture.sh <pkg> with no mode: it starts tracing, then force-stops and relaunches",
                   "If you meant to measure an interaction, pass --mode interactive to triage instead"],
        "check": ["startup capture is ok"]},
    "query_unfinished": {
        "stage": "capture", "title": "Finish the query that was abandoned",
        "change": ["Run triage again with a larger --query-timeout",
                   "Or capture a shorter window, so the trace is smaller"],
        "check": ["no \"did not finish\" finding"]},
    "no_trace_sections": {
        "stage": "measure", "title": "Add trace sections around the code you suspect",
        "change": ["Wrap each screen, transition and expensive call: androidx.tracing trace(\"Name\") { ... }",
                   "For Compose, turn on composition tracing so recomposition slices carry composable names",
                   "Put the same sections in the before build too. They change what you can see, not how fast it runs",
                   "Capture again and run triage with --app-repo"],
        "check": ["the report marks slices as your code",
                  "\"Late frames by the app's own trace sections\" appears"],
        "effort": "small"},
    "no_ttfd": {
        "stage": "measure", "title": "Report when the first screen is really drawn",
        "change": ["Call Activity.reportFullyDrawn() once real content is on screen",
                   "In Compose use ReportDrawn or ReportDrawnWhen { contentIsReady }"],
        "check": ["TTFD (ms)"], "effort": "small",
        "where_hint": "the Activity or root composable of the first screen"},
    "startup_waiting": {
        "stage": "fix", "title": "Take what startup waits for off the critical path",
        "change": ["15_startup_binder and 30_binder_main_thread list the calls the main thread waited on",
                   "Move each to a background dispatcher, cache its result, or defer it past the first frame",
                   "One call per change"],
        "check": START + ["main thread running in startup (%)"]},
    "startup_compute": {
        "stage": "fix", "title": "Defer or shrink the work done before the first frame",
        "change": ["Take the largest slice in 13_startup_top_slices that is the app's own",
                   "Defer anything the first screen does not need: lazy init, App Startup, a background dispatcher"],
        "check": START},
    "startup_mixed": {
        "stage": "fix", "title": "Take the single largest startup cost, running or waiting",
        "change": ["Compare the largest slice in 13_startup_top_slices with the largest call in 15_startup_binder",
                   "Fix the bigger of the two first, then measure before touching the other"],
        "check": START},
    "class_loading": {
        "stage": "fix", "title": "Ship or refresh the Baseline Profile",
        "change": ["See what is compiled: adb shell dumpsys package dexopt | grep -A2 <pkg>",
                   "Not speed-profile: the profile is missing or was not installed. Add one with BaselineProfileRule",
                   "Already speed-profile: the profile is stale. Regenerate it over the startup path",
                   "Measure both builds with ab_startup.sh <pkg> <activity> 10 profile, so both are compiled"],
        "check": ["classes loaded in startup", "class loading (ms)"] + START,
        "where_hint": "baseline-prof.txt, or the module that generates the profile"},
    "shader_compile": {
        "stage": "fix", "title": "Compile the shaders before the user is watching",
        "change": ["Draw the effect once offscreen, behind the splash or the first screen",
                   "A Baseline Profile does not help: it compiles app code, not GPU shaders",
                   "This is a first-run cost. Clear app data between runs, or the warm cache hides it"],
        "check": ["shader compile in late frames (ms)"] + LATE,
        "where_hint": "the view or composable that draws the effect on its first frame"},
    "jank_ui": {
        "stage": "fix", "title": "Cut the UI thread work in late frames",
        "change": ["23_jank_hot_slices ranks what ran in the late frames: measure, layout, recomposition",
                   "Hoist state so less recomposes, and take IO and parsing off the main thread"],
        "check": LATE},
    "jank_render": {
        "stage": "fix", "title": "Cut the draw work in late frames",
        "change": ["Decode bitmaps to display size and remove stacked opaque backgrounds",
                   "Avoid full-screen blurs, large RenderEffects and hardware-layer toggling during animation"],
        "check": LATE},
    "jank_split": {
        "stage": "fix", "title": "Late frames cost both threads: find the frames, not the thread",
        "change": ["Open the worst frames in the report and look at what each one ran",
                   "Fix the most common slice among them first"],
        "check": LATE},
    "layout_cost": {
        "stage": "fix", "title": "Reduce measure and layout work in late frames",
        "change": ["Flatten the hierarchy and avoid layouts that measure children twice",
                   "In Compose, keep recomposition scopes small and avoid SubcomposeLayout on hot paths"],
        "check": LATE},
    "main_binder": {
        "stage": "fix", "title": "Take the binder call off the main thread",
        "change": ["The finding names the interface. Find the framework API that wraps it",
                   "Call it from a background dispatcher, cache the result, or batch repeated calls"],
        "check": ["main-thread binder (ms)"]},
    "lock_contention": {
        "stage": "fix", "title": "Stop the main thread waiting on that lock",
        "change": ["32_monitor_contention names the method holding the lock",
                   "Shrink what runs while the lock is held, or keep the main thread off it"],
        "check": ["the lock finding is gone"]},
    "anr": {
        "stage": "fix", "title": "Work back from the ANR",
        "change": ["Find the ANR timestamp in 41_anrs and look at the main thread in the 5 seconds before it",
                   "Whatever it was blocked on is the fix"],
        "check": ["no ANR in the next trace"]},
    "late_frames_in_section": {
        "stage": "fix", "title": "Start inside the section most late frames overlap",
        "change": ["Split it into smaller trace sections to find the expensive part",
                   "Fix that part, then measure the section again"],
        "check": []},     # filled in with the section name
    "handoff_compose-expert": {
        "stage": "fix", "title": "Reduce the Compose work that runs every frame",
        "change": ["Use the compose-expert skill to find what recomposes or animates on every frame",
                   "Pause infinite animations when the screen is idle, covered or in the background",
                   "Read state as late as possible: in layout or draw, not in composition"],
        "check": LATE, "skill": "compose-expert"},
}
FIXES["sections_not_ranked"] = dict(FIXES["no_trace_sections"],
                                   title="Add trace sections where the time is actually going")

STAGE_ORDER = {"capture": 0, "measure": 1, "fix": 2}
SEV = {"blocker": 0, "high": 1, "medium": 2, "info": 3}


def build(findings: list, health: dict | None = None, code_map: dict | None = None,
          handoffs: list | None = None) -> dict:
    """findings as triage.py makes them -> {"summary", "review", "items"}."""
    compose = any(h.get("skill") == "compose-expert" for h in handoffs or [])
    evidence = {s["name"]: s.get("evidence") for s in (code_map or {}).get("slices", [])
                if s.get("evidence")}
    items, seen = [], set()
    # With Compose work ranking, "UI thread cost" and "layout cost" are the same
    # suggestion said three ways. Keep the Compose one and fold the others into it.
    folded = [f.get("title", "") for f in findings
              if compose and f.get("kind") in ("jank_ui", "layout_cost")]
    for f in findings:
        kind = f.get("kind") or "other"
        fix = FIXES.get(kind)
        if not fix or kind in seen or (compose and kind in ("jank_ui", "layout_cost")):
            continue
        seen.add(kind)
        item = {
            "kind": kind, "stage": fix["stage"], "title": fix["title"],
            "because": f.get("title", ""), "evidence": (f.get("detail") or "")[:400],
            "finding_severity": f.get("severity", "info"),
            "change": list(fix["change"]), "check": list(fix["check"]),
            "where": [], "effort": fix.get("effort", "medium"),
        }
        if fix.get("skill"):
            item["skill"] = fix["skill"]
        if fix.get("where_hint"):
            item["where_hint"] = fix["where_hint"]
        if kind == "handoff_compose-expert" and folded:
            item["also_because"] = folded
        if kind == "late_frames_in_section" and f.get("section"):
            section = f["section"]
            item["title"] = f"Start inside {section}, where most late frames fall"
            item["check"] = [f"late frames inside {section}"] + LATE
            if evidence.get(section):
                item["where"] = [evidence[section]]
        if kind == "main_binder":
            m = re.search(r"Main-thread binder: (.+?) costs", f.get("title", ""))
            if m:
                item["where"] = [m.group(1)]
        if fix["stage"] == "capture":
            item["priority"] = "first"
        elif fix["stage"] == "measure" or SEV.get(item["finding_severity"], 3) <= 2:
            item["priority"] = "next"
        else:
            item["priority"] = "later"
            item["why_later"] = ("Its finding is information in this trace. Nothing here says "
                                 "users see a problem from it yet.")
        items.append(item)

    pr = {"first": 0, "next": 1, "later": 2}
    items.sort(key=lambda i: (pr[i["priority"]], STAGE_ORDER[i["stage"]],
                              SEV.get(i["finding_severity"], 3)))
    # Only a blocker stops everything. "Build type not verified" comes first
    # too, but the other suggestions still point at real code paths.
    blocked = any(i["priority"] == "first" and i["finding_severity"] == "blocker" for i in items)
    for n, item in enumerate(items, 1):
        item["n"] = n
        if blocked and item["priority"] != "first":
            item["blocked_by_capture"] = True
    first = sum(1 for i in items if i["priority"] == "first")
    nxt = sum(1 for i in items if i["priority"] == "next")
    later = sum(1 for i in items if i["priority"] == "later")
    verdict = (health or {}).get("verdict")
    if first and blocked:
        summary = (f"Fix the capture first ({first} item{'s' if first > 1 else ''}). Until then the "
                   f"other {nxt + later} suggestions rest on numbers that cannot be trusted.")
    elif first:
        summary = (f"Settle the capture question first ({first} item{'s' if first > 1 else ''}). The "
                   f"other {nxt + later} suggestions point at real code paths, but their numbers "
                   f"cannot be quoted until then.")
    elif nxt:
        summary = (f"{nxt} suggested fix{'es' if nxt > 1 else ''} to review, in order"
                   + (f", and {later} that can wait." if later else "."))
    elif later:
        summary = (f"Nothing in this trace needs fixing now. {later} optional "
                   f"item{'s' if later > 1 else ''} below, for polish.")
    else:
        summary = "No fix to suggest from this trace."
    if verdict == "directional_only" and not first:
        summary += " The capture is directional only, so confirm each one with a trusted capture."
    return {"summary": summary, "review": REVIEW, "items": items}


def to_markdown(plan: dict, heading: str = "## Suggested fixes, in order") -> str:
    out = [heading, "", plan["summary"], ""]
    if not plan["items"]:
        return "\n".join(out)
    out.append("These are suggestions from one trace, not instructions. Before acting on any:")
    out.append("")
    out.extend(f"{i}. {step}" for i, step in enumerate(plan["review"], 1))
    out.append("")
    labels = {"first": "Do first: the capture", "next": "Review next", "later": "Can wait"}
    current = None
    for item in plan["items"]:
        if item["priority"] != current:
            current = item["priority"]
            out.append(f"### {labels[current]}")
            out.append("")
        out.append(f"**{item['n']}. {item['title']}**")
        out.append("")
        out.append(f"- Because: {item['because']}")
        for extra in item.get("also_because", []):
            out.append(f"- Also: {extra}")
        if item.get("why_later"):
            out.append(f"- Why it can wait: {item['why_later']}")
        if item.get("blocked_by_capture"):
            out.append("- Do not act on this until the capture is fixed.")
        out.append("- Change:")
        out.extend(f"  - {step}" for step in item["change"])
        out.append("- Where: " + (
            ", ".join(f"`{w}`" for w in item["where"]) if item["where"]
            else item["where_hint"] if item.get("where_hint")
            else "not known from the trace. Map the slices to code first (Step 4)."))
        if item["check"]:
            out.append("- Check: " + ", ".join(item["check"]))
        if item.get("skill"):
            out.append(f"- Skill: {item['skill']}")
        out.append("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_dir", help="a directory written by triage.py")
    ap.add_argument("--json", action="store_true", help="print the plan as JSON")
    args = ap.parse_args()
    path = os.path.join(args.out_dir, "findings.json")
    if not os.path.exists(path):
        sys.exit(f"no findings.json in {args.out_dir}. Run triage.py first.")
    with open(path) as fh:
        data = json.load(fh)
    plan = data.get("fix_plan") or build(data.get("findings") or [], data.get("health_status"),
                                         data.get("code_map"), data.get("handoffs"))
    print(json.dumps(plan, indent=2) if args.json else to_markdown(plan, "# Suggested fixes"))


if __name__ == "__main__":
    main()
