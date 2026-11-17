-- What was actually executing on the UI thread and RenderThread during the
-- frames that missed. Ranked. These slice names are your suspects.
--
-- A slice counts when it starts inside a late frame. The match is an interval
-- intersect, not a ts-range join, so it stays fast on long traces with
-- thousands of late frames.
INCLUDE PERFETTO MODULE android.frames.timeline;
INCLUDE PERFETTO MODULE android.frames.per_frame_metrics;
INCLUDE PERFETTO MODULE slices.with_context;
INCLUDE PERFETTO MODULE intervals.intersect;

-- One row per (late frame, thread that worked on it).
CREATE OR REPLACE PERFETTO TABLE _triage_late_frame_threads AS
SELECT row_number() OVER (ORDER BY ts, utid) AS id, ts, dur, utid
FROM (
  SELECT f.frame_id, f.ts, f.dur, f.ui_thread_utid AS utid
  FROM android_frames f
  JOIN android_frame_stats st USING(frame_id)
  WHERE st.overrun > 0 AND f.process_name GLOB '__PKG__'
  UNION
  SELECT f.frame_id, f.ts, f.dur, f.render_thread_utid AS utid
  FROM android_frames f
  JOIN android_frame_stats st USING(frame_id)
  WHERE st.overrun > 0 AND f.process_name GLOB '__PKG__'
)
WHERE utid IS NOT NULL AND dur > 0;

-- Slices on those threads. Instant slices get a 1 ns width so they can be
-- matched; their real duration is what gets summed. Unfinished slices
-- (negative duration) are left out.
CREATE OR REPLACE PERFETTO TABLE _triage_frame_thread_slices AS
SELECT ts.id, ts.ts, max(ts.dur, 1) AS dur, ts.utid, ts.name, ts.dur AS real_dur
FROM thread_slice ts
WHERE ts.dur >= 0
  AND ts.utid IN (SELECT utid FROM _triage_late_frame_threads);

SELECT
  s.name,
  count(*)                 AS n,
  sum(s.real_dur) / 1e6    AS total_ms,
  max(s.real_dur) / 1e6    AS max_ms
FROM _interval_intersect!(
       (_triage_late_frame_threads, _triage_frame_thread_slices), (utid)) ii
JOIN _triage_late_frame_threads f ON f.id = ii.id_0
JOIN _triage_frame_thread_slices s ON s.id = ii.id_1
WHERE s.ts >= f.ts
  AND s.ts < f.ts + f.dur
GROUP BY 1
ORDER BY total_ms DESC
LIMIT 25;
