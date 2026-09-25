# Reading a Perfetto trace: the mental model

This is the checklist a senior performance engineer runs, written down. It is
also the thing that gets encoded into the query pack — every query in
`queries/` exists to answer one of these questions.

## What a trace actually is

Not a profiler. A profiler samples *your* code. A trace records *events from
the whole system*, on one timeline:

| Layer | What it gives you | Perfetto table |
|---|---|---|
| Kernel scheduler (ftrace) | which thread was on which CPU, and why it stopped | `sched`, `thread_state` |
| Framework + app (atrace) | named spans: `doFrame`, `inflate`, `activityStart`, your own `Trace.beginSection` | `slice` |
| SurfaceFlinger FrameTimeline | per-frame deadlines and who missed them | `actual_frame_timeline_slice` |
| Binder driver | cross-process calls, with AIDL method names | `android_binder_txns` |
| ART | class load, JIT, GC | `slice` (dalvik category) |

That combination is the point. A profiler can tell you a method took 40 ms. A
trace tells you the method took 40 ms **because the thread was descheduled for
32 ms waiting on another process** — which is a completely different bug.

## Question 1: is this trace worth reading?

Before any analysis:

- `stats` with `severity='data_loss'` non-zero → the buffer overflowed. Numbers
  are undercounts. Recapture with a bigger buffer or shorter duration.
- No `actual_frame_timeline_slice` rows → no jank analysis is possible at all.
  Someone recorded with atrace categories only.
- No `sched` rows → you cannot answer running-vs-waiting, which is the most
  important question there is.
- Build is `debuggable` → everything is 2–5× slower and disproportionately so
  in ART. Findings are directional at best.

Queries: `00_health`, `01_coverage`, `02_processes`.

## Question 2: was the main thread running or waiting?

This is the fork in the road, and almost everyone skips it.

Look at the main thread's `thread_state` across the window you care about:

| State | Meaning | What it implies |
|---|---|---|
| `Running` | executing on a CPU | your code is the cost — go rank slices |
| `R` / `R+` (Runnable) | ready but no CPU given | CPU contention, small cores, thermal throttling, or too many threads |
| `S` (Sleeping) | waiting on something in userspace | blocked on IPC, a lock, or a callback that has not fired |
| `D` (Uninterruptible sleep) | waiting on the kernel | almost always disk IO — check `blocked_function` |

**If the thread is mostly Running**, the fix is in your code: less work, later
work, or cheaper work. Baseline Profiles help here, because JIT and class
verification burn CPU.

**If the thread is mostly Sleeping or D**, optimising your code changes
nothing. Find what it is waiting *on*. This is where teams lose weeks: the CPU
profiler shows a flat line, so they conclude "nothing is slow", and the app is
frozen the whole time.

Runnable (`R`) time is the sneaky one. The thread is ready to run and the
scheduler will not give it a core — you are competing with something, or you
got parked on a little core. That is a threading or a device problem, not an
algorithmic one.

Queries: `12_startup_main_thread_state`, `33_main_thread_blocking`.

## Question 3: which frame missed, and whose fault?

A frame has a deadline: 16.6 ms at 60 Hz, 11.1 ms at 90 Hz, 8.3 ms at 120 Hz.
Miss it and the previous frame stays on screen — that is jank.

Android's FrameTimeline records, per frame, both the *expected* and *actual*
timeline, and attributes the miss. `jank_type` is a comma-joined string:

| jank_type contains | Who is at fault | So what |
|---|---|---|
| `App Deadline Missed` | your app | your UI thread or RenderThread ran long |
| `Buffer Stuffing` | your app | you are producing frames faster than they drain; the queue is permanently full |
| `SurfaceFlinger CPU/GPU Deadline Missed` | the compositor | not your code |
| `SurfaceFlinger Scheduling`, `Prediction Error` | the compositor | not your code |
| `Display HAL` | the display pipeline | not your code |
| `None` | nobody | frame was fine |

Get this attribution before touching code. "The app is janky" is regularly
"another process is hammering the device" or "the emulator is the bottleneck".

Once the app owns the miss, split the cost:

