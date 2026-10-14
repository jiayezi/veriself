SELECT
    subject_id,
    max(date_key) AS date_key,
    avg(round(discretionary_spending / nullif(spending, 0), 4)) AS value
FROM evt_daily
GROUP BY
    subject_id,
    date_trunc('month', strptime(CAST(date_key AS VARCHAR), '%Y%m%d'))
