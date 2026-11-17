-- The named work on the main thread during startup, ranked. This is the list
-- you map back to functions in the codebase.
--
-- Second half: the same fallback as 12_startup_main_thread_state, for Android
-- versions where the stdlib cannot tie the launch to its process.
INCLUDE PERFETTO MODULE android.startup.startups;
SELECT
  startup_id,
  slice_name,
  count(*)            AS n,
  sum(slice_dur)/1e6  AS total_ms,
  max(slice_dur)/1e6  AS max_ms
FROM (
  SELECT startup_id, slice_name, slice_dur
  FROM android_thread_slices_for_all_startups
  JOIN android_startups USING(startup_id)
  WHERE is_main_thread
    AND package GLOB '__PKG__'
  UNION ALL
  SELECT s.startup_id, sl.name AS slice_name, sl.dur AS slice_dur
  FROM android_startups s
  JOIN process p ON p.name = s.package
  JOIN thread t ON t.upid = p.upid AND t.is_main_thread
  JOIN thread_track tt ON tt.utid = t.utid
  JOIN slice sl ON sl.track_id = tt.id
  WHERE s.package GLOB '__PKG__'
    AND NOT EXISTS (SELECT 1 FROM android_startup_processes x WHERE x.startup_id = s.startup_id)
    AND sl.ts < s.ts + s.dur
    AND sl.ts + sl.dur > s.ts
)
GROUP BY 1, 2
ORDER BY total_ms DESC
LIMIT 30;
