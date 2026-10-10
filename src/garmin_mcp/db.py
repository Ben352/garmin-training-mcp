"""DuckDB storage: connection, schema, and the two write patterns (merge, replace)."""

import os
from importlib.resources import files
from pathlib import Path
from typing import Any

import duckdb

DEFAULT_DB_PATH = "data/garmin.duckdb"


def db_path() -> Path:
    return Path(os.getenv("GARMIN_DB_PATH", DEFAULT_DB_PATH))


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the database. Writers should keep the connection short-lived: DuckDB
    allows only one process to hold a read-write connection at a time."""
    path = db_path()
    if read_only:
        return duckdb.connect(str(path), read_only=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(files("garmin_mcp").joinpath("schema.sql").read_text())
    return con


def _insert_sql(table: str, columns: list[str], or_replace: bool) -> str:
    verb = "INSERT OR REPLACE" if or_replace else "INSERT"
    cols = ", ".join(columns)
    params = ", ".join("?" for _ in columns)
    return f"{verb} INTO {table} ({cols}) VALUES ({params})"


def merge(
    con: duckdb.DuckDBPyConnection, table: str, rows: list[dict[str, Any]]
) -> None:
    """Insert rows, overwriting any existing row with the same primary key.

    Rows outside this batch are untouched, so repeated fetches of overlapping date
    ranges build up history without duplicates.
    """
    if not rows:
        return
    columns = list(rows[0])
    con.executemany(
        _insert_sql(table, columns, or_replace=True),
        [[r[c] for c in columns] for r in rows],
    )


def replace(
    con: duckdb.DuckDBPyConnection, table: str, rows: list[dict[str, Any]]
) -> None:
    """Replace the whole table's contents atomically."""
    con.execute("BEGIN")
    try:
        con.execute(f"DELETE FROM {table}")
        if rows:
            columns = list(rows[0])
            con.executemany(
                _insert_sql(table, columns, or_replace=False),
                [[r[c] for c in columns] for r in rows],
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
