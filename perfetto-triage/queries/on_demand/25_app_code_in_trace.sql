-- Which of the app's own classes, files and trace sections show up in this
-- trace at all, not only in the top rankings. This is how the code audit knows
-- which screens ran: an Activity in a lifecycle slice, a composable named by
-- composition tracing, a class being loaded, a section the app declares.

-- Run only with --app-repo. The name filter below is filled in from the
-- packages the app's source declares and the names it passes to tracing calls.
INCLUDE PERFETTO MODULE slices.with_context;
SELECT
  s.name,
  count(*)           AS n,
  sum(s.dur) / 1e6   AS total_ms,
  min(s.ts)          AS first_ts
FROM thread_slice s
WHERE s.process_name GLOB '__PKG__'
  AND (__NAME_MATCH__)
GROUP BY 1
ORDER BY total_ms DESC
LIMIT 600;
