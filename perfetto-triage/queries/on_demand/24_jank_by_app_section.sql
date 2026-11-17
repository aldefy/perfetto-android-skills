-- Which of the app's own trace sections were running when frames were late.
-- A late frame counts for a section when the two overlap in time in the same
-- process. Overlap is not blame: a section that is simply long overlaps more
-- frames. Read this as where to look first.

-- Run only with --app-repo. The section filter below is filled in from the
-- names the app passes to its tracing calls.
INCLUDE PERFETTO MODULE android.frames.timeline;
INCLUDE PERFETTO MODULE android.frames.per_frame_metrics;
INCLUDE PERFETTO MODULE slices.with_context;
INCLUDE PERFETTO MODULE intervals.intersect;

CREATE OR REPLACE PERFETTO TABLE _triage_app_sections AS
SELECT row_number() OVER (ORDER BY ts, name) AS id, ts, max(dur, 1) AS dur, upid, name, dur AS real_dur
FROM (
  SELECT s.ts, s.dur, s.upid, s.name FROM thread_slice s
  WHERE s.dur >= 0 AND s.upid IS NOT NULL AND s.process_name GLOB '__PKG__'
    AND (__SECTION_MATCH__)
  UNION ALL
  SELECT s.ts, s.dur, s.upid, s.name FROM process_slice s
  WHERE s.dur >= 0 AND s.upid IS NOT NULL AND s.process_name GLOB '__PKG__'
    AND (__SECTION_MATCH__)
);

CREATE OR REPLACE PERFETTO TABLE _triage_late_frames AS
SELECT row_number() OVER (ORDER BY f.ts, f.frame_id) AS id, f.ts, f.dur, f.upid, st.overrun
FROM android_frames f
JOIN android_frame_stats st USING(frame_id)
WHERE st.overrun > 0 AND f.dur > 0 AND f.upid IS NOT NULL
  AND f.process_name GLOB '__PKG__';

CREATE OR REPLACE PERFETTO TABLE _triage_section_hits AS
SELECT DISTINCT s.name, f.id AS frame, f.overrun
FROM _interval_intersect!((_triage_late_frames, _triage_app_sections), (upid)) ii
JOIN _triage_late_frames f ON f.id = ii.id_0
JOIN _triage_app_sections s ON s.id = ii.id_1;

SELECT
  t.name                                          AS section,
  t.n                                             AS times_run,
  t.total_ms,
  ifnull(h.late_frames, 0)                        AS late_frames,
  (SELECT count(*) FROM _triage_late_frames)      AS all_late_frames,
  (SELECT count(DISTINCT frame) FROM _triage_section_hits) AS late_frames_in_any_section,
  ifnull(h.worst_overrun_ms, 0)                   AS worst_overrun_ms
FROM (SELECT name, count(*) AS n, sum(real_dur) / 1e6 AS total_ms
      FROM _triage_app_sections GROUP BY 1) t
LEFT JOIN (SELECT name, count(*) AS late_frames, max(overrun) / 1e6 AS worst_overrun_ms
           FROM _triage_section_hits GROUP BY 1) h USING(name)
ORDER BY late_frames DESC, t.total_ms DESC
LIMIT 40;
