-- The individual frames that blew the deadline, worst first.
-- overrun_ms is how late the frame was. cpu_time is UI thread + RenderThread.
INCLUDE PERFETTO MODULE android.frames.timeline;
INCLUDE PERFETTO MODULE android.frames.per_frame_metrics;
SELECT
  f.process_name,
  f.frame_id,
  f.ts,
  st.overrun  / 1e6 AS overrun_ms,
  st.cpu_time / 1e6 AS cpu_ms,
  st.ui_time  / 1e6 AS ui_ms,
  a.jank_type,
  a.present_type
FROM android_frames f
JOIN android_frame_stats st USING(frame_id)
LEFT JOIN actual_frame_timeline_slice a ON a.id = f.actual_frame_timeline_id
WHERE st.overrun > 0
ORDER BY st.overrun DESC
LIMIT 20;
