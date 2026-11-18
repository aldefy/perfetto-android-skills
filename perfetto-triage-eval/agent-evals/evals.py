#!/usr/bin/env python3
"""The steps around the agent runs: write the prompts, hide the runs from the judge, tally.

    python3 evals.py prompts --tasks smoke --configs base,skill --models haiku --skill-dir /tmp/evals/skill-v1
    python3 evals.py judge-prep --round r1
    python3 evals.py tally

prompts     writes $EVAL_ROOT/prompts/<run-id>.txt and adds each run to $EVAL_ROOT/runs.json.
            --tasks: smoke (t1, t3, t6: three quick tasks to check the wiring), all, or t1,t5,...
            --attempts N repeats each task, config and model N times (ids end -a, -b, ...).
judge-prep  copies every finished run that has no grade yet into $EVAL_ROOT/judge-<round>/rNN/,
            renamed at random, with paths scrubbed so the judge cannot see the config or model.
            The key is kept in $EVAL_ROOT/judge-<round>-map.json.
tally       reads every judge-*/grades.json, decodes them with their maps and prints pass rates
            by config, by task and by model, median time, and the runs that broke the budget.

Agents themselves are started by whatever runs them (Claude Code sub-agents, Codex, a CI job):
give each one the text of its prompt file and nothing else.
"""
import argparse
import json
import os
import random
import re
import shutil
import statistics
import string
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_prompt import BUDGET, ROOT, TASKS, prompt  # noqa: E402

SMOKE = ["t1", "t3", "t6"]
MANIFEST = os.path.join(ROOT, "runs.json")


def load_manifest():
    return json.load(open(MANIFEST)) if os.path.exists(MANIFEST) else {}


def cmd_prompts(a):
    tasks = SMOKE if a.tasks == "smoke" else sorted(TASKS) if a.tasks == "all" else a.tasks.split(",")
    bad = [t for t in tasks if t not in TASKS]
    if bad:
        sys.exit(f"unknown task: {', '.join(bad)}")
    configs = a.configs.split(",")
    if any(c != "base" for c in configs) and not a.skill_dir:
        sys.exit("--skill-dir is needed for any config other than base")
    os.makedirs(os.path.join(ROOT, "prompts"), exist_ok=True)
    runs = load_manifest()
    made = []
    for t in tasks:
        for c in configs:
            for m in a.models.split(","):
                for n in range(a.attempts):
                    rid = f"{t}-{c}-{m}" + (f"-{string.ascii_lowercase[n]}" if a.attempts > 1 else "")
                    text = prompt(t, rid, "" if c == "base" else a.skill_dir, a.budget)
                    open(os.path.join(ROOT, "prompts", rid + ".txt"), "w").write(text)
                    runs[rid] = {"task": t, "config": c, "model": m, "skill_dir": "" if c == "base" else a.skill_dir,
                                 "budget": a.budget}
                    made.append(rid)
    json.dump(runs, open(MANIFEST, "w"), indent=1)
    print(f"{len(made)} prompts in {ROOT}/prompts/. Give each agent its file's text, with the model named in the run id.")
    for rid in made:
        print(f"  {rid}")


def log_stats(run_dir):
    """Seconds and command count from log.md, or (None, None)."""
    path = os.path.join(run_dir, "log.md")
    if not os.path.exists(path):
        return None, None
    lines = [x for x in open(path).read().splitlines() if x.strip()]
    start = next((int(x.split()[1]) for x in lines if x.startswith("start ") and x.split()[1].isdigit()), None)
    end = next((int(x.split()[1]) for x in lines if x.startswith("end ") and x.split()[1].isdigit()), None)
    cmds = [x for x in lines if not x.startswith(("start ", "end "))]
    return (end - start if start and end else None), len(cmds)


def graded_runs():
    out = set()
    for name in os.listdir(ROOT) if os.path.isdir(ROOT) else []:
        if name.startswith("judge-") and name.endswith("-map.json"):
            out |= set(json.load(open(os.path.join(ROOT, name))).values())
    return out


