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


def test_tool_is_listed_with_docstring_and_read_only_hint():
    async def go():
        async with Client(mcp) as client:
            return (await client.list_tools()).tools

    [tool] = anyio.run(go)
    assert tool.name == "list_activities"
    assert "newest first" in tool.description
    assert tool.annotations.read_only_hint is True
    assert set(tool.input_schema["required"]) == {"start", "end"}


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
