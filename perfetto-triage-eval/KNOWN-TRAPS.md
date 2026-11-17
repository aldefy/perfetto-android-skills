# Known traps

Real failure modes `perfetto-triage` hit, documented as an answer key so
future runs (or an eval harness) can check against them. Each trap is a real
incident, not a hypothetical.

| Trap | Status | Proof |
|---|---|---|
| 1. Debug-build contamination | Guard written, **unproven on a real debuggable trace** | `run_eval.py` reports UNPROVEN until `fixtures/debug-build-after-out` exists |
| 2. `android_startups` silent gap | Fixed | `run_eval.py` PASS on the original incident CSVs |
| 3. Health vs coverage | Fixed | `run_eval.py` PASS on the original incident CSVs |
| 4. No frame deadlines read as "all on time" | Fixed | `test_logic.py` and `test_visual_report.py`, on rows written to match the incident. No fixture: the incident trace is not public |

Each fixture's `findings.json` and `report.md` are the frozen output from the
day of the incident and are never regenerated. `run_eval.py` feeds the
fixture's raw `csv/` through the current `triage.py` and compares.

## Trap 1: debug-build contamination goes unreported

**What happened:** a capture ran against a `debuggable` APK by mistake.
`triage.py` produced 9 confident, ranked findings (jank, startup, binder)
with no warning that the build was debug. A debug build runs 2 to 5x
slower; every number in that report was directional at best, misleading at
worst, and nothing in the tool said so.

**Root cause:** none of the queries then in `queries/` checked the debuggable
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
debuggable, does `findings.json` say `health_status.build_type = debuggable`,
raise a blocker finding, and mark every other finding `directional_only`?

**Fix applied (three layers):**

1. `scripts/capture.sh` reads the package flags from `dumpsys package` before
   tracing and exits 3 on a debuggable build. `--allow-debug` captures anyway.
2. `queries/03_build_type.sql` reads `package_list.debuggable` out of the
   trace itself, so a trace captured some other way is still checked.
3. `triage.py` turns that into `health_status.build_type`
   (`release | debuggable | unknown`), a top-ranked blocker finding when
   debuggable, and `directional_only: true` on every other finding. When the
   build type cannot be read it says "Build type not verified" and the verdict
   is `directional_only`. Unknown is never treated as release.

**What is and is not proven:**

- Proven on the original incident data: the tool is no longer silent. The old
  fixture has no `package_list` CSV (it predates the query), and the current
  code reports `build_type=unknown` with an explicit finding instead of nine
  confident ones.
- Proven on a synthetic trace: `03_build_type.sql` returns `debuggable=1`
  from a hand-built `PackagesList` packet on trace_processor v57.2
  (`python3 synthetic_build_type_check.py`). That checks the SQL and the
  plumbing. It is not a capture.
- **Not proven:** the same detection on a real debuggable capture, and
  `capture.sh`'s flag parsing on a real device (tested against a stub `adb`
  only).

**To close it** (device connected, debuggable StickerExplode installed):

```bash
perfetto-triage/scripts/capture.sh com.example.stickerexplode --allow-debug
python3 perfetto-triage/scripts/triage.py traces/<the new file>.pftrace \
  --pkg com.example.stickerexplode \
  --out perfetto-triage-eval/fixtures/debug-build-after-out
python3 perfetto-triage-eval/run_eval.py   # Trap 1 should go UNPROVEN -> PASS
```

Also run `capture.sh` once without `--allow-debug` against the same build and
confirm it refuses with exit code 3.

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
data is fine" (correct)? At the time of the incident `triage.py` did
neither: it omitted any startup finding and moved on, which reads as
silence. A human or agent reading `report.md` cold would likely misread
that silence as "nothing to report" rather than "this measurement failed."

**Second fix applied (the tool now says so):** `triage.py` knows what the
capture was meant to contain, from `--mode startup|interactive` or from a
`capture.sh` filename (`<pkg>-startup-<stamp>.pftrace`).

