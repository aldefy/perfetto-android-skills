-- Synchronous binder calls blocking a main thread, ranked by total cost.
-- Every millisecond here is your app frozen waiting on another process.
INCLUDE PERFETTO MODULE android.binder;
SELECT
  client_process,
  ifnull(aidl_name, 'unknown')  AS aidl_name,
  server_process,
  count(*)                      AS n,
  sum(client_dur) / 1e6         AS total_ms,
  max(client_dur) / 1e6         AS max_ms
FROM android_binder_txns
WHERE is_main_thread AND is_sync
  AND client_process GLOB '__PKG__'
GROUP BY 1, 2, 3
ORDER BY total_ms DESC
LIMIT 25;
