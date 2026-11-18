---
name: perfetto-triage
description: Measure, diagnose, fix and re-measure Android app performance from a Perfetto trace. Use whenever someone asks why an app is slow, janky, or slow to start; mentions Perfetto, a .perfetto-trace/.pftrace file, systrace, jank, dropped frames, ANR, cold start, TTID/TTFD, Macrobenchmark, or Baseline Profiles; wants a trace read or triaged; or wants an existing (brownfield) app made faster with before/after numbers. Also use for Flutter, React Native and other cross-platform apps on Android, since the frame and startup pipeline is the same underneath.
---

# Perfetto triage

Turn "the app feels slow" into a ranked, evidence-backed list of causes, a fix,
and a re-measurement that proves the fix worked.

The rule this skill exists to enforce: **measure, change one thing, measure
again.** A performance claim without a before and an after is an opinion.

## The four questions, in order

Never start with "what is slow". Start here. Each question rules out a whole
class of wrong answers, and the order matters — answering question 3 before
question 2 is how people spend a week optimising code that was never running.

1. **Is the trace trustworthy?** Dropped data, missing data sources, a
   debuggable build, a thermally throttled device — any of these make every
   number downstream meaningless.
2. **Was the main thread RUNNING or WAITING?** Running means your code is slow;
   profile it. Waiting means it was blocked on IPC, IO or a lock; profiling
   your own code will find nothing. This single question changes the fix.
3. **Which deadline was missed, and by whom?** For jank, a frame owns a 16.6 ms
   budget at 60 Hz (11.1 ms at 90 Hz, 8.3 ms at 120 Hz). Perfetto attributes
   the miss to the app or to SurfaceFlinger. If it is SurfaceFlinger, stop
   reading your own code.
4. **Where does the time actually go?** Only now do you rank slices — and rank
   by *self* time, not total, because a wrapper slice inherits everything
   beneath it.

## Workflow

### Step 0 — Establish what you have

Ask, or work out from the repo and device:

- Is there a **trace file already**? Then go to Step 3.
- Is the app **installed on a connected device**? Then Step 1 (no build needed).
- Is there only **source**? Then Step 2.

`adb devices -l` and `adb shell pm list packages | grep <name>` answer this.
If no device is connected, say so plainly: this skill cannot invent
measurements, and a fabricated number is worse than no number. With source
but no phone, still do the **source-only checks** and hand back a capture plan:

1. **Baseline Profile already there?** Look for a `baselineprofile` module,
   `baseline-prof.txt`, or `androidx.profileinstaller` in the dependencies.
   If there is one, do not suggest adding one. The plan must prove it is
   applied on the test phone (Step 6, "Prove the profile is applied").
2. **Benchmark module already there?** A `macrobenchmark` or `benchmark`
   module means the plan uses it (Step 2b) rather than adb alone.
3. **What runs before the first frame:** `Application.onCreate`, AndroidX
   Startup initializers, content providers, the launcher Activity's
   `onCreate`. List them as *candidates to check in a trace*, never as the
   cause.
4. **`reportFullyDrawn` and trace sections:** note whether they exist.
   Without sections, a trace cannot name the app's own code.
5. **The capture plan:** a release-like (non-debuggable) build; a cold start,
   which means force-stopping before every launch (`capture.sh` does this);
   10 runs; the median, with device and build type. Then Step 3.

### Step 1 — Capture from an installed build

```bash
scripts/capture.sh com.example.app                  # cold start
scripts/capture.sh com.example.app --interactive 20 # you drive; captures jank
```

When you are driving the app yourself (a UI script, an MCP tool, a test),
do not guess a duration. Start the trace, run the flow, stop it:

```bash
scripts/capture.sh com.example.app --start
# ... drive the flow, waiting on each screen to appear, not on a timer ...
scripts/capture.sh com.example.app --stop
```

The trace then covers exactly the flow. A fixed window either cuts the end
off or pads the trace with idle time, and both skew the frame counts. `--stop`
finds the running capture from any folder and with any `OUTDIR`, and puts the
trace where `--start` said it would.

Beside every trace `capture.sh` writes a note with the same name ending
`.capture.txt`: the phone's SDK level and how the app is compiled
(`speed-profile`, `verify` and so on). Keep the two files together. Triage reads the note, because an Android 10 trace does
not say which Android version it came from, and no trace says how the app was
compiled. For a trace captured some other way, pass `--sdk 29` to triage.

