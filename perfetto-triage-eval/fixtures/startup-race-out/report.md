# Perfetto triage — `com.example.stickerexplode-startup-171407.pftrace`

Package filter: `com.example.stickerexplode`  ·  trace_processor v57.2

## Findings, worst first

### 1. 🟠 Main-thread binder: AIDL::cpp::android.gui.SensorEventConnection::#2::server costs 31 ms
*binder · medium*

2 synchronous calls from com.example.stickerexplode to system_server, worst single call 16.3 ms.

**What to do:** Synchronous binder on the main thread is a frozen UI. Cache the result, batch the calls, or move them to a background dispatcher. System APIs that look local (PackageManager, ConnectivityManager, DisplayManager, SharedPreferences-backed system settings) are binder calls underneath.

### 2. 🔵 Slow binder calls are queueing, not computing
*binder · info*

Dominant client-side reason is 'S' (72 ms).

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
| 79119 | 42348 | 152 | 2848 | 2730 | 826 | 11,876.80 |


#### `02_processes`

> Who is in this trace and who is doing the work. Find your package here.

| pid | process_name | slices | slice_ms |
|---|---|---|---|
| 548 | /system/bin/surfaceflinger | 35846 | 6,711.79 |
| 2325 | com.android.systemui | 9232 | 2,077.60 |
| 26788 | com.example.stickerexplode | 9147 | 11,115.31 |
| 1544 | system_server | 7474 | 5,794.10 |
| 550 | /vendor/bin/hw/android.hardware.composer.hwc3-service.pixel | 7463 | 4,355.18 |
| 665 | /vendor/bin/hw/android.hardware.power-service.pixel-libperfmgr | 1768 | 10,298.35 |
| 26775 | commands.monkey | 773 | 343.78 |
| 11335 | com.google.android.apps.nexuslauncher | 639 | 132.31 |
| 983 | /vendor/bin/hw/android.hardware.sensors-service.multihal | 516 | 1,473.51 |
| 7722 | com.google.android.gms | 295 | 167.86 |
| 2541 | com.shannon.imsservice | 235 | 106.46 |
| 2612 | com.android.phone | 162 | 124.23 |
| 549 | /vendor/bin/hw/android.hardware.graphics.allocator-V2-service | 144 | 73.44 |
| 4154 | com.google.android.as | 140 | 76.25 |
| 2462 | com.google.android.pixelsystemservice | 138 | 45.86 |
| 954 | /vendor/bin/hw/android.hardware.contexthub-service.generic | 113 | 71.04 |
| 3149 | com.google.android.apps.scone | 95 | 130.61 |
| 537 | /system/bin/hw/android.system.suspend-service | 84 | 85.98 |
| 14866 | com.google.android.euicc | 67 | 81.69 |
| 2559 | com.android.pixeldisplayservice | 65 | 38.54 |

_(5 more rows in csv/)_


### Startup: where does cold start time go?

#### `10_startup_summary`

> Every app launch in the trace, with time-to-initial-display and time-to-full-display. ttfd_ms is NULL when the app never calls reportFullyDrawn().

_no rows_


#### `11_startup_breakdown`

> Where cold-start time actually goes, bucketed by cause. reason values seen in the wild: launch_delay, binder, activity_start, inflate, choreographer_do_frame.

_no rows_


#### `12_startup_main_thread_state`

> The first question a senior engineer asks: during startup, was the main thread RUNNING or WAITING? Running -> your code is slow. Sleeping/blocked -> you are waiting on IPC, IO, or a lock, and optimising your code is pointless.

_no rows_


#### `13_startup_top_slices`

> The named work on the main thread during startup, ranked. This is the list you map back to functions in the codebase.

_no rows_


#### `14_startup_class_loading`

> Class loading + verification during startup. This is the number a Baseline Profile moves. High count with high ms == the profile is missing or stale.

_no rows_


#### `15_startup_binder`

> Synchronous binder calls made from the main thread during startup, over 1ms.

_no rows_


### Jank: which frames missed, and whose fault was it?

#### `20_jank_summary`

> Frame health per process, and the app-vs-SurfaceFlinger split. app_jank  -> you missed your deadline. Fix your code. sf_jank   -> the compositor or display pipeline missed. Usually not your bug.

| process_name | frames | janky | janky_pct | big_jank | huge_jank | worst_overrun_ms | app_jank | sf_jank |
|---|---|---|---|---|---|---|---|---|
| com.android.systemui | 67 | 1 | 1.49 | 0 | 0 | 4.23 | 0 | 0 |


