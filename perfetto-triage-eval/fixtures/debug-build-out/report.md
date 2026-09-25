# Perfetto triage — `com.example.stickerexplode-startup-163743.pftrace`

Package filter: `com.example.stickerexplode`  ·  trace_processor v57.2

## Findings, worst first

### 1. 🔴 com.example.stickerexplode: 31/32 frames janky (96.9%)
*jank · high*

Worst frame ran 156 ms over its deadline. App-attributed jank: 12, SurfaceFlinger-attributed: 5.

**What to do:** Blame lands on the app. Look at the UI-thread vs RenderThread split and the hot slices during janky frames.

### 2. 🟠 2991 classes loaded during startup, 31 ms on the critical path
*startup · medium*

startup_id=1 (com.example.stickerexplode). Note this counts explicit class-load slices only; JIT and verification cost sits in the running time above.

**What to do:** This is the clearest Baseline Profile signal there is. Generate one with BaselineProfileRule, then prove the win by benchmarking CompilationMode.None() against CompilationMode.Partial(BaselineProfileMode.Require).

### 3. 🟠 com.example.stickerexplode: cost is on the UI thread
*jank · medium*

doFrame 547 ms vs DrawFrame 404 ms (vsync delay 308 ms).

**What to do:** Look for measure/layout passes, recomposition, main-thread IO or deserialisation, and work posted onto the main Handler.

### 4. 🟠 Measure/layout/traversal cost during janky frames (1316 ms)
*jank · medium*

Slices matched: traversal

**What to do:** Flatten the hierarchy, avoid nested weights / double measure, and hoist state so recomposition scopes stay small.

### 5. 🟠 Main-thread binder: AIDL::cpp::android.gui.SensorEventConnection::#2::server costs 53 ms
*binder · medium*

2 synchronous calls from com.example.stickerexplode to system_server, worst single call 42.5 ms.

**What to do:** Synchronous binder on the main thread is a frozen UI. Cache the result, batch the calls, or move them to a background dispatcher. System APIs that look local (PackageManager, ConnectivityManager, DisplayManager, SharedPreferences-backed system settings) are binder calls underneath.

### 6. 🔵 App never calls reportFullyDrawn()
*startup · info*

com.example.stickerexplode has no time-to-full-display. TTID only measures the first frame, which is often a skeleton or spinner.

**What to do:** Call Activity.reportFullyDrawn() (or ReportDrawn* in Compose) once real content is on screen, so TTFD becomes measurable and Play Console reports it.

### 7. 🔵 Startup time is dominated by 'choreographer_do_frame'
*startup · info*

choreographer_do_frame 93 ms, binder 61 ms, Running 33 ms

**What to do:** First-frame rendering is expensive. Usually composition/measure/layout cost or shader compilation.

### 8. 🔵 Startup is COMPUTE-bound — main thread runs 66% of the time
*startup · info*

Running 172 ms vs waiting 90 ms (Running 172 ms, Sleeping 75 ms, Uninterruptible Sleep (IO) 9 ms, Runnable (Preempted) 3 ms, Runnable 3 ms, Uninterruptible Sleep (non-IO) 0 ms, Uninterruptible Sleep 0 ms).

**What to do:** Your code, class loading, or JIT is the cost. Baseline Profiles and deferring init work off the critical path are the levers.

### 9. 🔵 Slow binder calls are queueing, not computing
*binder · info*

Dominant client-side reason is 'S' (68 ms).

**What to do:** The server side is not the bottleneck; the client is descheduled or waiting for a free binder thread. Reducing call *count* helps more than making the server faster.

---

## Evidence

### Is this trace trustworthy?

#### `00_health`

> Is this trace even usable? Any non-zero row here means the data source was missing or the buffer dropped data, and every number downstream is a lie.

_no rows_


#### `01_coverage`

> Which data sources actually made it into the trace. 0 in a column means you cannot answer that class of question with this trace.

| slices | sched_rows | frametimeline_rows | blocked_reason_rows | binder_slices | processes | trace_span_ms |
|---|---|---|---|---|---|---|
| 205653 | 55088 | 887 | 4109 | 3197 | 822 | 11,905.92 |


#### `02_processes`

> Who is in this trace and who is doing the work. Find your package here.

