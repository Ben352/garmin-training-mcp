import duckdb
import pytest

from garmin_mcp import db
from garmin_mcp.fetch_activities import to_db_row, to_row


def day(date, steps):
    return {"date": date, "steps": steps}


def rows(con, sql):
    return con.execute(sql).fetchall()


def test_schema_creates_all_tables():
    with db.connect() as con:
        tables = {
            r[0] for r in rows(con, "SELECT table_name FROM information_schema.tables")
        }
    assert tables >= {
        "activities",
        "daily",
        "vo2max",
        "runs",
        "run_laps",
        "run_km_splits",
        "run_fetch_log",
    }


def test_merge_same_batch_twice_does_not_duplicate():
    batch = [day("2026-10-01", 9000), day("2026-10-02", 7000)]
    with db.connect() as con:
        db.merge(con, "daily", batch)
        db.merge(con, "daily", batch)
        assert rows(con, "SELECT count(*) FROM daily") == [(2,)]


def test_merge_updates_existing_and_keeps_history_outside_batch():
    with db.connect() as con:
        db.merge(con, "daily", [day("2026-09-01", 5000), day("2026-10-01", 9000)])
        # Later fetch with a narrower window that re-reports 10-01 with a new value.
        db.merge(con, "daily", [day("2026-10-01", 9500), day("2026-10-02", 7000)])
        assert rows(con, "SELECT date::VARCHAR, steps FROM daily ORDER BY date") == [
            ("2026-09-01", 5000),  # outside the new window: kept
            ("2026-10-01", 9500),  # overlapping: updated
            ("2026-10-02", 7000),  # new
        ]


def test_merge_persists_across_connections():
    with db.connect() as con:
        db.merge(con, "daily", [day("2026-10-01", 9000)])
    with db.connect(read_only=True) as con:
        assert rows(con, "SELECT steps FROM daily") == [(9000,)]


def test_replace_swaps_whole_table():
    with db.connect() as con:
        db.replace(
            con,
            "vo2max",
            [{"date": "2026-09-01", "vo2max": 50.0, "vo2max_precise": 50.2}],
        )
        db.replace(
            con,
            "vo2max",
            [{"date": "2026-10-01", "vo2max": 51.0, "vo2max_precise": 51.1}],
        )
        assert rows(con, "SELECT date::VARCHAR FROM vo2max") == [("2026-10-01",)]


def test_replace_rolls_back_on_error():
    with db.connect() as con:
        db.replace(
            con,
            "vo2max",
            [{"date": "2026-09-01", "vo2max": 50.0, "vo2max_precise": 50.2}],
        )
        bad = [{"date": "not-a-date", "vo2max": 1.0, "vo2max_precise": 1.0}]
        with pytest.raises(duckdb.ConversionException):
            db.replace(con, "vo2max", bad)
        assert rows(con, "SELECT count(*) FROM vo2max") == [(1,)]


def test_activity_db_row_cleaning():
    strength = to_row(
        {
            "activityId": 2,
            "activityType": {"typeKey": "strength_training"},
            "startTimeLocal": "2026-10-01 23:30:00",
            "startTimeGMT": "2026-10-01 21:30:00",
            "distance": 0.0,
            "duration": 2400.0000000001,
        }
    )
    row = to_db_row(strength)
    assert row["distance_m"] is None
    assert row["duration_s"] == 2400.0
    assert row["activity_date"] == "2026-10-01"
    assert (
        "start_time_gmt" not in row and row["start_time_utc"] == "2026-10-01 21:30:00"
    )
    with db.connect() as con:
        db.merge(con, "activities", [row])
        assert rows(
            con, "SELECT activity_date::VARCHAR, distance_m FROM activities"
        ) == [("2026-10-01", None)]
