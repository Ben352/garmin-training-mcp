"""MCP server exposing Garmin data from DuckDB to LLM agents (stdio transport).

Usage:
    uv run garmin-mcp
"""

import threading
from datetime import date, datetime
from typing import Any

import duckdb
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from garmin_mcp import db, sql_guard
from garmin_mcp.audit import audited
from garmin_mcp.fetch_activities import RUN_TYPES
from garmin_mcp.queries import sql

MAX_LIMIT = 500

mcp = MCPServer(
    name="garmin",
    instructions=(
        "Read-only access to the user's Garmin training data: runs and strength "
        "sessions. Use list_activities to find activities and their activity_id, "
        "then get_run for one run's splits, laps, HR zones and weather. Use "
        "daily_metrics for sleep, resting HR, stress and steps, compare_periods "
        "for period-over-period questions, and query (SQL; see schema://tables) "
        "for anything else. Units are in field names (distance_km, "
        "pace_min_per_km, ...); every *_s_per_km pace has a matching *_min_per_km "
        "'m:ss' string. Dates are the local calendar date where the activity started. Only state facts based on the data, don't make "
        "assumptions, and use few tokens where possible."
    ),
)


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
@audited
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
            con,
            sql("activities"),
            [start, end, activity_type, activity_type, limit + 1],
        )
    return {
        "activities": rows[:limit],
        "count": min(len(rows), limit),
        "truncated": len(rows) > limit,
    }


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@audited
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
        summary = _query(con, sql("run_summary"), [activity_id])
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
            "hr_zones": _query(con, sql("run_hr_zones"), [activity_id]),
            "weather": _query(con, sql("run_weather"), [activity_id])[0],
            "km_splits": _query(con, sql("run_km_splits"), [activity_id]),
            "laps": _query(con, sql("run_laps"), [activity_id]),
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
@audited
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


def _period(
    con: duckdb.DuckDBPyConnection, start: date, end: date, min_wear_hours: float
) -> dict[str, Any]:
    weeks = ((end - start).days + 1) / 7
    training = _query(con, sql("training_period"), [sorted(RUN_TYPES), start, end])[0]
    training["run_km_per_week"] = round(training["run_km"] / weeks, 1)
    if training["longest_run_km"] is not None:
        training["longest_run_km"] = round(training["longest_run_km"], 2)
    vo2 = con.execute(sql("vo2max_as_of"), [end]).fetchone()
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
@audited
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


GPS_COLUMNS = ("start_lat", "start_lon", "end_lat", "end_lon")
QUERY_TIMEOUT_S = 10

TABLE_NOTES = {
    "activities": "One row per run or strength session (key activity_id). "
    "activity_date is the local start date. avg_pace_s_per_km is moving-time "
    "based, runs only. distance_m is NULL for strength.",
    "daily": "One row per day (key date): steps, resting HR, stress, body "
    "battery, intensity minutes, sleep, overnight HRV. Sleep belongs to the "
    "wake-up date. Filter wear_s >= 4*3600 for reliable HR/steps. Sleep/HRV "
    "are sparse.",
    "vo2max": "Only dates Garmin changed VO2 max (key date). For the value on "
    "a given day use ASOF JOIN ... ON x.date >= vo2max.date.",
    "runs": "One row per run with full detail (key activity_id, joins "
    "activities). avg_pace_s_per_km is timer based; also moving, GAP and best "
    "pace. hr_z1_s..hr_z5_s = seconds per HR zone. workout_feel 0/25/50/75/100, "
    "workout_rpe 1-10. weather_temp_c, weather_wind_speed_mph.",
    "run_laps": "Laps as the watch recorded them (key activity_id, lap_index): "
    "auto-laps or workout steps; intensity_type INTERVAL/ACTIVE/RECOVERY/...",
    "run_km_splits": "Per-kilometre splits computed from the 1 s time series "
    "(key activity_id, km). is_partial marks the final short split.",
    "run_fetch_log": "Bookkeeping: which runs have details loaded.",
}


