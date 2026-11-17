-- How often each stage of a frame ran on the app's main thread. The counts say
-- what kind of work a frame was doing: an animation that ticks in every frame
-- while recomposition runs in few of them is being read while drawing, not
-- while composing.
INCLUDE PERFETTO MODULE slices.with_context;
SELECT
  CASE WHEN s.name GLOB 'Choreographer#doFrame*' THEN 'Choreographer#doFrame' ELSE s.name END AS name,
  count(*)           AS n,
  sum(s.dur) / 1e6   AS total_ms
FROM thread_slice s
WHERE s.process_name GLOB '__PKG__'
  AND s.is_main_thread
  AND (s.name GLOB 'Choreographer#doFrame*'
       OR s.name IN ('animation', 'Recomposer:animation', 'Compose:recompose',
                     'Recomposer:recompose', 'Compose:applyChanges', 'traversal', 'measure',
                     'layout', 'draw', 'AndroidOwner:onMeasure', 'AndroidOwner:onLayout',
                     'Record View#draw()', 'input'))
GROUP BY 1
ORDER BY n DESC;
