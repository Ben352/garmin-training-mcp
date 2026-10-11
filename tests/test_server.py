import anyio
import pytest
from mcp.client import Client

from garmin_mcp import db
from garmin_mcp.server import format_pace, mcp


def activity(aid, day, type_="running", distance_m=10000.0, pace=300.0):
    return {
        "activity_id": aid,
        "activity_type": type_,
        "activity_date": day,
        "start_time_local": f"{day} 07:00:00",
        "start_time_utc": f"{day} 05:00:00",
        "distance_m": distance_m,
        "duration_s": 3000.0,
        "avg_pace_s_per_km": pace,
        "start_lat": 51.5,
        "start_lon": -0.1,
    }


@pytest.fixture
def seeded():
    with db.connect() as con:
        db.merge(
            con,
            "activities",
            [
                activity(1, "2026-09-01"),
                activity(2, "2026-09-15", "strength_training", None, None),
                activity(3, "2026-09-30", pace=302.4),
                activity(4, "2026-10-05"),
            ],
        )


def call(name, args):
    async def go():
        async with Client(mcp) as client:
            return await client.call_tool(name, args)

    return anyio.run(go)


def test_tools_are_listed_with_docstrings_and_read_only_hint():
    async def go():
        async with Client(mcp) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in anyio.run(go)}
    assert set(tools) == {
        "list_activities",
        "get_run",
        "daily_metrics",
        "compare_periods",
        "query",
    }
    assert all(t.annotations.read_only_hint for t in tools.values())
    assert "newest first" in tools["list_activities"].description
    assert set(tools["list_activities"].input_schema["required"]) == {"start", "end"}
    assert tools["get_run"].input_schema["required"] == ["activity_id"]


def test_date_range_is_inclusive_and_newest_first(seeded):
    result = call("list_activities", {"start": "2026-09-01", "end": "2026-09-30"})
    assert not result.is_error
    data = result.structured_content
    assert [a["activity_id"] for a in data["activities"]] == [3, 2, 1]
    assert data["count"] == 3 and data["truncated"] is False


def test_filter_by_type(seeded):
    data = call(
        "list_activities",
        {"start": "2026-01-01", "end": "2026-12-31", "activity_type": "running"},
    ).structured_content
    assert [a["activity_id"] for a in data["activities"]] == [4, 3, 1]


def test_units_and_formatting(seeded):
    data = call(
        "list_activities", {"start": "2026-09-30", "end": "2026-09-30"}
    ).structured_content
    [run] = data["activities"]
    assert run["distance_km"] == 10.0
    assert run["duration_min"] == 50.0
    assert run["avg_pace_min_per_km"] == "5:02"
    assert run["activity_date"] == "2026-09-30"
    assert "start_lat" not in run  # GPS stays out of tool output


def test_limit_sets_truncated(seeded):
    data = call(
        "list_activities", {"start": "2026-01-01", "end": "2026-12-31", "limit": 2}
    ).structured_content
    assert data["count"] == 2 and data["truncated"] is True


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"start": "2026-10-01", "end": "2026-09-01"}, "must be on or before"),
        ({"start": "2026-09-01", "end": "2026-10-01", "limit": 0}, "limit must be"),
        ({"start": "2026-09-01", "end": "2026-10-01", "limit": 501}, "limit must be"),
        (
            {"start": "2026-09-01", "end": "2026-10-01", "activity_type": "run"},
            "Valid: running, strength_training",
        ),
    ],
)
def test_bad_input_returns_actionable_error(seeded, args, message):
    result = call("list_activities", args)
    assert result.is_error
    assert message in result.content[0].text


def test_missing_database_returns_actionable_error():
    result = call("list_activities", {"start": "2026-09-01", "end": "2026-10-01"})
    assert result.is_error
    assert "garmin-fetch" in result.content[0].text


def test_format_pace():
    assert format_pace(302.4) == "5:02"
    assert format_pace(359.6) == "6:00"
    assert format_pace(None) is None


# ---------------------------------------------------------------- get_run


