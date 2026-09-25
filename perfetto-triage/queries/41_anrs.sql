-- ANRs recorded in the trace, if any.
INCLUDE PERFETTO MODULE android.anrs;
SELECT process_name, pid, ts, subject, anr_type, anr_dur_ms
FROM android_anrs
ORDER BY ts;
