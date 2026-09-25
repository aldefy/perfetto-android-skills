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
If no device is connected, say so plainly and stop — this skill cannot invent
measurements, and a fabricated number is worse than no number.

### Step 1 — Capture from an installed build

```bash
scripts/capture.sh com.example.app                  # cold start
scripts/capture.sh com.example.app --interactive 20 # you drive; captures jank
```

This pushes `assets/startup_jank.pbtxt` to the device. Use that config, not the
lightweight `adb shell perfetto <categories>` form: the lightweight form cannot
enable `android.surfaceflinger.frametimeline`, so you get no jank attribution
at all. FrameTimeline needs Android 12+.

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

It parses the trace once into a warm `trace_processor` session, runs the query
pack in `queries/`, and writes:

- `triage-out/report.md` — findings worst-first, then the evidence tables
- `triage-out/findings.json` — the same findings, machine-readable
- `triage-out/csv/*.csv` — every raw result

Read `report.md` before doing anything else. The findings are already ordered
by severity and each one carries a concrete next action.

To ask a question the pack does not cover, write SQL directly — see
`references/sql-cookbook.md` for the schema and the idioms that matter:

```bash
trace_processor query trace.pftrace "INCLUDE PERFETTO MODULE android.startup.startups;
SELECT * FROM android_startups"
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

So: take the top slice names from `13_startup_top_slices` / `23_jank_hot_slices`
/ `40_slice_hotspots`, then grep the repo for those literals. If almost nothing
matches, that is itself the finding — **the app has no custom tracing**, and the
first fix is to add `androidx.tracing` sections around the suspicious paths and
re-capture. You cannot triage what you did not instrument.

For Compose, enable composition tracing so recomposition slices carry composable
names; for Flutter and React Native see `references/cross-platform.md`.

### Step 5 — Fix one thing

`references/fixes.md` maps each finding class to concrete fixes. Change **one**
thing. Batching three fixes and re-measuring once tells you the sum moved; it
does not tell you which one paid, and one of them is usually a regression
hiding behind the other two.

### Step 6 — Re-measure and compare

Re-run the identical capture and the identical triage, then diff:

```bash
python3 scripts/triage.py after.pftrace --pkg com.example.app --out after-out
python3 scripts/remeasure.py before-out after-out
```

`remeasure.py` prints what resolved, what's new, what's still present, the
jank-summary numbers side by side, and the top self-time hotspots side by
side. It does not compute a single "% improvement" verdict on purpose — a
single number invites picking the flattering run out of ten, which is
exactly what the next paragraph exists to prevent.

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

## Hard rules

- Do not present a number you did not measure in this session. If a step was
  skipped, say which and why.
- Do not analyse a `debuggable` build and report the numbers as real.
- Do not compare runs across different devices, thermal states, or build types.
- Empty result ≠ healthy app. Check `00_health` and `01_coverage` first — an
  empty jank table usually means FrameTimeline was never recorded.
- If the trace does not contain the interaction the user cares about, recapture.
  Do not reinterpret an unrelated trace to fit the question.

## Reference files

| File | Read it when |
|---|---|
| `references/reading-a-trace.md` | You need the human mental model, or you are explaining a trace to someone |
| `references/sql-cookbook.md` | You need to write PerfettoSQL beyond the shipped pack |
| `references/capture.md` | Capture is failing, or you need a non-default config |
| `references/benchmark-setup.md` | Adding Macrobenchmark / Baseline Profiles to a brownfield app |
| `references/fixes.md` | You have a finding and need the fix and its expected size |
| `references/cross-platform.md` | The app is Flutter, React Native, Unity or a WebView |
