#!/usr/bin/env python3
"""Write the prompt for one agent-eval run.

    python3 make_prompt.py <task> <run-id> [skill-dir] > prompt.txt

task:     t1 .. t7 (see rubric.md for what each one checks)
run-id:   a folder name, e.g. t1-skill-haiku-a. The agent works in $EVAL_ROOT/runs/<run-id>.
skill-dir: leave out for the baseline (no skill). Give a copy of perfetto-triage/
           for the skill run. Copy it: an agent must never edit the skill it is graded on.

$EVAL_BUDGET (default 20) caps the commands an agent may run, so slow and fast runs
are compared on the same footing. evals.py checks the cap from each run's log.md.

$EVAL_ROOT (default /tmp/evals) holds traces/ (the four public traces in README.md),
nia/ (a copy of Now in Android, for t7) and runs/.

The prompts are plain requests. None says where the answer lives, and four of
them carry a wrong premise on purpose: the "cold start" is warm, the old phone
cannot measure jank, the before and after are different apps, and the
"scrolling" trace is a Chrome browser trace.
"""
import os
import sys

ROOT = os.environ.get("EVAL_ROOT", "/tmp/evals")
TR = f"{ROOT}/traces"

TASKS = {
    "t1": f"I captured this trace during a cold start of my app: {TR}/api34_startup_cold.perfetto-trace\n"
          "Why is startup slow, and what should I fix first?",
    "t2": "Did my app drop frames while it was starting up? How many, and why?\n"
          f"Trace: {TR}/api24_startup_cold.perfetto-trace",
    "t3": "My release build is installed on a test phone with adb. dumpsys says status=speed-profile, "
          "but the JIT compiler still runs for about 2 seconds after every cold start. Should I "
          "regenerate my Baseline Profile? I don't have the phone right now, so just tell me what to do.",
    "t4": f"This 30-second trace is from my app on a test phone: {TR}/example_android_trace_30s.pb\n"
          "What's the single biggest performance problem, and how sure are you?",
    "t5": "I changed how my app initialises its SDKs. Before the change: "
          f"{TR}/api24_startup_cold.perfetto-trace\nAfter the change: {TR}/api34_startup_cold.perfetto-trace\n"
          "Did my change make cold start faster? By how much?",
    "t6": "My app janks when I scroll the feed. Here is a trace I recorded while scrolling: "
          f"{TR}/scrolling_with_blocked_nonblocked_frames_new.pftrace\nWhat is causing the jank?",
    "t7": f"My app is slow to start. The code is in {ROOT}/nia. Make it start faster. "
          "I don't have a phone with me right now.",
}


BUDGET = int(os.environ.get("EVAL_BUDGET", "20"))


def prompt(task: str, run_id: str, skill_dir: str = "", budget: int = BUDGET) -> str:
    tp = "~/.cache/perfetto-triage/v57.2/trace_processor_shell"
    keep_out = (f"Do not use the Skill tool. Do not read anything under ~/.claude or other "
                f"{ROOT}/skill-* folders. You may read {ROOT}/nia if the request points there. "
                f"Do not look at other folders in {ROOT}/runs.")
    if skill_dir:
        tools = (f"Tools you can use: a shell, and a skill for this kind of work in {skill_dir} "
                 "(start by reading its SKILL.md and follow it; its scripts download what they "
                 "need or use the cache at ~/.cache/perfetto-triage). Python 3 is installed. "
                 "No phone is connected.")
    else:
        tools = (f"Tools you can use: a shell. trace_processor_shell is at {tp} (it supports "
                 "`-q file.sql` and the PerfettoSQL stdlib via INCLUDE PERFETTO MODULE). "
                 "Python 3 is installed. No phone is connected.")
        keep_out = keep_out.replace(f"or other {ROOT}/skill-*", f"or {ROOT}/skill-*")
    out = f"{ROOT}/runs/{run_id}"
    return f"""You are a coding agent helping an Android developer. Work only in {out}/ (create it).

{tools}
{keep_out}
You may run at most {budget} commands (shell commands and file reads together). When you reach that, stop and answer from what you have found.

The developer's request:
---
{TASKS[task]}
---

Do the work, then write two files:
1. {out}/answer.md: your final answer to the developer, exactly as you would send it.
2. {out}/log.md: first line `start <epoch seconds>`, then one line per command you ran or file you read, in order, then `end <epoch seconds>`.
Your final reply to me is just: done."""


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in TASKS:
        sys.exit(__doc__)
    print(prompt(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ""))
