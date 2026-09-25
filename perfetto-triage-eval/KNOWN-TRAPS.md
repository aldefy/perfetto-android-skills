# Known traps

Real failure modes `perfetto-triage` hit, documented as an answer key so
future runs (or an eval harness) can check against them. Each trap is a real
incident, not a hypothetical.

## Trap 1: debug-build contamination goes unreported

**What happened:** a capture ran against a `debuggable` APK by mistake.
`triage.py` produced 9 confident, ranked findings (jank, startup, binder)
with no warning that the build was debug. A debug build runs 2 to 5x
slower; every number in that report was directional at best, misleading at
worst, and nothing in the tool said so.

**Root cause:** none of the 18 queries in `queries/` check the debuggable
flag. `00_health.sql` checks `trace_processor`'s own error/data-loss stats.
`01_coverage.sql` checks whether data sources are present. Neither checks
whether the *app itself* was built debuggable.

**Fixture:** `fixtures/debug-build-out/` (report.md, findings.json, csv/)
is the real triage output from this incident, captured against
StickerExplode on a Pixel 9 Pro Fold. 9 findings, 0 warnings about build
type.

**Correct behavior:** a query against `android.packages_list` (or
equivalent) should exist and should surface `debuggable=true` as a
`00_health`-tier finding, before any other ranking happens. This does not
exist yet. Anyone using `triage.py` today must check
`adb shell dumpsys package <pkg> | grep -i debuggable` themselves before
trusting a report.

**Eval check:** given a triage-out directory where the source APK was
debuggable, does `report.md` or `findings.json` contain any warning about
build type? As of this repo's current state: no. This is a known,
unresolved gap, not a fixed trap.

## Trap 2: `android_startups` returning zero rows looks identical to "app didn't launch"

**What happened:** three separate cold-start captures against the same app,
same device, same release build, all returned zero rows from
`10_startup_summary` and the related startup queries. The captures were
otherwise healthy: `sched_rows`, `slices`, and `binder_slices` were all
non-zero in `01_coverage`.

**Root cause:** `android_startups` is a PerfettoSQL stdlib view
(`android.startup.startups` module) that infers a launch from raw `am`
atrace events already in the trace. It is not a dedicated data source you
enable. `capture.sh` starts tracing, waits 1 second, then force-stops and
relaunches the app. If the `am` launch-lifecycle events fire close to that
1-second boundary, they can land outside the window `trace_processor`
associates with the new process, and the view finds nothing to report.

**Fix applied:** the delay was increased from `sleep 1` to `sleep 3`
between starting the trace and force-stopping the app, in
`perfetto-triage/scripts/capture.sh`. `fixtures/startup-race-out/` is a
capture from before the fix, reproducing the empty-`android_startups`
symptom.

**Eval check:** does a report with zero rows in `10_startup_summary` but
non-zero `sched_rows` and `slices` in `01_coverage` get treated as "no
startup happened" (wrong) or "the capture window missed the launch, other
data is fine" (correct)? `triage.py` currently does neither: it simply
omits any startup finding and moves on, which reads as silence, not as an
explicit flag. A human or agent reading `report.md` cold, without knowing
this trap exists, would likely misread the silence as "nothing to report"
rather than "this specific measurement failed."

## Trap 3: empty result set looks the same whether it means "healthy" or "broken"

**What happened:** `00_health.sql` returning zero rows is the *good* case
(no errors, no data loss). `01_coverage.sql` returning a zero in a specific
column is the *bad* case (that data source was never recorded). Both
present as "a query came back with little or no data" to anyone skimming
output, and the deck this repo was built alongside made exactly this
mistake on a draft slide before catching it: it showed `01_coverage`
returning zero for `frametimeline_rows` and described it as the honest
result for a trace that, when actually queried, had 898 non-zero rows.

**Root cause:** not a bug in the tool. This is a genuine trap for a human or
agent reading isolated query output without the surrounding context
`SKILL.md` provides ("empty result set is not the same as a healthy app",
stated explicitly in the hard rules).

**Eval check:** does a summary of triage output correctly distinguish
"`00_health` empty = pass" from "`01_coverage` column = 0 = fail" without
requiring the reader to already know the difference? Currently this
distinction lives only in prose (`SKILL.md`'s hard rules), not in the
tool's structured output. `findings.json` has no explicit pass/fail field
for either check; a consumer has to know to look for the *absence* of a
health-related finding as the "pass" signal.

## What this means for now

None of these three traps are fixed at the tool level yet. Run
`python3 run_eval.py` to check: as of this commit, all three fail. The
fixtures are real triage output from real incidents, not synthetic data,
and the eval script checks `perfetto-triage`'s actual output against them,
not a mock. It does not fix any of the three gaps; it makes them visible
and re-checkable so a future fix to `perfetto-triage` (a debuggable-flag
query, an explicit startup-capture-failed finding, a structured health
pass/fail field) can be verified against a real regression instead of
"it seemed fine when I tried it."
