# Fetching and storage

How the fetch commands work and what ends up in the database. Back to the [README](../README.md).

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

## Date ranges

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
°F weather, etc.) are in **[schema.md](schema.md)**.