`capture.sh` checks the package flags first and exits 3 on a `debuggable`
build. Do not work around that by default: get a release-like build. If debug
is truly all there is, `--allow-debug` captures it and the report flags it.

If it exits 4, more than one device is attached (the same phone on USB and
Wi-Fi shows up twice). Ask which one, then pass `--serial <id>`.

This pushes `assets/startup_jank.pbtxt` to the device. Use that config, not the
lightweight `adb shell perfetto <categories>` form: the lightweight form cannot
enable `android.surfaceflinger.frametimeline`, so you get no jank attribution
at all.

The phone's Android version decides what can be measured. Frame deadlines are
recorded from Android 12 (API 31). On an older phone `capture.sh` says so,
drops the config fields that phone's perfetto rejects, and still captures
startup, thread state and binder. Triage then reports late frames as "cannot
be measured on this phone". Never read a frame count from such a trace as
"0 late": it is missing data. An old, slow phone is a fair worst case for cold
start. For jank, use an Android 12+ phone.

Capture hygiene, because a bad capture wastes the whole session:

- Physical device, not an emulator. Emulator numbers are host numbers.
- Screen on, unlocked, plugged out of low-battery state.
- Release-ish build. A `debuggable` build is 2–5× slower and its trace is a
  lie. If all you have is debug, say so in the report and treat findings as
  directional only.
- Run the interaction 3+ times and keep the median. First run is always cold.

### Step 2 — No installed build: build and measure the repo

Two paths, cheapest first.

**2a. Just build and install**, then go to Step 1. `./gradlew :app:installRelease`
(or the closest release-like variant). Check
`references/benchmark-setup.md` if the release variant needs signing.

**2b. Add Macrobenchmark + Baseline Profile scaffolding** when the ask is
"make it faster and prove it". This gives you a repeatable A/B harness and
emits a Perfetto trace per iteration. Full brownfield setup, verified against
the current APIs, is in `references/benchmark-setup.md`. The headline:

```bash
./gradlew :app:generateBaselineProfile      # record the profile
./gradlew :macrobenchmark:connectedCheck    # A/B: None() vs Partial(Require)
```

Traces land in
`macrobenchmark/build/outputs/connected_android_test_additional_output/**/*.perfetto-trace`.

If adding a benchmark module is out of scope, `scripts/ab_startup.sh` gives you
a defensible before/after from adb alone:

```bash
scripts/ab_startup.sh com.example.app .MainActivity 10 none
```

### Step 3 — Triage the trace

```bash
python3 scripts/triage.py trace.pftrace --pkg com.example.app --out triage-out
```

`--pkg` is the app's real package name, from the developer, the capture note or
`adb shell pm list packages`. `com.example.app` in these docs is a placeholder:
never pass it. If you don't know the package, leave `--pkg` out. Triage then
reports on the one app the trace launches, or lists them when there are several.

It parses the trace once into a warm `trace_processor` session, runs the query
pack in `queries/`, and writes:

- `triage-out/report.md` — Trust check, findings worst-first, then the evidence tables
- `triage-out/findings.json` — a `health_status` block plus the same findings, machine-readable
- `triage-out/csv/*.csv` — every raw result
- `triage-out/report.html` — the same findings as a page for a person
- `triage-out/fix_plan.md` — suggested fixes in order, for you to review (Step 5)
- `triage-out/code_audit.md` — with `--app-repo`: the code that ran during the
  capture, read for causes that match the findings (Step 4)
- `triage-out/progress.txt` — one line saying what triage is doing right now

`report.html` opens in the person's browser as soon as triage starts. From the
first second it is a small status page that reloads itself and shows the
current step, and it becomes the report when the run finishes. `progress.txt`
is the same thing in one line. Do not make them ask you whether it is still
analysing, and do not make them go and find the file.

When you triage several traces in a row, pass `--no-open` to all but the one
you want them to read (usually the median run), or they get a tab per trace.
On a machine with no screen nothing opens: give them the path or a link.

`report.html` is what you show the person. It asks the four questions in
order and answers each with a chart drawn from the query results: the main
thread's states, the late frames measured from their deadline, the startup
breakdown, the self-time ranking. Under each chart it says what the chart
shows, what that means, and where to find the same thing in ui.perfetto.dev.