- **`Choreographer#doFrame` long** → UI thread work: measure/layout, recomposition,
  binding data, main-thread IO, deserialisation, work posted to the main Handler.
- **`DrawFrame` (RenderThread) long** → draw work: overdraw, huge bitmaps,
  hardware-layer thrash, and shader/pipeline compilation on first use.

Shader compilation deserves its own note: slices like `CreateGraphicsPipeline`
or `...CompileAfterCacheMiss` mean the GPU driver compiled a shader mid-frame.
It happens once per install, so it never reproduces for the developer whose
device has a warm cache — and every single user hits it.

Queries: `20_jank_summary`, `21_worst_frames`, `22_jank_cpu_split`,
`23_jank_hot_slices`.

## Question 4: where does cold start time go?

Cold start is a sequence with a fixed shape. Knowing the shape tells you which
part you can actually influence:

1. **Process fork + `bindApplication`** — the system starts your process. You
   mostly cannot change this, but a huge DEX/class count makes it worse.
2. **`Application.onCreate`** — every SDK anyone ever added initialises here.
   This is usually the biggest self-inflicted chunk.
3. **`activityStart` → `onCreate` → `onResume`** — your first screen.
4. **First `Choreographer#doFrame` + `DrawFrame`** — the first frame is drawn.
   The end of this is **TTID** (time to initial display).
5. **Real content appears** — the end of this is **TTFD** (time to full
   display), and it only exists if you call `reportFullyDrawn()`.

TTID without TTFD is the number teams game: show a skeleton fast, report a
great TTID, and the user still stares at a spinner. If the app never calls
`reportFullyDrawn()`, that is a finding in itself.

Perfetto's own breakdown bins startup time by reason: `launch_delay`, `binder`,
`activity_start`, `inflate`, `choreographer_do_frame`. Start there, then drill.

Class loading during startup is the clearest Baseline Profile signal: hundreds
of `Lcom/example/...;` load slices plus JIT slices mean the code is being
verified and compiled at the worst possible moment.

Queries: `10_startup_summary` … `15_startup_binder`.

## Question 5: where does binder/IPC contention hide?

Binder is the invisible one, because the API does not look remote. All of these
are cross-process calls that can block the main thread:

- `PackageManager` lookups
- `ConnectivityManager`, `TelephonyManager`, `DisplayManager` queries
- `AccountManager`
- `ContentResolver` queries against another app's provider
- `Settings.Secure/Global` reads
- anything from a bound `Service` in your own second process

A synchronous binder call from the main thread is a frozen UI for its duration.
Worse, they cluster: one call is 2 ms, and the same call in a loop over 40 list
items is 80 ms of nothing.

When a call is slow, ask *why* — the client-side breakdown distinguishes:

- reason `Running` → the server really was computing. Make it do less.
- reason `S` / `R` / `R+` → the client was descheduled or queueing for a free
  binder thread. Reducing the *number* of calls beats optimising the server.

Then look at **monitor contention** (Java lock contention) with the main thread
blocked. `android_monitor_contention` names both the blocking and blocked
method. This is the other case where nothing is "running" and everything is
stuck.

Queries: `30_binder_main_thread`, `31_binder_why_slow`, `32_monitor_contention`.

## Ranking hotspots without fooling yourself

Sort by **self time**, not total time. A slice's duration includes all its
children, so `runAll` or `traversal` will always top a total-time ranking and
tell you nothing. Self time = duration minus the sum of direct children.

And the count matters as much as the total: 1 call at 80 ms is an algorithm
problem; 400 calls at 0.2 ms is an architecture problem, and the fixes have
nothing in common.

Query: `40_slice_hotspots`.

## The shape of a good conclusion

> **Observation** — cold start TTID is 1,240 ms, median of 10 runs, Pixel 7,
> release build.
> **Evidence** — main thread waits for 62% of the window; the dominant blocking
> call is `ICameraService::connect`, 5 calls, 29 ms each, all from
> `MainActivity.onCreate`.
> **Cause** — camera acquisition is on the startup critical path.
> **Fix** — acquire lazily, after the first frame.
> **Expected effect** — ~145 ms off TTID; no change to TTFD until content loads.

Anything less specific than that is not a finding, it is a hunch.