| pid | process_name | slices | slice_ms |
|---|---|---|---|
| 24685 | com.example.stickerexplode | 107521 | 7,923.10 |
| 548 | /system/bin/surfaceflinger | 50611 | 9,439.90 |
| 550 | /vendor/bin/hw/android.hardware.composer.hwc3-service.pixel | 10884 | 6,106.94 |
| 1544 | system_server | 8050 | 9,085.78 |
| 2325 | com.android.systemui | 6934 | 1,486.01 |
| 23567 | in.equal.ai.assistant.staging | 3308 | 2,432.72 |
| 11335 | com.google.android.apps.nexuslauncher | 2169 | 404.36 |
| 665 | /vendor/bin/hw/android.hardware.power-service.pixel-libperfmgr | 1105 | 4,626.66 |
| 24667 | commands.monkey | 771 | 368.35 |
| 19175 | com.Slack | 575 | 604.15 |
| 2202 | com.google.android.apps.photos | 470 | 364.12 |
| 7722 | com.google.android.gms | 429 | 698.76 |
| 549 | /vendor/bin/hw/android.hardware.graphics.allocator-V2-service | 360 | 81.83 |
| 983 | /vendor/bin/hw/android.hardware.sensors-service.multihal | 233 | 590.71 |
| 2463 | com.google.pixel.camera.services | 194 | 162.96 |
| 2559 | com.android.pixeldisplayservice | 191 | 76.64 |
| 2612 | com.android.phone | 186 | 161.75 |
| 4154 | com.google.android.as | 151 | 88.06 |
| 4133 | com.google.android.providers.media.module | 140 | 198.39 |
| 2462 | com.google.android.pixelsystemservice | 85 | 139.49 |

_(5 more rows in csv/)_


### Startup: where does cold start time go?

#### `10_startup_summary`

> Every app launch in the trace, with time-to-initial-display and time-to-full-display. ttfd_ms is NULL when the app never calls reportFullyDrawn().

| startup_id | package | startup_type | startup_ms | ttid_ms | ttfd_ms |
|---|---|---|---|---|---|
| 1 | com.example.stickerexplode | cold | 290.20 | 288.26 | – |


#### `11_startup_breakdown`

> Where cold-start time actually goes, bucketed by cause. reason values seen in the wild: launch_delay, binder, activity_start, inflate, choreographer_do_frame.

| startup_id | package | reason | ms | n |
|---|---|---|---|---|
| 1 | com.example.stickerexplode | choreographer_do_frame | 93.41 | 134 |
| 1 | com.example.stickerexplode | binder | 61.43 | 280 |
| 1 | com.example.stickerexplode | Running | 32.87 | 39 |
| 1 | com.example.stickerexplode | launch_delay | 27.70 | 1 |
| 1 | com.example.stickerexplode | activity_start | 17.53 | 34 |
| 1 | com.example.stickerexplode | open_dex_files_from_oat | 17.46 | 174 |
| 1 | com.example.stickerexplode | bind_application | 16.34 | 50 |
| 1 | com.example.stickerexplode | activity_resume | 9.22 | 31 |
| 1 | com.example.stickerexplode | io | 9.11 | 2 |
| 1 | com.example.stickerexplode | resources_manager_get_resources | 1.26 | 2 |
| 1 | com.example.stickerexplode | R | 0.94 | 13 |
| 1 | com.example.stickerexplode | art_lock_contention | 0.82 | 148 |
| 1 | com.example.stickerexplode | inflate | 0.77 | 1 |
| 1 | com.example.stickerexplode | client_transaction_executed | 0.63 | 5 |
| 1 | com.example.stickerexplode | S | 0.57 | 3 |
| 1 | com.example.stickerexplode | D | 0.05 | 1 |
| 1 | com.example.stickerexplode | R+ | 0.04 | 3 |
| 1 | com.example.stickerexplode | monitor_contention | 0.02 | 6 |
| 1 | com.example.stickerexplode | verify_class | 0.00 | 1 |


#### `12_startup_main_thread_state`

> The first question a senior engineer asks: during startup, was the main thread RUNNING or WAITING? Running -> your code is slow. Sleeping/blocked -> you are waiting on IPC, IO, or a lock, and optimising your code is pointless.

| startup_id | main_thread_state | ms |
|---|---|---|
| 1 | Running | 172.32 |
| 1 | Sleeping | 74.60 |
| 1 | Uninterruptible Sleep (IO) | 9.11 |
| 1 | Runnable (Preempted) | 3.37 |
| 1 | Runnable | 3.04 |
| 1 | Uninterruptible Sleep (non-IO) | 0.05 |
| 1 | Uninterruptible Sleep | 0.01 |


