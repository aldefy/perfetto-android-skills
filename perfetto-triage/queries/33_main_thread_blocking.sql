-- Whole-trace answer to "running or waiting". blocked_function is the kernel
-- function the thread was stuck in (needs sched/sched_blocked_reason ftrace).
INCLUDE PERFETTO MODULE sched.states;
SELECT
  p.name AS process_name,
  sched_state_io_to_human_readable_string(ts.state, ts.io_wait) AS state,
  ifnull(ts.blocked_function, '-') AS blocked_function,
  count(*)           AS n,
  sum(ts.dur) / 1e6  AS total_ms
FROM thread_state ts
JOIN thread t USING(utid)
JOIN process p USING(upid)
WHERE t.is_main_thread
  AND p.name GLOB '__PKG__'
GROUP BY 1, 2, 3
ORDER BY total_ms DESC
LIMIT 25;
