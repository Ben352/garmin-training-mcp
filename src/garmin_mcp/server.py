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

MAX_LIMIT = 500

mcp = MCPServer(
    name="garmin",
    instructions=(
        "Read-only access to the user's Garmin training data: runs and strength "
        "sessions. Units are in field names (distance_km, avg_pace_min_per_km, ...). "
        "Dates are the local calendar date where the activity started."
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
        cursor = con.execute(
            ACTIVITY_SQL, [start, end, activity_type, activity_type, limit + 1]
        )
        columns = [d[0] for d in cursor.description]
        rows = cursor.fetchall()

    activities = []
    for row in rows[:limit]:
        activity = {col: _jsonable(val) for col, val in zip(columns, row)}
        activity["avg_pace_min_per_km"] = format_pace(activity["avg_pace_s_per_km"])
        activities.append(activity)
    return {
        "activities": activities,
        "count": len(activities),
        "truncated": len(rows) > limit,
    }


def main() -> None:
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
