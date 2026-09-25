-- When a binder call is slow, was the server busy, or was the client just
-- descheduled? reason: Running = real server work, S/R/R+ = waiting/queueing.
INCLUDE PERFETTO MODULE android.binder_breakdown;
INCLUDE PERFETTO MODULE android.binder;
SELECT
  b.reason,
  count(*)            AS n,
  sum(b.dur) / 1e6    AS total_ms
FROM android_binder_client_breakdown b
JOIN android_binder_txns t USING(binder_txn_id)
WHERE t.is_main_thread AND t.client_process GLOB '__PKG__'
GROUP BY 1
ORDER BY total_ms DESC;