def cmd_judge_prep(a):
    runs = load_manifest()
    done = graded_runs()
    todo = [r for r in sorted(runs) if r not in done and os.path.exists(os.path.join(ROOT, "runs", r, "answer.md"))]
    if not todo:
        sys.exit("no finished, ungraded runs")
    out = os.path.join(ROOT, f"judge-{a.round}")
    if os.path.exists(out):
        sys.exit(f"{out} exists; pick another --round")
    rng = random.Random(a.seed)
    ids = list(range(1, len(todo) + 1))
    rng.shuffle(ids)
    key = {}
    for rid, i in zip(todo, ids):
        d = os.path.join(out, f"r{i:02d}")
        os.makedirs(d)
        skill = runs[rid].get("skill_dir") or ""
        for f in ("answer.md", "log.md"):
            src = os.path.join(ROOT, "runs", rid, f)
            text = open(src).read() if os.path.exists(src) else ""
            text = text.replace(os.path.join(ROOT, "runs", rid), "/run")
            if skill:
                text = text.replace(skill.rstrip("/"), "/tool")
                text = re.sub(rf"\b{re.escape(os.path.basename(skill.rstrip('/')))}\b", "tool", text)
            # Any run id or skill folder name left in a log tells the judge the config or model.
            text = re.sub(r"\bskill-[\w.-]+", "tool", text)
            text = re.sub(rf"\bt\d+-[\w-]+", "this-run", text)
            open(os.path.join(d, f), "w").write(text)
        open(os.path.join(d, "task.txt"), "w").write(runs[rid]["task"] + "\n")
        key[f"r{i:02d}"] = rid
    json.dump(key, open(os.path.join(ROOT, f"judge-{a.round}-map.json"), "w"), indent=1)
    print(f"{len(todo)} runs in {out}/. Give the judge judge.md, rubric.md and that folder.")


def cmd_tally(a):
    runs = load_manifest()
    grades = {}
    for name in sorted(os.listdir(ROOT)):
        m = re.fullmatch(r"judge-(.+)-map\.json", name)
        if not m:
            continue
        gpath = os.path.join(ROOT, f"judge-{m.group(1)}", "grades.json")
        if not os.path.exists(gpath):
            continue
        key = json.load(open(os.path.join(ROOT, name)))
        for g in json.load(open(gpath)):
            if g.get("run") in key:
                grades[key[g["run"]]] = g  # a later round regrades
    if not grades:
        sys.exit("no grades yet")

    def row(label, items):
        n = len(items)
        p = sum(g["grade"] == "PASS" for _, g in items)
        i = sum(g["grade"] == "INCOMPLETE" for _, g in items)
        f = sum(g["grade"] == "FAIL" for _, g in items)
        secs = [s for s in (log_stats(os.path.join(ROOT, "runs", r))[0] for r, _ in items) if s]
        med = f"{statistics.median(secs):.0f} s" if secs else "-"
        print(f"| {label} | {p} / {n} ({100 * p // n}%) | {i} | {f} | {med} |")

    def table(title, keyfn):
        print(f"\n{title}\n\n| | Pass | Incomplete | Fail | Median time |\n|---|---|---|---|---|")
        groups = defaultdict(list)
        for r, g in grades.items():
            groups[keyfn(r)].append((r, g))
        for k in sorted(groups):
            row(k, groups[k])

    meta = lambda r: runs.get(r, {"task": r.split("-")[0], "config": r.split("-")[1], "model": r.split("-")[2]})
    table("By config", lambda r: meta(r)["config"])
    table("By config and model", lambda r: f"{meta(r)['config']}, {meta(r)['model']}")
    table("By task and config", lambda r: f"{meta(r)['task']}, {meta(r)['config']}")
    over = []
    for r in grades:
        _, n = log_stats(os.path.join(ROOT, "runs", r))
        cap = runs.get(r, {}).get("budget", BUDGET)
        if n is not None and n > cap:
            over.append(f"{r} ({n} of {cap})")
    print("\nOver the command budget: " + (", ".join(sorted(over)) if over else "none"))
    failing = [f"{r}: {', '.join(g.get('fail_reasons') or g.get('missing') or [])[:160]}"
               for r, g in sorted(grades.items()) if g["grade"] != "PASS"]
    if failing:
        print("\nNot passing:")
        for x in failing:
            print(f"- {x}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prompts")
    p.add_argument("--tasks", default="smoke")
    p.add_argument("--configs", default="base,skill")
    p.add_argument("--models", default="haiku")
    p.add_argument("--attempts", type=int, default=1)
    p.add_argument("--skill-dir", default="")
    p.add_argument("--budget", type=int, default=BUDGET)
    j = sub.add_parser("judge-prep")
    j.add_argument("--round", required=True)
    j.add_argument("--seed", type=int, default=7)
    sub.add_parser("tally")
    a = ap.parse_args()
    {"prompts": cmd_prompts, "judge-prep": cmd_judge_prep, "tally": cmd_tally}[a.cmd](a)


if __name__ == "__main__":
    main()
