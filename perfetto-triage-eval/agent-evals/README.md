# Agent evals

The other files in `perfetto-triage-eval/` test the scripts. This folder tests
something different: whether an agent that is handed a plain request, and this
skill, gives the developer a correct answer.

## How it works

1. **Seven plain requests** (`make_prompt.py`). None says where the answer
   lives. Four carry a wrong premise on purpose, because that is where agents
   go wrong: a "cold start" trace that holds a warm start, a jank question on a
   phone that cannot measure jank, a before and after that are different apps,
   and a "scrolling" trace that is a Chrome browser trace.
2. **Two configurations.** The baseline gets a shell and `trace_processor_shell`.
   The skill run gets the same plus a copy of `perfetto-triage/`.
3. **Two models**, a smaller and a larger one. Smaller models expose gaps that
   larger ones quietly work around.
4. **A blind judge.** A separate model grades each run against `rubric.md` as
   PASS, INCOMPLETE or FAIL, from the answer and the agent's own command log.
   Run folders are renamed at random first, so the judge cannot see the model
   or whether the skill was used. Passing runs are reviewed too, for wasted
   steps and unsupported claims.
5. **Fix only what a transcript shows**, then run the failing tasks again.

Each run writes `answer.md` (what the developer would read) and `log.md`
(every command, with start and end times).

## Traces

Four public traces from Perfetto's test data (`test/data` in the Perfetto
repo, fetched with `tools/test_data download`). Put them in
`$EVAL_ROOT/traces/` (default `/tmp/evals/traces/`):

| File | sha256 (first 16) |
|---|---|
| api24_startup_cold.perfetto-trace | ac006a3ec74fac70 |
| api34_startup_cold.perfetto-trace | 1958521dc5128cd4 |
| example_android_trace_30s.pb | 62656cc8ea2a624d |
| scrolling_with_blocked_nonblocked_frames_new.pftrace | 900f5e86e24df7bb |

Task t7 needs a copy of [Now in Android](https://github.com/android/nowinandroid)
in `$EVAL_ROOT/nia/`.

## Running it

Copy the skill once per version you test, so a run can never edit what it is
graded on:

```bash
cp -r perfetto-triage /tmp/evals/skill-v1
python3 make_prompt.py t1 t1-base-small > /tmp/evals/prompts/t1-base-small.txt
python3 make_prompt.py t1 t1-skill-small /tmp/evals/skill-v1 > /tmp/evals/prompts/t1-skill-small.txt
```

Hand each prompt to a fresh agent with no other context. We used Claude Code
sub-agents. Any agent with a shell works. Then give the judge `rubric.md` and
the renamed run folders, and ask for a JSON list with grade, missing items,
fail reasons, unsupported claims, wasted steps and seconds per run.

## Results, 18 November 2026

64 agent runs over 4 rounds, Haiku and Sonnet, with Opus as the judge.

**Baseline against the skill, same 7 tasks, 2 models:**

| | Pass | Incomplete | Fail | Median time |
|---|---|---|---|---|
| No skill | 5 / 14 (36%) | 5 | 4 | 36 s |
| With the skill | 13 / 14 (93%) | 1 | 0 | 19 s |

Without the skill, both models gave a dropped-frame count on an Android 7
trace that cannot measure one (t2), both diagnosed scroll jank from a Chrome
browser trace (t6), and neither told the developer to prove the Baseline
Profile was applied before regenerating it (t3).

**What the transcripts showed, and what changed:**

| Round | Found in the transcripts | Change |
|---|---|---|
| 1 | With no `--pkg`, the report's top finding was 313 ms of surfaceflinger binder: not the app. Sonnet noticed; the report itself was wrong. | Triage reports on the one app the trace launches, and says so. |
| 1 | The report never said the "cold start" was a warm, 110 ms one, only in an evidence table headed "cold start". | A warm or fast launch is now a headline finding. |
| 2 | Repo only, no phone: 1 of 4 skill runs passed. SKILL.md said "if no device, stop", so agents stopped before checking the repo for a Baseline Profile or planning a cold start. | Source-only checks and a capture plan in Step 0. 4 of 4 after. |
| 2 | On a trace with two apps, a 331 ms lock wait in system_server ranked as the top finding. | Platform processes no longer rank as the app's problem. |
| 3 | Haiku passed `--pkg com.example.app`, the placeholder from the docs, and `--pkg unknown`. Every query matched nothing, and the report said the launch was missed. | Triage ignores a package that matches nothing and says so. The docs say the example is a placeholder. 4 of 4 after. |

**Still open:** on the 30-second trace with two apps, one Haiku run in three
listed the right leads and then declined to pick one. That is the model
hedging on an ambiguous request, not a wrong report, so the skill was not
changed for it.

**Limits:** two attempts per task at most, so one run moves a cell by a lot.
The tasks use public traces only: no real app's capture, so no code audit and
no remeasure task yet. Our own app captures stay private.