def _connect_sandboxed() -> duckdb.DuckDBPyConnection:
    """Read-only connection for agent SQL: no file/network access (locked so SQL
    can't re-enable it) and GPS columns hidden behind a temp view."""
    if not db.db_path().exists():
        raise ToolError(
            f"No database at {db.db_path()}. Run `uv run garmin-fetch` first."
        )
    con = duckdb.connect(
        str(db.db_path()),
        read_only=True,
        config={"enable_external_access": False, "lock_configuration": True},
    )
    catalog = con.execute("SELECT current_database()").fetchone()[0]
    con.execute(
        "CREATE TEMP VIEW activities AS SELECT * EXCLUDE "
        f'({", ".join(GPS_COLUMNS)}) FROM "{catalog}".main.activities'
    )
    return con


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@audited
def query(sql: str) -> dict[str, Any]:
    """Run one read-only DuckDB SELECT over the Garmin tables, for questions the
    other tools can't answer (custom aggregations, correlations, joins).

    Prefer the dedicated tools when they fit. Read the schema://tables resource
    for columns, units and join keys. Tables: activities, daily, vo2max, runs,
    run_laps, run_km_splits, run_fetch_log. Units are in column names.

    Rules: a single SELECT (WITH/UNION fine), plain table names, no table
    functions or file access. At most 500 rows come back, so aggregate in SQL
    rather than pulling raw rows. Queries time out after 10 s.

    Args:
        sql: The DuckDB SQL query.

    Returns:
        {"columns": [...], "rows": [{...}], "row_count": n, "truncated": bool}.
        Every *_s_per_km column also gets a *_min_per_km "m:ss" string.
    """
    try:
        sql_guard.validate(sql)
    except sql_guard.UnsafeQuery as e:
        raise ToolError(str(e)) from None

    with _connect_sandboxed() as con:
        timer = threading.Timer(QUERY_TIMEOUT_S, con.interrupt)
        timer.start()
        try:
            cursor = con.execute(sql)
            columns = [d[0] for d in cursor.description]
            fetched = cursor.fetchmany(MAX_LIMIT + 1)
        except duckdb.InterruptException:
            raise ToolError(
                f"Query took longer than {QUERY_TIMEOUT_S} s. Simplify or aggregate it."
            ) from None
        except duckdb.Error as e:
            raise ToolError(f"DuckDB error: {e}") from None
        finally:
            timer.cancel()

    rows = []
    for values in fetched[:MAX_LIMIT]:
        row = {}
        for col, val in zip(columns, values):
            row[col] = _jsonable(val)
            if col.endswith("_s_per_km") and isinstance(val, int | float):
                row[col.removesuffix("_s_per_km") + "_min_per_km"] = format_pace(val)
        rows.append(row)
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": len(fetched) > MAX_LIMIT,
    }


@mcp.resource(
    "schema://tables",
    name="tables",
    description="Tables, columns, types, keys and row counts for the query tool.",
    mime_type="text/markdown",
)
def schema_tables() -> str:
    if not db.db_path().exists():
        return f"No database at {db.db_path()} yet. Run `uv run garmin-fetch` first."
    with db.connect(read_only=True) as con:
        columns = con.execute(sql("schema_columns")).fetchall()
        lines = [
            "# Garmin tables (DuckDB)",
            "",
            (
                "Units are in column names (_m, _s, _bpm, _s_per_km, _c, _pct, ...). "
                "Dates are local. Join run tables to activities on activity_id and "
                "daily/vo2max on activity_date = date."
            ),
        ]
        for table, note in TABLE_NOTES.items():
            # Table names can't be ? parameters; these come from TABLE_NOTES, not
            # the model, so formatting them in is safe.
            (n,) = con.execute(f"SELECT count(*) FROM {table}").fetchone()
            cols = [
                f"{c} {t}"
                for tbl, c, t in columns
                if tbl == table and c not in GPS_COLUMNS
            ]
            lines += ["", f"## {table} ({n} rows)", note, "", ", ".join(cols)]
    return "\n".join(lines) + "\n"


def main() -> None:
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
