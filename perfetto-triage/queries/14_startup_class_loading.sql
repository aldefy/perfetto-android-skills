-- Class loading + verification during startup. This is the number a Baseline
-- Profile moves. High count with high ms == the profile is missing or stale.
INCLUDE PERFETTO MODULE android.startup.startups;
SELECT c.startup_id, s.package, count(*) AS classes_loaded, sum(c.slice_dur)/1e6 AS ms
FROM android_class_loading_for_startup c
JOIN android_startups s USING(startup_id)
WHERE s.package GLOB '__PKG__'
GROUP BY 1, 2
ORDER BY ms DESC;
