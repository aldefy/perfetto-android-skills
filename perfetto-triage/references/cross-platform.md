# Reading Android traces for Flutter, React Native, Unity and WebView apps

The framework you write in does not change the pipeline underneath. Every
Android app, whatever the toolkit, ends up doing the same four things: fork a
process, run `Application.onCreate`, hand buffers to SurfaceFlinger, and hit a
vsync deadline. Perfetto sees all of that regardless of what produced it.

What changes is **which slice names you see**, and therefore how you map a
finding back to source.

## What is identical everywhere

- Cold start structure: process fork → `bindApplication` → `activityStart` →
  first frame. TTID and TTFD are computed the same way.
- Frame deadlines and FrameTimeline jank attribution — SurfaceFlinger does not
  know or care what drew the frame.
- Binder / IPC: any platform channel, native module or plugin that touches a
  system service makes the same blocking binder call.
- `thread_state`: running vs runnable vs sleeping vs uninterruptible sleep.
- Baseline Profiles: they optimise the **Java/Kotlin** side. Every one of these
  frameworks has a Java/Kotlin shell that runs during startup, so profiles still
  help — just less than in a fully native app.

So questions 1–3 of the checklist in `reading-a-trace.md` work unchanged. Only
step 4, mapping slices to code, is framework-specific.

## Flutter

**Threads to look at:** the platform (main) thread, plus Flutter's `1.ui`,
`1.raster` (formerly `1.gpu`) and `1.io` threads. In the trace they appear as
threads on the app process.

- Jank on `1.ui` → Dart build/layout work. The Android UI thread may look idle.
- Jank on `1.raster` → rasterisation, and **shader compilation jank** is the
  classic Flutter first-run stall. It shows in the same
  `CreateGraphicsPipeline` / `CompileAfterCacheMiss` slices.
- Long `Choreographer#doFrame` on the platform thread with `1.ui` idle → a
  platform channel call is blocking, not Dart.

**Mapping to code:** the Android trace names Dart frames only coarsely. Capture
a Flutter DevTools timeline in parallel for Dart-level attribution, and use the
Perfetto trace to answer "is it Dart, the raster thread, the platform channel,
or the system?" — which is the question DevTools cannot answer.

**Most common finding:** synchronous platform-channel calls on startup, and
shader jank on the first animation.

## React Native

**Threads to look at:** the Android main/UI thread, `mqt_js` (JavaScript), and
on the old architecture `mqt_native_modules`. On the new architecture look for
the Fabric/JSI work landing on the UI thread.

- Startup dominated by JS bundle load → look for the bundle read and evaluate
  slices; consider Hermes bytecode precompilation and inline requires.
- Jank with `mqt_js` busy → your JS is the bottleneck; the UI thread is waiting
  on a bridge response.
- Jank with the UI thread busy and `mqt_js` idle → view flattening, shadow-tree
  layout, or a native module.
- Bridge traffic on the old architecture shows as batched messages; a chatty
  list renderer produces a very characteristic sawtooth.

**Mapping to code:** the native side is nameable; the JS side needs the Hermes
sampling profiler. Same division of labour as Flutter — Perfetto tells you
*which side*, the JS profiler tells you *which function*.

**Most common finding:** startup is bundle evaluation, and list scroll jank is
bridge round-trips per item.

## Unity / games

The Unity player thread and render thread appear as ordinary threads. Frame
pacing is the usual issue — check `Buffer Stuffing` and whether the app is
producing frames the display cannot drain. Unity's own profiler is better for
in-engine attribution; Perfetto is for the boundary: thermal throttling, CPU
frequency, and how the frames land against vsync.

## WebView / hybrid

WebView runs its own renderer, often in a separate process. Look for the
WebView process in `02_processes`. Long `doFrame` on the host app with the
WebView process busy means the web content is the cost; the fix is in the page,
not the app shell. Chrome tracing categories give you the web-side detail.

## Practical advice for a cross-platform architect

1. **Run the checklist first, framework second.** Running-vs-waiting and
   app-vs-SurfaceFlinger attribution do not care about your toolkit, and they
   eliminate most wrong hypotheses before framework knowledge matters.
2. **Custom trace sections are the bridge.** Every one of these frameworks can
   emit Android trace sections — Flutter's `Timeline.startSync`, React Native's
   `Systrace`, or a thin native wrapper over `androidx.tracing`. Instrumenting
   five suspicious paths converts an unreadable trace into an actionable one.
3. **Do not skip Baseline Profiles because "it's not a native app."** The
   Java/Kotlin shell still runs, and on Flutter and React Native it is a
   meaningful share of cold start.
4. **The measurement discipline is the transferable part.** Median of ten,
   release build, physical device, one change at a time. That is the same
   whether the UI is Compose, Dart, or JSX — and it is the part teams actually
   get wrong.
