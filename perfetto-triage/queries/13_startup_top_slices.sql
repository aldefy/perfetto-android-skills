-- The named work on the main thread during startup, ranked. This is the list
-- you map back to functions in the codebase.
INCLUDE PERFETTO MODULE android.startup.startups;
SELECT
  startup_id,
  slice_name,
  count(*)            AS n,
  sum(slice_dur)/1e6  AS total_ms,
  max(slice_dur)/1e6  AS max_ms
FROM android_thread_slices_for_all_startups
JOIN android_startups USING(startup_id)
WHERE is_main_thread
  AND package GLOB '__PKG__'
GROUP BY 1, 2
ORDER BY total_ms DESC
LIMIT 30;
