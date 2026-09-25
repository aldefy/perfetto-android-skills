-- Java lock contention where the MAIN thread is the one blocked.
-- blocking_method is the code holding the lock. This is a top-3 cause of
-- "the profiler says nothing is running but the app is frozen".
INCLUDE PERFETTO MODULE android.monitor_contention;
SELECT
  process_name,
  short_blocking_method,
  short_blocked_method,
  count(*)         AS n,
  sum(dur) / 1e6   AS total_ms,
  max(dur) / 1e6   AS max_ms
FROM android_monitor_contention
WHERE is_blocked_thread_main
  AND process_name GLOB '__PKG__'
GROUP BY 1, 2, 3
ORDER BY total_ms DESC
LIMIT 20;
