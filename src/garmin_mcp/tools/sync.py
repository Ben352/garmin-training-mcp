"""sync_garmin: pull new data from Garmin by running the fetch commands.

Each command runs as a child process, exactly as from a terminal, so:
- its printed progress is captured and can't corrupt the MCP stdio channel;
- it has no terminal, so an expired token exits with a message instead of
  waiting for a password prompt;
- a crash or hang can't take down the server, and a timeout stops it.
"""

import subprocess
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from garmin_mcp.tools.common import connect_read_only, jsonable
from garmin_mcp.tools.coverage import has_table
from garmin_mcp.tools.status import data_status

COOLDOWN = timedelta(minutes=15)  # protects the Garmin account from rate limits
TIMEOUT_S = 180  # for all three commands together
OVERLAP_DAYS = 2  # re-fetch the last days: catches late uploads and edits
MAX_DAYS = 90  # per sync; older gaps are left to a manual fetch

# (command, module, takes --start/--end)
STEPS = [
    ("garmin-fetch", "garmin_mcp.fetch_activities", True),
    ("garmin-fetch-runs", "garmin_mcp.fetch_runs", False),
    ("garmin-fetch-daily", "garmin_mcp.fetch_daily", True),
]

_running = threading.Lock()


def _sync_state() -> tuple[datetime | None, date | None, int]:
    """(last sync time UTC, date both activities and daily are synced up to,
    highest sync_log id)."""
    with connect_read_only() as con:
        if not has_table(con, "sync_log"):
            return None, None, 0
        last_run, covered, max_id = con.execute(
            "SELECT max(ran_at_utc), "
            "least(max(range_end) FILTER (WHERE command = 'garmin-fetch'), "
            "      max(range_end) FILTER (WHERE command = 'garmin-fetch-daily')), "
            "coalesce(max(id), 0) "
            "FROM sync_log"
        ).fetchone()
    return last_run, covered, max_id


def _new_syncs(after_id: int) -> list[dict[str, Any]]:
    with connect_read_only() as con:
        rows = con.execute(
            "SELECT command, range_start, range_end, rows_written FROM sync_log "
            "WHERE id > ? ORDER BY id",
            [after_id],
        ).fetchall()
    return [
        {
            "command": c,
            "range_start": jsonable(s),
            "range_end": jsonable(e),
            "rows_written": n,
        }
        for c, s, e, n in rows
    ]


def _error_text(output: str) -> str:
    """The end of a failed command's output: where its exit message is."""
    lines = [line.strip() for line in output.replace("\r", "\n").splitlines()]
    return " ".join(line for line in lines if line)[-500:]


def sync_garmin() -> dict[str, Any]:
    """Fetch new data from Garmin Connect into the local database.

    Use this when data_status (or a tool's "notes") shows the data doesn't cover
    the days the user is asking about, and the user wants it updated. It contacts
    Garmin, so don't call it repeatedly: it's limited to once every 15 minutes.

    Syncs from 2 days before the last synced date up to today (at most 90 days):
    the activity list, details for new runs, VO2 max, and daily wellness/sleep.
    Usually takes a few seconds to a minute.

    Returns:
        {"synced": [{command, range_start, range_end, rows_written}, ...],
        "duration_s", "range_capped" (true if older days were left out),
        "data_status": the same as the data_status tool, after the sync}.
        If the Garmin session has expired, the error says to log in once from a
        terminal; tell the user that.
    """
    if not _running.acquire(blocking=False):
        raise ToolError("A sync is already running. Wait for it to finish.")
    try:
        last_run, covered, max_id = _sync_state()
        now = datetime.now(UTC).replace(tzinfo=None)
        # Clamped at 0 so a clock difference can't produce a negative "ago".
        elapsed = max(now - last_run, timedelta(0)) if last_run else None
        if elapsed is not None and elapsed < COOLDOWN:
            wait_min = int((COOLDOWN - elapsed).total_seconds() // 60) + 1
            raise ToolError(
                f"Last sync was {int(elapsed.total_seconds() // 60)} min ago. "
                "Syncing is limited to once every 15 minutes to avoid Garmin rate "
                f"limits; try again in {wait_min} min."
            )

        end = date.today()
        start = (covered - timedelta(days=OVERLAP_DAYS)) if covered else None
        earliest = end - timedelta(days=MAX_DAYS)
        range_capped = start is not None and start < earliest
        if start is None or range_capped:
            start = earliest
        start = min(start, end)

        began = time.monotonic()
        for command, module, dated in STEPS:
            args = ["--start", start.isoformat(), "--end", end.isoformat()]
            remaining = TIMEOUT_S - (time.monotonic() - began)
            try:
                done = subprocess.run(
                    [sys.executable, "-m", module, *(args if dated else [])],
                    check=False,  # non-zero exits are reported below
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=max(remaining, 1),
                )
            except subprocess.TimeoutExpired:
                raise ToolError(
                    f"Sync timed out after {TIMEOUT_S} s during {command}. Earlier "
                    "steps were saved; run the fetch commands from a terminal."
                ) from None
            if done.returncode != 0:
                saved = _new_syncs(max_id)
                raise ToolError(
                    f"{command} failed: {_error_text(done.stderr or done.stdout)} "
                    f"Steps completed before it: "
                    f"{', '.join(s['command'] for s in saved) or 'none'}."
                )
        return {
            "synced": _new_syncs(max_id),
            "duration_s": round(time.monotonic() - began, 1),
            "range_capped": range_capped,
            "data_status": data_status(),
        }
    finally:
        _running.release()
