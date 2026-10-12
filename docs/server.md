# MCP server

Tools, safety and the audit log. Back to the [README](../README.md).

## Tools

| Tool | What it does |
|---|---|
| `data_status()` | How current the data is: latest date, row count and last sync time per source (activities, runs, daily, VO2 max), plus runs still waiting for details. Claude is told to check it before answering about recent days, so a stale sync isn't mistaken for "no activity". |
| `list_activities(start, end, activity_type?, limit=10)` | Activities between two local dates (inclusive), newest first. `activity_type="running"` covers every run type (road, trail, treadmill, ...). Returns distance (km), duration (min), pace (`m:ss` and s/km), HR, cadence, elevation, training effect/load, and sets/reps for strength. `truncated: true` when more than `limit` match. |
| `get_run(activity_id)` | One run in full: summary (timer/moving/grade-adjusted/best pace, HR, power, running dynamics, training effect, feel/RPE, VO2 max as of that date), time in each HR zone with %, weather, km splits, and laps. About 1–3k tokens per run. |
| `daily_metrics(start, end, metrics?, min_wear_hours=4)` | One row per day of wellness data (steps, resting HR, stress, body battery, intensity minutes, sleep h/score, overnight HRV) plus `{n, mean, min, max}` per metric. Days the watch measured for less than `min_wear_hours` are skipped, since their HR and steps are unreliable (`0` keeps all). |
| `compare_periods(a_start, a_end, b_start, b_end, min_wear_hours=4)` | Baseline period `a` vs period `b`: runs, km, km/week, distance-weighted pace, time-weighted HR, longest run, strength sessions, training load, VO2 max at period end, and the mean + n of every daily metric. `delta` gives `b − a` and % change. |
| `query(sql)` | One read-only DuckDB `SELECT` for anything the other tools don't cover (correlations, custom aggregations, joins). At most 500 rows and 50 KB (`truncated` plus a `note` when cut), 10 s timeout. |
| `sync_garmin()` | Fetches new data from Garmin by running the three fetch commands as child processes: from 2 days before the last sync up to today (at most 90 days). At most once every 15 minutes, 3-minute timeout. The only tool that writes or goes online, so it's annotated as not read-only; Claude Code asks before running it. If the session token has expired it says to log in once from a terminal, since it can never prompt for a password. |

| Resource | What it holds |
|---|---|
| `schema://tables` | Every table with its row count, columns and types, plus notes on keys, units and caveats. The model reads it before writing SQL for `query`. |

Typical flow: Claude calls `list_activities` to find a run's `activity_id`, then `get_run`
for the detail.

**Stale data:** when `list_activities`, `daily_metrics` or `compare_periods` is asked about
dates past the last sync, the result includes a `notes` entry such as *"daily data only
reaches 2026-10-08 (last sync …); 2026-10-09 to 2026-10-11 isn't loaded"*, so Claude says
the data may be incomplete instead of reporting an empty week. Coverage comes from the
date ranges in `sync_log`, so a synced day with no activity isn't flagged. Every pace comes both as `*_s_per_km` (number) and `*_min_per_km`
(`"m:ss"`).

**Safety:**
- Every call except `sync_garmin` opens a fresh **read-only** connection, so the fetch
  commands can still write between calls. While a sync or fetch is writing, tools answer
  "Database is busy" instead of failing.
- Bad input (dates in the wrong order, a range over 500 days, a limit outside 1–500, an
  unknown activity type or metric, an id that isn't a run or whose details haven't been
  fetched) returns an error message that tells the model how to fix the call.
- GPS coordinates are stored but never included in tool output.
- `query` has two layers of protection:
  1. **Parser check** (`sql_guard.py`, using sqlglot): exactly one `SELECT` (CTEs and
     `UNION` are fine), plain names of the known tables only, no table functions
     (`read_csv`, `query`, ...), no schema-qualified names, and no functions that expose
     settings or environment variables.
  2. **Sandboxed connection**: read-only, with file and network access disabled and the
     setting locked so SQL can't re-enable it. `activities` is shadowed by a view without
     the GPS columns, so `SELECT *` can't return them either. Long queries are
     interrupted after 10 s.

## Audit log

Every tool call is recorded in `<data dir>/audit.duckdb` (override with `GARMIN_AUDIT_PATH`),
table `audit_log`: time (UTC), tool, arguments as JSON (including the SQL sent to
`query`), success or error message, duration, row count and result size in bytes.
Result contents are never stored. It's a separate file so the server never needs write
access to the main database. If the log can't be written, the call still succeeds and a
warning goes to stderr.

```bash
duckdb -readonly data/audit.duckdb \
  "SELECT called_at_utc, tool, ok, duration_ms, args FROM audit_log ORDER BY id DESC LIMIT 20"
```

## Use with Claude Code

```bash
claude mcp add garmin -- uv --directory /absolute/path/to/garmin-training-mcp run garmin-mcp
```

## Use with Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/garmin-training-mcp", "run", "garmin-mcp"]
    }
  }
}
```
