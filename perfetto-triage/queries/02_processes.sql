-- Who is in this trace and who is doing the work. Find your package here.
SELECT p.pid, p.name AS process_name, count(s.id) AS slices, sum(s.dur)/1e6 AS slice_ms
FROM process p
JOIN thread t USING(upid)
JOIN thread_track tt ON tt.utid = t.utid
JOIN slice s ON s.track_id = tt.id
GROUP BY p.upid
ORDER BY slices DESC
LIMIT 25;
