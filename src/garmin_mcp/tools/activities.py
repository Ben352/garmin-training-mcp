"""list_activities: find activities in a date range."""

from datetime import date
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from garmin_mcp.queries import sql
from garmin_mcp.tools.common import MAX_LIMIT, connect_read_only, query_rows


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

    with connect_read_only() as con:
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
        rows = query_rows(
            con,
            sql("activities"),
            [start, end, activity_type, activity_type, limit + 1],
        )
    return {
        "activities": rows[:limit],
        "count": min(len(rows), limit),
        "truncated": len(rows) > limit,
    }
