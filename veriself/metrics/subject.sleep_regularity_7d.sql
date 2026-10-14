SELECT
    subject_id,
    date_key,
    round(greatest(0, least(100, 100 - 40 * stddev_pop(value) OVER (
        PARTITION BY subject_id
        ORDER BY date_key
        ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
    ))), 2) AS value
FROM fact_metric_value
WHERE metric_id = 'subject.sleep_duration_daily'
  AND metric_version = 1
  AND valid_to IS NULL
