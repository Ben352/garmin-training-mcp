"""Helpers shared by the tools: read-only connection, row serialising, checks."""

from datetime import date, datetime
from typing import Any

import duckdb
from mcp.server.mcpserver.exceptions import ToolError

from garmin_mcp import db

MAX_LIMIT = 500
MAX_DAYS = 500


def format_pace(seconds_per_km: float | None) -> str | None:
    """302.4 -> '5:02'."""
    if seconds_per_km is None:
        return None
    minutes, seconds = divmod(round(seconds_per_km), 60)
    return f"{minutes}:{seconds:02d}"


def connect_read_only() -> duckdb.DuckDBPyConnection:
    if not db.db_path().exists():
        raise ToolError(
            f"No database at {db.db_path()}. Run `uv run garmin-fetch` first."
        )
    try:
        return db.connect(read_only=True)
    except db.DatabaseBusy as e:
        raise ToolError(str(e)) from None


def jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return value


def query_rows(
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
            row[col] = jsonable(val)
            if col.endswith("_s_per_km"):
                row[col.removesuffix("_s_per_km") + "_min_per_km"] = format_pace(val)
        rows.append(row)
    return rows


def check_range(start: date, end: date, label: str = "") -> None:
    if start > end:
        raise ToolError(
            f"{label}start ({start}) must be on or before {label}end ({end})."
        )
    if (end - start).days + 1 > MAX_DAYS:
        raise ToolError(f"Date range is limited to {MAX_DAYS} days; narrow it.")


def check_wear(min_wear_hours: float) -> None:
    if not 0 <= min_wear_hours <= 24:
        raise ToolError(
            f"min_wear_hours must be between 0 and 24, got {min_wear_hours}."
        )
