"""get_run: full detail for one run."""

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from garmin_mcp.fetch_activities import RUN_TYPES
from garmin_mcp.queries import sql
from garmin_mcp.tools.common import connect_read_only, query_rows


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
    with connect_read_only() as con:
        summary = query_rows(con, sql("run_summary"), [activity_id])
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
            "hr_zones": query_rows(con, sql("run_hr_zones"), [activity_id]),
            "weather": query_rows(con, sql("run_weather"), [activity_id])[0],
            "km_splits": query_rows(con, sql("run_km_splits"), [activity_id]),
            "laps": query_rows(con, sql("run_laps"), [activity_id]),
        }
