"""data_status: how current the data is, and when it was last synced."""

from datetime import UTC, date
from typing import Any

from garmin_mcp.fetch_activities import RUN_TYPES
from garmin_mcp.queries import sql
from garmin_mcp.tools.common import connect_read_only, jsonable

# Which fetch command keeps each data source up to date.
SOURCES = {
    "activities": "garmin-fetch",
    "runs": "garmin-fetch-runs",
    "daily": "garmin-fetch-daily",
    "vo2max": "garmin-fetch-runs",
}


def data_status() -> dict[str, Any]:
    """How current the user's Garmin data is: the latest date in each data source
    and when it was last synced from Garmin.

    Call this before answering about recent days ("this week", "yesterday",
    "my last run") to check the data actually covers them. If a source's
    latest_date is before the period asked about, say the data may be incomplete
    and when it was last synced, rather than answering as if nothing happened.
    latest_date alone can't tell "no activity" from "not synced": compare it with
    last_synced_utc.

    Returns:
        {"today": local date, "synthetic": bool or null (true = generated demo
        data), and for each of activities, runs, daily, vo2max: latest_date
        (newest date with data), rows, last_synced_utc (when the fetch command
        that updates it last ran; null if never recorded), days_since_sync}.
        runs also has pending_details (runs listed whose details aren't fetched
        yet); daily has latest_sleep_date. A "note" explains missing sync info.
    """
    today = date.today()
    with connect_read_only() as con:
        cursor = con.execute(sql("data_coverage"), [sorted(RUN_TYPES)])
        columns = [d[0] for d in cursor.description]
        coverage = dict(zip(columns, cursor.fetchone()))
        has_sync_log = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name = 'sync_log'"
        ).fetchone()[0]
        syncs = (
            {
                cmd: (ran, synth)
                for cmd, ran, synth in con.execute(sql("last_syncs")).fetchall()
            }
            if has_sync_log
            else {}
        )

    result: dict[str, Any] = {"today": today.isoformat()}
    for source, command in SOURCES.items():
        ran_at, _ = syncs.get(command, (None, None))
        info = {
            "latest_date": jsonable(coverage[f"{source}_latest_date"]),
            "rows": coverage[f"{source}_rows"],
            "last_synced_utc": jsonable(ran_at.replace(microsecond=0))
            if ran_at
            else None,
            "days_since_sync": (
                (today - ran_at.replace(tzinfo=UTC).astimezone().date()).days
                if ran_at
                else None
            ),
        }
        if source == "runs":
            info["pending_details"] = coverage["runs_pending_details"]
        if source == "daily":
            info["latest_sleep_date"] = jsonable(coverage["daily_latest_sleep_date"])
        result[source] = info

    flags = {synth for _, synth in syncs.values()}
    result["synthetic"] = flags.pop() if len(flags) == 1 else None
    if not syncs:
        result["note"] = (
            "No sync recorded yet: the database predates sync tracking or was never "
            "synced. latest_date still shows how far the data reaches."
        )
    return result
