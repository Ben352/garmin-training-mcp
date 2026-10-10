from garmin_mcp.fetch_activities import COLUMNS, to_row

RUN = {
    "activityId": 1,
    "activityName": "Hackney Running",
    "activityType": {"typeKey": "running"},
    "startTimeLocal": "2026-10-01 07:00:00",
    "distance": 10000.0,
    "duration": 3100.0,
    "movingDuration": 3000.0,
    "averageHR": 150.0,
    "startLatitude": 51.54,
    "startLongitude": -0.05,
    "locationName": "Hackney",
    "summaryPolyline": "abc",
}


def test_run_row_has_pace_from_moving_time():
    assert to_row(RUN)["avg_pace_s_per_km"] == 300.0


def test_row_includes_name_and_location():
    row = to_row(RUN)
    assert list(row) == COLUMNS
    assert row["activity_name"] == "Hackney Running"
    assert row["location_name"] == "Hackney"
    assert (row["start_lat"], row["start_lon"]) == (51.54, -0.05)
    assert row["end_lat"] is None


def test_strength_row_has_no_pace():
    row = to_row(
        {
            "activityId": 2,
            "activityType": {"typeKey": "strength_training"},
            "duration": 2400.0,
            "totalSets": 12,
            "totalReps": 96,
        }
    )
    assert row["avg_pace_s_per_km"] is None
    assert row["total_sets"] == 12
