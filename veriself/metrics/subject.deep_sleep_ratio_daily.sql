SELECT
    subject_id,
    date_key,
    round(deep_sleep_hours / nullif(sleep_hours, 0), 4) AS value
FROM obs_daily
