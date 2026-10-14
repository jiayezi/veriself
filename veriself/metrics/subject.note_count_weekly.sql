SELECT
    subject_id,
    max(date_key) AS date_key,
    sum(note_count) AS value
FROM evt_daily
GROUP BY
    subject_id,
    date_trunc('week', strptime(CAST(date_key AS VARCHAR), '%Y%m%d'))
