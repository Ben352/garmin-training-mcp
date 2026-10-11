import pytest

from garmin_mcp.sql_guard import UnsafeQuery, validate


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM runs",
        "SELECT * FROM daily WHERE wear_s >= 4*3600 ORDER BY date",
        (
            "WITH w AS (SELECT date_trunc('week', activity_date) wk FROM activities) "
            "SELECT wk, count(*) FROM w GROUP BY 1"
        ),
        "SELECT * FROM runs r JOIN activities a USING (activity_id)",
        (
            "SELECT a.activity_id, v.vo2max_precise FROM activities a "
            "ASOF LEFT JOIN vo2max v ON a.activity_date >= v.date"
        ),
        "SELECT date FROM daily UNION ALL SELECT date FROM vo2max",
        "SELECT corr(sleep_total_s, resting_hr_bpm) FROM daily",
        "SELECT (SELECT max(date) FROM daily) AS latest",
    ],
)
def test_allows_read_only_selects(sql):
    validate(sql)


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("DELETE FROM daily", "Only SELECT"),
        ("DROP TABLE runs", "Only SELECT"),
        ("INSERT INTO daily (date) VALUES ('2026-01-01')", "Only SELECT"),
        ("UPDATE runs SET steps = 0", "Only SELECT"),
        ("CREATE TABLE x AS SELECT 1", "Only SELECT"),
        ("ATTACH 'other.db'", "Only SELECT"),
        ("COPY daily TO 'out.csv'", "Only SELECT"),
        ("PRAGMA database_list", "Only SELECT"),
        ("SET enable_external_access = true", "Only SELECT"),
        ("DESCRIBE daily", "Only SELECT"),
        ("SELECT 1; DROP TABLE daily", "exactly one"),
        ("SELECT * FROM read_csv('data/daily.csv')", "Table functions"),
        ("SELECT * FROM query('SELECT * FROM main.activities')", "Table functions"),
        ("SELECT * FROM range(10)", "Table functions"),
        ("SELECT * FROM 'data/daily.csv'", "Unknown table"),
        ("SELECT * FROM secrets", "Unknown table"),
        ("SELECT * FROM main.activities", "without a schema"),
        ("SELECT * FROM garmin.main.activities", "without a schema"),
        ("SELECT getenv('GARMIN_PASSWORD')", r"getenv\(\) isn't allowed"),
        ("SELECT current_setting('threads')", "isn't allowed"),
        (
            "SELECT * FROM daily WHERE date IN (SELECT * FROM read_parquet('x'))",
            "Table",
        ),
        ("SELEC 1", "Could not parse SQL: .*line 1"),
    ],
)
def test_rejects_everything_else(sql, message):
    with pytest.raises(UnsafeQuery, match=message):
        validate(sql)
