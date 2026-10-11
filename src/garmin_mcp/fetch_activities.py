"""Fetch runs and strength sessions from Garmin Connect and write them to CSV.

Usage:
    uv run garmin-fetch                          # last 90 days
    uv run garmin-fetch --start 2026-01-01 --end 2026-10-10 --out data/activities.csv

Credentials come only from GARMIN_EMAIL / GARMIN_PASSWORD (loaded from .env).
After the first successful login the session token is cached in GARMINTOKENS
(default ~/.garminconnect), so later runs don't re-authenticate.

Output includes activity names, location names and start/end GPS
coordinates. It is written under data/, which is gitignored.
"""

from collections import Counter
from typing import Any

from garmin_mcp import db
from garmin_mcp.cli import login, parse_args, report_empty_columns, write_csv

RUN_TYPES = {
    "running",
    "trail_running",
    "treadmill_running",
    "track_running",
    "indoor_running",
    "street_running",
    "virtual_run",
    "ultra_run",
}
STRENGTH_TYPES = {"strength_training"}
WANTED_TYPES = RUN_TYPES | STRENGTH_TYPES

# CSV column -> Garmin activity-summary key. Units are in the column names.
FIELD_MAP = {
    "activity_name": "activityName",
    "location_name": "locationName",
    "start_lat": "startLatitude",
    "start_lon": "startLongitude",
    "end_lat": "endLatitude",
    "end_lon": "endLongitude",
    "start_time_local": "startTimeLocal",
    "start_time_gmt": "startTimeGMT",
    "duration_s": "duration",
    "moving_duration_s": "movingDuration",
    "distance_m": "distance",
    "avg_hr_bpm": "averageHR",
    "max_hr_bpm": "maxHR",
    "elevation_gain_m": "elevationGain",
    "calories_kcal": "calories",
    "avg_cadence_spm": "averageRunningCadenceInStepsPerMinute",
    "aerobic_training_effect": "aerobicTrainingEffect",
    "anaerobic_training_effect": "anaerobicTrainingEffect",
    "training_load": "activityTrainingLoad",
    "total_sets": "totalSets",
    "active_sets": "activeSets",
    "total_reps": "totalReps",
}
COLUMNS = ["activity_id", "activity_type", *FIELD_MAP, "avg_pace_s_per_km"]


def to_row(activity: dict[str, Any]) -> dict[str, Any]:
    """Map one Garmin activity summary to a CSV row."""
    row: dict[str, Any] = {
        "activity_id": activity["activityId"],
        "activity_type": activity.get("activityType", {}).get("typeKey"),
    }
    for column, key in FIELD_MAP.items():
        row[column] = activity.get(key)

    distance, moving = row["distance_m"], row["moving_duration_s"] or row["duration_s"]
    if row["activity_type"] in RUN_TYPES and distance and moving:
        row["avg_pace_s_per_km"] = round(moving / (distance / 1000), 1)
    else:
        row["avg_pace_s_per_km"] = None
    return row


def to_db_row(row: dict[str, Any]) -> dict[str, Any]:
    """CSV row -> `activities` table row (cleaning rules from docs/schema.md)."""
    out = dict(row)
    out["start_time_utc"] = out.pop("start_time_gmt")
    out["activity_date"] = row["start_time_local"][:10]  # local date, not UTC
    if row["activity_type"] not in RUN_TYPES and not row["distance_m"]:
        out["distance_m"] = None  # strength sessions report 0.0
    for col, digits in [
        ("duration_s", 1),
        ("moving_duration_s", 1),
        ("aerobic_training_effect", 1),
        ("anaerobic_training_effect", 1),
        ("training_load", 1),
        ("avg_cadence_spm", 1),
    ]:
        if out[col] is not None:
            out[col] = round(out[col], digits)
    return out


def main() -> None:
    args = parse_args(__doc__.splitlines()[0], "activities.csv")
    client = login()
    print(f"Logged in. Fetching activities {args.start} → {args.end} ...")
    activities = client.get_activities_by_date(
        args.start.isoformat(), args.end.isoformat(), sortorder="asc"
    )

    types = Counter(a.get("activityType", {}).get("typeKey") for a in activities)
    rows = [
        to_row(a)
        for a in activities
        if a.get("activityType", {}).get("typeKey") in WANTED_TYPES
    ]
    write_csv(args.out, COLUMNS, rows)
    with db.connect() as con:
        db.merge(con, "activities", [to_db_row(r) for r in rows])
        total = con.execute("SELECT count(*) FROM activities").fetchone()[0]

    # Summary only: counts and field coverage, never personal values.
    print(f"Fetched {len(activities)} activities; wrote {len(rows)} to {args.out}")
    print(f"Merged into {db.db_path()} (activities table now has {total} rows)")
    for type_key, n in sorted(types.items(), key=lambda kv: -kv[1]):
        print(f"  {'✓' if type_key in WANTED_TYPES else '·'} {type_key}: {n}")
    report_empty_columns(COLUMNS, rows)


if __name__ == "__main__":
    main()
