-- Weather for one run (all NULL for indoor runs).
-- Params: activity_id
SELECT weather_temp_c AS temp_c, weather_apparent_temp_c AS apparent_temp_c,
       weather_humidity_pct AS humidity_pct, weather_wind_speed_mph AS wind_speed_mph,
       weather_desc AS description
FROM runs WHERE activity_id = ?
