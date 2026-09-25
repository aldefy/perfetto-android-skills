-- Frame health per process, and the app-vs-SurfaceFlinger split.
-- app_jank  -> you missed your deadline. Fix your code.
-- sf_jank   -> the compositor or display pipeline missed. Usually not your bug.
INCLUDE PERFETTO MODULE android.frames.timeline;
INCLUDE PERFETTO MODULE android.frames.jank_type;
INCLUDE PERFETTO MODULE android.frames.per_frame_metrics;
SELECT
  f.process_name,
  count(*)                                                      AS frames,
  sum(ifnull(st.was_jank, 0))                                   AS janky,
  round(100.0 * sum(ifnull(st.was_jank, 0)) / count(*), 2)      AS janky_pct,
  sum(ifnull(st.was_big_jank, 0))                               AS big_jank,
  sum(ifnull(st.was_huge_jank, 0))                              AS huge_jank,
  max(st.overrun) / 1e6                                         AS worst_overrun_ms,
  sum(iif(android_is_app_jank_type(a.jank_type), 1, 0))         AS app_jank,
  sum(iif(android_is_sf_jank_type(a.jank_type), 1, 0))          AS sf_jank
FROM android_frames f
LEFT JOIN android_frame_stats st USING(frame_id)
LEFT JOIN actual_frame_timeline_slice a ON a.id = f.actual_frame_timeline_id
GROUP BY 1
HAVING frames > 5
ORDER BY janky DESC;
