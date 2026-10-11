-- How far each data source reaches, for data_status.
-- Params: run_types (list of activity_type values that count as runs)
SELECT
    (SELECT max(activity_date) FROM activities)                       AS activities_latest_date,
    (SELECT count(*) FROM activities)                                 AS activities_rows,
    (SELECT max(start_time_local)::DATE FROM runs)                    AS runs_latest_date,
    (SELECT count(*) FROM runs)                                       AS runs_rows,
    (SELECT count(*) FROM activities
     WHERE list_contains(?, activity_type)
       AND activity_id NOT IN (SELECT activity_id FROM runs))         AS runs_pending_details,
    (SELECT max(date) FROM daily)                                     AS daily_latest_date,
    (SELECT max(date) FROM daily WHERE sleep_total_s IS NOT NULL)     AS daily_latest_sleep_date,
    (SELECT count(*) FROM daily)                                      AS daily_rows,
    (SELECT max(date) FROM vo2max)                                    AS vo2max_latest_date,
    (SELECT count(*) FROM vo2max)                                     AS vo2max_rows