```bash
python3 scripts/triage.py trace.pftrace --pkg com.example.app --app-repo /path/to/app
```

Thread states and frames are drawn in the Perfetto UI's colours, so the page
and the trace viewer read the same way. Those never change. With `--app-repo`
the rest of the page (background, text, accent, ranked bars, corner radius,
font) comes from the app's own theme: a Compose colour scheme,
`res/values/colors.xml`, `Shapes`, bundled font files. If the app declares no
tokens, the page guesses from the colours its UI code uses most and says so in
the footer. A token that would make text unreadable is not used.

When your environment can display HTML (an artifact, a preview pane, a
browser), show `report.html` instead of describing it. It is one file with no
scripts and no network requests. Read `report.md` and `findings.json`
yourself; they hold the full tables.

Read the **Trust check** at the top of `report.md` before anything else. It is
question 1, answered explicitly, and mirrored in `findings.json`:

| `health_status` field | Values | Meaning |
|---|---|---|
| `trace_health` | `pass` / `fail` / `unknown` | `00_health`: zero error rows is the pass case |
| `coverage.<source>` | `ok` / `missing` / `unknown` | `01_coverage`: zero means never recorded |
| `build_type` | `release` / `debuggable` / `unknown` | read from `package_list` in the trace |
| `startup_capture` | `ok` / `missed` / `not_expected` / `unknown` | did a startup capture contain the launch |
| `verdict` | `trusted` / `directional_only` / `not_trustworthy` | what you may do with the numbers |

`unknown` means the check did not run or could not decide. Never read it as
pass. Act on the verdict:

- `trusted`: report numbers, with device, build type and run count.
- `directional_only`: findings carry `directional_only: true`. Say where the
  time goes; do not quote how much. Say why (the `reasons` list).
- `not_trustworthy`: stop and fix the capture.

While it runs, triage prints one line per step on stderr: loading the trace,
each query as `[13/21] 23_jank_hot_slices: 25 rows in 0.4s`, then the report
files. A step that takes long prints `still running, 40s so far` every 20
seconds. On a trace of several hundred MB the load and a few queries can take
minutes; that is slow, not stuck, so relay the current line instead of
guessing. A query that passes `--query-timeout` (default 600 seconds) is
abandoned, the run carries on, and the report opens with a blocker saying
that query did not finish. Never read its empty section as "nothing found".

If the trace was not named by `capture.sh`, pass `--mode startup` or
`--mode interactive` so a startup capture with no launch is reported as a
failed capture instead of an empty startup section.

Then read the findings. They are ordered by severity and each one carries a
concrete next action.

To ask a question the pack does not cover, write SQL directly — see
`references/sql-cookbook.md` for the schema and the idioms that matter. For
one question, query the file:

```bash
trace_processor query trace.pftrace "INCLUDE PERFETTO MODULE android.startup.startups;
SELECT * FROM android_startups"
```

For more than one, load the trace once and keep it loaded. Parsing is the
slow part, and re-parsing a large trace for every follow-up wastes minutes:

```bash
trace_processor server unix --name triage --daemonize trace.pftrace
trace_processor query --remote triage "SELECT count(*) FROM slice"
trace_processor server kill triage     # when you are done
```

### Step 4 — Map slice names to code

A finding is only actionable once it points at a file. Slice names come from
three places, and each maps back differently:

| Slice name looks like | Comes from | How to find it |
|---|---|---|
| `MyRepo.load`, custom text | `Trace.beginSection` / `androidx.tracing.trace {}` | grep the literal string in the repo |
| `Choreographer#doFrame`, `traversal`, `measure`, `layout` | Android framework | not your code directly — but the work *inside* it is |
| `inflate`, `ResourcesImpl#loadDrawable` | framework resource loading | find the layout/drawable in the args |
| `Recomposer`, `Compose:recompose` | Jetpack Compose runtime | needs composition tracing to name composables |
| `CreateGraphicsPipeline`, `...CompileAfterCacheMiss` | GPU driver shader compile | not code — a warm-up problem |
| `JIT compiling ...` | ART | the method name is in the slice; Baseline Profile territory |
| `binder transaction`, `AIDL::java::IFoo::bar` | IPC into another process | find the framework API that wraps it |

