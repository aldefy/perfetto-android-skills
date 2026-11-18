You are grading runs of coding agents that each answered an Android performance question.

Read rubric.md first. Each run is a folder rNN/ with:
- task.txt: which rubric section applies (t1 to t7)
- answer.md: the agent's final answer to the developer
- log.md: what it ran, with start and end times

Grade every run strictly by the rubric: PASS, INCOMPLETE or FAIL.
- PASS: every "must" holds and no "fail if" holds.
- INCOMPLETE: no "fail if" holds, but a "must" is missing.
- FAIL: any "fail if" holds.

Review passing runs as closely as failing ones: note wasted steps and any claim the trace or the
rubric's facts do not support. You may run trace_processor_shell on the traces, or read the
sample repo, to check a specific claim the rubric does not settle. Read nothing else.

Write grades.json in the judge folder: a JSON list with one object per run and these keys:
run, task, grade, missing (list, short), fail_reasons (list, short), unsupported_claims (list,
short), waste (one short line or empty).

Then reply with just: done.
