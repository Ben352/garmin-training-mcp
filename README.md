# garmin-mcp

Pull your own Garmin Connect data (runs, strength sessions, daily wellness, sleep,
VO2 max) into local CSV files. Later this will feed an MCP server so Claude can answer
questions about your training with real numbers.

All data is written to `data/`, which is gitignored. Nothing personal gets committed.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync
cp .env.example .env    # fill in GARMIN_EMAIL and GARMIN_PASSWORD
```

On the first run you log in with your email and password, and an MFA code if your
account has one. The session token is then cached in `~/.garminconnect` (override with
`GARMINTOKENS`), so later runs don't log in again. A failed login is never retried.

## Run order

```bash
uv run garmin-fetch          # 1. activity list         → data/activities.csv
uv run garmin-fetch-runs     # 2. run details + VO2 max → data/runs.csv, run_laps.csv, run_km_splits.csv, vo2max.csv
uv run garmin-fetch-daily    #    daily wellness/sleep  → data/daily.csv   (independent, any time)
```

**Step 1 must come before step 2.** `garmin-fetch-runs` reads its run ids from
`activities.csv`, so a new run only gets its details fetched after `garmin-fetch` has
listed it.

To update with new data, run the same commands again:

| Command | What a re-run does |
|---|---|
| `garmin-fetch` | Re-fetches the activity list for the date range and overwrites the CSV. Cheap: one paged request. |
| `garmin-fetch-runs` | Fetches **only runs not yet loaded** (see below). Already-loaded runs cost no API calls. VO2 max is re-fetched each time (one request). |
| `garmin-fetch-daily` | Re-fetches every day in the range (one request per day) and overwrites the CSV. |

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
   runs.csv, run_laps.csv, run_km_splits.csv  (rebuilt from the cache every time)
```

- A run is logged only after every call succeeded and its cache file was written. A run
  that fails partway stays pending and is retried next time.
- On a Garmin rate limit (HTTP 429) the script stops. Re-run later to continue.
- The CSVs are rebuilt from the cache, never appended, so they can't contain duplicates.
- `uv run garmin-fetch-runs --offline` rebuilds the CSVs from the cache with **no API
  calls**. Use it after changing parsing code.
- To force a re-fetch of one run, delete `data/raw/runs/<id>.json.gz`. To re-fetch
  everything, delete `data/raw/runs/` and `data/run_fetch_log.csv`.

## Output files

| File | One row per | Produced by |
|---|---|---|
| `data/activities.csv` | run or strength session | `garmin-fetch` |
| `data/runs.csv` | run (detailed: pace/GAP, HR zones, power, running dynamics, weather) | `garmin-fetch-runs` |
| `data/run_laps.csv` | lap as recorded by the watch (auto-lap or workout step) | `garmin-fetch-runs` |
| `data/run_km_splits.csv` | km, calculated from the 1 s time series | `garmin-fetch-runs` |
| `data/vo2max.csv` | date Garmin updated VO2 max | `garmin-fetch-runs` |
| `data/run_fetch_log.csv` | fully loaded run (tracking table) | `garmin-fetch-runs` |
| `data/daily.csv` | day (steps, HR, stress, body battery, sleep, HRV) | `garmin-fetch-daily` |

Column definitions, units and data caveats (timezones, placeholder values on unworn days,
°F weather, etc.) are in **[docs/schema.md](docs/schema.md)**.

## Development

```bash
uv run pytest          # tests use a fake Garmin client, no network or credentials
uv run ruff check .
uv run ruff format .
```