#### `13_startup_top_slices`

> The named work on the main thread during startup, ranked. This is the list you map back to functions in the codebase.

| startup_id | slice_name | n | total_ms | max_ms |
|---|---|---|---|---|
| 1 | Choreographer#doFrame 26045026 | 1 | 138.58 | 138.58 |
| 1 | Choreographer#doFrame - resynced to 26045031 in 23.2ms | 1 | 138.26 | 138.26 |
| 1 | traversal | 1 | 138.13 | 138.13 |
| 1 | binder transaction | 71 | 61.43 | 42.50 |
| 1 | bindApplication | 1 | 48.60 | 48.60 |
| 1 | Compose:onRemembered | 1 | 46.96 | 46.96 |
| 1 | clientTransactionExecuted | 2 | 36.04 | 35.98 |
| 1 | Compose:recompose | 2 | 30.22 | 29.79 |
| 1 | activityStart | 1 | 20.32 | 20.32 |
| 1 | /data/app/~~lRecWB07Rbipt3cR5vfGfw==/com.example.stickerexplode-_B-oEoFfKVfEy-cmm0J1fw==/base.apk | 1 | 19.41 | 19.41 |
| 1 | OpenDexFilesFromOat(/data/app/~~lRecWB07Rbipt3cR5vfGfw==/com.example.stickerexplode-_B-oEoFfKVfEy-cmm0J1fw==/base.apk) | 1 | 17.47 | 17.47 |
| 1 | location=/data/app/~~lRecWB07Rbipt3cR5vfGfw==/com.example.stickerexplode-_B-oEoFfKVfEy-cmm0J1fw==/oat/arm64/base.odex status=up-to-date filter=verify reason=boot-after-ota | 1 | 16.66 | 16.66 |
| 1 | AppImage:Loading | 1 | 16.62 | 16.62 |
| 1 | activityResume | 1 | 14.93 | 14.93 |
| 1 | madvising /data/app/~~lRecWB07Rbipt3cR5vfGfw==/com.example.stickerexplode-_B-oEoFfKVfEy-cmm0J1fw==/base.apk size=44789760 chunks=342 | 1 | 13.74 | 13.74 |
| 1 | performCreate:com.example.stickerexplode.MainActivity | 1 | 12.42 | 12.42 |
| 1 | Mutator threads suspended for EnableDebugFeatures | 1 | 11.23 | 11.23 |
| 1 | draw-VRI[MainActivity] | 1 | 9.23 | 9.23 |
| 1 | ActivityThreadMain | 1 | 8.81 | 8.81 |
| 1 | measure | 2 | 8.30 | 8.10 |

_(10 more rows in csv/)_


#### `14_startup_class_loading`

> Class loading + verification during startup. This is the number a Baseline Profile moves. High count with high ms == the profile is missing or stale.

| startup_id | package | classes_loaded | ms |
|---|---|---|---|
| 1 | com.example.stickerexplode | 2991 | 30.56 |


#### `15_startup_binder`

> Synchronous binder calls made from the main thread during startup, over 1ms.

| startup_id | thread_name | server_process | n | total_ms | max_ms |
|---|---|---|---|---|---|
| 1 | .stickerexplode | system_server | 4 | 49.16 | 42.50 |


### Jank: which frames missed, and whose fault was it?

#### `20_jank_summary`

> Frame health per process, and the app-vs-SurfaceFlinger split. app_jank  -> you missed your deadline. Fix your code. sf_jank   -> the compositor or display pipeline missed. Usually not your bug.

| process_name | frames | janky | janky_pct | big_jank | huge_jank | worst_overrun_ms | app_jank | sf_jank |
|---|---|---|---|---|---|---|---|---|
| com.example.stickerexplode | 32 | 31 | 96.88 | 3 | 0 | 156.49 | 12 | 5 |
| in.equal.ai.assistant.staging | 6 | 3 | 50.00 | 1 | 0 | 34.77 | 1 | 0 |
| com.google.android.apps.nexuslauncher | 29 | 0 | 0.00 | 0 | 0 | -11.61 | 0 | 0 |
| com.android.systemui | 63 | 0 | 0.00 | 0 | 0 | -7.29 | 0 | 0 |


#### `21_worst_frames`

> The individual frames that blew the deadline, worst first. overrun_ms is how late the frame was. cpu_time is UI thread + RenderThread.

