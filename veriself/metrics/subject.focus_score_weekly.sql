SELECT
    subject_id,
    max(date_key) AS date_key,
    avg(value) AS value
FROM fact_metric_value
WHERE metric_id = 'subject.focus_score_daily'
  AND metric_version = 2
  AND valid_to IS NULL
GROUP BY
    subject_id,
    date_trunc('week', strptime(CAST(date_key AS VARCHAR), '%Y%m%d'))
