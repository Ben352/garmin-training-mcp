-- DuckDB schema. Column meanings, units and caveats: docs/schema.md.
--
-- Merged on their key every fetch (history builds up): activities, daily, vo2max
-- Replaced from the raw run cache every fetch: runs, run_laps, run_km_splits, run_fetch_log

CREATE TABLE IF NOT EXISTS activities (
    activity_id               BIGINT PRIMARY KEY,
    activity_type             VARCHAR NOT NULL,
    activity_name             VARCHAR,
    location_name             VARCHAR,
    start_lat                 DOUBLE,
    start_lon                 DOUBLE,
    end_lat                   DOUBLE,
    end_lon                   DOUBLE,
    activity_date             DATE NOT NULL,     -- local date of start_time_local
    start_time_local          TIMESTAMP NOT NULL,
    start_time_utc            TIMESTAMP NOT NULL,
    duration_s                DOUBLE,
    moving_duration_s         DOUBLE,
    distance_m                DOUBLE,            -- NULL (not 0) for non-distance activities
    avg_hr_bpm                SMALLINT,
    max_hr_bpm                SMALLINT,
    elevation_gain_m          DOUBLE,
    calories_kcal             INTEGER,
    avg_cadence_spm           DOUBLE,
    aerobic_training_effect   DOUBLE,
    anaerobic_training_effect DOUBLE,
    training_load             DOUBLE,
    total_sets                SMALLINT,
    active_sets               SMALLINT,
    total_reps                SMALLINT,
    avg_pace_s_per_km         DOUBLE             -- moving-time based, runs only
);

CREATE TABLE IF NOT EXISTS daily (
    date                        DATE PRIMARY KEY,
    wear_s                      INTEGER,
    steps                       INTEGER,
    step_goal                   INTEGER,
    distance_m                  INTEGER,
    active_kcal                 DOUBLE,
    total_kcal                  DOUBLE,
    resting_hr_bpm              SMALLINT,
    resting_hr_7d_avg_bpm       SMALLINT,
    min_hr_bpm                  SMALLINT,
    max_hr_bpm                  SMALLINT,
    avg_stress                  SMALLINT,
    max_stress                  SMALLINT,
    body_battery_high           SMALLINT,
    body_battery_low            SMALLINT,
    body_battery_charged        SMALLINT,
    body_battery_drained        SMALLINT,
    moderate_intensity_min      SMALLINT,
    vigorous_intensity_min      SMALLINT,
    floors_ascended             DOUBLE,
    highly_active_s             INTEGER,
    active_s                    INTEGER,
    sedentary_s                 INTEGER,
    avg_waking_respiration_brpm DOUBLE,
    sleep_start_local           TIMESTAMP,
    sleep_end_local             TIMESTAMP,
    sleep_total_s               INTEGER,
    sleep_deep_s                INTEGER,
    sleep_light_s               INTEGER,
    sleep_rem_s                 INTEGER,
    sleep_awake_s               INTEGER,
    sleep_score                 SMALLINT,
    sleep_quality               VARCHAR,
    sleep_avg_hr_bpm            DOUBLE,
    sleep_avg_respiration_brpm  DOUBLE,
    sleep_body_battery_change   SMALLINT,
    hrv_overnight_avg_ms        DOUBLE,
    hrv_7d_avg_ms               DOUBLE,
    hrv_status                  VARCHAR
);

CREATE TABLE IF NOT EXISTS vo2max (
    date           DATE PRIMARY KEY,  -- only dates Garmin updated it; use ASOF JOIN for others
    vo2max         DOUBLE,
    vo2max_precise DOUBLE
);

CREATE TABLE IF NOT EXISTS runs (
    activity_id                 BIGINT PRIMARY KEY,
    start_time_local            TIMESTAMP,
    distance_m                  DOUBLE,
    duration_s                  DOUBLE,
    moving_duration_s           DOUBLE,
    elapsed_duration_s          DOUBLE,
    avg_pace_s_per_km           DOUBLE,   -- timer based (Garmin Connect "Avg Pace")
    avg_moving_pace_s_per_km    DOUBLE,
    avg_gap_s_per_km            DOUBLE,
    best_pace_s_per_km          DOUBLE,
    avg_hr_bpm                  SMALLINT,
    max_hr_bpm                  SMALLINT,
    min_hr_bpm                  SMALLINT,
    avg_cadence_spm             DOUBLE,
    max_cadence_spm             DOUBLE,
    avg_power_w                 SMALLINT,
    max_power_w                 SMALLINT,
    normalized_power_w          SMALLINT,
    elevation_gain_m            DOUBLE,
    elevation_loss_m            DOUBLE,
    min_elevation_m             DOUBLE,
    max_elevation_m             DOUBLE,
    avg_ground_contact_ms       DOUBLE,
    avg_stride_length_cm        DOUBLE,
    avg_vertical_oscillation_cm DOUBLE,
    avg_vertical_ratio_pct      DOUBLE,
    aerobic_training_effect     DOUBLE,
    anaerobic_training_effect   DOUBLE,
    training_effect_label       VARCHAR,
    training_load               DOUBLE,
    workout_feel                SMALLINT,
    workout_rpe                 DOUBLE,
    body_battery_change         SMALLINT,
    steps                       INTEGER,
    n_laps                      SMALLINT,
    hr_z1_s                     INTEGER,
    hr_z2_s                     INTEGER,
    hr_z3_s                     INTEGER,
    hr_z4_s                     INTEGER,
    hr_z5_s                     INTEGER,
    weather_temp_c              DOUBLE,
    weather_apparent_temp_c     DOUBLE,
    weather_humidity_pct        DOUBLE,
    weather_wind_speed_mph      DOUBLE,
    weather_desc                VARCHAR
);

CREATE TABLE IF NOT EXISTS run_laps (
    activity_id                 BIGINT,
    lap_index                   SMALLINT,
    intensity_type              VARCHAR,
    start_time_gmt              TIMESTAMP,
    distance_m                  DOUBLE,
    duration_s                  DOUBLE,
    moving_duration_s           DOUBLE,
    pace_s_per_km               DOUBLE,
    gap_s_per_km                DOUBLE,
    avg_hr_bpm                  SMALLINT,
    max_hr_bpm                  SMALLINT,
    avg_cadence_spm             DOUBLE,
    avg_power_w                 SMALLINT,
    elevation_gain_m            DOUBLE,
    elevation_loss_m            DOUBLE,
    avg_ground_contact_ms       DOUBLE,
    avg_stride_length_cm        DOUBLE,
    avg_vertical_oscillation_cm DOUBLE,
    PRIMARY KEY (activity_id, lap_index)
);

CREATE TABLE IF NOT EXISTS run_km_splits (
    activity_id      BIGINT,
    km               SMALLINT,
    distance_m       DOUBLE,
    is_partial       BOOLEAN,
    duration_s       DOUBLE,
    pace_s_per_km    DOUBLE,
    gap_s_per_km     DOUBLE,
    avg_hr_bpm       SMALLINT,
    max_hr_bpm       SMALLINT,
    avg_cadence_spm  DOUBLE,
    avg_power_w      SMALLINT,
    elevation_gain_m DOUBLE,
    elevation_loss_m DOUBLE,
    PRIMARY KEY (activity_id, km)
);

CREATE TABLE IF NOT EXISTS run_fetch_log (
    activity_id    BIGINT PRIMARY KEY,
    fetched_at_utc TIMESTAMP,
    n_samples      INTEGER,
    has_weather    BOOLEAN
);