| process_name | frame_id | ts | overrun_ms | cpu_ms | ui_ms | jank_type | present_type |
|---|---|---|---|---|---|---|---|
| com.example.stickerexplode | 26045026 | 274447407326696 | 156.49 | 184.39 | 138.58 | App Deadline Missed, App Resynced Jitter | Late Present |
| com.example.stickerexplode | 26045279 | 274447565294999 | 113.53 | 114.85 | 80.79 | App Deadline Missed, App Resynced Jitter | Late Present |
| com.example.stickerexplode | 26045316 | 274447660904741 | 110.24 | 107.71 | 9.74 | App Deadline Missed, App Resynced Jitter | Late Present |
| com.example.stickerexplode | 26045703 | 274447707924800 | 53.83 | 27.48 | 15.54 | App Deadline Missed | Late Present |
| com.example.stickerexplode | 26045900 | 274447785750199 | 51.96 | 41.59 | 16.69 | Buffer Stuffing | On-time Present |
| com.example.stickerexplode | 26045878 | 274447766189367 | 48.88 | 38.37 | 18.80 | App Deadline Missed | Late Present |
| com.example.stickerexplode | 26045812 | 274447739675249 | 48.49 | 26.01 | 7.52 | App Deadline Missed | Late Present |
| com.example.stickerexplode | 26045674 | 274447699892534 | 47.22 | 25.71 | 7.37 | App Deadline Missed | Late Present |
| com.example.stickerexplode | 26045937 | 274447803256709 | 47.05 | 33.85 | 10.75 | SurfaceFlinger Scheduling | Early Present |
| com.example.stickerexplode | 26045856 | 274447756485876 | 45.24 | 29.41 | 8.77 | App Deadline Missed | Late Present |
| com.example.stickerexplode | 26045981 | 274447814959997 | 43.02 | 37.12 | 7.92 | Buffer Stuffing | On-time Present |
| com.example.stickerexplode | 26046182 | 274447921021846 | 41.96 | 35.13 | 14.15 | Buffer Stuffing | On-time Present |
| com.example.stickerexplode | 26045783 | 274447731615393 | 41.71 | 16.46 | 1.89 | SurfaceFlinger Scheduling | Early Present |
| com.example.stickerexplode | 26046122 | 274447887254676 | 41.65 | 30.74 | 10.53 | Buffer Stuffing | On-time Present |
| com.example.stickerexplode | 26045645 | 274447690246415 | 40.91 | 18.21 | 8.27 | App Deadline Missed | Late Present |
| com.example.stickerexplode | 26046069 | 274447854491532 | 40.02 | 30.64 | 10.45 | Buffer Stuffing | On-time Present |
| com.example.stickerexplode | 26046167 | 274447910580440 | 39.24 | 31.65 | 9.80 | Buffer Stuffing | On-time Present |
| com.example.stickerexplode | 26046241 | 274447947558671 | 38.86 | 40.07 | 14.33 | Buffer Stuffing | On-time Present |
| com.example.stickerexplode | 26046107 | 274447876494014 | 38.84 | 27.60 | 10.10 | Buffer Stuffing | On-time Present |
| com.example.stickerexplode | 26046018 | 274447823642574 | 38.23 | 31.34 | 19.21 | SurfaceFlinger Scheduling | Early Present |


#### `22_jank_cpu_split`

> For janky frames, is the cost in Choreographer#doFrame (your UI thread work: measure/layout/composition/binding) or in RenderThread DrawFrame (draw commands, overdraw, shader compile)? This decides where you look next.

| process_name | janky_frames | vsync_delay_ms | ui_thread_ms | render_thread_ms |
|---|---|---|---|---|
| com.example.stickerexplode | 31 | 308.05 | 546.65 | 403.61 |
| in.equal.ai.assistant.staging | 3 | 33.43 | 60.90 | 16.95 |


#### `23_jank_hot_slices`

> What was actually executing on the UI thread and RenderThread during the frames that missed. Ranked. These slice names are your suspects.

