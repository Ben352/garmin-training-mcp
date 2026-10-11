"""MCP server exposing Garmin data from DuckDB to LLM agents (stdio transport).

Usage:
    uv run garmin-mcp
"""

from datetime import date, datetime
from typing import Any

import duckdb
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from garmin_mcp import db
from garmin_mcp.fetch_activities import RUN_TYPES

MAX_LIMIT = 500

mcp = MCPServer(
    name="garmin",
    instructions=(
        "Read-only access to the user's Garmin training data: runs and strength "
        "sessions. Use list_activities to find activities and their activity_id, "
        "then get_run for one run's splits, laps, HR zones and weather. Use "
        "daily_metrics for sleep, resting HR, stress and steps, and compare_periods "
        "for period-over-period questions. Units are in "
        "field names (distance_km, pace_min_per_km, ...); every *_s_per_km pace has a "
        "matching *_min_per_km 'm:ss' string. Dates are the local calendar date where "
        "the activity started. Only state facts based on the data, don't make "
        "assumptions, and use few tokens where possible."
    ),
)

# Columns returned to the agent. GPS coordinates are stored but not exposed here.
ACTIVITY_SQL = """
SELECT
    activity_id,
    activity_type,
    activity_name,
    location_name,
    activity_date,
    start_time_local,
    round(distance_m / 1000, 2)        AS distance_km,
    round(duration_s / 60, 1)          AS duration_min,
    avg_pace_s_per_km,
    avg_hr_bpm,
    max_hr_bpm,
    elevation_gain_m,
    calories_kcal,
    avg_cadence_spm,
    aerobic_training_effect,
    anaerobic_training_effect,
    training_load,
    total_sets,
    total_reps
FROM activities
WHERE activity_date BETWEEN ? AND ?
  AND (? IS NULL OR activity_type = ?)
ORDER BY start_time_local DESC
LIMIT ?
"""


def format_pace(seconds_per_km: float | None) -> str | None:
    """302.4 -> '5:02'."""
    if seconds_per_km is None:
        return None
    minutes, seconds = divmod(round(seconds_per_km), 60)
    return f"{minutes}:{seconds:02d}"


