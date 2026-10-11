"""Fetch daily wellness stats (steps, heart rate, stress, sleep) and write them to CSV.

Usage:
    uv run garmin-fetch-daily                    # last 90 days
    uv run garmin-fetch-daily --start 2026-01-01 --end 2026-10-10 --out data/daily.csv

One row per calendar date. Sources:
  - daily summary (one request per day): steps, heart rate, stress, body battery
  - sleep stats (one request per 28 days): sleep stages, score, overnight HR/HRV

Sleep is attributed to the date you wake up (Garmin's convention). Dates with no
data at all (watch not worn) are left out.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from garmin_mcp import db
from garmin_mcp.cli import login, parse_args, report_empty_columns, write_csv

# CSV column -> key in the daily summary (get_user_summary).
SUMMARY_FIELDS = {
    "steps": "totalSteps",
    "step_goal": "dailyStepGoal",
    "distance_m": "totalDistanceMeters",
    "active_kcal": "activeKilocalories",
    "total_kcal": "totalKilocalories",
    "resting_hr_bpm": "restingHeartRate",
    "resting_hr_7d_avg_bpm": "lastSevenDaysAvgRestingHeartRate",
    "min_hr_bpm": "minHeartRate",
    "max_hr_bpm": "maxHeartRate",
    "avg_stress": "averageStressLevel",
    "max_stress": "maxStressLevel",
    "body_battery_high": "bodyBatteryHighestValue",
    "body_battery_low": "bodyBatteryLowestValue",
    "body_battery_charged": "bodyBatteryChargedValue",
    "body_battery_drained": "bodyBatteryDrainedValue",
    "moderate_intensity_min": "moderateIntensityMinutes",
    "vigorous_intensity_min": "vigorousIntensityMinutes",
    "floors_ascended": "floorsAscended",
    "highly_active_s": "highlyActiveSeconds",
    "active_s": "activeSeconds",
    "sedentary_s": "sedentarySeconds",
    "avg_waking_respiration_brpm": "avgWakingRespirationValue",
}

PLACEHOLDERS_WHEN_NOT_WORN = (
    "step_goal",
    "distance_m",
    "active_kcal",
    "total_kcal",
    "moderate_intensity_min",
    "vigorous_intensity_min",
    "floors_ascended",
    "highly_active_s",
    "active_s",
    "sedentary_s",
)

# CSV column -> key in sleep stats `values` (get_sleep_daily).
SLEEP_FIELDS = {
    "sleep_total_s": "totalSleepTimeInSeconds",
    "sleep_deep_s": "deepTime",
    "sleep_light_s": "lightTime",
    "sleep_rem_s": "remTime",
    "sleep_awake_s": "awakeTime",
    "sleep_score": "sleepScore",
    "sleep_quality": "sleepScoreQuality",
    "sleep_avg_hr_bpm": "avgHeartRate",
    "sleep_avg_respiration_brpm": "respiration",
    "sleep_body_battery_change": "bodyBatteryChange",
    "hrv_overnight_avg_ms": "avgOvernightHrv",
    "hrv_7d_avg_ms": "hrv7dAverage",
    "hrv_status": "hrvStatus",
}

COLUMNS = [
    "date",
    "wear_s",
    *SUMMARY_FIELDS,
    "sleep_start_local",
    "sleep_end_local",
    *SLEEP_FIELDS,
]


def local_millis_to_str(ms: int | None) -> str | None:
    """Garmin 'local' epoch millis encode local wall-clock time as if it were UTC."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def summary_fields(summary: dict[str, Any]) -> dict[str, Any]:
    """Map the daily summary to columns, blanking Garmin's placeholder values."""
    row = {col: summary.get(key) for col, key in SUMMARY_FIELDS.items()}
    # Seconds the watch actually measured you (awake + asleep), None if zero.
    # Use it to filter out partial-wear days, where resting HR and steps are unreliable.
    awake, asleep = (
        summary.get("measurableAwakeDuration"),
        summary.get("measurableAsleepDuration"),
    )
    row["wear_s"] = (awake or 0) + (asleep or 0) or None
    # Negative stress means "not enough data", not a real reading.
    for col in ("avg_stress", "max_stress"):
        if row[col] is not None and row[col] < 0:
            row[col] = None
    # No steps means the watch wasn't worn: Garmin still fills goals, estimated
    # calories and zeros. Real sensor readings (HR, body battery) are kept.
    if row["steps"] is None:
        row |= dict.fromkeys(PLACEHOLDERS_WHEN_NOT_WORN)
    return row


def sleep_fields(values: dict[str, Any]) -> dict[str, Any]:
    row = {col: values.get(key) for col, key in SLEEP_FIELDS.items()}
    row["sleep_start_local"] = local_millis_to_str(
        values.get("localSleepStartTimeInMillis")
    )
    row["sleep_end_local"] = local_millis_to_str(
        values.get("localSleepEndTimeInMillis")
    )
    return row


def build_rows(
    summaries: dict[str, dict[str, Any]], sleep: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Join per-date summary and sleep data; drop dates where every metric is empty."""
    rows = []
    for day in sorted(summaries.keys() | sleep.keys()):
        row = {"date": day, **dict.fromkeys(COLUMNS[1:])}
        row |= summary_fields(summaries.get(day, {}))
        if day in sleep:
            row |= sleep_fields(sleep[day])
        if any(row[c] is not None for c in COLUMNS[1:]):
            rows.append(row)
    return rows


def main() -> None:
    args = parse_args(__doc__.splitlines()[0], "daily.csv")
    client = login()
    days = (args.end - args.start).days + 1
    print(
        f"Logged in. Fetching daily stats {args.start} → {args.end} ({days} days) ..."
    )

    summaries: dict[str, dict[str, Any]] = {}
    for i in range(days):
        day = (args.start + timedelta(days=i)).isoformat()
        summaries[day] = client.get_user_summary(day)
        print(f"\r  daily summaries: {i + 1}/{days}", end="", flush=True)
    print()

    sleep = {
        s["calendarDate"]: s.get("values") or {}
        for s in client.get_sleep_daily(args.start.isoformat(), args.end.isoformat())
    }

    rows = build_rows(summaries, sleep)
    write_csv(args.out, COLUMNS, rows)
    with db.connect() as con:
        db.merge(con, "daily", rows)
        total = con.execute("SELECT count(*) FROM daily").fetchone()[0]

    # Summary only: coverage counts, never personal values.
    def count(col: str) -> int:
        return sum(r[col] is not None for r in rows)

    print(f"Wrote {len(rows)} of {days} days to {args.out}")
    print(f"Merged into {db.db_path()} (daily table now has {total} rows)")
    print(
        f"  with steps: {count('steps')}  resting HR: {count('resting_hr_bpm')}"
        f"  sleep: {count('sleep_total_s')}  HRV: {count('hrv_overnight_avg_ms')}"
    )
    report_empty_columns(COLUMNS, rows)


if __name__ == "__main__":
    main()
