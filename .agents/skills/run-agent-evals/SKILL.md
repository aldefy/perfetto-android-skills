---
name: run-agent-evals
description: Run the perfetto-triage agent evals, with and without the skill, graded blind. Use when asked to eval, benchmark or check the skill after a change, compare models on it, or smoke-test the eval wiring.
---

# Run the agent evals

These evals hand plain requests to fresh agents, some with the perfetto-triage skill and some
without, and have a separate judge grade the answers blind. Everything lives in
`perfetto-triage-eval/agent-evals/`. Read its README.md once for what the tasks test.

## 1. Settle the run before starting

Work these out from the request. Ask only about what you cannot work out.

- **Which skill.** The current checkout's `perfetto-triage/` unless told otherwise. Copy it to
  `/tmp/evals/skill-<label>/`. An agent must never run against, or be able to edit, the checkout
  being graded.
- **Configs.** `base,skill` to measure what the skill adds. `skill` alone to compare two versions
  of the skill (copy each to its own folder and run `prompts` once per folder).
- **Models.** Default `haiku,sonnet`. Smaller models expose gaps that larger ones work around.
- **Tasks.** Start with `smoke`. Run `all` only after the smoke run graded cleanly.
- **Attempts.** 1 for a smoke run. 2 or more before believing a difference: one run moves a
  7-task cell by 14 points.
- **Budget.** 20 commands per run, the default. Keep it the same across everything you compare.

## 2. Set up

```bash
export EVAL_ROOT=/tmp/evals
mkdir -p $EVAL_ROOT/traces $EVAL_ROOT/runs
```

- The four traces in the README's table go in `$EVAL_ROOT/traces/`. Check their sha256 prefixes.
  If they are missing, ask the user to supply them. Do not substitute other traces.
- Task t7 needs Now in Android in `$EVAL_ROOT/nia/` (a shallow clone is enough).
- `trace_processor_shell` v57.2 must be at `~/.cache/perfetto-triage/v57.2/`. Running
  `perfetto-triage/scripts/triage.py` once on any trace downloads it.

## 3. Write the prompts

```bash
cd perfetto-triage-eval/agent-evals
python3 evals.py prompts --tasks smoke --configs base,skill --models haiku --skill-dir /tmp/evals/skill-v1
```

## 4. Run the agents

One fresh agent per prompt file, on the model named in its run id, given the file's text and
nothing else. With Claude Code, that is a sub-agent per file with that model. Run them in
parallel, up to your session's limit.

When they are done, check that every run has `answer.md` and `log.md` under
`$EVAL_ROOT/runs/<run-id>/`. Rerun any that is missing one. Check that no run changed the skill
copy (`diff -r` against the checkout).

## 5. Grade blind

```bash
python3 evals.py judge-prep --round r1
```

Then start one judge agent, on a model at least as strong as the largest one being graded,
with this prompt: read `judge.md`, then `rubric.md`, then grade every folder in
`$EVAL_ROOT/judge-r1/`. Do not tell the judge which runs used the skill.

## 6. Report

```bash
python3 evals.py tally
```

Report the config table first, then what failed and why, then any run over the budget. Say how
many attempts each cell had. Do not report a difference as real when it is one run.

## 7. Fix, then rerun only what failed

Read the transcripts of failing runs before changing anything. Change the skill only where a
transcript shows the problem. Then copy the skill to a new folder (`skill-v2`), write prompts
for the failing tasks only with `--attempts 2`, and grade them as a new round.

## Never

- Point an agent at the checkout itself, or let a run see another run's folder.
- Tell the judge which config or model a run used.
- Add a task to `rubric.md` without facts you checked against the trace yourself.
- Put private app traces, names or output into this repo.
