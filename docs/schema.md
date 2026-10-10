# Data schemas

Reference for coding against Garmin data. Built by profiling real exports. This file
contains structure and observed behaviour only, never personal values.

Last profiled: 2026-10-10, over a 90-day window (2026-07-12 → 2026-10-10):
- `data/activities.csv`: 31 rows (18 `running` + 13 `strength_training`; 1 `hiking` and
  1 `paddelball` were filtered out)
- `data/daily.csv`: 70 of 91 days
- `data/runs.csv` / `run_laps.csv` / `run_km_splits.csv`: 18 runs, 177 laps, 159 km splits
- `data/vo2max.csv`: 15 dated values

## Database overview

`data/garmin.duckdb` (`GARMIN_DB_PATH`). DDL in `src/garmin_mcp/schema.sql` (the single
source of truth for types). Write helpers in `src/garmin_mcp/db.py`:

- **`merge`** (`INSERT OR REPLACE` on the primary key) for `activities`, `daily` and
  `vo2max`. Re-fetching overlapping ranges updates rows and never duplicates them. Rows
  outside the batch are kept.
- **`replace`** (delete-all + insert in one transaction) for `runs`, `run_laps`,
  `run_km_splits` and `run_fetch_log`, which are rebuilt from the raw run cache.

Relationships: `runs`, `run_laps`, `run_km_splits` and `run_fetch_log` → `activities`
on `activity_id`. `daily` and `vo2max` are keyed by date (join activities on
`activity_date`, and use `ASOF JOIN` for VO2 max).

Writers keep connections short, because DuckDB allows one read-write process at a time.

---

## `activities.csv`

Produced by `uv run garmin-fetch` (`src/garmin_mcp/fetch_activities.py`). One row per
activity, sorted ascending by `start_time_local`. Source is the Garmin activity-list
endpoint (`get_activities_by_date`).

| Column | Garmin key | CSV type | Unit | Running | Strength | Notes |
|---|---|---|---|:-:|:-:|---|
| `activity_id` | `activityId` | int | — | ✓ | ✓ | Unique. Natural key. |
| `activity_type` | `activityType.typeKey` | str | — | ✓ | ✓ | Observed: `running`, `strength_training`. Fetch also accepts the other run types (see below). |
| `activity_name` | `activityName` | str | — | ✓ | ✓ | Runs: Garmin auto-name, `"<location_name> Running"`. Strength: always a generic name. |
| `location_name` | `locationName` | str | — | ✓ | ∅ | City/area. Empty for strength (indoor). |
| `start_lat` | `startLatitude` | float | deg | ✓ | ∅ | Up to 15 decimals. |
| `start_lon` | `startLongitude` | float | deg | ✓ | ∅ | |
| `end_lat` | `endLatitude` | float | deg | ✓ | ∅ | |
| `end_lon` | `endLongitude` | float | deg | ✓ | ∅ | |
| `start_time_local` | `startTimeLocal` | ts | local wall clock | ✓ | ✓ | Format `YYYY-MM-DD HH:MM:SS`, no offset. |
| `start_time_gmt` | `startTimeGMT` | ts | UTC | ✓ | ✓ | Same format. |
| `duration_s` | `duration` | float | s | ✓ | ✓ | Elapsed time. Up to 13 decimals. |
| `moving_duration_s` | `movingDuration` | float | s | ✓ | ✓ | Strength: always equals `duration_s`. |
| `distance_m` | `distance` | float | m | ✓ | **0.0** | Strength sessions report `0.0`, not empty. |
| `avg_hr_bpm` | `averageHR` | float | bpm | ✓ | ✓ | Always a whole number (`150.0`). |
| `max_hr_bpm` | `maxHR` | float | bpm | ✓ | ✓ | Always a whole number. |
| `elevation_gain_m` | `elevationGain` | float | m | ✓ | ∅ | 1 decimal. |
| `calories_kcal` | `calories` | float | kcal | ✓ | ✓ | Always a whole number. |
| `avg_cadence_spm` | `averageRunningCadenceInStepsPerMinute` | float | steps/min | ✓ | ∅ | Up to 6 decimals. |
| `aerobic_training_effect` | `aerobicTrainingEffect` | float | 0.0–5.0 | ✓ | ✓ | Up to 16 decimals. |
| `anaerobic_training_effect` | `anaerobicTrainingEffect` | float | 0.0–5.0 | ✓ | ✓ | Up to 17 decimals. |
| `training_load` | `activityTrainingLoad` | float | Garmin load units | ✓ | ✓ | Not a whole number. Input for future ATL/CTL. |
| `total_sets` | `totalSets` | int | count | ∅ | ✓ | |
| `active_sets` | `activeSets` | int | count | ∅ | ✓ | Equal to `total_sets` in every row seen so far. |
| `total_reps` | `totalReps` | int | count | ∅ | ✓ | |
| `avg_pace_s_per_km` | *derived* | float | s/km | ✓ | ∅ | `moving_duration_s / (distance_m / 1000)`, rounded to 1 decimal. Runs only. |

