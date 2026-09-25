# perfetto-triage-eval

Checks whether `perfetto-triage`'s output can be trusted, using real
fixture captures from real incidents instead of synthetic mocks.

```bash
python3 run_eval.py
```

## What it checks

Three known traps, documented in `KNOWN-TRAPS.md`, each with a real
before-the-fix (or still-unfixed) fixture in `fixtures/`:

1. A debug-build capture should warn that the build is debug. It doesn't.
2. A capture that missed the app launch (an `android_startups` race
   condition) should say so explicitly, not just omit startup findings.
   It doesn't.
3. `00_health` passing and `01_coverage` failing should be distinguishable
   without already knowing the difference between the two checks. They
   aren't, in the current `findings.json` shape.

All three currently fail. That's the honest state of `perfetto-triage`
today, not a bug in this eval.

## Adding a trap

1. Reproduce the real failure against a real capture. Don't invent one.
2. Copy the `triage-out` directory into `fixtures/<name>-out/`.
3. Write it up in `KNOWN-TRAPS.md`: what happened, root cause, what a real
   fix would check for.
4. Add a check function to `run_eval.py`. Match on structured fields where
   they exist; if matching on finding text, use `has_word()` and
   `findings_text_only()`, not a raw substring search against the whole
   JSON blob — the trace's own file path can contain trigger words by
   accident (a `.pftrace` filename contains the substring "race").
