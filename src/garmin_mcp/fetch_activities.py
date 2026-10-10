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

import argparse
import csv
import os
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

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

DEFAULT_TOKENSTORE = "~/.garminconnect"


def to_row(activity: dict[str, Any]) -> dict[str, Any]:
    """Map one Garmin activity summary to an allowlisted CSV row."""
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


def login() -> Garmin:
    """Log in once using cached tokens or env credentials. Exits on failure, never retries."""
    load_dotenv()
    tokenstore = os.getenv("GARMINTOKENS", DEFAULT_TOKENSTORE)
    Path(tokenstore).expanduser().mkdir(mode=0o700, parents=True, exist_ok=True)

    client = Garmin(
        email=os.getenv("GARMIN_EMAIL"),
        password=os.getenv("GARMIN_PASSWORD"),
        prompt_mfa=lambda: input("Garmin MFA code: ").strip(),
    )
    try:
        client.login(tokenstore)
    except GarminConnectTooManyRequestsError:
        sys.exit("Login rate-limited by Garmin (429). Wait a while before retrying.")
    except GarminConnectAuthenticationError as e:
        sys.exit(
            f"Garmin login failed: {e}. Check GARMIN_EMAIL / GARMIN_PASSWORD in .env."
        )
    except GarminConnectConnectionError as e:
        sys.exit(f"Could not reach Garmin Connect: {e}")
    return client


def main() -> None:
    today = date.today()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--start", type=date.fromisoformat, default=today - timedelta(days=90)
    )
    parser.add_argument("--end", type=date.fromisoformat, default=today)
    parser.add_argument("--out", type=Path, default=Path("data/activities.csv"))
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must be on or before --end")

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

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    # Summary only: counts and field coverage, never personal values.
    print(f"Fetched {len(activities)} activities; wrote {len(rows)} to {args.out}")
    for type_key, n in sorted(types.items(), key=lambda kv: -kv[1]):
        print(f"  {'✓' if type_key in WANTED_TYPES else '·'} {type_key}: {n}")
    empty = [c for c in COLUMNS if rows and all(r[c] is None for r in rows)]
    if empty:
        print(f"Columns empty for every row (may be expected): {', '.join(empty)}")


if __name__ == "__main__":
    main()
