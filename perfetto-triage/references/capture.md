# Capturing a usable trace

Most failed trace analyses are failed captures. This page is the checklist.

## The one thing people get wrong

```bash
adb shell perfetto -o /data/misc/perfetto-traces/t.pftrace -t 10s -b 32mb -a '*' \
  am wm gfx view sched
```

This "lightweight mode" **cannot enable `android.surfaceflinger.frametimeline`**.
It only supports atrace categories and ftrace events. So you get slices and
scheduling, and zero jank attribution — no `actual_frame_timeline_slice`, no
`jank_type`, no app-vs-SurfaceFlinger answer.

For jank work you need a full `TraceConfig`. Use `assets/startup_jank.pbtxt`:

```bash
cat assets/startup_jank.pbtxt | adb shell perfetto -c - --txt \
  -o /data/misc/perfetto-traces/t.pftrace
adb pull /data/misc/perfetto-traces/t.pftrace
```

FrameTimeline requires **Android 12 (API 31)+**. On older devices, startup and
binder analysis still work; jank attribution does not.

## Device hygiene

| Requirement | Why |
|---|---|
| Physical device, never an emulator | emulator timings are host timings |
| Screen on and unlocked | a dozing device produces meaningless gaps |
| Not thermally throttled | let it cool between runs; throttling silently halves clocks |
| Not on low battery | Android downclocks aggressively |
| Airplane mode, if the flow allows | removes network variance |
| Release-like build | a `debuggable` build is 2–5× slower, disproportionately in ART |
| Same device for before and after | numbers are not portable across devices |

Check what you actually have:

```bash
adb shell getprop ro.build.version.sdk        # API level
adb shell getprop ro.product.model
adb shell dumpsys thermalservice | head -20   # throttling status
adb shell dumpsys package <pkg> | grep -i debuggable
```

## What to record

The config in `assets/startup_jank.pbtxt` covers app triage. The load-bearing
parts, and what breaks if you drop them:

| Setting | Drop it and you lose |
|---|---|
| `atrace_categories: "am"`, `"wm"` | launch lifecycle; startup analysis stops working |
| `atrace_categories: "gfx"`, `"view"` | `doFrame`, `DrawFrame`, measure/layout |
| `atrace_categories: "dalvik"` | class loading, JIT, GC — the Baseline Profile signal |
| `atrace_categories: "aidl"` | AIDL method names; binder calls become anonymous |
| `atrace_categories: "binder_driver"`, `"binder_lock"` | IPC visibility |
| `atrace_apps: "<pkg>"` | your own `Trace.beginSection` spans |
| `ftrace: sched/sched_switch`, `sched_waking` | running vs waiting — the most important question |
| `ftrace: sched/sched_blocked_reason` | `blocked_function`; you can see the thread is in `D` but not why |
| `android.surfaceflinger.frametimeline` | all jank attribution |
| `linux.process_stats` | process names; the trace becomes a wall of PIDs |

Narrow `atrace_apps` to your package plus `system_server` when the trace is too
big or the buffer is dropping.

## Buffer sizing

Data loss makes every number an undercount, and `00_health` will flag it. Rules
of thumb:

- 32 MB is enough for ~10 s of a single app.
- 128 MB for a busy device or 20 s+.
- If `stats` still reports loss, shorten the capture rather than growing the
  buffer further.

## Capture patterns

**Cold start.** Force-stop, start tracing, launch, wait for first frame, stop.
`scripts/capture.sh <pkg>` does exactly this. Do it 3+ times; the first run
after install is never representative.

**Jank on a specific interaction.** Start tracing, perform the interaction the
same way each time, stop. `scripts/capture.sh <pkg> --interactive 20`. Hand
gestures vary — for anything you plan to compare before/after, drive it from
Macrobenchmark instead so the gesture is identical.

**ANR.** Traces need to be running when it happens, so use a long ring-buffer
capture and reproduce.

## Alternative recorders

**`record_android_trace`** — Google's script; handy for ad-hoc work, opens the
UI automatically.

```bash
curl -O https://raw.githubusercontent.com/google/perfetto/main/tools/record_android_trace
python3 record_android_trace -o trace.perfetto-trace -t 10s -b 32mb -a '*' \
  sched freq gfx view am wm binder_driver
```

Note `-a*` with no space, or the shell expands the glob. Same limitation as
lightweight mode unless you pass `-c <config>`.

**Android Studio Profiler** — fine for exploring, awkward for reproducible
before/after. It records a Perfetto trace you can export and feed to
`triage.py`.

**Macrobenchmark** — the best option when you want comparable numbers, because
the interaction is scripted and it emits one trace per iteration. See
`references/benchmark-setup.md`.

**On-device Developer Options → System Tracing** — good for catching something
you cannot reproduce on demand.

## Verifying the capture before you analyse it

```bash
python3 scripts/triage.py trace.pftrace --only 00,01,02
```

That runs only the health, coverage and process queries. If `frametimeline_rows`
is 0 and you came for jank, recapture now rather than after an hour of
analysis.
