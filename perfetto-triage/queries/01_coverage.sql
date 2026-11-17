-- Which data sources actually made it into the trace.
-- 0 in a column means you cannot answer that class of question with this trace.
SELECT
  (SELECT count(*) FROM slice)                                            AS slices,
  (SELECT count(*) FROM sched)                                            AS sched_rows,
  (SELECT count(*) FROM actual_frame_timeline_slice)                      AS frametimeline_rows,
  (SELECT count(*) FROM thread_state WHERE blocked_function IS NOT NULL)  AS blocked_reason_rows,
  (SELECT count(*) FROM slice WHERE name GLOB 'binder transaction*')      AS binder_slices,
  (SELECT count(*) FROM process)                                          AS processes,
  (SELECT (max(ts+dur) - min(ts)) / 1e6 FROM slice)                       AS trace_span_ms,
  -- The phone's Android version decides what can be recorded at all: the frame
  -- timeline needs API 31 (Android 12). NULL when the trace does not say.
  (SELECT int_value FROM metadata WHERE name = 'android_sdk_version')     AS android_sdk,
  (SELECT str_value FROM metadata WHERE name = 'android_build_fingerprint') AS build_fingerprint,
  -- Android 10 traces carry neither of the two above. The framework's own
  -- package, "android", has the SDK level as its version code, so the package
  -- list is a second way to read it.
  (SELECT version_code FROM package_list WHERE package_name = 'android' LIMIT 1) AS framework_version;
