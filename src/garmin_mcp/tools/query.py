"""query: one read-only SELECT written by the model, in a sandbox."""

import threading
from typing import Any

import duckdb
from mcp.server.mcpserver.exceptions import ToolError

from garmin_mcp import db, sql_guard
from garmin_mcp.tools.common import MAX_LIMIT, format_pace, jsonable

GPS_COLUMNS = ("start_lat", "start_lon", "end_lat", "end_lon")
QUERY_TIMEOUT_S = 10


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


def query(sql: str) -> dict[str, Any]:
    """Run one read-only DuckDB SELECT over the Garmin tables, for questions the
    other tools can't answer (custom aggregations, correlations, joins).

    Prefer the dedicated tools when they fit. Read the schema://tables resource
    for columns, units and join keys. Tables: activities, daily, vo2max, runs,
    run_laps, run_km_splits, run_fetch_log, sync_log. Units are in column names.

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
            row[col] = jsonable(val)
            if col.endswith("_s_per_km") and isinstance(val, int | float):
                row[col.removesuffix("_s_per_km") + "_min_per_km"] = format_pace(val)
        rows.append(row)
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": len(fetched) > MAX_LIMIT,
    }
