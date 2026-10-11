"""Audit log of MCP tool calls, in its own DuckDB file (data/audit.duckdb).

One row per call: when, which tool, the arguments, whether it succeeded, how long it
took and how big the result was. Result contents are never stored. A failure to write
the log is reported on stderr and never fails the tool call itself.
"""

import functools
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

DEFAULT_AUDIT_PATH = "data/audit.duckdb"

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS audit_log_id;
CREATE TABLE IF NOT EXISTS audit_log (
    id           BIGINT PRIMARY KEY DEFAULT nextval('audit_log_id'),
    called_at_utc TIMESTAMP NOT NULL,
    tool         VARCHAR NOT NULL,
    args         JSON,
    ok           BOOLEAN NOT NULL,
    error        VARCHAR,
    duration_ms  DOUBLE,
    row_count    INTEGER,
    result_bytes INTEGER
);
"""


def audit_path() -> Path:
    return Path(os.getenv("GARMIN_AUDIT_PATH", DEFAULT_AUDIT_PATH))


def _row_count(result: Any) -> int | None:
    if not isinstance(result, dict):
        return None
    for key in ("count", "row_count"):
        if isinstance(result.get(key), int):
            return result[key]
    if isinstance(result.get("days"), list):
        return len(result["days"])
    return None


def record(
    tool: str,
    args: dict[str, Any],
    ok: bool,
    duration_ms: float,
    error: str | None = None,
    result: Any = None,
) -> None:
    """Append one row. Never raises: the log must not break the tool call."""
    try:
        path = audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with duckdb.connect(str(path)) as con:
            con.execute(SCHEMA)
            con.execute(
                "INSERT INTO audit_log (called_at_utc, tool, args, ok, error, "
                "duration_ms, row_count, result_bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    datetime.now(UTC).replace(tzinfo=None),
                    tool,
                    json.dumps(args, default=str),
                    ok,
                    error,
                    round(duration_ms, 1),
                    _row_count(result),
                    None if result is None else len(json.dumps(result, default=str)),
                ],
            )
    except Exception as e:  # noqa: BLE001 - auditing is best effort by design
        print(f"audit log write failed: {type(e).__name__}: {e}", file=sys.stderr)


def audited(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Log every call of a tool function. Put it under @mcp.tool(); functools.wraps
    keeps the signature and docstring the SDK builds the tool schema from."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        call_args = {**dict(zip(fn.__code__.co_varnames, args)), **kwargs}
        start = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            record(
                fn.__name__,
                call_args,
                ok=False,
                duration_ms=(time.perf_counter() - start) * 1000,
                error=str(e),
            )
            raise
        record(
            fn.__name__,
            call_args,
            ok=True,
            duration_ms=(time.perf_counter() - start) * 1000,
            result=result,
        )
        return result

    return wrapper
