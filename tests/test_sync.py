"""sync_garmin runs the real fetch commands as child processes. With
GARMIN_SYNTHETIC=1 they use the synthetic client, so no Garmin account is needed."""

import re
from datetime import UTC, date, datetime, timedelta

import anyio
import pytest
from mcp.client import Client

from garmin_mcp import db
from garmin_mcp.server import mcp


def call(name, args=None):
    async def go():
        async with Client(mcp) as client:
            return await client.call_tool(name, args or {})

    return anyio.run(go)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Child processes inherit this env: CSVs and raw cache go to tmp, the database
    is the test's temp database (from conftest)."""
    monkeypatch.setenv("GARMIN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GARMINTOKENS", str(tmp_path / "no-tokens"))
    return tmp_path


def last_synced(days_ago, ran_hours_ago=24):
    """A previous sync covering up to `days_ago` days before today."""
    covered = date.today() - timedelta(days=days_ago)
    with db.connect() as con:
        for command in ("garmin-fetch", "garmin-fetch-runs", "garmin-fetch-daily"):
            db.log_sync(con, command, covered - timedelta(days=30), covered, 1)
        con.execute(
            "UPDATE sync_log SET ran_at_utc = ?",
            [datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=ran_hours_ago)],
        )


def test_sync_fetches_from_last_synced_date_to_today(workspace, monkeypatch):
    monkeypatch.setenv("GARMIN_SYNTHETIC", "1")
    last_synced(days_ago=5)
    result = call("sync_garmin")
    assert not result.is_error, result.content[0].text
    data = result.structured_content
    start = (date.today() - timedelta(days=7)).isoformat()  # 5 days + 2 overlap
    assert [s["command"] for s in data["synced"]] == [
        "garmin-fetch",
        "garmin-fetch-runs",
        "garmin-fetch-daily",
    ]
    assert data["synced"][0]["range_start"] == start
    assert data["synced"][0]["range_end"] == date.today().isoformat()
    assert data["synced"][2]["rows_written"] > 0  # daily rows for the new days
    assert data["range_capped"] is False
    assert data["data_status"]["daily"]["days_since_sync"] == 0


def test_first_sync_is_capped_at_90_days(workspace, monkeypatch):
    monkeypatch.setenv("GARMIN_SYNTHETIC", "1")
    last_synced(days_ago=400)
    data = call("sync_garmin").structured_content
    assert data["range_capped"] is True
    assert (
        data["synced"][0]["range_start"]
        == (date.today() - timedelta(days=90)).isoformat()
    )


def test_sync_is_refused_within_cooldown(workspace):
    last_synced(days_ago=0, ran_hours_ago=0.1)  # 6 minutes ago
    result = call("sync_garmin")
    assert result.is_error
    assert "once every 15 minutes" in result.content[0].text
    assert re.search(r"was 6 min ago.*try again in (9|10) min", result.content[0].text)


def test_expired_login_error_is_passed_through(workspace, monkeypatch):
    # Real (non-synthetic) mode with an empty token store: the child process has no
    # terminal, so login exits with a message instead of prompting. No network used.
    monkeypatch.delenv("GARMIN_SYNTHETIC", raising=False)
    last_synced(days_ago=3)
    result = call("sync_garmin")
    assert result.is_error
    text = result.content[0].text
    assert "garmin-fetch failed" in text
    assert "in a terminal once to log in" in text
    assert "Steps completed before it: none" in text