Pass `--app-repo <path>` to `triage.py` and it does the first pass of this for
you. Each hot slice name is checked against the app's source and called the
app's own only on evidence: the string passed to a tracing call, a file and
line the slice names, a `Class.method` the app defines, or an app class inside
a framework slice. The result is the "Whose code" table in `report.md`,
`code_map` in `findings.json`, and a "your code" mark on the bars in
`report.html`. "Not in your source" describes the search, not the slice: a
name built at runtime, or code in a dependency, will not be found. Check the
ones that matter by hand.

When the app does declare trace sections, `--app-repo` also answers "which
part of the app was running when frames were late". `24_jank_by_app_section`
counts the late frames that overlap each section, the report draws them as
"Late frames by the app's own trace sections", and `remeasure.py` gives each
section its own before/after row. Overlap is not blame: a long section
overlaps more frames. Use it to decide where to add smaller sections next.

#### Audit the code on the path you profiled

A trace says what was slow. It rarely names the line. So `--app-repo` also
reads the code that ran during the capture and looks in it for causes that
match what the trace measured. The result is `code_audit.md`, `code_audit` in
`findings.json`, and the candidates listed under each suggestion in
`fix_plan.md` and section 5 of `report.html`.

The path is worked out like this:

- A cold start runs the Application class, the startup initializers and the
  launcher Activity from `AndroidManifest.xml`, then the app code those refer
  to, two steps out. It ends on the first screen, which is read from the
  navigation set-up (`startDestination = ...`, a start key, a Navigation 3
  back stack, a Voyager `Navigator`), and that screen's own tree is followed
  three steps in. When the first screen depends on the app's state, every
  branch is listed in the order the code checks them: say which one the
  capture was in. If `code_audit.md` has no "First screen" line, the
  navigation was not recognised: pass the screen with `--flow`.
- A flow you drove runs the screens you opened. The trace does not name them
  unless the app has trace sections, so say which ones:

```bash
python3 scripts/triage.py trace.pftrace --pkg com.example.app \
    --app-repo /path/to/app --flow Home,TripDetail,Checkout
```

  Use the screen names as they appear in the code. A name that matches a file
  or a composable exactly wins over one that only contains it, so `Home` finds
  `HomeScreen.kt` and not every file with Home in its name. A description
  works too ("guided setup, then Pairing Device"): it is split into
  the screens it names. Whatever matches nothing is reported, so check that
  list. If you scripted the flow, you already know the screens: pass them
  every time.
- Every app class, file and trace section the trace itself names is added, and
  marked "named in the trace". This is the only direct evidence of what ran. It
  needs readable names: an Activity in a lifecycle slice, composition tracing,
  or the app's own trace sections.

If a screen you know ran is missing from the list in `code_audit.md`, the audit
did not read it. Add it with `--flow` and run triage again before you conclude
anything about that screen.

The audit then checks those files against the findings. Each check runs only
when a finding asks for it: system-service and telephony calls when the trace
shows main-thread binder time, blocking calls and SDK set-up when startup is
slow or waiting, never-ending animations and unkeyed lazy lists when Compose
work or late frames rank, a missing Baseline Profile when the JIT was busy,
a missing `reportFullyDrawn` when there is no TTFD, and where to put trace
sections when the app has none.

Every item is a candidate. It is a pattern match in source: it cannot see
which thread a line runs on, or whether the line ran at all. A line with a
background dispatcher just above it is flagged as probably fine. Code behind a
debug check (`if (BuildConfig.DEBUG) { ... }`, after `if (!BuildConfig.DEBUG)
return`, or a private function only called from such a place) is left out of a
release capture and listed at the end of `code_audit.md`. Before you
tell the person something is the cause:

1. Open the file and read the function around the line.
2. Decide whether it runs on the path you captured and on the main thread.
3. Drop the ones that do not, and say which you dropped and why.

When the app kept drawing after launch with no input, the finding also says
where the animated value is read. If the frame clock ran in every frame and
recomposition in few of them, it is read in a draw block, and the candidates
next to a `Canvas`, `drawBehind` or `graphicsLayer` block are listed first.
Composition tracing cannot name that kind: nothing recomposes. Find it by
elimination, one suspect per capture, watching the frame count.

Then read the path files yourself with the findings in mind. The audit only
knows the patterns it was given, and the cause is often something specific to
the app that no pattern covers. The list of files it read is in
`code_audit.md`, in the order they are reached from the entry points.

