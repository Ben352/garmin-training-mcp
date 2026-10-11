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
    assert set(tools) == {"list_activities", "get_run"}
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
