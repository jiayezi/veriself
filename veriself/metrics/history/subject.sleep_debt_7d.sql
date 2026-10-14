SELECT
    subject_id,
    date_key,
    sum(greatest(-value, 0)) OVER (
        PARTITION BY subject_id
        ORDER BY date_key
        ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
    ) AS value
FROM fact_metric_value
WHERE metric_id = 'subject.sleep_need_deviation_daily'
  AND metric_version = 1
  AND valid_to IS NULL
