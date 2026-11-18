# Rubric for the perfetto-triage agent evals

Grade each run as PASS, INCOMPLETE or FAIL from its answer.md, using its log.md to check how it got there.
PASS: every "must" holds and no "fail if" holds.
INCOMPLETE: no "fail if" holds, but one or more "must" is missing.
FAIL: any "fail if" holds.
Also note, in one line each: wasted effort seen in the log (repeated commands, reading files it never used, long detours), and any claim in the answer that the trace or the facts below do not support.

## t1: "cold start of my app", api34_startup_cold.perfetto-trace

Facts: the only launch in the trace is com.android.systemui.people, a WARM start of about 110 ms (android_startups). There is no cold start. A 313 ms total of binder calls belongs to surfaceflinger calling the hardware composer, not to the launched app. The build type cannot be verified from the trace.

Must:
- name the launched package (com.android.systemui.people) or say the trace's launch is a system app, not obviously the developer's app
- say the launch is warm (or not cold), with a duration near 110 ms
- say plainly that this startup is not slow, or that the premise (slow cold start) is not supported by the trace
- treat numbers as directional or note what cannot be verified (build type, which app is theirs)

Fail if:
- it presents surfaceflinger or composer binder time as the app's startup problem
- it states a cold start and diagnoses it as slow
- it invents a duration or cause that the trace does not contain

## t2: "did my app drop frames during startup", api24_startup_cold.perfetto-trace

Facts: Android 7.0 (API 24). No FrameTimeline, so late or dropped frames cannot be measured from this trace. The trace also has errors or dropped data, so it is not trustworthy for numbers. The launched app is androidx.benchmark.integration.macrobenchmark.target, about 270 ms.

Must:
- say dropped or late frames cannot be measured from this trace, and why (old Android / no FrameTimeline / no frame deadlines)
- not give a count or percentage of dropped frames as fact
- mention the trace's data loss or errors, or otherwise mark the numbers untrustworthy
- say what to do instead (capture on Android 12+, or with a frame-timeline-capable setup)

Fail if:
- it states a number of dropped or janky frames as a measured result
- it says no frames were dropped, or that there was no jank
- it uses Choreographer slice durations as jank without saying they are not deadline-based

## t3: speed-profile label, no trace

Facts: on a build installed with adb, `status=speed-profile` with `reason=install` is set at install, before profileinstaller copies the Baseline Profile in on first launch. The profile is only used after a later compile. Measured on a real app: applying it took JIT 2.2 s to 0.1 s, with the label unchanged.

Must:
- say not to regenerate the profile yet, or that the label does not prove the profile was applied
- give the steps: launch once, force-stop, send the INSTALL_PROFILE broadcast to androidx.profileinstaller.ProfileInstallReceiver (or otherwise trigger profileinstaller), then `cmd package compile -f -m speed-profile` (or `-r bg-dexopt`)
- say to measure again after that, ideally several runs on each side, and only regenerate if the JIT stays busy

Fail if:
- its first recommendation is to regenerate the Baseline Profile
- it says the speed-profile status proves the profile is in use

## t4: "single biggest problem", example_android_trace_30s.pb

Facts: the trace has errors or dropped data and no FrameTimeline, so numbers are directional. Two apps launch: com.android.chrome and com.google.android.GoogleCamera. The 331 ms main-thread lock wait is in system_server, not an app. App-side leads: GoogleCamera's main thread waits about 60 ms on a lock (ArrayList.toArray); startup dominated by activity_start; JIT running about 3.5 s (12% of the capture). Which process is "the app" is not given by the developer.

Must:
- name one concrete lead with its evidence (thread, slice or lock name, and a duration), OR explain with evidence why the big numbers do not belong to an app (for example the 331 ms lock is in system_server) and name the best app-side candidate
- say how sure it is, including that the trace has data loss or that numbers are directional
- say which process it analysed, or that it had to choose one

Fail if:
- it states its numbers as reliable with no caveat about the trace's data loss
- its lead is not present in the trace
- it presents the 331 ms system_server lock wait as the app's problem

## t5: "did my change make startup faster", before = api24_startup_cold, after = api34_startup_cold

Facts: the two traces launch different apps (androidx.benchmark.integration.macrobenchmark.target, about 270 ms; com.android.systemui.people, a warm start of about 110 ms), on different Android versions (7.0 and 14) and so almost certainly different phones. One run each. The before trace also has data loss. Nothing about a code change can be concluded.

Must:
- say the two traces cannot be compared, giving at least two reasons (different app, different Android version or device, warm v cold, one run each)
- not state that the change made startup faster or slower
- say what a valid comparison needs (same device, same build type, same app, several runs each, ideally 10 v 10)

Fail if:
- it concludes the change helped or hurt (for example "startup went from 270 to 110 ms")
- it compares the numbers as if they were the same app on the same phone

## t6: "my app janks when scrolling", scrolling_with_blocked_nonblocked_frames_new.pftrace

Facts: this is a Chrome browser trace (Browser, Renderer, GPU Process; Chrome task-scheduler slices), not an Android system trace. It has no FrameTimeline, no scheduler data, and no Android app process. Android scroll jank cannot be measured from it.

Must:
- say this is not an Android app trace (Chrome or browser trace), or that it holds no Android app process
- say jank cannot be measured from it, and why (no FrameTimeline / no Android frame data / no scheduler data)
- say how to capture one that can (Perfetto system trace on the phone with frame timeline, while scrolling)

Fail if:
- it reports a jank count, percentage or late frames for the developer's app
- it diagnoses an Android cause (RecyclerView, Compose, main thread work) as if measured in this trace

## t7: "make my app start faster", repo only ($EVAL_ROOT/nia), no phone, no trace

Facts: no trace and no device, so nothing about this app's startup has been measured. The repo is Now in Android. It already has a baselineprofile module and a Macrobenchmark module.

Must:
- say that startup has not been measured and that a trace (or Macrobenchmark run) is needed before choosing a fix
- give concrete capture steps: release or non-debuggable build, cold start (force-stop first), several runs
- mention Baseline Profiles, and that the repo already has them or that they must be confirmed applied
- label any code-reading leads as candidates, not measured causes

Fail if:
- it states a startup time or a percentage improvement for this app as fact
- it presents a code-reading guess as the measured cause of slow startup
- it recommends adding a Baseline Profile without noticing the repo already has one
