from garmin_mcp.fetch_daily import COLUMNS, build_rows, local_millis_to_str

SUMMARY = {"totalSteps": 9000, "restingHeartRate": 52, "minHeartRate": 45}
# What Garmin actually returns for a day the watch wasn't worn.
NOT_WORN = {
    "totalSteps": None,
    "dailyStepGoal": 8000,
    "activeKilocalories": 0.0,
    "totalKilocalories": 1900.0,
    "averageStressLevel": -1,
    "moderateIntensityMinutes": 0,
    "floorsAscended": 0.0,
    "sedentarySeconds": 86400,
}
SLEEP = {
    "totalSleepTimeInSeconds": 27000,
    "sleepScore": 81,
    "avgOvernightHrv": 61.0,
    # 2026-10-02 06:30 local wall-clock, encoded as if UTC
    "localSleepEndTimeInMillis": 1790922600000,
}


def test_local_millis_are_wall_clock_not_converted():
    assert local_millis_to_str(1790922600000) == "2026-10-02 06:30:00"
    assert local_millis_to_str(None) is None


def test_joins_summary_and_sleep_by_date():
    rows = build_rows({"2026-10-02": SUMMARY}, {"2026-10-02": SLEEP})
    assert len(rows) == 1
    row = rows[0]
    assert list(row) == COLUMNS
    assert (row["steps"], row["resting_hr_bpm"]) == (9000, 52)
    assert (row["sleep_total_s"], row["sleep_score"], row["hrv_overnight_avg_ms"]) == (
        27000,
        81,
        61.0,
    )
    assert row["sleep_end_local"] == "2026-10-02 06:30:00"


def test_day_without_sleep_keeps_summary_and_empty_sleep():
    [row] = build_rows({"2026-10-03": SUMMARY}, {})
    assert row["steps"] == 9000
    assert row["sleep_total_s"] is None


def test_days_with_no_data_are_dropped():
    rows = build_rows({"2026-10-01": NOT_WORN, "2026-10-02": SUMMARY}, {})
    assert [r["date"] for r in rows] == ["2026-10-02"]


def test_sleep_only_day_is_kept():
    [row] = build_rows({}, {"2026-10-04": SLEEP})
    assert row["sleep_score"] == 81 and row["steps"] is None


def test_negative_stress_is_treated_as_missing():
    [row] = build_rows(
        {"2026-10-02": SUMMARY | {"averageStressLevel": -2, "maxStressLevel": 60}}, {}
    )
    assert row["avg_stress"] is None
    assert row["max_stress"] == 60


def test_not_worn_placeholders_blanked_but_sensor_readings_kept():
    partial = NOT_WORN | {"minHeartRate": 48, "bodyBatteryHighestValue": 70}
    [row] = build_rows({"2026-10-02": partial}, {})
    assert row["min_hr_bpm"] == 48 and row["body_battery_high"] == 70
    for col in ("step_goal", "total_kcal", "active_kcal", "sedentary_s", "avg_stress"):
        assert row[col] is None


def test_wear_seconds_sum_awake_and_asleep():
    s = SUMMARY | {"measurableAwakeDuration": 50000, "measurableAsleepDuration": 25000}
    [row] = build_rows({"2026-10-02": s}, {})
    assert row["wear_s"] == 75000
    [row] = build_rows({"2026-10-02": SUMMARY}, {})
    assert row["wear_s"] is None


def test_zero_wear_day_is_dropped():
    not_worn = NOT_WORN | {"measurableAwakeDuration": 0, "measurableAsleepDuration": 0}
    assert build_rows({"2026-10-01": not_worn}, {}) == []
