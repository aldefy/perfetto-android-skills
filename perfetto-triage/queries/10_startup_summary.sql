-- Every app launch in the trace, with time-to-initial-display and
-- time-to-full-display. ttfd_ms is NULL when the app never calls reportFullyDrawn().
--
-- startup_type: older Android versions do not record it, and the stdlib then
-- leaves it NULL. A bindApplication slice on the app's main thread inside the
-- launch means the process was created for this launch, which is what a cold
-- start is, so that case is filled in as 'cold'.
INCLUDE PERFETTO MODULE android.startup.startups;
INCLUDE PERFETTO MODULE android.startup.time_to_display;
SELECT
  s.startup_id,
  s.package,
  coalesce(s.startup_type, (
    SELECT 'cold'
    FROM process p
    JOIN thread t ON t.upid = p.upid AND t.is_main_thread
    JOIN thread_track tt ON tt.utid = t.utid
    JOIN slice sl ON sl.track_id = tt.id
    WHERE p.name = s.package
      AND sl.name = 'bindApplication'
      AND sl.ts >= s.ts AND sl.ts < s.ts + s.dur
    LIMIT 1
  ))                               AS startup_type,
  s.dur / 1e6                      AS startup_ms,
  t.time_to_initial_display / 1e6  AS ttid_ms,
  t.time_to_full_display / 1e6     AS ttfd_ms
FROM android_startups s
LEFT JOIN android_startup_time_to_display t USING(startup_id)
WHERE s.package GLOB '__PKG__'
ORDER BY s.dur DESC;
