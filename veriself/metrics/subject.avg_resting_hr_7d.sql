SELECT
    subject_id,
    date_key,
    avg(resting_hr) OVER (
        PARTITION BY subject_id
        ORDER BY date_key
        ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
    ) AS value
FROM obs_daily
