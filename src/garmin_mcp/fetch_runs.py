"""Fetch per-run detail (summary, laps, HR zones, weather, time series) and VO2 max.

Usage:
    uv run garmin-fetch           # first: refresh data/activities.csv
    uv run garmin-fetch-runs      # fetch details for runs not yet loaded
    uv run garmin-fetch-runs --offline   # rebuild CSVs from the raw cache, no API calls

Each run is fetched once. Its raw API responses are cached in
data/raw/runs/<activity_id>.json.gz and the run is recorded in
data/run_fetch_log.csv. Runs in the log are never fetched again.

The CSVs below are rebuilt from the raw cache on every invocation:
    data/runs.csv           one row per run
    data/run_laps.csv       one row per lap, as recorded by the watch
    data/run_km_splits.csv  one row per km, computed from the time series
    data/vo2max.csv         one row per date Garmin updated VO2 max
"""

import argparse
import csv
import gzip
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from garminconnect import (
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from garmin_mcp.cli import login, report_empty_columns, write_csv
from garmin_mcp.fetch_activities import RUN_TYPES

DATA = Path("data")
RAW_DIR = DATA / "raw" / "runs"
LOG_PATH = DATA / "run_fetch_log.csv"
LOG_COLUMNS = ["activity_id", "fetched_at_utc", "n_samples", "has_weather"]

MAX_CHART_POINTS = 10_000  # default 2000 downsamples long runs
MIN_FINAL_SPLIT_M = 10  # drop a trailing "split" shorter than this
# Elevation changes smaller than this are treated as sensor noise. Tuned on real
# runs: 1 m puts summed split gain at ~1.1x Garmin's own gain (raw diffs gave ~3x).
ELEVATION_HYSTERESIS_M = 1.0


def pace(speed_mps: float | None) -> float | None:
    """Speed in m/s -> pace in s/km."""
    return round(1000 / speed_mps, 1) if speed_mps else None


def rnd(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


# ---------------------------------------------------------------- fetch & cache


def fetch_raw(client, activity_id: int) -> dict[str, Any]:
    """Call every per-run endpoint once. Weather is optional (missing indoors)."""
    aid = str(activity_id)
    try:
        weather = client.get_activity_weather(aid) or None
    except GarminConnectConnectionError:
        weather = None
    return {
        "activity_id": activity_id,
        "fetched_at_utc": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        "activity": client.get_activity(aid),
        "splits": client.get_activity_splits(aid),
        "hr_zones": client.get_activity_hr_in_timezones(aid),
        "weather": weather,
        "details": client.get_activity_details(aid, maxchart=MAX_CHART_POINTS),
    }


def raw_path(activity_id: int) -> Path:
    return RAW_DIR / f"{activity_id}.json.gz"


def save_raw(raw: dict[str, Any]) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = raw_path(raw["activity_id"])
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt") as f:
        json.dump(raw, f)
    tmp.replace(path)  # atomic: a crash never leaves a half-written cache file


def load_raw(activity_id: int) -> dict[str, Any]:
    with gzip.open(raw_path(activity_id), "rt") as f:
        return json.load(f)


def load_log() -> dict[int, dict[str, str]]:
    """Fully loaded runs: logged AND raw cache file present."""
    if not LOG_PATH.exists():
        return {}
    with LOG_PATH.open() as f:
        rows = {int(r["activity_id"]): r for r in csv.DictReader(f)}
    return {aid: r for aid, r in rows.items() if raw_path(aid).exists()}


def append_log(raw: dict[str, Any]) -> None:
    new = not LOG_PATH.exists()
    with LOG_PATH.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_COLUMNS)
        if new:
            writer.writeheader()
        writer.writerow(
            {
                "activity_id": raw["activity_id"],
                "fetched_at_utc": raw["fetched_at_utc"],
                "n_samples": len(raw["details"].get("activityDetailMetrics") or []),
                "has_weather": raw["weather"] is not None,
            }
        )


def runs_from_activities(path: Path) -> dict[int, date]:
    """Run activity_id -> local start date, from activities.csv."""
    with path.open() as f:
        return {
            int(r["activity_id"]): date.fromisoformat(r["start_time_local"][:10])
            for r in csv.DictReader(f)
            if r["activity_type"] in RUN_TYPES
        }


# ---------------------------------------------------------------- parse: runs

RUN_COLUMNS = [
    "activity_id",
    "start_time_local",
    "distance_m",
    "duration_s",
    "moving_duration_s",
    "elapsed_duration_s",
    "avg_pace_s_per_km",
    "avg_moving_pace_s_per_km",
    "avg_gap_s_per_km",
    "best_pace_s_per_km",
    "avg_hr_bpm",
    "max_hr_bpm",
    "min_hr_bpm",
    "avg_cadence_spm",
    "max_cadence_spm",
    "avg_power_w",
    "max_power_w",
    "normalized_power_w",
    "elevation_gain_m",
    "elevation_loss_m",
    "min_elevation_m",
    "max_elevation_m",
    "avg_ground_contact_ms",
    "avg_stride_length_cm",
    "avg_vertical_oscillation_cm",
    "avg_vertical_ratio_pct",
    "aerobic_training_effect",
    "anaerobic_training_effect",
    "training_effect_label",
    "training_load",
    "workout_feel",
    "workout_rpe",
    "body_battery_change",
    "steps",
    "n_laps",
    "hr_z1_s",
    "hr_z2_s",
    "hr_z3_s",
    "hr_z4_s",
    "hr_z5_s",
    "weather_temp_c",
    "weather_apparent_temp_c",
    "weather_humidity_pct",
    "weather_wind_speed_mph",
    "weather_desc",
]


def f_to_c(f: float | None) -> float | None:
    return None if f is None else round((f - 32) * 5 / 9, 1)


def parse_run(raw: dict[str, Any]) -> dict[str, Any]:
    s = raw["activity"]["summaryDTO"]
    zones = {z["zoneNumber"]: z.get("secsInZone") for z in raw["hr_zones"] or []}
    w = raw["weather"] or {}
    distance, duration = s.get("distance"), s.get("duration")
    return {
        "activity_id": raw["activity_id"],
        "start_time_local": (s.get("startTimeLocal") or "").replace("T", " ")[:19],
        "distance_m": rnd(distance),
        "duration_s": rnd(duration),
        "moving_duration_s": rnd(s.get("movingDuration")),
        "elapsed_duration_s": rnd(s.get("elapsedDuration")),
        # Timer-based, matches what Garmin Connect shows as "Avg Pace".
        "avg_pace_s_per_km": (
            round(duration / (distance / 1000), 1) if distance and duration else None
        ),
        "avg_moving_pace_s_per_km": pace(s.get("averageMovingSpeed")),
        "avg_gap_s_per_km": pace(s.get("avgGradeAdjustedSpeed")),
        "best_pace_s_per_km": pace(s.get("maxSpeed")),
        "avg_hr_bpm": rnd(s.get("averageHR"), 0),
        "max_hr_bpm": rnd(s.get("maxHR"), 0),
        "min_hr_bpm": rnd(s.get("minHR"), 0),
        "avg_cadence_spm": rnd(s.get("averageRunCadence")),
        "max_cadence_spm": rnd(s.get("maxRunCadence")),
        "avg_power_w": rnd(s.get("averagePower"), 0),
        "max_power_w": rnd(s.get("maxPower"), 0),
        "normalized_power_w": rnd(s.get("normalizedPower"), 0),
        "elevation_gain_m": rnd(s.get("elevationGain")),
        "elevation_loss_m": rnd(s.get("elevationLoss")),
        "min_elevation_m": rnd(s.get("minElevation")),
        "max_elevation_m": rnd(s.get("maxElevation")),
        "avg_ground_contact_ms": rnd(s.get("groundContactTime")),
        "avg_stride_length_cm": rnd(s.get("strideLength")),
        "avg_vertical_oscillation_cm": rnd(s.get("verticalOscillation")),
        "avg_vertical_ratio_pct": rnd(s.get("verticalRatio")),
        "aerobic_training_effect": rnd(s.get("trainingEffect")),
        "anaerobic_training_effect": rnd(s.get("anaerobicTrainingEffect")),
        "training_effect_label": s.get("trainingEffectLabel"),
        "training_load": rnd(s.get("activityTrainingLoad")),
        "workout_feel": s.get("directWorkoutFeel"),
        # Garmin stores RPE x10 (10-100); feel is 0/25/50/75/100 (very weak..very strong).
        "workout_rpe": rpe / 10
        if (rpe := s.get("directWorkoutRpe")) is not None
        else None,
        "body_battery_change": s.get("differenceBodyBattery"),
        "steps": s.get("steps"),
        "n_laps": len(raw["splits"].get("lapDTOs") or []),
        **{f"hr_z{i}_s": rnd(zones.get(i), 0) for i in range(1, 6)},
        "weather_temp_c": f_to_c(w.get("temp")),
        "weather_apparent_temp_c": f_to_c(w.get("apparentTemp")),
        "weather_humidity_pct": w.get("relativeHumidity"),
        # Unit not documented; assumed mph since the same payload gives temp in °F.
        "weather_wind_speed_mph": w.get("windSpeed"),
        "weather_desc": (w.get("weatherTypeDTO") or {}).get("desc"),
    }


# ---------------------------------------------------------------- parse: laps

LAP_COLUMNS = [
    "activity_id",
    "lap_index",
    "intensity_type",
    "start_time_gmt",
    "distance_m",
    "duration_s",
    "moving_duration_s",
    "pace_s_per_km",
    "gap_s_per_km",
    "avg_hr_bpm",
    "max_hr_bpm",
    "avg_cadence_spm",
    "avg_power_w",
    "elevation_gain_m",
    "elevation_loss_m",
    "avg_ground_contact_ms",
    "avg_stride_length_cm",
    "avg_vertical_oscillation_cm",
]


def parse_laps(raw: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for lap in raw["splits"].get("lapDTOs") or []:
        distance, duration = lap.get("distance"), lap.get("duration")
        rows.append(
            {
                "activity_id": raw["activity_id"],
                "lap_index": lap.get("lapIndex"),
                "intensity_type": lap.get("intensityType"),
                "start_time_gmt": (lap.get("startTimeGMT") or "").replace("T", " ")[
                    :19
                ],
                "distance_m": rnd(distance),
                "duration_s": rnd(duration),
                "moving_duration_s": rnd(lap.get("movingDuration")),
                "pace_s_per_km": (
                    round(duration / (distance / 1000), 1)
                    if distance and duration
                    else None
                ),
                "gap_s_per_km": pace(lap.get("avgGradeAdjustedSpeed")),
                "avg_hr_bpm": rnd(lap.get("averageHR"), 0),
                "max_hr_bpm": rnd(lap.get("maxHR"), 0),
                "avg_cadence_spm": rnd(lap.get("averageRunCadence")),
                "avg_power_w": rnd(lap.get("averagePower"), 0),
                "elevation_gain_m": rnd(lap.get("elevationGain")),
                "elevation_loss_m": rnd(lap.get("elevationLoss")),
                "avg_ground_contact_ms": rnd(lap.get("groundContactTime")),
                "avg_stride_length_cm": rnd(lap.get("strideLength")),
                "avg_vertical_oscillation_cm": rnd(lap.get("verticalOscillation")),
            }
        )
    return rows


# ---------------------------------------------------------------- parse: km splits

SPLIT_COLUMNS = [
    "activity_id",
    "km",
    "distance_m",
    "is_partial",
    "duration_s",
    "pace_s_per_km",
    "gap_s_per_km",
    "avg_hr_bpm",
    "max_hr_bpm",
    "avg_cadence_spm",
    "avg_power_w",
    "elevation_gain_m",
    "elevation_loss_m",
]


def time_series(details: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn Garmin's index-based metric arrays into a list of {key: value} samples."""
    index = {
        d["key"]: d["metricsIndex"] for d in details.get("metricDescriptors") or []
    }
    return [
        {key: s["metrics"][i] for key, i in index.items()}
        for s in details.get("activityDetailMetrics") or []
    ]


def _interp(a: float, b: float, frac: float) -> float:
    return a + (b - a) * frac


def _mean(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def elevation_steps(
    elevations: list[float | None], threshold: float = ELEVATION_HYSTERESIS_M
) -> list[tuple[float, float]]:
    """Per-sample (gain, loss) in metres, ignoring wiggles smaller than `threshold`.

    A change is only counted once elevation has moved `threshold` away from the last
    counted reference point, and it is attributed to the sample where that happens.
    """
    steps = []
    ref = None
    for e in elevations:
        gain = loss = 0.0
        if e is not None:
            if ref is None:
                ref = e
            elif e - ref >= threshold:
                gain, ref = e - ref, e
            elif ref - e >= threshold:
                loss, ref = ref - e, e
        steps.append((gain, loss))
    return steps


def km_splits(activity_id: int, samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cut the time series at every 1000 m of distance.

    Split duration uses the timer (sumDuration, excludes auto-pause), interpolated
    at the exact km crossing, so split durations sum to the activity duration.
    """
    samples = [
        dict(s)  # copy: per-sample elevation steps are added below
        for s in samples
        if s.get("sumDistance") is not None and s.get("sumDuration") is not None
    ]
    if not samples:
        return []
    for sample, (gain, loss) in zip(
        samples, elevation_steps([x.get("directElevation") for x in samples])
    ):
        sample["_gain"], sample["_loss"] = gain, loss

    rows: list[dict[str, Any]] = []
    total = samples[-1]["sumDistance"]
    start_dist, start_time = 0.0, 0.0
    bucket: list[dict[str, Any]] = []
    km = 1
    prev = None

    def close(end_dist: float, end_time: float, partial: bool) -> None:
        dist = end_dist - start_dist
        dur = end_time - start_time
        has_elevation = any(s.get("directElevation") is not None for s in bucket)
        cadence = _mean([s.get("directDoubleCadence") for s in bucket])
        rows.append(
            {
                "activity_id": activity_id,
                "km": km,
                "distance_m": round(dist, 1),
                "is_partial": partial,
                "duration_s": round(dur, 1),
                "pace_s_per_km": round(dur / (dist / 1000), 1) if dist else None,
                "gap_s_per_km": pace(
                    _mean([s.get("directGradeAdjustedSpeed") for s in bucket])
                ),
                "avg_hr_bpm": rnd(_mean([s.get("directHeartRate") for s in bucket]), 0),
                "max_hr_bpm": max(
                    (
                        s["directHeartRate"]
                        for s in bucket
                        if s.get("directHeartRate") is not None
                    ),
                    default=None,
                ),
                "avg_cadence_spm": rnd(cadence),
                "avg_power_w": rnd(_mean([s.get("directPower") for s in bucket]), 0),
                "elevation_gain_m": (
                    round(sum(s["_gain"] for s in bucket), 1) if has_elevation else None
                ),
                "elevation_loss_m": (
                    round(sum(s["_loss"] for s in bucket), 1) if has_elevation else None
                ),
            }
        )

    for s in samples:
        d, t = s["sumDistance"], s["sumDuration"]
        while d >= km * 1000:
            boundary = km * 1000
            if prev is None or d == prev["sumDistance"]:
                t_cross = t
            else:
                frac = (boundary - prev["sumDistance"]) / (d - prev["sumDistance"])
                t_cross = _interp(prev["sumDuration"], t, frac)
            close(boundary, t_cross, partial=False)
            start_dist, start_time = boundary, t_cross
            bucket = []
            km += 1
        bucket.append(s)
        prev = s

    if total - start_dist >= MIN_FINAL_SPLIT_M:
        close(total, samples[-1]["sumDuration"], partial=True)
    return rows


# ---------------------------------------------------------------- VO2 max

VO2_COLUMNS = ["date", "vo2max", "vo2max_precise"]
VO2_CHUNK_DAYS = 90


def fetch_vo2max(client, start: date, end: date) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=VO2_CHUNK_DAYS - 1), end)
        for item in (
            client.get_max_metrics_range(chunk_start.isoformat(), chunk_end.isoformat())
            or []
        ):
            g = item.get("generic") or {}
            if g.get("calendarDate") and g.get("vo2MaxPreciseValue") is not None:
                rows[g["calendarDate"]] = {
                    "date": g["calendarDate"],
                    "vo2max": g.get("vo2MaxValue"),
                    "vo2max_precise": g.get("vo2MaxPreciseValue"),
                }
        chunk_start = chunk_end + timedelta(days=1)
    return [rows[d] for d in sorted(rows)]


# ---------------------------------------------------------------- main


def rebuild_csvs(loaded: list[int]) -> tuple[int, int, int]:
    runs, laps, splits = [], [], []
    for aid in sorted(loaded):
        raw = load_raw(aid)
        runs.append(parse_run(raw))
        laps.extend(parse_laps(raw))
        splits.extend(km_splits(aid, time_series(raw["details"])))
    runs.sort(key=lambda r: r["start_time_local"])
    write_csv(DATA / "runs.csv", RUN_COLUMNS, runs)
    write_csv(DATA / "run_laps.csv", LAP_COLUMNS, laps)
    write_csv(DATA / "run_km_splits.csv", SPLIT_COLUMNS, splits)
    report_empty_columns(RUN_COLUMNS, runs)
    return len(runs), len(laps), len(splits)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--activities", type=Path, default=DATA / "activities.csv")
    parser.add_argument(
        "--offline", action="store_true", help="rebuild CSVs from the raw cache only"
    )
    args = parser.parse_args()

    loaded = load_log()
    if not args.offline:
        if not args.activities.exists():
            raise SystemExit(
                f"{args.activities} not found. Run `uv run garmin-fetch` first."
            )
        run_dates = runs_from_activities(args.activities)
        run_ids = list(run_dates)
        pending = [aid for aid in run_ids if aid not in loaded]
        print(
            f"{len(run_ids)} runs in {args.activities}: {len(loaded)} already loaded, {len(pending)} to fetch"
        )

        client = login()
        failed = 0
        for i, aid in enumerate(pending, 1):
            try:
                raw = fetch_raw(client, aid)
            except GarminConnectTooManyRequestsError:
                print(
                    "\nRate-limited by Garmin (429). Stopping; re-run later to continue."
                )
                break
            except GarminConnectConnectionError as e:
                failed += 1
                print(
                    f"\n  run {aid}: fetch failed ({type(e).__name__}), will retry next time"
                )
                continue
            save_raw(raw)
            append_log(raw)
            print(f"\r  fetched {i}/{len(pending)}", end="", flush=True)
        if pending:
            print()
        if failed:
            print(f"{failed} run(s) failed and stay pending.")
        loaded = load_log()

        if run_dates:
            vo2 = fetch_vo2max(client, min(run_dates.values()), max(run_dates.values()))
            write_csv(DATA / "vo2max.csv", VO2_COLUMNS, vo2)
            print(f"VO2 max: {len(vo2)} dated values → {DATA / 'vo2max.csv'}")

    n_runs, n_laps, n_splits = rebuild_csvs(list(loaded))
    print(f"Rebuilt from cache: {n_runs} runs, {n_laps} laps, {n_splits} km splits")


if __name__ == "__main__":
    main()
