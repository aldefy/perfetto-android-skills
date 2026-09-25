-- Is this trace even usable? Any non-zero row here means the data source was
-- missing or the buffer dropped data, and every number downstream is a lie.
SELECT name, value FROM stats
WHERE value > 0 AND (severity = 'error' OR severity = 'data_loss')
ORDER BY value DESC;
