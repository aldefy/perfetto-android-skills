# perfetto-android-skills

An agent skill that turns "the app feels slow" into a ranked, evidence-backed
list of causes, then a fix, then a re-measurement that proves the fix worked.

This isn't a wrapper around Perfetto's UI. It's a checklist a senior Android
performance engineer already runs by hand, encoded so an agent (or you) can
run it the same way every time: capture a real trace, ask the same four
questions in the same order, rank findings by *self* time, map slice names
back to files in your repo, then measure again after the fix.

Built alongside the talk [From Trace to Triage](https://trace-to-triage.pages.dev),
on reading Perfetto traces and automating that reading.

## What's inside

```
perfetto-triage/
  SKILL.md                    the method: four questions, six workflow steps, hard rules
  scripts/
    capture.sh                 pulls a real trace off a connected device (cold-start or interactive)
    triage.py                  parses a trace once, runs the query pack, writes a ranked report
    visual_report.py           turns that output into one self-contained report.html with charts
    code_map.py                checks each hot slice name against the app's source: yours, or not
    remeasure.py                before against after: one run each, or many runs each with medians and a noise verdict
    ab_startup.sh               adb-only before/after harness when a benchmark module is out of scope
  queries/                    20 PerfettoSQL files, 00_health.sql through 41_anrs.sql
    on_demand/                 run only with --app-repo: late frames by the app's own trace sections
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
perfetto-triage/scripts/capture.sh com.example.app --start          # a flow of unknown length:
perfetto-triage/scripts/capture.sh com.example.app --stop           # start, run it, stop

# 2. triage — parses once, runs all 20 queries, writes a ranked report
python3 perfetto-triage/scripts/triage.py trace.pftrace --pkg com.example.app --out triage-out

#    it prints its progress as it goes: load, [n/20] per query, then the report files

# 3. open triage-out/report.html, or read triage-out/report.md
#    Trust check first, then findings worst-first
```

## The visual report

`triage.py` also writes `triage-out/report.html`: the four questions, each
answered with a chart and a plain explanation of what the chart means and
where to find the same thing in ui.perfetto.dev. It is one file with no
scripts and no network requests, so it can be opened, attached or published
as is.

```bash
python3 perfetto-triage/scripts/triage.py trace.pftrace --pkg com.example.app \
    --app-repo ~/code/app          # theme the page from the app's own colours
python3 perfetto-triage/scripts/visual_report.py triage-out --app-repo ~/code/app   # re-render only
python3 perfetto-triage/scripts/visual_report.py --print-theme --app-repo ~/code/app
```

`--app-repo` also answers "whose code is this?". Each hot slice name is
checked against the app's source and marked as the app's own only on
evidence: a trace section the app declares, a file and line the slice names,
a `Class.method` the app defines. When nothing matches, the report says so
plainly and tells you to add trace sections, because a report can only name
what the app has instrumented.

Lines marked `inference` on the page are the tool's reading of a chart. The
charts and numbers are measured.

Thread states and frames keep the Perfetto UI's colours, so a strip on the
page and the track in the trace viewer read the same way. Everything else
takes the app's theme when `--app-repo` finds one: a Compose colour scheme,
`res/values/colors.xml`, `Shapes`, bundled fonts. An app with no declared
tokens gets a guess from the colours its UI code uses most, and the footer
says it is a guess. Tokens that would make text unreadable are skipped.
`--no-html` turns the page off.

`capture.sh` refuses a debuggable build (exit 3). Pass `--allow-debug` to
capture one anyway; the report will flag it. Use `--start` and `--stop`
when a script or an agent drives the app: the trace then holds exactly the
flow, with no guessed duration. A started capture stops by itself after
`--max` seconds (default 600). With more than one device
attached (a phone on USB and Wi-Fi counts twice) it stops and lists them:
pick one with `--serial <id>` or `ANDROID_SERIAL`.

No trace file and no device means nothing runs. This skill will not
fabricate a measurement. See the hard rules in `SKILL.md`.

## Trust check

Question 1 is answered by the tool, not left to the reader. `report.md` opens
with a Trust check table and `findings.json` carries the same data as a
`health_status` block:

| Field | Values | Read from |
|---|---|---|
| `trace_health` | `pass`, `fail`, `unknown` | `00_health`: zero error rows is the pass case |
| `coverage.<source>` | `ok`, `missing`, `unknown` | `01_coverage`: a zero means that source was never recorded |
| `build_type` | `release`, `debuggable`, `unknown` | `03_build_type`: `package_list.debuggable` in the trace |
| `startup_capture` | `ok`, `missed`, `not_expected`, `unknown` | `10_startup_summary` plus the capture mode |
| `verdict` | `trusted`, `directional_only`, `not_trustworthy` | all of the above |

`unknown` always means the check did not run or could not decide. It never
means pass, and an unknown build type is never treated as release. When the
verdict is not `trusted`, every finding carries `directional_only: true`:
the code paths it points at are real, the numbers are not quotable.

`triage.py` works out whether a launch was expected from a `capture.sh`
filename (`<pkg>-startup-<stamp>.pftrace`). For a trace named some other way,
pass `--mode startup` or `--mode interactive`.

Every finding also carries a `source` list: the query stems its numbers came
from, so each one can be re-checked in `csv/<stem>.csv`. Findings about where
janky-frame time went (thread split, hot slices) are capped at the severity of
the jank rate itself, so a trace with 0.3% janky frames does not produce a
medium "cost is on the RenderThread".

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

`remeasure.py` prints the trust verdict for each side (and warns when the
build types differ), then resolved findings, new findings, what's still present,
jank numbers side by side, and the top self-time hotspots side by side. It
does not collapse this into a single "% improvement" number on purpose.
Report the median of at least 10 runs, not the best one, or say plainly that
you didn't.

## Source app

The talk uses [StickerExplode](https://github.com/aldefy/StickerExplode)
(public) on a Pixel 9 Pro Fold, release build, as the test app. Known issues
hit while preparing it:

- A debug-build capture produced nine confident findings with no warning.
  `capture.sh` now refuses debuggable builds and `triage.py` reads the build
  type from the trace. The guard is written and tested on synthetic data, but
  not yet proven on a real debuggable capture.
- A capture race in `android_startups` left the startup section silently
  empty. The delay between starting the trace and force-stopping the app was
  increased, and `triage.py` now reports a missed launch as a failed capture.
- An empty `00_health` (good) and a zero in `01_coverage` (bad) looked alike.
  Both are now explicit fields.

One applied app fix: a custom AGSL shader skipped when the device is
near-flat, which produced a partial improvement, not a full fix.

`perfetto-triage-eval/` holds the real fixtures from those incidents and an
eval that reruns the current code against them. Current state: 2 of 3 traps
pass, 1 is unproven. See `perfetto-triage-eval/KNOWN-TRAPS.md`.

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
is to point it at `perfetto-triage/SKILL.md` directly and ask it to
follow the workflow as a runbook. A proper plugin package is on the list;
contributions welcome.

**Antigravity, or any other harness:** same approach. Read `SKILL.md`'s
"Workflow" section top to bottom and execute the steps yourself, or paste it
into the agent's context and ask it to follow the four questions in order.
We have not verified a specific install path for these yet.

## Other Perfetto skills

`perfetto-triage` is a small fixed first pass for startup, jank and binder:
the same 20 queries and the same report every run, a capture that refuses a
build it cannot trust, and a re-measure step after the fix.

There are other good skills in this space. Pick by the job:

| Skill | Good for |
|---|---|
| `perfetto-triage` (this repo) | A repeatable first pass on startup, jank and binder, with before/after |
| [Perfetto AI skill](https://github.com/google/perfetto/tree/main/ai/skills) | Any trace question, with guided workflows for Android memory and GPU |
| [Android profiler skill](https://github.com/android/skills) | Recording plus root-cause analysis that follows the blocking chain |
| [premex-ab/claude-marketplace](https://github.com/premex-ab/claude-marketplace) | Android skills packaged as Claude Code plugins |

## Works with

Triage finds where the time goes. When the cause is Compose runtime work
(`Recomposer:*` / `Compose:*` slices worth 50 ms or more), the report hands
the fix to [compose-expert](https://github.com/aldefy/compose-skill) and
prints its install command. To count recompositions directly, see
[compose-rebound](https://github.com/aldefy/compose-rebound).

## Roadmap

- Report Runnable (waiting for a CPU) separately from Sleeping. Today both
  count as "waiting".
- Check for throttling and memory pressure before blaming app code.
- Evals that run an agent with and without the skill.
- One-command install as a plugin.

## Talk

**From Trace to Triage: Reading Perfetto, Then Automating It**, first given
at BLR Droid, 2026. Deck, live demo screenshots, and the real traces this
skill produced are at [trace-to-triage.pages.dev](https://trace-to-triage.pages.dev).