@pytest.fixture
def seeded_run(seeded):
    """Activity 3 (2026-09-30) gets full run detail; activity 4 is listed but not fetched."""
    with db.connect() as con:
        db.merge(
            con,
            "runs",
            [
                {
                    "activity_id": 3,
                    "start_time_local": "2026-09-30 07:00:00",
                    "distance_m": 2500.0,
                    "duration_s": 750.0,
                    "avg_pace_s_per_km": 300.0,
                    "avg_gap_s_per_km": 295.0,
                    "avg_hr_bpm": 150,
                    "workout_rpe": 6.0,
                    "hr_z1_s": 75,
                    "hr_z2_s": 300,
                    "hr_z3_s": 375,
                    "hr_z4_s": 0,
                    "hr_z5_s": 0,
                    "weather_temp_c": 14.5,
                    "weather_desc": "Cloudy",
                }
            ],
        )
        db.merge(
            con,
            "run_km_splits",
            [
                {
                    "activity_id": 3,
                    "km": k,
                    "distance_m": d,
                    "is_partial": d < 1000,
                    "duration_s": t,
                    "pace_s_per_km": t / (d / 1000),
                }
                for k, d, t in [
                    (2, 1000.0, 295.0),
                    (1, 1000.0, 305.0),
                    (3, 500.0, 150.0),
                ]
            ],
        )
        db.merge(
            con,
            "run_laps",
            [
                {
                    "activity_id": 3,
                    "lap_index": 1,
                    "intensity_type": "ACTIVE",
                    "distance_m": 2500.0,
                    "duration_s": 750.0,
                    "pace_s_per_km": 300.0,
                }
            ],
        )
        db.merge(
            con,
            "vo2max",
            [
                {"date": "2026-09-10", "vo2max": 50.0, "vo2max_precise": 50.4},
                {"date": "2026-09-28", "vo2max": 51.0, "vo2max_precise": 50.9},
                {"date": "2026-10-02", "vo2max": 52.0, "vo2max_precise": 51.6},
            ],
        )


def test_get_run_returns_all_sections(seeded_run):
    result = call("get_run", {"activity_id": 3})
    assert not result.is_error
    run = result.structured_content
    assert set(run) == {"summary", "hr_zones", "weather", "km_splits", "laps"}
    s = run["summary"]
    assert s["distance_km"] == 2.5 and s["duration_min"] == 12.5
    assert s["avg_pace_min_per_km"] == "5:00" and s["avg_gap_min_per_km"] == "4:55"
    assert s["workout_rpe"] == 6.0
    assert run["weather"] == {
        "temp_c": 14.5,
        "apparent_temp_c": None,
        "humidity_pct": None,
        "wind_speed_mph": None,
        "description": "Cloudy",
    }
    assert run["laps"][0]["intensity_type"] == "ACTIVE"


def test_get_run_vo2max_is_latest_on_or_before_run_date(seeded_run):
    s = call("get_run", {"activity_id": 3}).structured_content["summary"]
    assert s["vo2max_at_run"] == 50.9  # 09-28 value, not the later 10-02 one
    assert s["vo2max_as_of"] == "2026-09-28"


def test_get_run_hr_zones_have_minutes_and_percent(seeded_run):
    zones = call("get_run", {"activity_id": 3}).structured_content["hr_zones"]
    assert [z["zone"] for z in zones] == [1, 2, 3, 4, 5]
    assert [z["pct"] for z in zones] == [10.0, 40.0, 50.0, 0.0, 0.0]
    assert zones[1]["minutes"] == 5.0


def test_get_run_splits_ordered_with_formatted_pace(seeded_run):
    splits = call("get_run", {"activity_id": 3}).structured_content["km_splits"]
    assert [(x["km"], x["pace_min_per_km"], x["is_partial"]) for x in splits] == [
        (1, "5:05", False),
        (2, "4:55", False),
        (3, "5:00", True),
    ]


@pytest.mark.parametrize(
    ("activity_id", "message"),
    [
        (999, "No activity with id 999"),
        (2, "'strength_training', not a run"),
        (4, "garmin-fetch-runs"),
    ],
)
def test_get_run_errors_explain_what_to_do(seeded_run, activity_id, message):
    result = call("get_run", {"activity_id": activity_id})
    assert result.is_error
    assert message in result.content[0].text


