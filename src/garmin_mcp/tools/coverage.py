"""Coverage notes: warn when a tool is asked about dates the data doesn't reach yet,
so "not synced" isn't mistaken for "nothing happened"."""

from datetime import date, timedelta

import duckdb

from garmin_mcp.queries import sql

# Data source -> (fetch command that syncs it, SQL for its latest stored date).
SOURCES = {
    "activities": ("garmin-fetch", "SELECT max(activity_date) FROM activities"),
    "daily": ("garmin-fetch-daily", "SELECT max(date) FROM daily"),
}


def has_table(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    return bool(
        con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
            [name],
        ).fetchone()[0]
    )


def coverage_note(con: duckdb.DuckDBPyConnection, source: str, end: date) -> str | None:
    """A note if `end` is past what's been synced for `source`, else None.

    Coverage is the furthest date a sync of that source covered (sync_log), so a
    synced day with no activity counts as covered. Databases without sync records
    fall back to the latest stored date.
    """
    command, latest_sql = SOURCES[source]
    covered, last_sync = None, None
    if has_table(con, "sync_log"):
        covered, last_sync = con.execute(sql("sync_coverage"), [command]).fetchone()
    if covered is None:
        covered = con.execute(latest_sql).fetchone()[0]
    if covered is None:
        return f"No {source} data has been loaded yet (run `uv run {command}`)."
    if end <= covered:
        return None
    synced = f" (last sync {last_sync:%Y-%m-%d %H:%M} UTC)" if last_sync else ""
    return (
        f"{source} data only reaches {covered}{synced}; "
        f"{covered + timedelta(days=1)} to {end} isn't loaded. Say the data may be "
        "incomplete for those days rather than treating them as empty."
    )
