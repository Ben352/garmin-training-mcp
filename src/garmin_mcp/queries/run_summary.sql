-- One run's summary, with the latest VO2 max on or before the run date.
-- Params: activity_id
SELECT
    r.activity_id,
    a.activity_name,
    a.location_name,
    a.activity_date,
    r.start_time_local,
    round(r.distance_m / 1000, 2)          AS distance_km,
    round(r.duration_s / 60, 1)            AS duration_min,
    round(r.moving_duration_s / 60, 1)     AS moving_duration_min,
    round(r.elapsed_duration_s / 60, 1)    AS elapsed_duration_min,
    r.avg_pace_s_per_km,
    r.avg_moving_pace_s_per_km,
    r.avg_gap_s_per_km,
    r.best_pace_s_per_km,
    r.avg_hr_bpm, r.max_hr_bpm, r.min_hr_bpm,
    r.avg_cadence_spm, r.max_cadence_spm,
    r.avg_power_w, r.max_power_w, r.normalized_power_w,
    r.elevation_gain_m, r.elevation_loss_m, r.min_elevation_m, r.max_elevation_m,
    r.avg_ground_contact_ms, r.avg_stride_length_cm,
    r.avg_vertical_oscillation_cm, r.avg_vertical_ratio_pct,
    r.aerobic_training_effect, r.anaerobic_training_effect,
    r.training_effect_label, r.training_load,
    r.workout_feel, r.workout_rpe,
    r.body_battery_change, r.steps, r.n_laps,
    v.vo2max_precise                      AS vo2max_at_run,
    v.date                                AS vo2max_as_of
FROM runs r
JOIN activities a USING (activity_id)
ASOF LEFT JOIN vo2max v ON a.activity_date >= v.date
WHERE r.activity_id = ?
