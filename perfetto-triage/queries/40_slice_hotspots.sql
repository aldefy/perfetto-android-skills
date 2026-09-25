-- Whole-process hotspot ranking by SELF time (own cost, excluding children).
-- Sorting by total_ms lies: a wrapper slice inherits everything below it.
INCLUDE PERFETTO MODULE slices.with_context;
SELECT
  s.name,
  s.thread_name,
  count(*)                                        AS n,
  sum(s.dur) / 1e6                                AS total_ms,
  sum(s.dur - ifnull(c.child_dur, 0)) / 1e6       AS self_ms
FROM thread_slice s
LEFT JOIN (
  SELECT parent_id, sum(dur) AS child_dur
  FROM slice WHERE parent_id IS NOT NULL GROUP BY parent_id
) c ON c.parent_id = s.id
WHERE s.process_name GLOB '__PKG__'
GROUP BY 1, 2
ORDER BY self_ms DESC
LIMIT 30;