def _connect_read_only() -> duckdb.DuckDBPyConnection:
    if not db.db_path().exists():
        raise ToolError(
            f"No database at {db.db_path()}. Run `uv run garmin-fetch` first."
        )
    return db.connect(read_only=True)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _query(
    con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None
) -> list[dict[str, Any]]:
    """Run SQL and return JSON-ready dicts. Every `*_s_per_km` column also gets a
    `*_min_per_km` "m:ss" string, so paces read the same in every tool."""
    cursor = con.execute(sql, params or [])
    columns = [d[0] for d in cursor.description]
    rows = []
    for values in cursor.fetchall():
        row = {}
        for col, val in zip(columns, values):
            row[col] = _jsonable(val)
            if col.endswith("_s_per_km"):
                row[col.removesuffix("_s_per_km") + "_min_per_km"] = format_pace(val)
        rows.append(row)
    return rows


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def list_activities(
    start: date,
    end: date,
    activity_type: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List the user's activities between two dates, newest first.

    Use this to find runs or strength sessions in a period and get their headline
    numbers (distance, duration, pace, heart rate, training load).

    Args:
        start: First local calendar date to include (YYYY-MM-DD), inclusive.
        end: Last local calendar date to include (YYYY-MM-DD), inclusive.
        activity_type: Filter to one type, e.g. "running" or "strength_training".
            Omit to include all types. An unknown type returns an error listing
            the valid ones.
        limit: Maximum activities to return (1-500, default 50). If more match,
            `truncated` is true: narrow the date range or raise the limit.

    Returns:
        {"activities": [...], "count": n, "truncated": bool}. Each activity has:
        activity_id, activity_type, activity_name, location_name, activity_date,
        start_time_local, distance_km, duration_min (timer time),
        avg_pace_min_per_km (moving pace as "m:ss", runs only), avg_pace_s_per_km
        (same, as a number for calculations), avg_hr_bpm, max_hr_bpm,
        elevation_gain_m, calories_kcal, avg_cadence_spm (steps/min),
        aerobic_training_effect and anaerobic_training_effect (0-5),
        training_load, and for strength sessions total_sets and total_reps.
        Fields that don't apply to an activity are null.
    """
    if start > end:
        raise ToolError(f"start ({start}) must be on or before end ({end}).")
    if not 1 <= limit <= MAX_LIMIT:
        raise ToolError(f"limit must be between 1 and {MAX_LIMIT}, got {limit}.")

    with _connect_read_only() as con:
        if activity_type is not None:
            known = [
                r[0]
                for r in con.execute(
                    "SELECT DISTINCT activity_type FROM activities ORDER BY 1"
                ).fetchall()
            ]
            if activity_type not in known:
                raise ToolError(
                    f"Unknown activity_type {activity_type!r}. Valid: {', '.join(known)}."
                )
        rows = _query(
            con, ACTIVITY_SQL, [start, end, activity_type, activity_type, limit + 1]
        )
    return {
        "activities": rows[:limit],
        "count": min(len(rows), limit),
        "truncated": len(rows) > limit,
    }


RUN_SQL = """
SELECT
    r.activity_id,
    a.activity_name,
    a.location_name,
    a.activity_date,
    r.start_time_local,
    round(r.distance_m / 1000, 2)          AS distance_km,
    round(r.duration_s / 60, 1)            AS duration_min,
    round(r.moving_duration_s / 60, 1)     AS moving_duration_min,
    round(r.elapsed_duration_s / 60, 1)    AS elapsed_duration_min,
    r.avg_pace_s_per_km,
    r.avg_moving_pace_s_per_km,
    r.avg_gap_s_per_km,
    r.best_pace_s_per_km,
    r.avg_hr_bpm, r.max_hr_bpm, r.min_hr_bpm,
    r.avg_cadence_spm, r.max_cadence_spm,
    r.avg_power_w, r.max_power_w, r.normalized_power_w,
    r.elevation_gain_m, r.elevation_loss_m, r.min_elevation_m, r.max_elevation_m,
    r.avg_ground_contact_ms, r.avg_stride_length_cm,
    r.avg_vertical_oscillation_cm, r.avg_vertical_ratio_pct,
    r.aerobic_training_effect, r.anaerobic_training_effect,
    r.training_effect_label, r.training_load,
    r.workout_feel, r.workout_rpe,
    r.body_battery_change, r.steps, r.n_laps,
    v.vo2max_precise                      AS vo2max_at_run,
    v.date                                AS vo2max_as_of
FROM runs r
JOIN activities a USING (activity_id)
ASOF LEFT JOIN vo2max v ON a.activity_date >= v.date
WHERE r.activity_id = ?
"""

HR_ZONES_SQL = """
SELECT zone, seconds, round(seconds / 60, 1) AS minutes,
       round(100 * seconds / nullif(sum(seconds) OVER (), 0), 1) AS pct
FROM (
    UNPIVOT (SELECT hr_z1_s, hr_z2_s, hr_z3_s, hr_z4_s, hr_z5_s FROM runs WHERE activity_id = ?)
    ON hr_z1_s, hr_z2_s, hr_z3_s, hr_z4_s, hr_z5_s INTO NAME zone_col VALUE seconds
)
CROSS JOIN LATERAL (SELECT CAST(substr(zone_col, 5, 1) AS INTEGER) AS zone)
ORDER BY zone
"""

WEATHER_SQL = """
SELECT weather_temp_c AS temp_c, weather_apparent_temp_c AS apparent_temp_c,
       weather_humidity_pct AS humidity_pct, weather_wind_speed_mph AS wind_speed_mph,
       weather_desc AS description
FROM runs WHERE activity_id = ?
"""

KM_SPLITS_SQL = """
SELECT km, round(distance_m / 1000, 3) AS distance_km, is_partial,
       duration_s, pace_s_per_km, gap_s_per_km,
       avg_hr_bpm, max_hr_bpm, avg_cadence_spm, avg_power_w,
       elevation_gain_m, elevation_loss_m
FROM run_km_splits WHERE activity_id = ? ORDER BY km
"""

LAPS_SQL = """
SELECT lap_index, intensity_type, round(distance_m / 1000, 3) AS distance_km,
       duration_s, pace_s_per_km, gap_s_per_km,
       avg_hr_bpm, max_hr_bpm, avg_cadence_spm, avg_power_w,
       elevation_gain_m, elevation_loss_m
FROM run_laps WHERE activity_id = ? ORDER BY lap_index
"""


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def get_run(activity_id: int) -> dict[str, Any]:
    """Get full detail for one run: summary, HR zones, weather, km splits and laps.

    Use this after list_activities (which gives the activity_id) to answer questions
    about how a specific run went: pacing, negative splits, fade, heart-rate drift,
    effort distribution, interval performance, conditions.

    Args:
        activity_id: The run's id, from list_activities.

    Returns:
        {
          "summary": distance_km, duration_min (timer), moving/elapsed_duration_min,
            avg_pace (timer based, what Garmin shows), avg_moving_pace,
            avg_gap (grade-adjusted pace), best_pace, HR avg/max/min (bpm),
            cadence (steps/min), power (W, incl. normalized), elevation gain/loss/
            min/max (m), running dynamics (ground contact ms, stride cm, vertical
            oscillation cm, vertical ratio %), aerobic/anaerobic training effect
            (0-5) and training_effect_label, training_load, workout_feel (self-
            reported 0/25/50/75/100 = very weak..very strong), workout_rpe (self-
            reported 1-10), body_battery_change, steps, n_laps, vo2max_at_run
            (latest Garmin VO2 max on or before the run date) and vo2max_as_of.
          "hr_zones": [{zone 1-5, seconds, minutes, pct of run}],
          "weather": {temp_c, apparent_temp_c, humidity_pct, wind_speed_mph,
            description} from the nearest weather station (null fields if indoor),
          "km_splits": per kilometre, computed from the 1-second time series:
            km, distance_km (< 1 only for the final partial split, is_partial=true),
            duration_s, pace, gap, avg/max HR, cadence, power, elevation gain/loss.
          "laps": as recorded by the watch (auto-lap or structured-workout steps;
            intensity_type INTERVAL/ACTIVE/RECOVERY/...), same metrics as splits.
        }
        Pace fields come as both *_s_per_km (number) and *_min_per_km ("m:ss").
    """
    with _connect_read_only() as con:
        summary = _query(con, RUN_SQL, [activity_id])
        if not summary:
            found = con.execute(
                "SELECT activity_type FROM activities WHERE activity_id = ?",
                [activity_id],
            ).fetchone()
            if found is None:
                raise ToolError(
                    f"No activity with id {activity_id}. "
                    "Use list_activities to find valid ids."
                )
            if found[0] not in RUN_TYPES:
                raise ToolError(
                    f"Activity {activity_id} is {found[0]!r}, not a run. "
                    "get_run only covers runs; list_activities has its summary."
                )
            raise ToolError(
                f"Run {activity_id} is listed but its details haven't been fetched. "
                "Run `uv run garmin-fetch-runs` to load them."
            )
        return {
            "summary": summary[0],
            "hr_zones": _query(con, HR_ZONES_SQL, [activity_id]),
            "weather": _query(con, WEATHER_SQL, [activity_id])[0],
            "km_splits": _query(con, KM_SPLITS_SQL, [activity_id]),
            "laps": _query(con, LAPS_SQL, [activity_id]),
        }


# Daily metrics exposed to the agent: name -> SQL expression over `daily`.
# Tools only ever build SQL from these keys, never from user strings.
DAILY_METRICS = {
    "steps": "steps",
    "resting_hr_bpm": "resting_hr_bpm",
    "avg_stress": "avg_stress",
    "body_battery_high": "body_battery_high",
    "body_battery_low": "body_battery_low",
    "moderate_intensity_min": "moderate_intensity_min",
    "vigorous_intensity_min": "vigorous_intensity_min",
    "active_kcal": "active_kcal",
    "sleep_h": "round(sleep_total_s / 3600, 2)",
    "sleep_deep_h": "round(sleep_deep_s / 3600, 2)",
    "sleep_rem_h": "round(sleep_rem_s / 3600, 2)",
    "sleep_score": "sleep_score",
    "hrv_overnight_avg_ms": "hrv_overnight_avg_ms",
}
MAX_DAYS = 500


def _check_range(start: date, end: date, label: str = "") -> None:
    if start > end:
        raise ToolError(
            f"{label}start ({start}) must be on or before {label}end ({end})."
        )
    if (end - start).days + 1 > MAX_DAYS:
        raise ToolError(f"Date range is limited to {MAX_DAYS} days; narrow it.")


def _check_wear(min_wear_hours: float) -> None:
    if not 0 <= min_wear_hours <= 24:
        raise ToolError(
            f"min_wear_hours must be between 0 and 24, got {min_wear_hours}."
        )


def _wear_filter(min_wear_hours: float) -> str:
    # 0 keeps every stored day, including ones with no wear_s at all.
    return "TRUE" if min_wear_hours == 0 else "wear_s >= ?"


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def daily_metrics(
    start: date,
    end: date,
    metrics: list[str] | None = None,
    min_wear_hours: float = 4,
) -> dict[str, Any]:
    """Daily wellness values (steps, resting HR, stress, body battery, sleep, HRV)
    for each day in a date range, plus a per-metric summary.

    Use this for questions about recovery, sleep, resting heart rate or activity
    level over time. Sleep belongs to the date you woke up. Sleep and HRV are often
    missing (the watch isn't always worn overnight): check each summary's n.

    Args:
        start: First local date (YYYY-MM-DD), inclusive.
        end: Last local date, inclusive. At most 500 days in total.
        metrics: Which metrics to return. Omit for all. Valid: steps,
            resting_hr_bpm, avg_stress (0-100), body_battery_high/low (0-100),
            moderate_intensity_min, vigorous_intensity_min, active_kcal, sleep_h,
            sleep_deep_h, sleep_rem_h, sleep_score (0-100), hrv_overnight_avg_ms.
        min_wear_hours: Skip days the watch measured for less than this (default
            4). Below ~4h resting HR and steps are unreliable. 0 keeps every day.

    Returns:
        {"days": [{date, wear_h, <metric>...}], "summary": {<metric>: {n, mean,
        min, max}}, "days_excluded_low_wear": int}. Missing values are null and
        don't count towards n.
    """
    _check_range(start, end)
    _check_wear(min_wear_hours)
    names = list(DAILY_METRICS) if metrics is None else metrics
    unknown = [m for m in names if m not in DAILY_METRICS]
    if unknown or not names:
        raise ToolError(
            f"Unknown metrics {unknown}. Valid: {', '.join(DAILY_METRICS)}."
        )
    cols = ", ".join(f"{DAILY_METRICS[m]} AS {m}" for m in names)
    wear_params = [] if min_wear_hours == 0 else [min_wear_hours * 3600]
    sql = f"""
        SELECT date, round(wear_s / 3600, 1) AS wear_h, {cols}
        FROM daily
        WHERE date BETWEEN ? AND ? AND {_wear_filter(min_wear_hours)}
        ORDER BY date
    """
    with _connect_read_only() as con:
        days = _query(con, sql, [start, end, *wear_params])
        total = con.execute(
            "SELECT count(*) FROM daily WHERE date BETWEEN ? AND ?", [start, end]
        ).fetchone()[0]
    summary = {}
    for m in names:
        values = [d[m] for d in days if d[m] is not None]
        summary[m] = {
            "n": len(values),
            "mean": round(sum(values) / len(values), 2) if values else None,
            "min": min(values, default=None),
            "max": max(values, default=None),
        }
    return {
        "days": days,
        "summary": summary,
        "days_excluded_low_wear": total - len(days),
    }


RUN_TYPES_SQL = ", ".join(f"'{t}'" for t in sorted(RUN_TYPES))

TRAINING_SQL = f"""
SELECT
    count(*) FILTER (WHERE activity_type IN ({RUN_TYPES_SQL}))       AS runs,
    round(coalesce(sum(distance_m) FILTER (
        WHERE activity_type IN ({RUN_TYPES_SQL})), 0) / 1000, 1)     AS run_km,
    round(sum(moving_duration_s) FILTER (WHERE activity_type IN ({RUN_TYPES_SQL}))
        / nullif(sum(distance_m) FILTER (
            WHERE activity_type IN ({RUN_TYPES_SQL})), 0) * 1000, 1) AS run_avg_pace_s_per_km,
    round(sum(avg_hr_bpm * duration_s) FILTER (WHERE activity_type IN ({RUN_TYPES_SQL}))
        / nullif(sum(duration_s) FILTER (WHERE activity_type IN ({RUN_TYPES_SQL})
            AND avg_hr_bpm IS NOT NULL), 0), 1)                     AS run_avg_hr_bpm,
    max(distance_m) FILTER (WHERE activity_type IN ({RUN_TYPES_SQL})) / 1000
                                                                     AS longest_run_km,
    count(*) FILTER (WHERE activity_type = 'strength_training')      AS strength_sessions,
    round(coalesce(sum(training_load), 0), 1)                        AS training_load_total
FROM activities
WHERE activity_date BETWEEN ? AND ?
"""


def _period(
    con: duckdb.DuckDBPyConnection, start: date, end: date, min_wear_hours: float
) -> dict[str, Any]:
    weeks = ((end - start).days + 1) / 7
    training = _query(con, TRAINING_SQL, [start, end])[0]
    training["run_km_per_week"] = round(training["run_km"] / weeks, 1)
    if training["longest_run_km"] is not None:
        training["longest_run_km"] = round(training["longest_run_km"], 2)
    vo2 = con.execute(
        "SELECT vo2max_precise, date FROM vo2max WHERE date <= ? "
        "ORDER BY date DESC LIMIT 1",
        [end],
    ).fetchone()
    training["vo2max_at_end"] = vo2[0] if vo2 else None

    wear_params = [] if min_wear_hours == 0 else [min_wear_hours * 3600]
    aggregates = ", ".join(
        f"round(avg({expr}), 2) AS {m}, count({expr}) AS {m}__n"
        for m, expr in DAILY_METRICS.items()
    )
    row = _query(
        con,
        f"SELECT {aggregates} FROM daily "
        f"WHERE date BETWEEN ? AND ? AND {_wear_filter(min_wear_hours)}",
        [start, end, *wear_params],
    )[0]
    daily = {m: {"mean": row[m], "n": row[f"{m}__n"]} for m in DAILY_METRICS}
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": (end - start).days + 1,
        "training": training,
        "daily_avg": daily,
    }


def _delta(a: float | None, b: float | None) -> dict[str, Any]:
    if a is None or b is None:
        return {"change": None, "pct": None}
    return {
        "change": round(b - a, 2),
        "pct": round(100 * (b - a) / a, 1) if a else None,
    }


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def compare_periods(
    a_start: date,
    a_end: date,
    b_start: date,
    b_end: date,
    min_wear_hours: float = 4,
) -> dict[str, Any]:
    """Compare two date ranges: training volume/intensity and daily wellness.

    Use this for "this month vs last month", "before vs after", or block-to-block
    questions. Period a is the baseline, b is compared against it. Periods may have
    different lengths: compare run_km_per_week, not run_km.

    Args:
        a_start, a_end: Baseline period, local dates inclusive.
        b_start, b_end: Comparison period, local dates inclusive.
        min_wear_hours: Daily averages skip days worn less than this (default 4;
            0 keeps every day). Training numbers are unaffected.

    Returns:
        {"a": period, "b": period, "delta": {...}} where each period has
        training: runs, run_km, run_km_per_week, run_avg_pace_s_per_km/_min_per_km
        (moving time / distance over all runs), run_avg_hr_bpm (time-weighted),
        longest_run_km, strength_sessions, training_load_total, vo2max_at_end
        (latest VO2 max on or before the period end); and daily_avg: for each
        daily metric (see daily_metrics) {mean, n}. delta gives b - a as change
        and pct (% of a) for every training number and daily mean. For pace a
        negative change means faster.
    """
    _check_range(a_start, a_end, "a_")
    _check_range(b_start, b_end, "b_")
    _check_wear(min_wear_hours)
    with _connect_read_only() as con:
        a = _period(con, a_start, a_end, min_wear_hours)
        b = _period(con, b_start, b_end, min_wear_hours)
    numeric = [k for k, v in a["training"].items() if not isinstance(v, str)]
    delta = {
        "training": {k: _delta(a["training"][k], b["training"][k]) for k in numeric},
        "daily_avg": {
            m: _delta(a["daily_avg"][m]["mean"], b["daily_avg"][m]["mean"])
            for m in DAILY_METRICS
        },
    }
    return {"a": a, "b": b, "delta": delta}


def main() -> None:
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
