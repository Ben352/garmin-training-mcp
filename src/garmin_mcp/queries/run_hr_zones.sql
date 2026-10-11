-- Time in each HR zone for one run, as rows with % of the run.
-- Params: activity_id
SELECT zone, seconds, round(seconds / 60, 1) AS minutes,
       round(100 * seconds / nullif(sum(seconds) OVER (), 0), 1) AS pct
FROM (
    UNPIVOT (SELECT hr_z1_s, hr_z2_s, hr_z3_s, hr_z4_s, hr_z5_s FROM runs WHERE activity_id = ?)
    ON hr_z1_s, hr_z2_s, hr_z3_s, hr_z4_s, hr_z5_s INTO NAME zone_col VALUE seconds
)
CROSS JOIN LATERAL (SELECT CAST(substr(zone_col, 5, 1) AS INTEGER) AS zone)
ORDER BY zone
