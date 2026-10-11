-- Per-km splits for one run.
-- Params: activity_id
SELECT km, round(distance_m / 1000, 3) AS distance_km, is_partial,
       duration_s, pace_s_per_km, gap_s_per_km,
       avg_hr_bpm, max_hr_bpm, avg_cadence_spm, avg_power_w,
       elevation_gain_m, elevation_loss_m
FROM run_km_splits WHERE activity_id = ? ORDER BY km