- Startup capture, no launch for the package: blocker finding "Startup
  capture missed the app launch" and `health_status.startup_capture = missed`.
- Interactive capture, no launch: `not_expected`, no finding. Zero startups
  is normal there.
- Mode unknown, no launch: an info finding that names both readings, and
  `startup_capture = unknown`.

`run_eval.py` checks this on `fixtures/startup-race-out` (must be flagged)
and uses `fixtures/debug-build-out`, which did contain the launch, as the
control (must not be flagged).

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
requiring the reader to already know the difference? At the time of the
incident this lived only in prose (`SKILL.md`'s hard rules). `findings.json`
had no pass/fail field, so a consumer had to treat the *absence* of a
health finding as the pass signal.

**Fix applied:** `findings.json` now carries a `health_status` block, and
`report.md` opens with the same data as a "Trust check" table:

```json
"health_status": {
  "trace_health": "pass",
  "coverage": {"slices": "ok", "sched": "ok", "frametimeline": "ok",
               "blocked_reasons": "ok", "binder": "ok"},
  "coverage_gaps": [],
  "build_type": "release",
  "startup_capture": "ok",
  "verdict": "trusted",
  "reasons": []
}
```

Every value is from a closed set, and `unknown` always means the check did
not run or could not decide. It never means pass. `verdict` is `trusted`,
`directional_only` or `not_trustworthy`. `run_eval.py` checks each field
against the fixture's own CSVs.

## Trap 4: a phone with no frame deadlines reads as a perfectly smooth app

**What happened:** a release build was captured on an Android 10 phone. The
frame timeline, which records each frame's deadline and who missed it, exists
from Android 12. The trace still had frames, counted from Choreographer slices,
so the report showed several hundred frames, zero late, "100% on time", under a
verdict of "Trusted. The capture passed every check" printed above two rows
marked Fail. The person running it had to add a note by hand saying the frames
section was not real.

Four more wrong readings came from the same run:

- A capture made with `capture.sh --start` was marked not trustworthy because
  of `config_write_into_file_no_flush`. That stat means trace_processor has to
  load the file in one piece. It loses nothing.
- The start type was shown as `[NULL] start`, and the startup charts were
  empty. The stdlib ties a launch to its process through an `activityResume`
  slice that older Android does not write.
- Binder calls were titled "unknown", because that Android version does not
  name the interface.
- The top two "hot slices" were `waitForever`: threads parked waiting for the
  GPU, which is idle time.

**The fix:** `health_status()` now carries the device (`device.sdk`) and a
`frames` state: `measured`, `not_recorded`, `unsupported` or `unknown`. With
`unsupported` the jank findings are not produced at all, the page says "late
frames: not measured" and why, and `remeasure.py` leaves the frame metrics out
of a comparison. A missing data source that does not decide the verdict is
shown as "Missing", not "Fail", and the verdict sentence lists what the trace
cannot answer. The start type falls back to `cold` when `bindApplication` runs
inside the launch, and the startup queries fall back to the main thread of the
process named after the package.

**Why it matters:** this is Trap 3 again in a new place. An empty table and a
table of zeros look the same as a good result unless something states that the
measurement was not possible.

## What this means for now

Run `python3 run_eval.py`. As of this commit: Traps 2 and 3 pass against the
original incident data, and Trap 1 is UNPROVEN, which keeps the exit code
non-zero. The fixtures are real triage output from real incidents, and the
eval runs the real `triage.py` logic over their raw CSVs, not a mock.

Trap 1 stays open until a real debuggable capture is triaged into
`fixtures/debug-build-after-out`. Until then, do not describe it as closed.

`test_logic.py` is separate on purpose. It pins the decision table on
hand-written rows, including branches no real fixture covers. Passing it
shows the logic is right, not that a real trace produces those rows.