#### `21_worst_frames`

> The individual frames that blew the deadline, worst first. overrun_ms is how late the frame was. cpu_time is UI thread + RenderThread.

| process_name | frame_id | ts | overrun_ms | cpu_ms | ui_ms | jank_type | present_type |
|---|---|---|---|---|---|---|---|
| com.android.systemui | 26065731 | 276637193509854 | 4.23 | 14.85 | 3.47 | Non Animating | Unspecified Present |


#### `22_jank_cpu_split`

> For janky frames, is the cost in Choreographer#doFrame (your UI thread work: measure/layout/composition/binding) or in RenderThread DrawFrame (draw commands, overdraw, shader compile)? This decides where you look next.

| process_name | janky_frames | vsync_delay_ms | ui_thread_ms | render_thread_ms |
|---|---|---|---|---|
| com.android.systemui | 1 | 8.15 | 3.47 | 3.24 |


#### `23_jank_hot_slices`

> What was actually executing on the UI thread and RenderThread during the frames that missed. Ranked. These slice names are your suspects.

_no rows_


### Blocking: binder, IPC and locks

#### `30_binder_main_thread`

> Synchronous binder calls blocking a main thread, ranked by total cost. Every millisecond here is your app frozen waiting on another process.

| client_process | aidl_name | server_process | n | total_ms | max_ms |
|---|---|---|---|---|---|
| com.example.stickerexplode | AIDL::cpp::android.gui.SensorEventConnection::#2::server | system_server | 2 | 31.11 | 16.25 |
| com.example.stickerexplode | unknown | /system/bin/servicemanager | 15 | 14.22 | 2.42 |
| com.example.stickerexplode | AIDL::java::android.content.IContentProvider::#21::server | system_server | 3 | 10.57 | 6.43 |
| com.example.stickerexplode | AIDL::java::android.view.IWindowSession::addToDisplayAsUser::server | system_server | 1 | 6.10 | 6.10 |
| com.example.stickerexplode | AIDL::java::android.content.IContentService::registerContentObserver::server | system_server | 1 | 4.19 | 4.19 |
| com.example.stickerexplode | AIDL::java::android.app.IActivityManager::getContentProvider::server | system_server | 1 | 3.30 | 3.30 |
| com.example.stickerexplode | AIDL::java::android.app.IActivityManager::attachApplication::server | system_server | 1 | 2.82 | 2.82 |
| com.example.stickerexplode | AIDL::java::android.app.IActivityManager::checkPermissionForDevice::server | system_server | 3 | 2.58 | 1.30 |
| com.example.stickerexplode | AIDL::java::android.os.IVibratorManagerService::getTargetVibratorIds::server | system_server | 1 | 2.22 | 2.22 |
| com.example.stickerexplode | AIDL::java::android.app.IActivityManager::finishAttachApplication::server | system_server | 1 | 2.08 | 2.08 |
| com.example.stickerexplode | AIDL::java::android.view.IWindowManager::hasNavigationBar::server | system_server | 1 | 1.96 | 1.96 |
| com.example.stickerexplode | AIDL::cpp::android.gui.IDisplayEventConnection::getLatestVsyncEventData::server | /system/bin/surfaceflinger | 4 | 1.93 | 0.75 |
| com.example.stickerexplode | AIDL::java::android.companion.virtualnative.IVirtualDeviceManagerNative::getDeviceIdsForUid::server | system_server | 1 | 1.90 | 1.90 |
| com.example.stickerexplode | AIDL::java::android.view.accessibility.IAccessibilityManager::getEnabledAccessibilityServiceList::server | system_server | 1 | 1.86 | 1.86 |
| com.example.stickerexplode | AIDL::cpp::android.gui.SensorServer::#10::server | system_server | 1 | 1.75 | 1.75 |
| com.example.stickerexplode | AIDL::cpp::android.gui.SensorServer::#1::server | system_server | 1 | 1.70 | 1.70 |
| com.example.stickerexplode | AIDL::java::android.app.IActivityTaskManager::getActivityClientController::server | system_server | 1 | 1.54 | 1.54 |
| com.example.stickerexplode | AIDL::java::android.view.accessibility.IAccessibilityManager::addClient::server | system_server | 1 | 1.52 | 1.52 |
| com.example.stickerexplode | AIDL::java::android.os.IVibratorManagerService::getVibratorInfo::server | system_server | 1 | 1.32 | 1.32 |
| com.example.stickerexplode | AIDL::java::android.view.IGraphicsStats::requestBufferForProcess::server | system_server | 1 | 1.28 | 1.28 |

