SELECT
    subject_id,
    date_key,
    round(greatest(0, least(100, focus_score)), 2) AS value
FROM obs_daily
