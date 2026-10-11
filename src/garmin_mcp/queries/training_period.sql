-- Training totals for a date range. Paces are distance-weighted, HR time-weighted.
-- Params: run_types (list of activity_type values that count as runs), start, end
WITH a AS (
    SELECT *, list_contains(?, activity_type) AS is_run
    FROM activities
    WHERE activity_date BETWEEN ? AND ?
)
SELECT
    count(*) FILTER (WHERE is_run)                                     AS runs,
    round(coalesce(sum(distance_m) FILTER (WHERE is_run), 0) / 1000, 1) AS run_km,
    round(sum(moving_duration_s) FILTER (WHERE is_run)
        / nullif(sum(distance_m) FILTER (WHERE is_run), 0) * 1000, 1)  AS run_avg_pace_s_per_km,
    round(sum(avg_hr_bpm * duration_s) FILTER (WHERE is_run)
        / nullif(sum(duration_s) FILTER (WHERE is_run AND avg_hr_bpm IS NOT NULL), 0), 1)
                                                                       AS run_avg_hr_bpm,
    max(distance_m) FILTER (WHERE is_run) / 1000                       AS longest_run_km,
    count(*) FILTER (WHERE activity_type = 'strength_training')        AS strength_sessions,
    round(coalesce(sum(training_load), 0), 1)                          AS training_load_total
FROM a