| name | n | total_ms | max_ms |
|---|---|---|---|
| traversal | 121 | 1,316.19 | 138.13 |
| draw-VRI[MainActivity] | 121 | 1,177.87 | 28.92 |
| Drawing  0.00  0.00 1080.00 2424.00 | 108 | 1,149.78 | 23.83 |
| postAndWait | 121 | 1,109.76 | 17.49 |
| flush commands | 118 | 984.94 | 12.39 |
| Vulkan finish frame | 118 | 984.69 | 12.39 |
| auto skgpu::ganesh::OpsTask::onExecute(GrOpFlushState *)::(anonymous class)::operator()() const | 54874 | 464.54 | 7.44 |
| animation | 126 | 253.54 | 51.54 |
| dequeueBuffer | 103 | 202.68 | 9.36 |
| waitForBufferRelease | 40 | 201.72 | 9.35 |
| Recomposer:animation | 123 | 161.51 | 2.07 |
| auto skgpu::ganesh::OpsTask::onPrepare(GrOpFlushState *)::(anonymous class)::operator()() const | 57377 | 147.60 | 0.99 |
| Choreographer#doFrame - resynced to 26045031 in 23.2ms | 1 | 138.26 | 138.26 |
| FillRectOp | 56957 | 114.36 | 0.26 |
| TextureOp | 57934 | 110.59 | 1.27 |
| Choreographer#doFrame - resynced to 26045902 in 19.9ms | 5 | 81.83 | 16.37 |
| Recomposer:recompose | 121 | 81.67 | 51.36 |
| Choreographer#doFrame - resynced to 26045281 in 23.6ms | 1 | 80.63 | 80.63 |
| DrawFrames 26045981 | 4 | 79.82 | 19.96 |
| DrawFrames 26045856 | 4 | 79.46 | 19.87 |

_(5 more rows in csv/)_


### Blocking: binder, IPC and locks

#### `30_binder_main_thread`

> Synchronous binder calls blocking a main thread, ranked by total cost. Every millisecond here is your app frozen waiting on another process.

| client_process | aidl_name | server_process | n | total_ms | max_ms |
|---|---|---|---|---|---|
| com.example.stickerexplode | AIDL::cpp::android.gui.SensorEventConnection::#2::server | system_server | 2 | 52.69 | 42.50 |
| com.example.stickerexplode | AIDL::java::android.app.IActivityManager::attachApplication::server | system_server | 1 | 4.00 | 4.00 |
| com.example.stickerexplode | unknown | /system/bin/servicemanager | 16 | 2.43 | 0.27 |
| com.example.stickerexplode | AIDL::java::android.content.IContentProvider::#21::server | system_server | 3 | 1.83 | 0.85 |
| com.example.stickerexplode | AIDL::java::android.app.IActivityManager::finishAttachApplication::server | system_server | 1 | 1.42 | 1.42 |
| com.example.stickerexplode | AIDL::java::android.view.IWindowSession::addToDisplayAsUser::server | system_server | 1 | 1.24 | 1.24 |
| com.example.stickerexplode | AIDL::java::android.content.IContentService::registerContentObserver::server | system_server | 1 | 0.94 | 0.94 |
| com.example.stickerexplode | AIDL::java::android.app.IActivityManager::getContentProvider::server | system_server | 1 | 0.54 | 0.54 |
| com.example.stickerexplode | AIDL::java::android.app.IUiModeManager::addCallback::server | system_server | 2 | 0.51 | 0.36 |
| com.example.stickerexplode | AIDL::java::android.view.IGraphicsStats::requestBufferForProcess::server | system_server | 1 | 0.48 | 0.48 |
| com.example.stickerexplode | AIDL::java::android.hardware.display.IDisplayManager::getPreferredWideGamutColorSpaceId::server | system_server | 1 | 0.41 | 0.41 |
| com.example.stickerexplode | AIDL::java::android.app.IActivityManager::publishContentProviders::server | system_server | 1 | 0.41 | 0.41 |
| com.example.stickerexplode | AIDL::java::android.hardware.display.IDisplayManager::getDisplayInfo::server | system_server | 1 | 0.40 | 0.40 |
| com.example.stickerexplode | AIDL::java::com.android.internal.view.IInputMethodManager::startInputOrWindowGainedFocus::server | system_server | 1 | 0.38 | 0.38 |
| com.example.stickerexplode | AIDL::java::android.view.IWindowManager::openSession::server | system_server | 1 | 0.36 | 0.36 |
| com.example.stickerexplode | AIDL::java::android.view.IWindowManager::getCurrentAnimatorScale::server | system_server | 1 | 0.31 | 0.31 |
| com.example.stickerexplode | AIDL::java::android.app.IActivityManager::checkPermissionForDevice::server | system_server | 3 | 0.30 | 0.13 |
| com.example.stickerexplode | AIDL::java::android.app.IActivityManager::setRenderThread::server | system_server | 2 | 0.29 | 0.18 |
| com.example.stickerexplode | AIDL::java::android.content.pm.IPackageManager::getApplicationInfo::server | system_server | 2 | 0.29 | 0.15 |
| com.example.stickerexplode | AIDL::cpp::android.gui.SensorServer::#1::server | system_server | 1 | 0.26 | 0.26 |

