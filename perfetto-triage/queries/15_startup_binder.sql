-- Synchronous binder calls made from the main thread during startup, over 1ms.
INCLUDE PERFETTO MODULE android.startup.startups;
SELECT
  s.startup_id,
  b.thread_name,
  b.process    AS server_process,
  count(*)     AS n,
  sum(b.slice_dur)/1e6 AS total_ms,
  max(b.slice_dur)/1e6 AS max_ms
FROM android_startups s
JOIN android_binder_transaction_slices_for_startup(s.startup_id, 1e6) b
WHERE b.is_main_thread
  AND s.package GLOB '__PKG__'
GROUP BY 1, 2, 3
ORDER BY total_ms DESC
LIMIT 25;
