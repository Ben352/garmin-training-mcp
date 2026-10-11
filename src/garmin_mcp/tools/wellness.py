"""daily_metrics and compare_periods: daily wellness data and period comparison."""

from datetime import date
from typing import Any

import duckdb
from mcp.server.mcpserver.exceptions import ToolError

from garmin_mcp.fetch_activities import RUN_TYPES
from garmin_mcp.queries import sql
from garmin_mcp.tools.common import (
    check_range,
    check_wear,
    connect_read_only,
    query_rows,
)

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


def _wear_filter(min_wear_hours: float) -> str:
    # 0 keeps every stored day, including ones with no wear_s at all.
    return "TRUE" if min_wear_hours == 0 else "wear_s >= ?"


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
    check_range(start, end)
    check_wear(min_wear_hours)
    names = list(DAILY_METRICS) if metrics is None else metrics
    unknown = [m for m in names if m not in DAILY_METRICS]
    if unknown or not names:
        raise ToolError(
            f"Unknown metrics {unknown}. Valid: {', '.join(DAILY_METRICS)}."
        )
    cols = ", ".join(f"{DAILY_METRICS[m]} AS {m}" for m in names)
    wear_params = [] if min_wear_hours == 0 else [min_wear_hours * 3600]
    daily_sql = f"""
        SELECT date, round(wear_s / 3600, 1) AS wear_h, {cols}
        FROM daily
        WHERE date BETWEEN ? AND ? AND {_wear_filter(min_wear_hours)}
        ORDER BY date
    """
    with connect_read_only() as con:
        days = query_rows(con, daily_sql, [start, end, *wear_params])
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
    training = query_rows(con, sql("training_period"), [sorted(RUN_TYPES), start, end])[
        0
    ]
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
    row = query_rows(
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
    check_range(a_start, a_end, "a_")
    check_range(b_start, b_end, "b_")
    check_wear(min_wear_hours)
    with connect_read_only() as con:
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
