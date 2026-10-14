SELECT
    subject_id,
    max(date_key) AS date_key,
    sum(value) AS value
FROM fact_metric_value
WHERE metric_id = 'subject.spending_daily'
  AND metric_version = 1
  AND valid_to IS NULL
GROUP BY
    subject_id,
    date_trunc('month', strptime(CAST(date_key AS VARCHAR), '%Y%m%d'))
