import os
import subprocess
import sys
from datetime import date

import anyio
import pytest
from mcp.client import Client

from garmin_mcp import synthetic
from garmin_mcp.fetch_runs import km_splits, parse_run, time_series
from garmin_mcp.server import mcp
from garmin_mcp.synthetic import SyntheticGarmin, activity_id

RACE_ID = activity_id(synthetic.RACE_DAY, synthetic.SLOT_RUN)


def run_ids(start="2026-09-14", end="2026-09-30"):
    return [
        a["activityId"]
        for a in SyntheticGarmin().get_activities_by_date(start, end, sortorder="asc")
        if "run" in a["activityType"]["typeKey"]
    ]


def test_deterministic_across_clients():
    a, b = SyntheticGarmin(), SyntheticGarmin()
    assert a.get_activities_by_date("2026-05-01", "2026-05-31") == (
        b.get_activities_by_date("2026-05-01", "2026-05-31")
    )
    assert a.get_user_summary("2026-05-04") == b.get_user_summary("2026-05-04")


@pytest.mark.parametrize("aid", run_ids())
def test_run_responses_are_internally_consistent(aid):
    g = SyntheticGarmin()
    summary = g.get_activity(str(aid))["summaryDTO"]
    laps = g.get_activity_splits(str(aid))["lapDTOs"]
    samples = time_series(g.get_activity_details(str(aid), maxchart=10000))
    zones = g.get_activity_hr_in_timezones(str(aid))
    assert sum(lap["distance"] for lap in laps) == pytest.approx(
        summary["distance"], abs=1
    )
    assert sum(lap["duration"] for lap in laps) == pytest.approx(
        summary["duration"], abs=1
    )
    assert samples[-1]["sumDistance"] == pytest.approx(summary["distance"], abs=0.1)
    assert sum(z["secsInZone"] for z in zones) == pytest.approx(
        summary["duration"], abs=2
    )
    splits = km_splits(aid, samples)
    assert sum(s["duration_s"] for s in splits) == pytest.approx(
        summary["duration"], abs=1
    )


def test_uses_real_api_encodings():
    g = SyntheticGarmin()
    raw = {
        "activity_id": RACE_ID,
        "activity": g.get_activity(str(RACE_ID)),
        "splits": g.get_activity_splits(str(RACE_ID)),
        "hr_zones": g.get_activity_hr_in_timezones(str(RACE_ID)),
        "weather": g.get_activity_weather(str(RACE_ID)),
    }
    run = parse_run(raw)
    assert 0 < run["weather_temp_c"] < 30  # converted from the °F payload
    assert run["workout_rpe"] is None or 1 <= run["workout_rpe"] <= 10  # stored x10
    assert run["distance_m"] == pytest.approx(21097.5, abs=1)


def test_planted_patterns():
    g = SyntheticGarmin()
    assert g.get_activities_by_date("2026-07-13", "2026-07-19") == []  # illness
    types = {
        a["activityType"]["typeKey"]
        for a in g.get_activities_by_date("2026-03-30", "2026-09-30")
    }
    assert {
        "treadmill_running",
        "trail_running",
        "strength_training",
        "cycling",
    } <= types
    vo2 = [
        v["generic"]["vo2MaxPreciseValue"]
        for v in g.get_max_metrics_range("2026-03-30", "2026-09-30")
    ]
    assert vo2[0] < 47 and vo2[-1] > 49
    assert (
        synthetic.rest_hr(date(2026, 7, 15)) > synthetic.rest_hr(date(2026, 7, 8)) + 3
    )


def test_unworn_day_looks_like_garmin_placeholders():
    g = SyntheticGarmin()
    days = [
        d
        for d in synthetic._dates("2026-03-30", "2026-09-30")
        if synthetic._wear(d)["status"] == "unworn"
    ]
    assert days
    s = g.get_user_summary(days[0].isoformat())
    assert s["totalSteps"] is None and s["sedentarySeconds"] == 86400
    assert s["averageStressLevel"] < 0


def test_end_to_end_without_garmin_account(tmp_path, monkeypatch):
    """The real fetch commands, on synthetic data, then the MCP server on the result."""
    data = tmp_path / "synthetic"
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "GARMIN_DB_PATH",
            "GARMIN_AUDIT_PATH",
            "GARMIN_EMAIL",
            "GARMIN_PASSWORD",
        }
    }
    env |= {
        "GARMIN_SYNTHETIC": "1",
        "GARMIN_DATA_DIR": str(data),
        "GARMINTOKENS": str(tmp_path / "no-tokens"),
    }
    window = ["--start", "2026-09-14", "--end", "2026-09-30"]
    for module, args in [
        ("fetch_activities", window),
        ("fetch_runs", []),
        ("fetch_daily", window),
    ]:
        subprocess.run(
            [sys.executable, "-m", f"garmin_mcp.{module}", *args],
            env=env,
            check=True,
            capture_output=True,
            cwd=tmp_path,
        )
    assert not (tmp_path / "no-tokens").exists()  # never tried to log in

    monkeypatch.setenv("GARMIN_DB_PATH", str(data / "garmin.duckdb"))

    async def go():
        async with Client(mcp) as client:
            listed = await client.call_tool(
                "list_activities",
                {
                    "start": "2026-09-27",
                    "end": "2026-09-27",
                    "activity_type": "running",
                },
            )
            race = listed.structured_content["activities"][0]
            run = await client.call_tool(
                "get_run", {"activity_id": race["activity_id"]}
            )
            daily = await client.call_tool(
                "daily_metrics", {"start": "2026-09-14", "end": "2026-09-30"}
            )
            return race, run.structured_content, daily.structured_content

    race, run, daily = anyio.run(go)
    assert race["activity_name"] == "Riverside Half Marathon"
    assert race["distance_km"] == 21.1
    assert len(run["km_splits"]) == 22 and run["summary"]["vo2max_at_run"] is not None
    assert daily["summary"]["steps"]["n"] > 10
