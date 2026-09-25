-- What was actually executing on the UI thread and RenderThread during the
-- frames that missed. Ranked. These slice names are your suspects.
INCLUDE PERFETTO MODULE android.frames.timeline;
INCLUDE PERFETTO MODULE android.frames.per_frame_metrics;
INCLUDE PERFETTO MODULE slices.with_context;
SELECT
  ts.name,
  count(*)             AS n,
  sum(ts.dur) / 1e6    AS total_ms,
  max(ts.dur) / 1e6    AS max_ms
FROM android_frames f
JOIN android_frame_stats st USING(frame_id)
JOIN thread_slice ts
  ON ts.utid IN (f.ui_thread_utid, f.render_thread_utid)
 AND ts.ts >= f.ts
 AND ts.ts < f.ts + f.dur
WHERE st.overrun > 0
  AND f.process_name GLOB '__PKG__'
GROUP BY 1
ORDER BY total_ms DESC
LIMIT 25;