✓ = always filled for this type · ∅ = always empty for this type

Run types accepted by the fetch filter (`RUN_TYPES`): `running`, `trail_running`,
`treadmill_running`, `track_running`, `indoor_running`, `street_running`, `virtual_run`,
`ultra_run`. Only `running` has been seen so far.

### Things the code must handle

1. **Multiple timezones.** Local−UTC offsets seen: +2h (27 rows), −7h (3), +1h (1), so you
   were travelling. Derive `activity_date` from `start_time_local`, not from UTC. Never
   assume one fixed offset.
2. **More than one activity per day** (2 days so far). Never key activities by date; use
   `activity_id`.
3. **Strength `distance_m = 0.0`.** Treat 0 as missing (NULL) for non-distance types, or
   averages of distance will be pulled down.
4. **Coordinates exist only for outdoor runs.** Treadmill or indoor runs will have no
   coordinates or `location_name`, and no `elevation_gain_m`.
5. **Spurious float precision.** Garmin returns values like `3000.0000000001`. Round
   during normalization (durations to 0.1 s, training effect to 1 decimal, load to
   1 decimal).
6. **Whole-number floats.** HR and calories arrive as floats but are integers. Cast them.
7. **`activity_name` duplicates `location_name`** on runs, so it holds little extra
   information.

---

## DuckDB table: `activities`

DDL: `src/garmin_mcp/schema.sql`. Key `activity_id`. **Merged** on every
`garmin-fetch`, so history builds up beyond the CSV's date range.

The columns are the same as `activities.csv`, with these cleaning rules applied
(`fetch_activities.to_db_row`):

| Change | Why |
|---|---|
| `start_time_gmt` → `start_time_utc` | Clearer name. |
| New `activity_date DATE` = local date of `start_time_local` | Activities span several timezones, so never derive dates from UTC. |
| `distance_m` = NULL when 0 for non-run types | Strength sessions report `0.0`, which would distort averages. |
| Durations, training effect, load and cadence rounded to 1 decimal | Removes Garmin's spurious float precision. |
| HR → `SMALLINT`, calories → `INTEGER` | They are whole numbers stored as floats. |

---

## `daily.csv`

Produced by `uv run garmin-fetch-daily` (`src/garmin_mcp/fetch_daily.py`). One row per
calendar date, sorted ascending, dates unique. Days with no data at all (watch not worn)
are dropped.

Sources:
- **Daily summary**: `get_user_summary(date)`, one request per day.
- **Sleep stats**: `get_sleep_daily(start, end)`, one request per 28 days; fields come
  from each row's `values`.
- The HRV endpoint (`get_hrv_data_range`) is **not used**. Its baseline was empty and
  status always `NONE` (it needs about 3 weeks of nightly wear), and the overnight HRV
  values are already in sleep stats. SpO2 and skin temperature were empty every night,
  so they're left out too.

### Coverage (90 days)

| Group | Days filled | Notes |
|---|---|---|
| Summary (steps etc.) | 67 / 91 | |
| Resting HR | 62 / 91 | |
| Min/max HR, body battery | ~66 / 91 | Can exist on days without steps (partial wear). |
| **Sleep / HRV** | **13 / 91** | The watch is rarely worn overnight. |
| `hrv_7d_avg_ms` | 7 / 91 | Needs several recent nights. |

### Columns: daily summary