_(5 more rows in csv/)_


#### `31_binder_why_slow`

> When a binder call is slow, was the server busy, or was the client just descheduled? reason: Running = real server work, S/R/R+ = waiting/queueing.

| reason | n | total_ms |
|---|---|---|
| S | 71 | 68.20 |
| R | 71 | 1.77 |
| Running | 159 | 1.73 |
| R+ | 9 | 1.66 |


#### `32_monitor_contention`

> Java lock contention where the MAIN thread is the one blocked. blocking_method is the code holding the lock. This is a top-3 cause of "the profiler says nothing is running but the app is frozen".

| process_name | short_blocking_method | short_blocked_method | n | total_ms | max_ms |
|---|---|---|---|---|---|
| com.example.stickerexplode | java.lang.Thread.setPosixNicenessInternal | java.lang.Object.wait | 1 | 0.02 | 0.02 |


#### `33_main_thread_blocking`

> Whole-trace answer to "running or waiting". blocked_function is the kernel function the thread was stuck in (needs sched/sched_blocked_reason ftrace).

| process_name | state | blocked_function | n | total_ms |
|---|---|---|---|---|
| com.example.stickerexplode | Sleeping | - | 153 | 4,889.46 |
| com.example.stickerexplode | Running | - | 294 | 388.99 |
| com.example.stickerexplode | Uninterruptible Sleep (IO) | folio_wait_bit_common | 2 | 9.11 |
| com.example.stickerexplode | Runnable | - | 175 | 6.58 |
| com.example.stickerexplode | Runnable (Preempted) | - | 102 | 3.49 |
| com.example.stickerexplode | Uninterruptible Sleep (non-IO) | synchronize_rcu_expedited | 1 | 0.05 |
| com.example.stickerexplode | Uninterruptible Sleep | - | 2 | 0.03 |
| com.example.stickerexplode | Exit (Zombie) | - | 1 | -0.00 |


### Hotspots and ANRs

#### `40_slice_hotspots`

> Whole-process hotspot ranking by SELF time (own cost, excluding children). Sorting by total_ms lies: a wrapper slice inherits everything below it.

| name | thread_name | n | total_ms | self_ms |
|---|---|---|---|---|
| Compiling baseline | Jit thread pool | 8174 | 841.35 | 746.10 |
| waitForever | GPU completion | 32 | 373.34 | 373.34 |
| postAndWait | .stickerexplode | 32 | 264.86 | 264.86 |
| EmojiCompat.MetadataRepo.create | EmojiCompatInit | 1 | 184.47 | 182.20 |
| waitForever | HWC release | 20 | 107.21 | 107.21 |
| ScopedCodeCacheWrite | Jit thread pool | 16798 | 95.94 | 95.94 |
| auto skgpu::ganesh::OpsTask::onExecute(GrOpFlushState *)::(anonymous class)::operator()() const | RenderThread | 15924 | 142.78 | 87.42 |
| Vulkan finish frame | RenderThread | 32 | 262.53 | 83.07 |
| binder transaction | .stickerexplode | 79 | 73.37 | 73.37 |
| Drawing  0.00  0.00 1080.00 2424.00 | RenderThread | 32 | 349.99 | 62.96 |
| Compose:recompose | .stickerexplode | 10 | 63.02 | 56.81 |
| waitForBufferRelease | RenderThread | 10 | 50.43 | 50.05 |
| Recomposer:animation | .stickerexplode | 30 | 37.73 | 35.77 |
| FillRectOp | RenderThread | 16222 | 33.18 | 32.52 |
| TextureOp | RenderThread | 16420 | 31.34 | 31.14 |
| AndroidOwner:measureAndLayout | .stickerexplode | 4 | 29.59 | 26.55 |
| traversal | .stickerexplode | 33 | 449.49 | 26.15 |
| auto skgpu::ganesh::OpsTask::onPrepare(GrOpFlushState *)::(anonymous class)::operator()() const | RenderThread | 15924 | 42.21 | 23.50 |
| Record View#draw() | .stickerexplode | 32 | 50.00 | 19.95 |
| Compose:applyChanges | .stickerexplode | 9 | 18.03 | 17.10 |

_(10 more rows in csv/)_


#### `41_anrs`

> ANRs recorded in the trace, if any.

_no rows_

