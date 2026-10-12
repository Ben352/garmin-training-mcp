# garmin-training-mcp

[![CI](https://github.com/Ben352/garmin-training-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Ben352/garmin-training-mcp/actions/workflows/ci.yml)

Pull your own Garmin Connect data (runs, strength sessions, daily wellness, sleep,
VO2 max) into a local DuckDB database, and expose it to Claude through an MCP server so
it can answer questions about your training with real numbers.

All data is written to `data/` (database `data/garmin.duckdb`, plus CSV exports), which
is gitignored. Nothing personal gets committed.

*Unofficial: not affiliated with or endorsed by Garmin.*

## Try it without a Garmin account

The repo runs end to end on synthetic data:

```bash
uv sync
uv run garmin-demo                       # writes data/synthetic/ (about 20 s)
GARMIN_SYNTHETIC=1 uv run garmin-mcp     # MCP server on the synthetic database
```

`garmin-demo` runs the normal fetch commands, but `GARMIN_SYNTHETIC=1` swaps the Garmin
login for `SyntheticGarmin` (`src/garmin_mcp/synthetic.py`). It returns deterministic
fake responses in Garmin's raw API shapes, including the quirks (°F weather, RPE ×10,
placeholder values on unworn days), so the same parsing code is exercised. Output goes
to `data/synthetic/`, never mixed with real data.

The fictional athlete covers 2026-03-30 → 2026-09-30, about 110 runs, with patterns you
can ask Claude to find:

- easy pace improves from ~6:00 to ~5:30 /km and VO2 max from ~46 to ~50
- 4-week training blocks with a lighter recovery week
- an illness week (2026-07-13 → 07-19): no training, resting HR up, HRV down
- higher HR on hot days, higher resting HR after short sleep or a hard day
- a half marathon on 2026-09-27 after a taper

To use it from Claude Code:

```bash
claude mcp add garmin-demo -e GARMIN_SYNTHETIC=1 -- uv --directory /absolute/path/to/garmin-training-mcp run garmin-mcp
```

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
git clone https://github.com/Ben352/garmin-training-mcp.git
cd garmin-training-mcp
uv sync
```

No config file is needed. Optionally `export GARMIN_EMAIL=you@example.com` so login
doesn't ask for your email.

**Your password is never stored.** On the first run you're asked for it in the terminal
(it isn't echoed), plus an MFA code if your account has one. Only the resulting session
token is saved, in `~/.garminconnect` (override with `GARMINTOKENS`), so later runs log in
without asking. You're asked again only when the token expires. A failed login is never
retried. Without a terminal (e.g. a scheduled job) and without a valid token, the
commands exit and tell you to log in once by hand.

## Run order

```bash
uv run garmin-fetch          # 1. activity list         → activities
uv run garmin-fetch-runs     # 2. run details + VO2 max → runs, run_laps, run_km_splits, vo2max, run_fetch_log
uv run garmin-fetch-daily    #    daily wellness/sleep  → daily   (independent, any time)
```

Each command writes its tables to `data/garmin.duckdb` and also exports them as
`data/<table>.csv`.

**Step 1 must come before step 2.** `garmin-fetch-runs` reads its run ids from
`activities.csv`, so a new run only gets its details fetched after `garmin-fetch` has
listed it.

To update with new data, run the same commands again:

| Command | What a re-run does |
|---|---|
| `garmin-fetch` | Re-fetches the activity list for the date range (one paged request) and **merges** it into `activities`. |
| `garmin-fetch-runs` | Fetches **only runs not yet loaded** (see below). Already-loaded runs cost no API calls. VO2 max is re-fetched each time (one request) and merged. |
| `garmin-fetch-daily` | Re-fetches every day in the range (one request per day) and **merges** it into `daily`. |

**Merge** means rows are inserted or updated by their key (`activity_id` or `date`), and
rows outside the fetched range are kept. History in the database builds up across runs,
so fetching only the last 90 days never deletes older data. The CSV exports, by
contrast, contain only the most recent fetch.

### Date ranges

`garmin-fetch` and `garmin-fetch-daily` default to the **last 90 days**:

```bash
uv run garmin-fetch --start 2026-01-01 --end 2026-10-10
uv run garmin-fetch-daily --start 2026-01-01 --end 2026-10-10
```

`garmin-fetch-runs` takes no dates. It covers whatever runs are in `activities.csv`, so
to backfill older runs, widen the range of `garmin-fetch` and then run
`garmin-fetch-runs`.

## How run details are fetched only once

Each run needs 5 API calls (summary, laps, HR zones, weather, per-second time series),
so `garmin-fetch-runs` never fetches the same run twice:

```
activities.csv ──► runs not in data/run_fetch_log.csv
                        │  5 API calls per run
                        ▼
              data/raw/runs/<id>.json.gz      (raw responses, cached)
                        │  then logged in run_fetch_log.csv
                        ▼
   runs, run_laps, run_km_splits, run_fetch_log  (DB tables + CSVs, rebuilt from the cache every time)
```

- A run is logged only after every call succeeded and its cache file was written. A run
  that fails partway stays pending and is retried next time.
- On a Garmin rate limit (HTTP 429) the script stops. Re-run later to continue.
- The run tables are rebuilt from the cache in one transaction, never appended, so they
  can't contain duplicates.
- `uv run garmin-fetch-runs --offline` rebuilds the run tables and CSVs from the cache
  with **no API calls**. Use it after changing parsing code.
- To force a re-fetch of one run, delete `data/raw/runs/<id>.json.gz`. To re-fetch
  everything, delete `data/raw/runs/` and `data/run_fetch_log.csv`.

## Database

`data/garmin.duckdb` (override with `GARMIN_DB_PATH`). Schema: `src/garmin_mcp/schema.sql`.

| Env var | Default | Meaning |
|---|---|---|
| `GARMIN_SYNTHETIC` | unset | `1` = synthetic data, no Garmin account |
| `GARMIN_DATA_DIR` | `data/` (`data/synthetic/` when synthetic) | CSVs, raw cache, databases |
| `GARMIN_DB_PATH` | `<data dir>/garmin.duckdb` | main database |
| `GARMIN_AUDIT_PATH` | `<data dir>/audit.duckdb` | MCP audit log |

| Table | Key | One row per | Written by | How |
|---|---|---|---|---|
| `activities` | `activity_id` | run or strength session | `garmin-fetch` | merge |
| `daily` | `date` | day (steps, HR, stress, body battery, sleep, HRV) | `garmin-fetch-daily` | merge |
| `vo2max` | `date` | date Garmin updated VO2 max | `garmin-fetch-runs` | merge |
| `runs` | `activity_id` | run (pace/GAP, HR zones, power, running dynamics, weather) | `garmin-fetch-runs` | rebuilt from cache |
| `run_laps` | `activity_id, lap_index` | lap as recorded (auto-lap or workout step) | `garmin-fetch-runs` | rebuilt from cache |
| `run_km_splits` | `activity_id, km` | km, calculated from the 1 s time series | `garmin-fetch-runs` | rebuilt from cache |
| `run_fetch_log` | `activity_id` | fully loaded run (tracking table) | `garmin-fetch-runs` | rebuilt from log CSV |
| `sync_log` | `id` | fetch command run against Garmin (when, date range, rows) | every fetch command | appended |

Each table is also exported as `data/<table>.csv`.

Query it with the DuckDB CLI or Python:

```bash
duckdb -readonly data/garmin.duckdb
```

```sql
-- VO2 max as of each run (VO2 max is only stored on the dates it changed)
SELECT r.start_time_local, r.distance_m, v.vo2max_precise
FROM runs r ASOF LEFT JOIN vo2max v ON r.start_time_local::DATE >= v.date;
```

DuckDB lets only one process open the file for writing at a time, so close any
read-write session (for example a `duckdb` CLI opened without `-readonly`) before running
a fetch command.

**What to keep:** the database holds the accumulated history of `activities`, `daily` and
`vo2max`. The run tables can be rebuilt from `data/raw/runs/`. Back up `data/` as a whole.

Column definitions, units and data caveats (timezones, placeholder values on unworn days,
°F weather, etc.) are in **[docs/schema.md](docs/schema.md)**.

## MCP server

`uv run garmin-mcp` starts the server over stdio. It reads `data/garmin.duckdb`, so run
the fetch commands first.

### Tools

| Tool | What it does |
|---|---|
| `data_status()` | How current the data is: latest date, row count and last sync time per source (activities, runs, daily, VO2 max), plus runs still waiting for details. Claude is told to check it before answering about recent days, so a stale sync isn't mistaken for "no activity". |
| `list_activities(start, end, activity_type?, limit=10)` | Activities between two local dates (inclusive), newest first. Returns distance (km), duration (min), pace (`m:ss` and s/km), HR, cadence, elevation, training effect/load, and sets/reps for strength. `truncated: true` when more than `limit` match. |
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

### Audit log

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

### Use with Claude Code

```bash
claude mcp add garmin -- uv --directory /absolute/path/to/garmin-training-mcp run garmin-mcp
```

### Use with Claude Desktop

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

## Development

```bash
uv run pytest          # synthetic/fake Garmin clients + temp DBs: no network, credentials or real data
uv run ruff check .
uv run ruff format .
```

Pre-commit hooks run on every `git commit`: **gitleaks** (secrets such as API keys,
tokens and private keys), a **500 KB file-size limit** (a backstop in case a database or
CSV slips past `.gitignore`), private-key and merge-conflict checks, and **ruff** lint and
format. Install them once per clone:

```bash
uv run pre-commit install
uv run pre-commit run --all-files    # optional: check the whole repo now
```
