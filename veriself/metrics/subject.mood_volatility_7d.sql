SELECT
    subject_id,
    date_key,
    stddev_pop(value) OVER (
        PARTITION BY subject_id
        ORDER BY date_key
        ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
    ) AS value
FROM fact_metric_value
WHERE metric_id = 'subject.mood_score_daily'
  AND metric_version = 1
  AND valid_to IS NULL
