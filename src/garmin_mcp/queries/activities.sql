-- Activities in a date range, newest first. GPS columns deliberately left out.
-- Params: start, end, activity_type (or NULL), activity_type (again), limit
SELECT
    activity_id,
    activity_type,
    activity_name,
    location_name,
    activity_date,
    start_time_local,
    round(distance_m / 1000, 2)        AS distance_km,
    round(duration_s / 60, 1)          AS duration_min,
    avg_pace_s_per_km,
    avg_hr_bpm,
    max_hr_bpm,
    elevation_gain_m,
    calories_kcal,
    avg_cadence_spm,
    aerobic_training_effect,
    anaerobic_training_effect,
    training_load,
    total_sets,
    total_reps
FROM activities
WHERE activity_date BETWEEN ? AND ?
  AND (? IS NULL OR activity_type = ?)
ORDER BY start_time_local DESC
LIMIT ?
