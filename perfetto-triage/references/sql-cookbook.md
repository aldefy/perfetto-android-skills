# PerfettoSQL cookbook

Everything here was executed against `trace_processor_shell v57.2` on a real
Android trace. Column names are the verified ones — several differ from what
older blog posts and even some current docs claim.

## Running queries

```bash
# one-shot
trace_processor query trace.pftrace "SELECT ts, dur, name FROM slice LIMIT 5"

# from a file
trace_processor query -f queries.sql trace.pftrace

# warm session: parse once, query many times (what triage.py does)
trace_processor server unix --name s1 --daemonize trace.pftrace
trace_processor query --remote s1 "SELECT count(*) FROM slice"
trace_processor server kill s1
```

Get the binary from `https://get.perfetto.dev/trace_processor` (a Python
wrapper that fetches the native binary), or directly:

```
https://commondatastorage.googleapis.com/perfetto-luci-artifacts/v57.2/linux-amd64/trace_processor_shell
https://commondatastorage.googleapis.com/perfetto-luci-artifacts/v57.2/mac-arm64/trace_processor_shell
```

### Two sharp edges

1. **Output is CSV only.** No JSON flag exists. Strings are quoted, NULL prints
   as `[NULL]`. Each statement emits its own result set separated by a blank
   line, so `INCLUDE PERFETTO MODULE` lines produce empty blocks — parse the
   last non-empty block.
2. **Only the final statement may be a SELECT.** Two SELECTs in one file fails
   with *"Result rows were returned for multiple queries"*. One question per
   file.

## The standard library

`INCLUDE PERFETTO MODULE <path>;` pulls tables into the global namespace. The
modules that matter for app triage:

| Module | Gives you |
|---|---|
| `android.startup.startups` | `android_startups`, `android_startup_threads`, `android_thread_slices_for_all_startups`, `android_class_loading_for_startup` |
| `android.startup.time_to_display` | `android_startup_time_to_display` (TTID/TTFD) |
| `android.startup.startup_breakdowns` | `android_startup_opinionated_breakdown` |
| `android.frames.timeline` | `android_frames`, `android_frames_choreographer_do_frame`, `android_frames_draw_frame` |
| `android.frames.per_frame_metrics` | `android_frame_stats`, `android_cpu_time_per_frame` |
| `android.frames.jank_type` | `android_is_app_jank_type()`, `android_is_sf_jank_type()`, `android_is_missed_frame_type()` |
| `android.binder` | `android_binder_txns` |
| `android.binder_breakdown` | `android_binder_client_breakdown`, `..._server_breakdown` |
| `android.monitor_contention` | `android_monitor_contention` |
| `android.anrs` | `android_anrs` |
| `slices.with_context` | `thread_slice`, `process_slice` (slice joined to thread + process) |
| `sched.states` | `sched_state_to_human_readable_string()`, `sched_state_io_to_human_readable_string()` |
| `sched.with_context` | `sched_with_thread_process` |

### Names that are commonly gotten wrong

| People write | Actually called |
|---|---|
| `android_startup_breakdowns` | `android_startup_opinionated_breakdown` |
| `android_startup_time_to_initial_display` | column on `android_startup_time_to_display` |
| `android_sysui_cujs` | `android_sysui_jank_cujs` |
| `android_critical_blocking_calls` | private `_android_critical_blocking_calls`; the public one is `android_cuj_blocking_calls` in `android.cujs.sysui_cujs` |
| `android_binder_txns.ts` / `.dur` | `client_ts` / `client_dur` / `server_ts` / `server_dur` |
| `android_startups.upid` | join `android_startup_processes USING(startup_id)` |

## Core tables

### `thread_state`

`id, ts, dur, cpu, utid, state, io_wait, blocked_function, waker_utid, waker_id, irq_context, ucpu`

`state` holds raw short codes — `Running`, `R`, `R+`, `S`, `D`, `I`, `X`. There
is **no** `'Uninterruptible Sleep'` value; that string is what
`sched_state_io_to_human_readable_string(state, io_wait)` returns.

`blocked_function` is only populated if the trace recorded the ftrace event
`sched/sched_blocked_reason`.

### `actual_frame_timeline_slice` (no INCLUDE needed)

`id, ts, dur, track_id, name, upid, display_frame_token, surface_frame_token,
layer_name, present_type, on_time_finish, gpu_composition, jank_type,
jank_severity_type, prediction_type, jank_tag`

