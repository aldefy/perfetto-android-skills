-- Which data sources actually made it into the trace.
-- 0 in a column means you cannot answer that class of question with this trace.
SELECT
  (SELECT count(*) FROM slice)                                            AS slices,
  (SELECT count(*) FROM sched)                                            AS sched_rows,
  (SELECT count(*) FROM actual_frame_timeline_slice)                      AS frametimeline_rows,
  (SELECT count(*) FROM thread_state WHERE blocked_function IS NOT NULL)  AS blocked_reason_rows,
  (SELECT count(*) FROM slice WHERE name GLOB 'binder transaction*')      AS binder_slices,
  (SELECT count(*) FROM process)                                          AS processes,
  (SELECT (max(ts+dur) - min(ts)) / 1e6 FROM slice)                       AS trace_span_ms;