def day(d, wear_h=12, steps=8000, rhr=50, sleep_h=None, hrv=None):
    return {
        "date": d,
        "wear_s": None if wear_h is None else int(wear_h * 3600),
        "steps": steps,
        "resting_hr_bpm": rhr,
        "sleep_total_s": None if sleep_h is None else int(sleep_h * 3600),
        "hrv_overnight_avg_ms": hrv,
    }


@pytest.fixture
def seeded_daily():
    with db.connect() as con:
        db.merge(
            con,
            "daily",
            [
                day("2026-09-01", sleep_h=7.5, hrv=60.0),
                day("2026-09-02", rhr=52),
                day("2026-09-03", wear_h=2, steps=300, rhr=95),  # partial wear
                day("2026-09-04", wear_h=None, steps=None, rhr=None),
            ],
        )


def test_daily_metrics_filters_low_wear_and_summarises(seeded_daily):
    data = call(
        "daily_metrics",
        {
            "start": "2026-09-01",
            "end": "2026-09-30",
            "metrics": ["resting_hr_bpm", "sleep_h"],
        },
    ).structured_content
    assert [d["date"] for d in data["days"]] == ["2026-09-01", "2026-09-02"]
    assert data["days_excluded_low_wear"] == 2
    assert set(data["days"][0]) == {"date", "wear_h", "resting_hr_bpm", "sleep_h"}
    assert data["summary"]["resting_hr_bpm"] == {
        "n": 2,
        "mean": 51.0,
        "min": 50,
        "max": 52,
    }
    assert data["summary"]["sleep_h"] == {"n": 1, "mean": 7.5, "min": 7.5, "max": 7.5}


def test_daily_metrics_wear_zero_keeps_every_day(seeded_daily):
    data = call(
        "daily_metrics",
        {"start": "2026-09-01", "end": "2026-09-30", "min_wear_hours": 0},
    ).structured_content
    assert len(data["days"]) == 4 and data["days_excluded_low_wear"] == 0
    assert data["summary"]["steps"]["n"] == 3


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"metrics": ["vo2"]}, "Valid: steps"),
        ({"min_wear_hours": 25}, "min_wear_hours must be"),
        ({"end": "2028-06-01"}, "limited to 500 days"),
    ],
)
def test_daily_metrics_errors(seeded_daily, args, message):
    result = call("daily_metrics", {"start": "2026-09-01", "end": "2026-09-30", **args})
    assert result.is_error
    assert message in result.content[0].text


@pytest.fixture
def seeded_periods(seeded_daily):
    with db.connect() as con:
        runs = [
            # a: Aug 1-14, two runs, 20 km at 300 s/km
            activity(10, "2026-08-02"),
            activity(11, "2026-08-09"),
            # b: Sep 1-14, three runs, 30 km at 280 s/km, plus one strength session
            activity(12, "2026-09-01", pace=280.0),
            activity(13, "2026-09-05", pace=280.0),
            activity(14, "2026-09-10", pace=280.0),
            activity(15, "2026-09-11", "strength_training", None, None),
        ]
        for r in runs:
            r["moving_duration_s"] = (
                r["avg_pace_s_per_km"] * r["distance_m"] / 1000
                if r["distance_m"]
                else None
            )
            r["training_load"] = 100.0
        db.merge(con, "activities", runs)
        db.merge(
            con,
            "vo2max",
            [
                {"date": "2026-08-01", "vo2max": 50, "vo2max_precise": 50.0},
                {"date": "2026-09-08", "vo2max": 52, "vo2max_precise": 51.6},
            ],
        )