_(5 more rows in csv/)_


#### `31_binder_why_slow`

> When a binder call is slow, was the server busy, or was the client just descheduled? reason: Running = real server work, S/R/R+ = waiting/queueing.

| reason | n | total_ms |
|---|---|---|
| S | 49 | 72.24 |
| R+ | 31 | 29.48 |
| Running | 156 | 7.01 |
| R | 48 | 3.18 |


#### `32_monitor_contention`

> Java lock contention where the MAIN thread is the one blocked. blocking_method is the code holding the lock. This is a top-3 cause of "the profiler says nothing is running but the app is frozen".

_no rows_


#### `33_main_thread_blocking`

> Whole-trace answer to "running or waiting". blocked_function is the kernel function the thread was stuck in (needs sched/sched_blocked_reason ftrace).

| process_name | state | blocked_function | n | total_ms |
|---|---|---|---|---|
| com.example.stickerexplode | Sleeping | - | 76 | 4,337.56 |
| com.example.stickerexplode | Running | - | 728 | 2,154.15 |
| com.example.stickerexplode | Runnable | - | 620 | 125.81 |
| com.example.stickerexplode | Runnable (Preempted) | - | 100 | 57.68 |
| com.example.stickerexplode | Uninterruptible Sleep (non-IO) | synchronize_rcu_expedited | 1 | 0.08 |
| com.example.stickerexplode | Uninterruptible Sleep | - | 1 | 0.05 |
| com.example.stickerexplode | Uninterruptible Sleep (non-IO) | mmap_read_lock_killable | 1 | 0.01 |


### Hotspots and ANRs

#### `40_slice_hotspots`

> Whole-process hotspot ranking by SELF time (own cost, excluding children). Sorting by total_ms lies: a wrapper slice inherits everything below it.

| name | thread_name | n | total_ms | self_ms |
|---|---|---|---|---|
| Compose:recompose | .stickerexplode | 7 | 758.97 | 243.15 |
| EmojiCompat.MetadataRepo.create | EmojiCompatInit | 1 | 131.11 | 126.71 |
| binder transaction | .stickerexplode | 76 | 111.91 | 111.91 |
| Extract dex file /data/app/~~bv6Lu28t7uoP-L3cJn68WA==/com.example.stickerexplode-7wD8RW5M-l7zxDyyQ4ec8w==/base.apk | .stickerexplode | 3 | 88.55 | 88.55 |
| traversal | .stickerexplode | 3 | 1,485.57 | 77.21 |
| Compose:applyChanges | .stickerexplode | 6 | 116.43 | 69.06 |
| Compiling baseline | Jit thread pool | 14 | 57.89 | 54.39 |
| Verify dex file /data/app/~~bv6Lu28t7uoP-L3cJn68WA==/com.example.stickerexplode-7wD8RW5M-l7zxDyyQ4ec8w==/base.apk | .stickerexplode | 1 | 42.23 | 42.23 |
| VerifyClass kotlin.collections.ArraysKt___ArraysKt | .stickerexplode | 1 | 44.14 | 41.97 |
| binder transaction | EmojiCompatInit | 15 | 37.31 | 37.31 |
| AndroidOwner:onMeasure | .stickerexplode | 2 | 92.07 | 35.94 |
| performCreate:com.example.stickerexplode.MainActivity | .stickerexplode | 1 | 239.00 | 34.77 |
| layout | .stickerexplode | 1 | 40.84 | 26.03 |
| Compose:onRemembered | .stickerexplode | 3 | 51.22 | 23.24 |
| driver.CreateDevice | RenderThread | 1 | 22.98 | 22.98 |
| VerifyClass androidx.compose.ui.platform.AndroidComposeView | .stickerexplode | 1 | 43.79 | 20.73 |
| Verify dex file /data/app/~~bv6Lu28t7uoP-L3cJn68WA==/com.example.stickerexplode-7wD8RW5M-l7zxDyyQ4ec8w==/base.apk!1 | .stickerexplode | 1 | 19.37 | 19.37 |
| VerifyClass androidx.compose.ui.platform.AndroidComposeViewAccessibilityDelegateCompat | .stickerexplode | 1 | 20.55 | 15.65 |
| activityStart | .stickerexplode | 1 | 295.47 | 14.22 |
| preInitBufferAllocator:GraphicBufferAllocator | hwuiTask1 | 1 | 18.42 | 14.19 |

_(10 more rows in csv/)_


#### `41_anrs`

> ANRs recorded in the trace, if any.

_no rows_

