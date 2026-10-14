SELECT
    subject_id,
    date_key,
    CASE
        WHEN resting_hr IS NOT NULL AND hrv IS NOT NULL THEN
            round(100 * (
                0.6 * least(greatest((70 - resting_hr) / 30.0, 0), 1)
                + 0.4 * least(greatest((hrv - 20) / 60.0, 0), 1)
            ), 2)
    END AS value
FROM obs_daily