`jank_type` is comma-joined, e.g. `"App Deadline Missed, Buffer Stuffing"`, so
match with `GLOB '*App Deadline Missed*'` or use the helper functions.

### `android_frame_stats` (from `android.frames.per_frame_metrics`)

`frame_id, overrun, cpu_time, ui_time, was_jank, was_slow_frame, was_big_jank,
was_huge_jank`

The `was_*` columns are `1` or **NULL**, never `0` — they are built with
`iif(cond, 1, NULL)`. Always wrap in `ifnull(x, 0)` before summing or your
counts silently drop rows.

Thresholds baked in: slow > 20 ms CPU, big jank > 50 ms, huge jank > 200 ms.

### `android_binder_txns`

`aidl_name, interface, method_name, binder_txn_id, client_process, client_thread,
client_utid, client_tid, is_main_thread, client_ts, client_dur, binder_reply_id,
server_process, server_thread, server_ts, server_dur, is_sync,
client_oom_score, server_oom_score, ...`

`aidl_name` is only populated if the `aidl` atrace category was recorded;
otherwise it is NULL and you only see the raw transaction.

## Idioms

### Self time (exclude children)

```sql
INCLUDE PERFETTO MODULE slices.with_context;
SELECT s.name,
       count(*) AS n,
       sum(s.dur) / 1e6 AS total_ms,
       sum(s.dur - ifnull(c.child_dur, 0)) / 1e6 AS self_ms
FROM thread_slice s
LEFT JOIN (
  SELECT parent_id, sum(dur) AS child_dur
  FROM slice WHERE parent_id IS NOT NULL GROUP BY parent_id
) c ON c.parent_id = s.id
WHERE s.process_name = 'com.example.app'
GROUP BY 1 ORDER BY self_ms DESC LIMIT 30;
```

### Intersect a state series with a time window

Clipping, not containment — a `thread_state` row usually straddles the window
edge, and `WHERE ts BETWEEN` silently drops the biggest ones.

```sql
sum(min(ts.ts + ts.dur, w.ts + w.dur) - max(ts.ts, w.ts)) AS ms
...
WHERE ts.ts < w.ts + w.dur AND ts.ts + ts.dur > w.ts
```

SQLite's two-argument `min`/`max` are scalar, which is what makes this work
inside an aggregate.

### Span join (the heavyweight version of the above)

```sql
CREATE VIRTUAL TABLE x USING SPAN_JOIN(
  android_monitor_contention PARTITIONED upid,
  startups PARTITIONED upid);
```

Both inputs need `ts`, `dur` and the partition column, and must be sorted and
non-overlapping within a partition.

### Everything the main thread did during startup

```sql
INCLUDE PERFETTO MODULE android.startup.startups;
SELECT startup_id, slice_name, count(*) n, sum(slice_dur)/1e6 total_ms
FROM android_thread_slices_for_all_startups
JOIN android_startups USING(startup_id)
WHERE is_main_thread AND package GLOB 'com.example.*'
GROUP BY 1, 2 ORDER BY total_ms DESC;
```

### Frames that missed, with app-vs-SF attribution

```sql
INCLUDE PERFETTO MODULE android.frames.timeline;
INCLUDE PERFETTO MODULE android.frames.jank_type;
SELECT f.process_name, count(*) n
FROM android_frames f
JOIN actual_frame_timeline_slice a ON a.id = f.actual_frame_timeline_id
WHERE android_is_app_jank_type(a.jank_type)
GROUP BY 1 ORDER BY n DESC;
```

### Why the main thread was in uninterruptible sleep

```sql
SELECT blocked_function, count(*) n, sum(dur)/1e6 ms
FROM thread_state
JOIN thread USING(utid) JOIN process USING(upid)
WHERE process.name = 'com.example.app' AND thread.is_main_thread AND state = 'D'
GROUP BY 1 ORDER BY ms DESC;
```

## Sanity checks worth running on any trace

```sql
-- data loss
SELECT name, value FROM stats WHERE value > 0 AND severity != 'info';

-- is FrameTimeline present at all
SELECT count(*) FROM actual_frame_timeline_slice;

-- did anyone record blocking reasons
SELECT count(*) FROM thread_state WHERE blocked_function IS NOT NULL;
```
