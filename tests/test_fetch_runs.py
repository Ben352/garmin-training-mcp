import csv
from collections import Counter

import pytest

from garmin_mcp import fetch_runs
from garmin_mcp.fetch_runs import km_splits, time_series


def series(total_m: float, speed_mps: float = 4.0, step_s: float = 1.0):
    """Constant-speed samples every step_s seconds up to total_m."""
    out, t = [], 0.0
    while True:
        d = min(t * speed_mps, total_m)
        out.append({"sumDistance": d, "sumDuration": t, "directHeartRate": 150})
        if d >= total_m:
            return out
        t += step_s


def test_km_splits_constant_pace_with_partial_final():
    rows = km_splits(1, series(2500))
    assert [r["distance_m"] for r in rows] == [1000, 1000, 500]
    assert [r["is_partial"] for r in rows] == [False, False, True]
    assert [r["pace_s_per_km"] for r in rows] == [250, 250, 250]


def test_km_split_durations_sum_to_total_even_with_coarse_samples():
    samples = series(3200, speed_mps=3.3, step_s=7)  # boundaries fall between samples
    rows = km_splits(1, samples)
    assert sum(r["duration_s"] for r in rows) == pytest.approx(
        samples[-1]["sumDuration"], abs=0.2
    )
    assert rows[0]["duration_s"] == pytest.approx(1000 / 3.3, abs=0.1)


def test_tiny_trailing_split_is_dropped():
    rows = km_splits(1, series(2005))
    assert [r["km"] for r in rows] == [1, 2]


def test_time_series_maps_by_descriptor_index_not_position():
    details = {
        "metricDescriptors": [
            {"key": "sumDistance", "metricsIndex": 1},
            {"key": "directHeartRate", "metricsIndex": 0},
        ],
        "activityDetailMetrics": [{"metrics": [140, 12.5]}],
    }
    assert time_series(details) == [{"sumDistance": 12.5, "directHeartRate": 140}]


class FakeClient:
    def __init__(self):
        self.calls = Counter()

    def _details(self):
        return {
            "metricDescriptors": [
                {"key": "sumDistance", "metricsIndex": 0},
                {"key": "sumDuration", "metricsIndex": 1},
            ],
            "activityDetailMetrics": [
                {"metrics": [d, d / 4]} for d in range(0, 1201, 4)
            ],
        }

    def get_activity(self, aid):
        self.calls["activity"] += 1
        return {
            "summaryDTO": {
                "startTimeLocal": "2026-10-01T07:00:00.0",
                "distance": 1200.0,
                "duration": 300.0,
                "directWorkoutRpe": 70,
            }
        }

    def get_activity_splits(self, aid):
        self.calls["splits"] += 1
        return {"lapDTOs": [{"lapIndex": 1, "distance": 1200.0, "duration": 300.0}]}

    def get_activity_hr_in_timezones(self, aid):
        self.calls["zones"] += 1
        return [{"zoneNumber": 1, "secsInZone": 300.0}]

    def get_activity_weather(self, aid):
        self.calls["weather"] += 1
        return {}  # indoor runs have no weather

    def get_activity_details(self, aid, maxchart):
        self.calls["details"] += 1
        return self._details()

    def get_max_metrics_range(self, start, end):
        self.calls["vo2"] += 1
        return [
            {
                "generic": {
                    "calendarDate": "2026-10-01",
                    "vo2MaxValue": 50.0,
                    "vo2MaxPreciseValue": 50.3,
                }
            }
        ]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(fetch_runs, "DATA", data)
    monkeypatch.setattr(fetch_runs, "RAW_DIR", data / "raw" / "runs")
    monkeypatch.setattr(fetch_runs, "LOG_PATH", data / "run_fetch_log.csv")
    with (data / "activities.csv").open("w", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=["activity_id", "activity_type", "start_time_local"]
        )
        w.writeheader()
        w.writerow(
            {
                "activity_id": 11,
                "activity_type": "running",
                "start_time_local": "2026-10-01 07:00:00",
            }
        )
        w.writerow(
            {
                "activity_id": 12,
                "activity_type": "strength_training",
                "start_time_local": "2026-10-02 07:00:00",
            }
        )
        w.writerow(
            {
                "activity_id": 13,
                "activity_type": "trail_running",
                "start_time_local": "2026-10-03 07:00:00",
            }
        )
    client = FakeClient()
    monkeypatch.setattr(fetch_runs, "login", lambda: client)
    monkeypatch.setattr(
        "sys.argv", ["garmin-fetch-runs", "--activities", str(data / "activities.csv")]
    )
    return data, client


def test_second_invocation_makes_no_per_run_calls(workspace):
    data, client = workspace
    fetch_runs.main()
    assert client.calls["activity"] == 2  # both runs, not the strength session
    assert client.calls["details"] == 2

    fetch_runs.main()
    assert client.calls["activity"] == 2  # unchanged: already loaded
    assert client.calls["details"] == 2

    with (data / "run_fetch_log.csv").open() as f:
        assert sorted(int(r["activity_id"]) for r in csv.DictReader(f)) == [11, 13]
    with (data / "runs.csv").open() as f:
        assert len(list(csv.DictReader(f))) == 2  # rebuilt, not appended twice
    with (data / "run_km_splits.csv").open() as f:
        assert [r["km"] for r in csv.DictReader(f)] == ["1", "2", "1", "2"]


def test_logged_run_with_missing_cache_file_is_refetched(workspace):
    data, client = workspace
    fetch_runs.main()
    (data / "raw" / "runs" / "11.json.gz").unlink()
    fetch_runs.main()
    assert client.calls["activity"] == 3


def test_offline_rebuild_makes_no_api_calls(workspace, monkeypatch):
    data, client = workspace
    fetch_runs.main()
    before = sum(client.calls.values())
    (data / "runs.csv").unlink()
    monkeypatch.setattr("sys.argv", ["garmin-fetch-runs", "--offline"])
    fetch_runs.main()
    assert sum(client.calls.values()) == before
    assert (data / "runs.csv").exists()


def test_parse_run_converts_units(workspace):
    data, _ = workspace
    fetch_runs.main()
    with (data / "runs.csv").open() as f:
        row = next(csv.DictReader(f))
    assert row["workout_rpe"] == "7.0"
    assert row["avg_pace_s_per_km"] == "250.0"


def test_weather_fahrenheit_converted_to_celsius():
    assert fetch_runs.f_to_c(68) == 20.0
    assert fetch_runs.f_to_c(None) is None


def test_elevation_noise_below_threshold_is_ignored():
    noisy_flat = [100.0, 100.4, 99.7, 100.3, 99.8, 100.2]
    assert sum(g for g, _ in fetch_runs.elevation_steps(noisy_flat)) == 0


def test_elevation_climb_counted_once():
    climb = [100.0, 100.5, 101.2, 101.9, 103.0, 102.6, 103.1]
    steps = fetch_runs.elevation_steps(climb)
    assert sum(g for g, _ in steps) == pytest.approx(3.0)
    assert sum(lo for _, lo in steps) == 0
