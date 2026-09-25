-- Where cold-start time actually goes, bucketed by cause.
-- reason values seen in the wild: launch_delay, binder, activity_start,
-- inflate, choreographer_do_frame.
INCLUDE PERFETTO MODULE android.startup.startups;
INCLUDE PERFETTO MODULE android.startup.startup_breakdowns;
SELECT b.startup_id, s.package, b.reason, sum(b.dur)/1e6 AS ms, count(*) AS n
FROM android_startup_opinionated_breakdown b
JOIN android_startups s USING(startup_id)
WHERE s.package GLOB '__PKG__'
GROUP BY 1, 2, 3
ORDER BY b.startup_id, ms DESC;