def test_compare_periods(seeded_periods):
    data = call(
        "compare_periods",
        {
            "a_start": "2026-08-01",
            "a_end": "2026-08-14",
            "b_start": "2026-09-01",
            "b_end": "2026-09-14",
        },
    ).structured_content
    a, b = data["a"]["training"], data["b"]["training"]
    assert (a["runs"], a["run_km"], a["run_km_per_week"]) == (2, 20.0, 10.0)
    assert (b["runs"], b["run_km"], b["run_km_per_week"]) == (3, 30.0, 15.0)
    assert (
        a["run_avg_pace_min_per_km"] == "5:00" and b["run_avg_pace_s_per_km"] == 280.0
    )
    assert (a["strength_sessions"], b["strength_sessions"]) == (0, 1)
    assert (a["vo2max_at_end"], b["vo2max_at_end"]) == (50.0, 51.6)
    assert data["delta"]["training"]["run_km_per_week"] == {"change": 5.0, "pct": 50.0}
    assert data["delta"]["training"]["run_avg_pace_s_per_km"]["change"] == -20.0
    # a has no daily rows: mean null, n 0, delta null
    assert data["a"]["daily_avg"]["steps"] == {"mean": None, "n": 0}
    assert data["b"]["daily_avg"]["resting_hr_bpm"] == {"mean": 51.0, "n": 2}
    assert data["delta"]["daily_avg"]["steps"] == {"change": None, "pct": None}


def test_compare_periods_rejects_reversed_range(seeded_periods):
    result = call(
        "compare_periods",
        {
            "a_start": "2026-08-14",
            "a_end": "2026-08-01",
            "b_start": "2026-09-01",
            "b_end": "2026-09-14",
        },
    )
    assert result.is_error
    assert "a_start" in result.content[0].text


def test_query_returns_rows_and_pace_strings(seeded):
    data = call(
        "query",
        {
            "sql": "SELECT activity_id, avg_pace_s_per_km FROM activities "
            "WHERE activity_type = 'running' ORDER BY activity_id"
        },
    ).structured_content
    assert data["columns"] == ["activity_id", "avg_pace_s_per_km"]
    assert data["row_count"] == 3 and data["truncated"] is False
    assert data["rows"][1] == {
        "activity_id": 3,
        "avg_pace_s_per_km": 302.4,
        "avg_pace_min_per_km": "5:02",
    }


def test_query_caps_rows_at_500(seeded):
    data = call(
        "query",
        {
            "sql": "SELECT a.activity_id FROM activities a, activities b, activities c, "
            "activities d, activities e"
        },  # 4^5 = 1024 rows
    ).structured_content
    assert data["row_count"] == 500 and data["truncated"] is True


def test_query_hides_gps_even_with_star(seeded):
    data = call("query", {"sql": "SELECT * FROM activities"}).structured_content
    assert not set(data["columns"]) & {"start_lat", "start_lon", "end_lat", "end_lon"}
    for sql in [
        "SELECT start_lat FROM activities",
        "SELECT a.start_lon FROM activities a",
    ]:
        result = call("query", {"sql": sql})
        assert result.is_error and "start_" in result.content[0].text
    struct = call(
        "query", {"sql": "SELECT activities FROM activities LIMIT 1"}
    ).structured_content
    assert "start_lat" not in struct["rows"][0]["activities"]


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("DELETE FROM activities", "Only SELECT"),
        ("SELECT * FROM read_csv('data/activities.csv')", "Table functions"),
        ("SELECT nope FROM daily", "DuckDB error"),
    ],
)
def test_query_errors(seeded, sql, message):
    result = call("query", {"sql": sql})
    assert result.is_error
    assert message in result.content[0].text


def test_query_times_out(seeded, monkeypatch):
    monkeypatch.setattr("garmin_mcp.server.QUERY_TIMEOUT_S", 0.2)
    result = call(
        "query",
        {
            "sql": "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t "
            "WHERE n < 10000000000) SELECT count(*) FROM t"
        },
    )
    assert result.is_error
    assert "longer than" in result.content[0].text


def test_schema_resource(seeded):
    async def go():
        async with Client(mcp) as client:
            listed = (await client.list_resources()).resources
            read = await client.read_resource("schema://tables")
            return listed, read.contents[0].text

    listed, text = anyio.run(go)
    assert [str(r.uri) for r in listed] == ["schema://tables"]
    assert "## activities (4 rows)" in text
    assert "## run_km_splits (0 rows)" in text
    assert "activity_date DATE" in text
    assert "start_lat" not in text
