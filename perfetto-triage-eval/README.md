# perfetto-triage-eval

Checks whether `perfetto-triage`'s output can be trusted, using real
fixture captures from real incidents instead of synthetic mocks.

```bash
python3 run_eval.py                      # real fixtures, current triage.py
python3 test_logic.py                    # synthetic rows, decision table only
python3 test_visual_report.py            # report.html: theme discovery, rendering, escaping
python3 test_code_map.py                 # whose code is a hot slice: positives need evidence
python3 test_remeasure.py                # many runs per side: medians, spread, noise verdict
python3 synthetic_build_type_check.py    # synthetic trace through trace_processor
```

No device and no trace file are needed for any of these. The third one
downloads `trace_processor` on first run, the same way `triage.py` does.

## What it checks

Three known traps, documented in `KNOWN-TRAPS.md`, each with a real
fixture in `fixtures/` from the capture that went wrong:

1. A debug-build capture should warn that the build is debug.
2. A capture that missed the app launch (an `android_startups` race
   condition) should say so explicitly, not just omit startup findings.
3. `00_health` passing and `01_coverage` failing should be distinguishable
   without already knowing the difference between the two checks.

## How a fixture is used

A fixture is a `triage-out` directory. Its two halves do different jobs:

- `findings.json` and `report.md` are **frozen**: what the tool said on the
  day. They are the incident record and are never regenerated.
- `csv/` is the raw query output from that same real trace. `run_eval.py`
  runs the current `triage.py` (`health_status` + `analyse`) over it.

Each trap prints both: what was said at the incident, and what the current
code says on the same data.

## Statuses

- `PASS`: current code handles the real incident data correctly.
- `FAIL`: it does not.
- `UNPROVEN`: the guard is written, but no real fixture exercises it. This
  is not a pass. The exit code stays non-zero.
- `SKIP`: fixture files are missing.

Current state: Traps 2 and 3 pass. Trap 1 is UNPROVEN, because the original
debug-build fixture predates `03_build_type.sql` and has no `package_list`
data. `KNOWN-TRAPS.md` has the three commands that close it.

## Adding a trap

1. Reproduce the real failure against a real capture. Don't invent one.
2. Copy the `triage-out` directory into `fixtures/<name>-out/`.
3. Write it up in `KNOWN-TRAPS.md`: what happened, root cause, what a real
   fix would check for.
4. Add a check function to `run_eval.py`. Use `rederive()` to run the
   current code over the fixture's CSVs, and match on structured fields
   (`health_status`, a finding's `area`, `severity` and exact `title`), not
   on substrings of the JSON. The trace's own file path can contain trigger
   words by accident (a `.pftrace` filename contains the substring "race").
5. Add a control where you can: a fixture where the trap did not happen,
   which must not be flagged.