| Column | Garmin key | Type | Unit | Notes |
|---|---|---|---|---|
| `date` | `calendarDate` | date | — | `YYYY-MM-DD`. Natural key. |
| `wear_s` | `measurableAwakeDuration` + `measurableAsleepDuration` | int | s | Time the watch actually measured you. Empty if 0. **Use to filter partial-wear days.** |
| `steps` | `totalSteps` | int | count | |
| `step_goal` | `dailyStepGoal` | int | count | |
| `distance_m` | `totalDistanceMeters` | int | m | All-day distance (includes activities). |
| `active_kcal` | `activeKilocalories` | float | kcal | |
| `total_kcal` | `totalKilocalories` | float | kcal | Includes BMR. |
| `resting_hr_bpm` | `restingHeartRate` | int | bpm | Unreliable on low-wear days (see below). |
| `resting_hr_7d_avg_bpm` | `lastSevenDaysAvgRestingHeartRate` | int | bpm | Garmin's rolling value. |
| `min_hr_bpm` | `minHeartRate` | int | bpm | |
| `max_hr_bpm` | `maxHeartRate` | int | bpm | |
| `avg_stress` | `averageStressLevel` | int | 0–100 | Negative values (Garmin's "not enough data") are blanked. |
| `max_stress` | `maxStressLevel` | int | 0–100 | Same. |
| `body_battery_high` | `bodyBatteryHighestValue` | int | 0–100 | |
| `body_battery_low` | `bodyBatteryLowestValue` | int | 0–100 | |
| `body_battery_charged` | `bodyBatteryChargedValue` | int | points | |
| `body_battery_drained` | `bodyBatteryDrainedValue` | int | points | |
| `moderate_intensity_min` | `moderateIntensityMinutes` | int | min | 0 is a real value on worn days. |
| `vigorous_intensity_min` | `vigorousIntensityMinutes` | int | min | |
| `floors_ascended` | `floorsAscended` | float | floors | Fractional, up to 5 decimals. |
| `highly_active_s` | `highlyActiveSeconds` | int | s | |
| `active_s` | `activeSeconds` | int | s | |
| `sedentary_s` | `sedentarySeconds` | int | s | |
| `avg_waking_respiration_brpm` | `avgWakingRespirationValue` | float | breaths/min | |

### Columns: sleep (empty on nights without sleep tracking)

| Column | Garmin key (`values.*`) | Type | Unit | Notes |
|---|---|---|---|---|
| `sleep_start_local` | `localSleepStartTimeInMillis` | ts | local wall clock | Converted from "local epoch ms" (wall clock encoded as UTC). |
| `sleep_end_local` | `localSleepEndTimeInMillis` | ts | local wall clock | Its date always equals `date`: sleep belongs to the **wake-up date**. |
| `sleep_total_s` | `totalSleepTimeInSeconds` | int | s | Equals deep + light + REM (verified 13/13). Excludes awake time. |
| `sleep_deep_s` | `deepTime` | int | s | |
| `sleep_light_s` | `lightTime` | int | s | |
| `sleep_rem_s` | `remTime` | int | s | |
| `sleep_awake_s` | `awakeTime` | int | s | |
| `sleep_score` | `sleepScore` | int | 0–100 | |
| `sleep_quality` | `sleepScoreQuality` | str | — | Seen: `EXCELLENT`, `GOOD`, `FAIR` (probably also `POOR`). |
| `sleep_avg_hr_bpm` | `avgHeartRate` | float | bpm | Overnight average. |
| `sleep_avg_respiration_brpm` | `respiration` | float | breaths/min | |
| `sleep_body_battery_change` | `bodyBatteryChange` | int | points | Charge gained overnight. |
| `hrv_overnight_avg_ms` | `avgOvernightHrv` | float | ms | The nightly HRV value to use. |
| `hrv_7d_avg_ms` | `hrv7dAverage` | float | ms | Often empty. |
| `hrv_status` | `hrvStatus` | str | — | Always `NONE` so far (no baseline yet). |

### Things the code must handle

1. **Placeholder data on unworn days.** Garmin returns a step goal, estimated BMR
   calories, zeros for intensity/floors/active seconds, and about 24h of `sedentary_s`
   even with no steps. The fetch blanks these when `steps` is empty. Real sensor readings
   (HR, body battery) are kept, since a watch worn briefly can record them without steps.
2. **Partial-wear days are unreliable.** Every day with resting HR above 90, resting HR
   below that day's minimum, or under 500 steps had **`wear_s` under 4h**. Recommended
   filter for HR/step analysis: `wear_s >= 4*3600`. 26 of 67 step days are under 4h, so
   this is a real trade-off and should be a parameter, not hard-coded.
3. **Travel days are longer than 24h.** One day logs 7h more than 86400 s of activity
   seconds (the UTC+2 → UTC−7 trip). Don't assert that the seconds add up to 24h.
4. **Two sleep timezones** (+2h on 11 nights, −7h on 2). Use the `*_local` columns for
   "what time did I go to bed" questions.
5. **Sleep is sparse** (13 nights). Any sleep↔HRV or sleep↔training analysis has very
   little data. Report `n` alongside every average.

---

## DuckDB table: `daily`

DDL: `src/garmin_mcp/schema.sql`. Key `date`. **Merged** on every
`garmin-fetch-daily`. The columns and cleaning rules are identical to `daily.csv` above.

This is one wide table instead of the four tables in the original plan
(`daily_sleep`/`daily_hrv`/`daily_rhr`). All the data shares the same date key, and a
wide table makes sleep↔HRV↔load questions single-table queries. If needed, it can be
split into separate tables later.

---

## Run detail (`garmin-fetch-runs`)

Produced by `uv run garmin-fetch-runs` (`src/garmin_mcp/fetch_runs.py`). Reads the run
ids from `activities.csv`, so run `garmin-fetch` first.

### Fetch-once design

```
activities.csv ──► pending = runs not in run_fetch_log.csv
                        │  5 API calls per run (activity, splits, hr zones, weather, details)
                        ▼
              data/raw/runs/<id>.json.gz   (atomic write)
                        │  then append to run_fetch_log.csv
                        ▼
   rebuild runs.csv, run_laps.csv, run_km_splits.csv from ALL cached raw files
```

- A run counts as **fully loaded** when it's in `run_fetch_log.csv` **and** its raw file
  exists. Loaded runs are never fetched again. A logged run whose raw file is missing is
  fetched again.
- The log is written only after every required call succeeded and the raw file was
  saved, so a run that fails partway stays pending.
- Weather is the only optional call (it can be missing indoors). On a 429 the script
  stops; other failed runs stay pending for the next run.
- Output CSVs are **rebuilt** from the cache every time, never appended, so they can't get
  duplicates. To change the parsing, edit the code and run `--offline` (no API calls).
- VO2 max is one range request, fetched again on every run since Garmin can revise it.
- The time series is requested with `maxchart=10000`. The default of 2000 downsamples
  long runs; at 10000 the samples are 1 s apart.
- The cache is about 200 KB per run (gzip).

### `run_fetch_log.csv`

| Column | Type | Notes |
|---|---|---|
| `activity_id` | int | Key. |
| `fetched_at_utc` | ts | |
| `n_samples` | int | Time-series sample count. |
| `has_weather` | bool | |

### `runs.csv`: one row per run (source: `get_activity().summaryDTO`, HR zones, weather)

| Column | Unit | Notes |
|---|---|---|
| `activity_id`, `start_time_local` | | Join to `activities.csv`. |
| `distance_m`, `duration_s`, `moving_duration_s`, `elapsed_duration_s` | m, s | `duration` = timer (excludes auto-pause); `elapsed` = wall clock. |
| `avg_pace_s_per_km` | s/km | **Timer-based**, the same as Garmin Connect's "Avg Pace". |
| `avg_moving_pace_s_per_km` | s/km | Moving-time based. Equals `activities.avg_pace_s_per_km` (verified 18/18). |
| `avg_gap_s_per_km` | s/km | Grade-adjusted pace. |
| `best_pace_s_per_km` | s/km | From `maxSpeed`. |
| `avg_hr_bpm`, `max_hr_bpm`, `min_hr_bpm` | bpm | |
| `avg_cadence_spm`, `max_cadence_spm` | steps/min | Total steps (both feet). Equals `activities.avg_cadence_spm`. |
| `avg_power_w`, `max_power_w`, `normalized_power_w` | W | Wrist running power. |
| `elevation_gain_m`, `elevation_loss_m`, `min_elevation_m`, `max_elevation_m` | m | |
| `avg_ground_contact_ms` | ms | |
| `avg_stride_length_cm` | cm | |
| `avg_vertical_oscillation_cm` | cm | |
| `avg_vertical_ratio_pct` | % | |
| `aerobic_training_effect`, `anaerobic_training_effect` | 0–5 | |
| `training_effect_label` | enum | Seen: `AEROBIC_BASE`, `TEMPO`, `LACTATE_THRESHOLD`, `VO2MAX`, `RECOVERY`. |
| `training_load` | load units | |
| `workout_feel` | 0/25/50/75/100 | Very weak … very strong. Self-reported after the run. |
| `workout_rpe` | 1–10 | Garmin stores ×10, divided during parsing. Self-reported. |
| `body_battery_change` | points | |
| `steps` | count | |
| `n_laps` | count | |
| `hr_z1_s` … `hr_z5_s` | s | Time in each HR zone; sums to `duration_s` (verified 18/18). |
| `weather_temp_c`, `weather_apparent_temp_c` | °C | **Garmin returns °F**, converted here. Weather station data, not the watch sensor (which reads about 5°C high from body heat). |
| `weather_humidity_pct` | % | |
| `weather_wind_speed_mph` | mph | Unit undocumented; **assumed** mph because the same payload uses °F. |
| `weather_desc` | str | |

### `run_laps.csv`: one row per lap, as recorded (source: `get_activity_splits().lapDTOs`)

Laps are whatever the watch recorded: auto-lap, button presses, or **workout steps**
(most of these runs are structured workouts: 2–16 laps per run).

| Column | Unit | Notes |
|---|---|---|
| `activity_id`, `lap_index` | | Key: (`activity_id`, `lap_index`). |
| `intensity_type` | enum | Seen: `INTERVAL` (148), `ACTIVE` (25), `RECOVERY` (3), empty (1). |
| `start_time_gmt` | UTC ts | |
| `distance_m`, `duration_s`, `moving_duration_s` | m, s | Lap distances sum to the run distance (verified 18/18). |
| `pace_s_per_km` | s/km | Timer-based. |
| `gap_s_per_km` | s/km | |
| `avg_hr_bpm`, `max_hr_bpm`, `avg_cadence_spm`, `avg_power_w` | | |
| `elevation_gain_m`, `elevation_loss_m` | m | |
| `avg_ground_contact_ms`, `avg_stride_length_cm`, `avg_vertical_oscillation_cm` | | |

### `run_km_splits.csv`: one row per km, computed from the time series

Calculated from `get_activity_details` (1 s samples) by cutting at every 1000 m of
`sumDistance`. The timer time at each km boundary is interpolated linearly between the
two samples on either side.

| Column | Unit | Notes |
|---|---|---|
| `activity_id`, `km` | | Key. `km` starts at 1. |
| `distance_m` | m | 1000, or less for the final partial split. |
| `is_partial` | bool | True for the final, shorter split. |
| `duration_s` | s | Timer (`sumDuration`) difference. |
| `pace_s_per_km` | s/km | `duration_s / km`. |
| `gap_s_per_km` | s/km | Mean of per-second grade-adjusted speed, converted to pace. |
| `avg_hr_bpm`, `max_hr_bpm` | bpm | |
| `avg_cadence_spm` | steps/min | From `directDoubleCadence` (total steps; `directRunCadence` is half of it). |
| `avg_power_w` | W | |
| `elevation_gain_m`, `elevation_loss_m` | m | Changes under 1 m are ignored as noise, with the threshold applied across the whole run. Summed gain is about 1.1× Garmin's own (range 0.9–1.4×); raw per-second differences gave about 3×. |

Notes:
- A trailing piece **under 10 m is dropped** because its pace would be meaningless. So
  split distance and duration add up to the run total except for that tail (4–7 m on 4
  of 18 runs).
- Splits with cadence under 140 are real walking or recovery segments.

### `vo2max.csv` (source: `get_max_metrics_range`, `generic`)

| Column | Notes |
|---|---|
| `date` | Only dates where Garmin updated VO2 max (15 in 90 days), not every day. Use the last known value for a date in between. |
| `vo2max` | Whole-number value shown in the app. |
| `vo2max_precise` | One decimal. Use this one for trends. |