When you explain a finding to the person, pair the two halves: what the trace
measured, and the line in their code that most likely causes it. "79 ms of
main-thread calls to system_server" is a measurement. "`MainActivity.kt:41`
asks the TelephonyManager for the network country in `onCreate`" is something
they can fix.

Without `--app-repo`: take the top slice names from `13_startup_top_slices` /
`23_jank_hot_slices` / `40_slice_hotspots`, then grep the repo for those literals. If almost nothing
matches, that is itself the finding — **the app has no custom tracing**, and the
first fix is to add `androidx.tracing` sections around the suspicious paths and
re-capture. You cannot triage what you did not instrument.

For Compose, enable composition tracing so recomposition slices carry composable
names; for Flutter and React Native see `references/cross-platform.md`.

### Step 5 — Fix one thing

Start from `fix_plan.md` (the same list is `fix_plan` in `findings.json` and
section 5 of `report.html`). Each item says what to change, where if the trace
can tell, and which number should move. With `--app-repo` each item also lists
the candidates the code audit found on the profiled path, with file and line. The list is ordered: the capture
first, then making the app measurable, then fixes whose findings are high or
medium. Fixes whose findings are only information in this trace are listed
under "Can wait".

These are suggestions from one trace, not instructions. Review them before
you act:

1. Read the code each one points at. Drop any that do not apply, and tell the
   person which you dropped and why. A candidate from the code audit is a
   pattern match: confirm it runs on this path, and on which thread, before
   you call it the cause.
2. Take the first one left. One fix per change.
3. Build before and after from the same build type, trace sections in both.
4. Capture 10 or more runs per side and compare them with `remeasure.py`.
5. Raise the fix (a branch, a pull request) only if the number under "Check"
   comes back `clear` or `likely`. Put that table in the pull request. If it
   comes back `noise`, say so, and do not ship it as a win.

If the plan says nothing needs fixing now, say that. Do not go looking for
work in the "Can wait" list unless the person asks for polish.

`references/fixes.md` maps each finding class to concrete fixes. Change **one**
thing. Batching three fixes and re-measuring once tells you the sum moved; it
does not tell you which one paid, and one of them is usually a regression
hiding behind the other two.

Trace sections are not a fix. They change what you can see, not how fast the
app runs. When you add them, add them to the before build as well, so the
only difference between before and after is the fix itself.

Keep the before build. A rebuild overwrites the APK in the build folder, so
copy it aside (`before.apk`) before you build the fix, then install each in
turn on the same device.

### Step 6 — Re-measure and compare

Re-run the identical capture and the identical triage, then diff:

```bash
python3 scripts/triage.py after.pftrace --pkg com.example.app --out after-out
python3 scripts/remeasure.py before-out after-out
```

`remeasure.py` prints the trust verdict of each side first, and warns if the
two build types differ or either side is not `trusted`. Then what resolved,
what's new, what's still present, the jank-summary numbers side by side, and
the top self-time hotspots side by side. It does not compute a single
"% improvement" verdict on purpose — a
single number invites picking the flattering run out of ten, which is
exactly what the next paragraph exists to prevent.

**Prove the profile is applied.** A build installed with adb can read
`status=speed-profile` with an empty profile behind it: the label is set at
install, and the profileinstaller library only copies the Baseline Profile in
on first launch. Measure that install and every startup number is slower than
what store users get. On a real app the JIT went from 2.2 s to 0.1 s and TTID
from 674 to 487 ms (10 v 10, both clear) once the profile was applied, with the
label the same before and after. So before any startup baseline, and on the
before and after builds alike:

```bash
adb shell monkey -p com.example.app -c android.intent.category.LAUNCHER 1; sleep 10
adb shell am force-stop com.example.app
adb shell am broadcast -a androidx.profileinstaller.action.INSTALL_PROFILE \
  com.example.app/androidx.profileinstaller.ProfileInstallReceiver
adb shell cmd package compile -f -m speed-profile com.example.app
scripts/ab_startup.sh com.example.app .MainActivity 10 profile   # does the above, then measures
```

Only when the JIT is still busy after that is the profile stale.

Use the same mode for both builds.

For a number you can quote, capture 10 or more runs per side, triage each
into its own directory, and hand them all to `remeasure.py`. Do not work the
medians out by hand:

```bash
python3 scripts/remeasure.py --before runs/before-* --after runs/after-*
```

