-- Frame health per process, and the app-vs-SurfaceFlinger split.
-- app_jank  -> you missed your deadline. Fix your code.
-- sf_jank   -> the compositor or display pipeline missed. Usually not your bug.
INCLUDE PERFETTO MODULE android.frames.timeline;
INCLUDE PERFETTO MODULE android.frames.jank_type;
INCLUDE PERFETTO MODULE android.frames.per_frame_metrics;
--
-- With no frame timeline in the trace there are no deadlines, so nothing can be
-- called late. The frame count still stands, and every other column is NULL
-- then. A zero would read as "no late frames", which the trace cannot say.
SELECT
  f.process_name,
  count(*)                                                      AS frames,
  iif(d.have, sum(ifnull(st.was_jank, 0)), NULL)                AS janky,
  iif(d.have, round(100.0 * sum(ifnull(st.was_jank, 0)) / count(*), 2), NULL) AS janky_pct,
  iif(d.have, sum(ifnull(st.was_big_jank, 0)), NULL)            AS big_jank,
  iif(d.have, sum(ifnull(st.was_huge_jank, 0)), NULL)           AS huge_jank,
  max(st.overrun) / 1e6                                         AS worst_overrun_ms,
  iif(d.have, sum(iif(android_is_app_jank_type(a.jank_type), 1, 0)), NULL) AS app_jank,
  iif(d.have, sum(iif(android_is_sf_jank_type(a.jank_type), 1, 0)), NULL)  AS sf_jank
FROM android_frames f
CROSS JOIN (SELECT count(*) > 0 AS have FROM actual_frame_timeline_slice) d
LEFT JOIN android_frame_stats st USING(frame_id)
LEFT JOIN actual_frame_timeline_slice a ON a.id = f.actual_frame_timeline_id
GROUP BY 1
HAVING frames > 5
ORDER BY janky DESC, frames DESC;
