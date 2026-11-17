-- The first question a senior engineer asks: during startup, was the main
-- thread RUNNING or WAITING? Running -> your code is slow. Sleeping/blocked ->
-- you are waiting on IPC, IO, or a lock, and optimising your code is pointless.
--
-- On older Android (seen on API 24 and on Android 10) the stdlib cannot tie a
-- launch to its process, because it looks for an activityResume slice those
-- versions do not write. Then the main thread of the process named after the
-- package is used instead, over the same launch window.
INCLUDE PERFETTO MODULE android.startup.startups;
INCLUDE PERFETTO MODULE sched.states;
WITH main AS (
  SELECT st.startup_id, st.ts, st.dur, st.utid
  FROM android_startup_threads st
  JOIN android_startups s USING(startup_id)
  WHERE st.is_main_thread AND s.package GLOB '__PKG__'
  UNION ALL
  SELECT s.startup_id, s.ts, s.dur, t.utid
  FROM android_startups s
  JOIN process p ON p.name = s.package
  JOIN thread t ON t.upid = p.upid AND t.is_main_thread
  WHERE s.package GLOB '__PKG__'
    AND NOT EXISTS (SELECT 1 FROM android_startup_processes x WHERE x.startup_id = s.startup_id)
)
SELECT
  st.startup_id,
  sched_state_io_to_human_readable_string(ts.state, ts.io_wait) AS main_thread_state,
  sum(min(ts.ts + ts.dur, st.ts + st.dur) - max(ts.ts, st.ts)) / 1e6 AS ms
FROM main st
JOIN thread_state ts ON ts.utid = st.utid
WHERE ts.ts < st.ts + st.dur
  AND ts.ts + ts.dur > st.ts
GROUP BY 1, 2
ORDER BY st.startup_id, ms DESC;
