SELECT
    u.subject_id,
    u.date_key,
    u.value - s.sleep_need_h AS value
FROM fact_metric_value AS u
LEFT JOIN subject_asof AS s
    ON s.subject_id = u.subject_id
   AND CAST(strptime(CAST(u.date_key AS VARCHAR), '%Y%m%d') AS TIMESTAMP) >= s.valid_from
   AND (
        s.valid_to IS NULL
        OR CAST(strptime(CAST(u.date_key AS VARCHAR), '%Y%m%d') AS TIMESTAMP) < s.valid_to
   )
WHERE u.metric_id = 'subject.sleep_duration_daily'
  AND u.metric_version = 1
  AND u.valid_to IS NULL
