import json

import anyio
import duckdb
from mcp.client import Client

from garmin_mcp import audit, db
from garmin_mcp.server import mcp


def call(name, args):
    async def go():
        async with Client(mcp) as client:
            return await client.call_tool(name, args)

    return anyio.run(go)


def audit_rows():
    with duckdb.connect(str(audit.audit_path()), read_only=True) as con:
        cursor = con.execute("SELECT * FROM audit_log ORDER BY id")
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, r)) for r in cursor.fetchall()]


def test_successful_and_failed_calls_are_logged(isolated_db):
    with db.connect() as con:
        db.merge(
            con,
            "daily",
            [{"date": "2026-09-01", "wear_s": 50000, "steps": 9000}],
        )
    assert not call(
        "daily_metrics", {"start": "2026-09-01", "end": "2026-09-02"}
    ).is_error
    assert call("query", {"sql": "DROP TABLE daily"}).is_error

    ok, failed = audit_rows()
    assert ok["tool"] == "daily_metrics" and ok["ok"] is True
    assert json.loads(ok["args"]) == {
        "start": "2026-09-01",
        "end": "2026-09-02",
        "metrics": None,
        "min_wear_hours": 4,
    }
    assert ok["row_count"] == 1 and ok["result_bytes"] > 0
    assert ok["duration_ms"] >= 0 and ok["error"] is None
    assert failed["tool"] == "query" and failed["ok"] is False
    assert "Only SELECT" in failed["error"]
    assert failed["result_bytes"] is None


def test_result_contents_are_not_stored(isolated_db):
    with db.connect() as con:
        db.merge(
            con, "daily", [{"date": "2026-09-01", "wear_s": 50000, "steps": 98765}]
        )
    call("query", {"sql": "SELECT steps FROM daily"})
    [row] = audit_rows()
    assert "98765" not in json.dumps(row, default=str)


def test_audit_failure_never_breaks_the_tool(isolated_db, monkeypatch, capsys):
    db.connect().close()
    # A directory can't be opened as a database file.
    monkeypatch.setenv("GARMIN_AUDIT_PATH", str(isolated_db.parent))
    result = call("query", {"sql": "SELECT 1 AS x"})
    assert not result.is_error
    assert result.structured_content["rows"] == [{"x": 1}]
    assert "audit log write failed" in capsys.readouterr().err
