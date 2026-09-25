# From finding to fix

Each entry: what the trace showed, what it means, what to change, and roughly
how much it is worth. Sizes are order-of-magnitude on a mid-range device —
measure your own.

## Startup

### Hundreds of class-load slices, JIT slices, main thread mostly Running
**Cause:** no Baseline Profile, or a stale one. Every class is verified and
interpreted on the critical path.
**Fix:** generate a Baseline Profile covering the launch path plus the first
real interaction. Prove it with `CompilationMode.None()` vs
`Partial(BaselineProfileMode.Require)`.
**Worth:** commonly 20–30% off cold start. Google's own Now in Android sample
goes 324.8 ms → 229.0 ms.
**Trap:** a profile that only covers `startActivityAndWait()` misses the first
scroll, which is where users actually feel it.

### `Application.onCreate` is a wall of third-party init
**Cause:** every SDK initialises eagerly, in order, on the main thread.
**Fix:** move to `androidx.startup` with explicit dependencies, and make
anything not needed for the first frame lazy. Analytics, crash reporting
(usually keep this eager), image loaders, feature flags, DI graph warm-up — most
can be deferred to after first frame or to a background dispatcher.
**Worth:** often the single biggest chunk, 100–400 ms.
**How to see it:** `13_startup_top_slices` — but only if the SDKs emit trace
sections. If they do not, wrap each initialiser in `androidx.tracing.trace("init:Foo")`
and recapture. That measurement alone is usually worth the session.

### Startup dominated by `inflate`
**Cause:** deep view hierarchy, or inflating screens that are not visible yet.
**Fix:** flatten, drop unused `include`s, defer offscreen inflation with
`ViewStub`, avoid inflating the whole tab set on launch.
**Worth:** 30–150 ms.

### Startup dominated by `binder`, main thread Sleeping
**Cause:** the first screen queries the system or another process synchronously.
**Fix:** see the binder section below.
**Worth:** proportional to the blocking total in `15_startup_binder`.

### TTID looks fine, users still complain
**Cause:** TTID measures the first frame, which may be a skeleton. There is no
TTFD because nothing calls `reportFullyDrawn()`.
**Fix:** call `Activity.reportFullyDrawn()` — or Compose's `ReportDrawn` /
`ReportDrawnWhen { }` — the moment real content is on screen. Then measure
again and expect the honest number to be worse.
**Worth:** nothing directly. It makes the problem visible, which is the
prerequisite for fixing it.

### `launch_delay` dominates
**Cause:** time before your code ran: process fork, system_server queueing, cold
page cache.
**Fix:** mostly outside your control. Reducing DEX size and class count helps a
little. Do not spend a sprint here.

## Jank

### App Deadline Missed, cost on the UI thread (`doFrame` long)
**Cause:** measure/layout, recomposition, main-thread IO, JSON parsing, or work
posted onto the main Handler.
**Fix:**
- Views: flatten, kill double-measure from nested weights, avoid `requestLayout`
  in a scroll listener.
- Compose: hoist state so recomposition scopes stay small, use `derivedStateOf`
  for values read during scroll, pass lambdas not values where possible, and
  give `LazyColumn` items stable `key`s. Turn on composition tracing before
  guessing.
- Anything blocking: get it off the main thread entirely.
**Worth:** removes the jank if the frame was only marginally over.

### App Deadline Missed, cost on the RenderThread (`DrawFrame` long)
**Cause:** overdraw, oversized bitmaps, hardware-layer thrash, or shader compile.
**Fix:** decode bitmaps to display size, remove stacked opaque backgrounds, stop
toggling `LAYER_TYPE_HARDWARE` per frame, avoid full-screen blurs and large
`RenderEffect`s during animation.
**Worth:** 5–40 ms per frame.

### `CreateGraphicsPipeline` / `CompileAfterCacheMiss` during the first animation
**Cause:** the GPU driver compiled a shader mid-frame. Once per install per
program — invisible to a developer whose cache is warm, hit by every user.
**Fix:** ship a Baseline Profile (ProfileInstaller also primes the shader
cache), and/or run the animation once offscreen behind the splash.
**Worth:** removes a 50–200 ms first-run stall. This is the highest
"users notice it, developers never see it" ratio on the list.

### Buffer Stuffing
**Cause:** producing frames faster than they drain; the buffer queue is
permanently full, so latency stays high even though no single frame is slow.
**Fix:** usually an animation or invalidation loop driving redraws that are not
needed. Find what is calling `invalidate()` every frame.

### SurfaceFlinger-attributed jank
**Cause:** the compositor or display pipeline missed, not you.
**Fix:** first confirm the device was not thermally throttled and no other
process was hammering it. Re-measure on a cool device. If it persists across
devices with your app in the foreground, look at what you hand the compositor:
too many layers, `SurfaceView` churn, huge dirty regions.
**Worth:** frequently zero — the value is in *not* spending a week on it.

## Blocking

### Synchronous binder on the main thread
**Cause:** the API does not look remote. `PackageManager`,
`ConnectivityManager`, `TelephonyManager`, `DisplayManager`, `AccountManager`,
`Settings.Secure`, `ContentResolver` against another app — all binder.
**Fix:** cache the result for the process lifetime, batch calls, or move them
to a background dispatcher and render a placeholder. Watch for calls inside
list binding — 40 items × 2 ms is 80 ms of frozen UI.
**Worth:** whatever `30_binder_main_thread` reports as `total_ms`.

### Slow binder calls where the reason is `S` / `R` / `R+`, not `Running`
**Cause:** the server was not busy; the client was descheduled or queueing for a
binder thread.
**Fix:** reduce the *number* of calls. Making the server faster will not help.

### Monitor contention with the main thread blocked
**Cause:** a background thread holds a lock the main thread wants. Nothing shows
up in a CPU profiler because nothing is running.
**Fix:** shrink the critical section, copy-on-read instead of locking, or put
the shared state behind a single-threaded dispatcher / actor. Look hard at
`SharedPreferences.commit()` (synchronous, disk, holds a lock) — use `apply()`
or move to DataStore.
**Worth:** the full contended duration, and it is usually a spike rather than
an average, so it shows up as an occasional freeze rather than general slowness.

### Main thread in `D` (uninterruptible sleep) with a `blocked_function`
**Cause:** disk IO on the main thread. `blocked_function` names the kernel path.
**Fix:** move file, database and SharedPreferences reads off the main thread.
StrictMode with `detectDiskReads()` will find them in development.

### Main thread in `R` (runnable) for long stretches
**Cause:** the scheduler will not give you a core. Too many app threads
competing, a busy device, or your work landed on a little core.
**Fix:** cut thread-pool sizes, stop doing parallel work during startup, and
check whether the device was thermally throttled. This is rarely fixed by
making your code faster.

## Method

### Nothing in the repo matches the hot slice names
**Cause:** the app has no custom tracing. You are looking only at framework
spans.
**Fix:** add `androidx.tracing` sections around suspect paths —
`trace("Feed:bind") { … }` — and recapture. Low cost, and it is the difference
between "the UI thread is busy" and "`FeedAdapter.onBindViewHolder` is busy".
This is usually the correct *first* fix in a brownfield app.

### The fix looks like it worked
Re-measure with the same device, same build type, same run count, median of
≥10. Then check the *other* metrics did not regress — a startup win that costs
you 5 ms per frame in scroll is not a win.
