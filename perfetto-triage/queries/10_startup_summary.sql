-- Every app launch in the trace, with time-to-initial-display and
-- time-to-full-display. ttfd_ms is NULL when the app never calls reportFullyDrawn().
INCLUDE PERFETTO MODULE android.startup.startups;
INCLUDE PERFETTO MODULE android.startup.time_to_display;
SELECT
  s.startup_id,
  s.package,
  s.startup_type,
  s.dur / 1e6                      AS startup_ms,
  t.time_to_initial_display / 1e6  AS ttid_ms,
  t.time_to_full_display / 1e6     AS ttfd_ms
FROM android_startups s
LEFT JOIN android_startup_time_to_display t USING(startup_id)
WHERE s.package GLOB '__PKG__'
ORDER BY s.dur DESC;
