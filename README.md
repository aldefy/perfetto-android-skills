# perfetto-android-skills

An agent skill that turns "the app feels slow" into a ranked, evidence-backed
list of causes, then a fix, then a re-measurement that proves the fix worked.

This isn't a wrapper around Perfetto's UI. It's a checklist a senior Android
performance engineer already runs by hand, encoded so an agent (or you) can
run it the same way every time: capture a real trace, ask the same four
questions in the same order, rank findings by *self* time, map slice names
back to files in your repo, then measure again after the fix.

Built for [BLR Droid](https://trace-to-triage.pages.dev), a talk on reading
Perfetto traces and automating that reading.

## What's inside

```
perfetto-triage/
  SKILL.md                    the method: four questions, six workflow steps, hard rules
  scripts/
    capture.sh                 pulls a real trace off a connected device (cold-start or interactive)
    triage.py                  parses a trace once, runs the query pack, writes a ranked report
    remeasure.py                diffs two triage-out directories: findings, jank numbers, self-time hotspots
    ab_startup.sh               adb-only before/after harness when a benchmark module is out of scope
  queries/                    18 PerfettoSQL files, 00_health.sql through 41_anrs.sql
  references/
    reading-a-trace.md         the human mental model: what a track/slice/timeline actually means
    sql-cookbook.md             PerfettoSQL schema and idioms for questions the query pack doesn't cover
    capture.md                  capture troubleshooting, non-default configs
    benchmark-setup.md          adding Macrobenchmark + Baseline Profiles to a brownfield app
    fixes.md                    finding-class to concrete fix to expected size
    cross-platform.md           Flutter, React Native, Unity, WebView: same pipeline underneath
  assets/
    startup_jank.pbtxt          the real trace config capture.sh pushes to the device
```

## The four questions

Every finding this skill produces traces back to one of these, asked in this
order. Answering question 3 before question 2 is how people spend a week
optimizing code that was never running.

1. **Is the trace trustworthy?** Dropped data, a debuggable build, missing
   data sources: any of these make every number downstream meaningless.
2. **Was the main thread running or waiting?** Running means your code is
   slow, profile it. Waiting means it's blocked on IPC, IO, or a lock, and
   profiling your own code will find nothing. This one question changes the
   fix.
3. **Which deadline was missed, and by whom?** A frame owns 16.6ms at 60Hz.
   Perfetto attributes the miss to the app or to SurfaceFlinger. If it's
   SurfaceFlinger, stop reading your own code.
4. **Where does the time actually go?** Only now do you rank slices, by
   *self* time, not total, because a wrapper slice inherits everything
   nested under it. Sorting by total time lies.

## Quickstart

```bash
# 1. capture — device connected, app installed, screen on
perfetto-triage/scripts/capture.sh com.example.app                  # cold start
perfetto-triage/scripts/capture.sh com.example.app --interactive 20 # you drive, it records

# 2. triage — parses once, runs all 18 queries, writes a ranked report
python3 perfetto-triage/scripts/triage.py trace.pftrace --pkg com.example.app --out triage-out

# 3. read triage-out/report.md — findings worst-first, each with a next action
```

No trace file and no device means nothing runs. This skill will not
fabricate a measurement. See the hard rules in `SKILL.md`.

## Fix and re-measure

Triage names the problem. It does not touch code. Once you've picked one
finding and changed one thing (`references/fixes.md` maps finding classes to
concrete fixes), recapture the identical interaction and diff against the
baseline:

```bash
# capture + triage again, same interaction, same device, same build type
perfetto-triage/scripts/capture.sh com.example.app --interactive 20
python3 perfetto-triage/scripts/triage.py after.pftrace --pkg com.example.app --out after-out

# diff against the baseline triage-out directory
python3 perfetto-triage/scripts/remeasure.py triage-out after-out
```

`remeasure.py` prints resolved findings, new findings, what's still present,
jank numbers side by side, and the top self-time hotspots side by side. It
does not collapse this into a single "% improvement" number on purpose.
Report the median of at least 10 runs, not the best one, or say plainly that
you didn't.

## Source app

The BLR Droid talk used [StickerExplode](https://github.com/aldefy/StickerExplode)
(public) on a Pixel 9 Pro Fold, release build, as the test app. Known issues
hit during that session: a debug-build contamination bug (fixed in
`capture.sh`'s verification), and a capture race condition in
`android_startups` (fixed by increasing the delay between starting the trace
and force-stopping the app). One applied fix: a custom AGSL shader skipped
when the device is near-flat, which produced a partial improvement, not a
full fix. See `perfetto-triage-eval/` for the known failure modes this
surfaced.

## Agent skill installation guide

This is a [Claude Code](https://claude.com/claude-code) agent skill: a
`SKILL.md` file with YAML frontmatter (`name`, `description`) that Claude
reads to decide when to load it, plus supporting scripts and reference docs
it can call or read once loaded.

### Install for yourself (personal skills directory)

```bash
mkdir -p ~/.claude/skills
cp -r perfetto-triage ~/.claude/skills/perfetto-triage
```

That's it. Claude Code scans `~/.claude/skills/*/SKILL.md` at the start of
every session. The next time you ask something like *"why is this app
janky"* or *"triage this trace"*, or paste a `.pftrace`/`.perfetto-trace`
path, Claude will recognize the skill's `description` field and offer to use
it, or invoke it directly if you're specific enough.

### Install for a team (project-scoped)

Drop it inside a repo instead of your home directory, so it travels with the
project and shows up for anyone using Claude Code in that repo:

```bash
mkdir -p .claude/skills
cp -r perfetto-triage .claude/skills/perfetto-triage
git add .claude/skills/perfetto-triage
git commit -m "Add perfetto-triage agent skill"
```

Project-scoped skills take precedence over personal ones of the same name,
so a repo can pin its own version even if a teammate has a different one
installed globally.

### Verify it loaded

Start a new Claude Code session in a directory where the skill is visible
(either `~/.claude/skills/` or the project's `.claude/skills/`) and ask:

```
what skills do you have available?
```

`perfetto-triage` should be in the list. If it isn't, check that
`SKILL.md`'s frontmatter is valid YAML and that the file sits at
`<skills-dir>/perfetto-triage/SKILL.md`, not nested one level deeper.

### Requirements

- `adb` on your `PATH`, a connected Android device. A physical device is
  recommended; emulator numbers are host numbers, not device numbers.
- Python 3 (`triage.py` downloads `trace_processor` on first run, no other
  Python dependencies)
- A release or release-like build of the app under test. A `debuggable`
  build runs 2 to 5x slower and its trace is not representative. The skill
  will still triage it, but flags every finding as directional-only.

### Other agent harnesses

Nothing here is Claude-specific past the `SKILL.md` frontmatter. The scripts
and query pack are plain bash, Python, and SQL, so any harness that can read
a markdown file and run shell commands can use this.

**Codex CLI** installs plugins from a marketplace (`codex plugin add
<plugin>@<marketplace>`, sources registered with `codex plugin marketplace
add`) rather than a flat skills folder. We have not packaged this repo as a
Codex plugin yet and do not want to guess at the manifest format in a
document other people will follow. If you use Codex, the fastest path
tonight is to point it at `perfetto-triage/SKILL.md` directly and ask it to
follow the workflow as a runbook. A proper plugin package is on the list;
contributions welcome.

**Antigravity, or any other harness:** same approach. Read `SKILL.md`'s
"Workflow" section top to bottom and execute the steps yourself, or paste it
into the agent's context and ask it to follow the four questions in order.
We have not verified a specific install path for these yet.

## Talk

**From Trace to Triage: Reading Perfetto, Then Automating It**, BLR Droid,
2026. Deck, live demo screenshots, and the real traces this skill produced
are at [trace-to-triage.pages.dev](https://trace-to-triage.pages.dev).
