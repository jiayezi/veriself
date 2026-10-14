SELECT
    subject_id,
    date_key,
    round(greatest(0, least(100, 100 * (focus_score - 40) / 50.0)), 2) AS value
FROM obs_daily
