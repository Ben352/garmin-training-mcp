"""schema://tables: tables, columns and row counts, for writing SQL for `query`."""

from garmin_mcp import db
from garmin_mcp.queries import sql
from garmin_mcp.tools.query import GPS_COLUMNS

TABLE_NOTES = {
    "activities": "One row per run or strength session (key activity_id). "
    "activity_date is the local start date. avg_pace_s_per_km is moving-time "
    "based, runs only. distance_m is NULL for strength.",
    "daily": "One row per day (key date): steps, resting HR, stress, body "
    "battery, intensity minutes, sleep, overnight HRV. Sleep belongs to the "
    "wake-up date. Filter wear_s >= 4*3600 for reliable HR/steps. Sleep/HRV "
    "are sparse.",
    "vo2max": "Only dates Garmin changed VO2 max (key date). For the value on "
    "a given day use ASOF JOIN ... ON x.date >= vo2max.date.",
    "runs": "One row per run with full detail (key activity_id, joins "
    "activities). avg_pace_s_per_km is timer based; also moving, GAP and best "
    "pace. hr_z1_s..hr_z5_s = seconds per HR zone. workout_feel 0/25/50/75/100, "
    "workout_rpe 1-10. weather_temp_c, weather_wind_speed_mph.",
    "run_laps": "Laps as the watch recorded them (key activity_id, lap_index): "
    "auto-laps or workout steps; intensity_type INTERVAL/ACTIVE/RECOVERY/...",
    "run_km_splits": "Per-kilometre splits computed from the 1 s time series "
    "(key activity_id, km). is_partial marks the final short split.",
    "run_fetch_log": "Bookkeeping: which runs have details loaded.",
    "sync_log": "One row per fetch command run against Garmin: command, ran_at_utc, "
    "the date range covered and rows written. Latest row per command = last sync.",
}


def schema_tables() -> str:
    if not db.db_path().exists():
        return f"No database at {db.db_path()} yet. Run `uv run garmin-fetch` first."
    try:
        con = db.connect(read_only=True)
    except db.DatabaseBusy as e:
        return str(e)
    with con:
        columns = con.execute(sql("schema_columns")).fetchall()
        lines = [
            "# Garmin tables (DuckDB)",
            "",
            (
                "Units are in column names (_m, _s, _bpm, _s_per_km, _c, _pct, ...). "
                "Dates are local. Join run tables to activities on activity_id and "
                "daily/vo2max on activity_date = date."
            ),
        ]
        existing = {tbl for tbl, _, _ in columns}
        for table, note in TABLE_NOTES.items():
            if table not in existing:  # older database: created by the next fetch
                lines += [
                    "",
                    f"## {table} (not created yet: run a fetch command)",
                    note,
                ]
                continue
            # Table names can't be ? parameters; these come from TABLE_NOTES, not
            # the model, so formatting them in is safe.
            (n,) = con.execute(f"SELECT count(*) FROM {table}").fetchone()
            cols = [
                f"{c} {t}"
                for tbl, c, t in columns
                if tbl == table and c not in GPS_COLUMNS
            ]
            lines += ["", f"## {table} ({n} rows)", note, "", ", ".join(cols)]
    return "\n".join(lines) + "\n"