It prints the median and range of each metric per side (TTID, late frames,
class loading, and any of the app's own trace sections when triage ran with
`--app-repo`), the change in the median, and a verdict on that change:
`clear` (every after run is on one side of every before run), `likely`, or
`noise`. Report only what is `clear` or `likely`. A change marked `noise` is
not a result, however good its percentage looks.

If all you need is the cold start time, `scripts/ab_startup.sh` measures 10
launches per build without recording a trace at all. Record traces when you
also need to know where the time went.

Report the **median of ≥10 runs**, not the best run and not the mean. Startup
distributions have a long right tail; a mean shifted by one thermal outlier is
how people fake wins. State the device and build type next to the number, or
the number is not comparable to anyone else's.

## Reporting

Write findings as: **observation → evidence → cause → fix → expected effect.**

> Cold start TTID is 1,240 ms (median of 10, Pixel 7, release build).
> The main thread is waiting 62% of that window
> (`12_startup_main_thread_state`), and the dominant blocking call is
> `ICameraService::connect` at 29 ms × 5 from `MainActivity.onCreate`
> (`30_binder_main_thread`). Moving camera acquisition behind first frame
> should return roughly 145 ms.

Never report a number without the device, the build type, and the run count.

`report.html` covers the observation and the evidence. Add what it cannot
know: which hot slice names are the app's own code (Step 4), the cause you
infer, and the fix. On a `directional_only` report the page still draws the
numbers so the shape is visible. Its banner says not to quote them, and
neither should you.

Keep measured and inferred apart. Every finding carries a `source`: the query
stems its numbers came from, re-checkable in `csv/<stem>.csv`. That part is
measured. The cause you propose and the fix you recommend are inferences, so
say so. When the trace cannot answer the question, the answer is "unknown",
with the reason, not a guess.

Do not stop at the report. A table of findings is not what the person came
for. Unless they asked for a diagnosis only, the next thing you do after triage
is Step 4 and Step 5: read the top candidates, say which one you would fix
first and why, name the file and line, and ask to make that change. If they
asked you to fix it, make it, then measure it (Step 6). End every triage reply
with that next action, not with a summary of the tables.

## Hard rules

- Do not present a number you did not measure in this session. If a step was
  skipped, say which and why.
- Do not analyse a `debuggable` build and report the numbers as real.
  `health_status.build_type` must be `release` before any number is quoted;
  `unknown` is not `release`.
- Do not compare runs across different devices, thermal states, or build types.
- Empty result ≠ healthy app. Read `health_status` first: an empty `00_health`
  is `trace_health: pass`, a zero in `01_coverage` is `missing`, and an empty
  startup section on a startup capture is `startup_capture: missed`.
- If the trace does not contain the interaction the user cares about, recapture.
  Do not reinterpret an unrelated trace to fit the question.

## Related skills

Triage says where the time goes. For some causes the fix is another skill's
field. When triage sees one it adds a finding and a `handoffs` entry in
`findings.json` with the skill, the repo and the install command.

| Triage sees | Hand the fix to | Install |
|---|---|---|
| `Recomposer:*` / `Compose:*` slices worth 50 ms or more | `compose-expert` ([aldefy/compose-skill](https://github.com/aldefy/compose-skill)): recomposition, state, lazy lists, animation | Claude Code: `/plugin marketplace add aldefy/compose-skill`, then `/plugin install compose-expert`. Copilot CLI: `copilot plugin install aldefy/compose-skill` |

Check your own skill list first. If `compose-expert` is there, use it. If it
is not, tell the person which command installs it and ask before installing
anything: slash commands are theirs to type, and copying a skill into their
skills folder is their call. Then come back here to re-measure (Step 6), so
the Compose fix is proven the same way as any other.

To count recompositions directly instead of inferring them from a trace, see
[compose-rebound](https://github.com/aldefy/compose-rebound).

## Reference files

| File | Read it when |
|---|---|
| `references/reading-a-trace.md` | You need the human mental model, or you are explaining a trace to someone |
| `references/sql-cookbook.md` | You need to write PerfettoSQL beyond the shipped pack |
| `references/capture.md` | Capture is failing, or you need a non-default config |
| `references/benchmark-setup.md` | Adding Macrobenchmark / Baseline Profiles to a brownfield app |
| `references/fixes.md` | You have a finding and need the fix and its expected size |
| `references/cross-platform.md` | The app is Flutter, React Native, Unity or a WebView |
