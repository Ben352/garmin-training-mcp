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
        "then get_run for one run's splits, laps, HR zones and weather. Units are in "
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


def main() -> None:
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
