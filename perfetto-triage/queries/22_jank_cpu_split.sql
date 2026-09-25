-- For janky frames, is the cost in Choreographer#doFrame (your UI thread work:
-- measure/layout/composition/binding) or in RenderThread DrawFrame (draw
-- commands, overdraw, shader compile)? This decides where you look next.
INCLUDE PERFETTO MODULE android.frames.timeline;
INCLUDE PERFETTO MODULE android.frames.per_frame_metrics;
SELECT
  f.process_name,
  count(*)                          AS janky_frames,
  sum(c.app_vsync_delay) / 1e6      AS vsync_delay_ms,
  sum(c.do_frame_dur)    / 1e6      AS ui_thread_ms,
  sum(c.draw_frame_dur)  / 1e6      AS render_thread_ms
FROM android_frames f
JOIN android_frame_stats st USING(frame_id)
JOIN android_cpu_time_per_frame c USING(frame_id)
WHERE st.overrun > 0
GROUP BY 1
ORDER BY janky_frames DESC;
