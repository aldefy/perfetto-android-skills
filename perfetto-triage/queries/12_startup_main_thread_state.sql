-- The first question a senior engineer asks: during startup, was the main
-- thread RUNNING or WAITING? Running -> your code is slow. Sleeping/blocked ->
-- you are waiting on IPC, IO, or a lock, and optimising your code is pointless.
INCLUDE PERFETTO MODULE android.startup.startups;
INCLUDE PERFETTO MODULE sched.states;
SELECT
  st.startup_id,
  sched_state_io_to_human_readable_string(ts.state, ts.io_wait) AS main_thread_state,
  sum(min(ts.ts + ts.dur, st.ts + st.dur) - max(ts.ts, st.ts)) / 1e6 AS ms
FROM android_startup_threads st
JOIN android_startups s USING(startup_id)
JOIN thread_state ts ON ts.utid = st.utid
WHERE st.is_main_thread
  AND s.package GLOB '__PKG__'
  AND ts.ts < st.ts + st.dur
  AND ts.ts + ts.dur > st.ts
GROUP BY 1, 2
ORDER BY st.startup_id, ms DESC;
