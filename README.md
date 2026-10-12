# garmin-training-mcp

[![CI](https://github.com/Ben352/garmin-training-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Ben352/garmin-training-mcp/actions/workflows/ci.yml)

I wanted to ask Claude questions about my running ("is my easy pace getting faster?",
"how did I sleep before the race?") and get answers based on my actual numbers. This
project pulls my Garmin Connect data into a local DuckDB database and exposes it to
Claude through an MCP server. An eval checks that the answers are actually right.


## Installation

You need [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
git clone https://github.com/Ben352/garmin-training-mcp.git
cd garmin-training-mcp
uv sync
```

### With your own Garmin data

```bash
uv run garmin-fetch          # activity list (last 90 days by default)
uv run garmin-fetch-runs     # details for each run: splits, laps, HR zones, weather
uv run garmin-fetch-daily    # sleep, resting HR, HRV, stress, steps
claude mcp add garmin -- uv --directory "$PWD" run garmin-mcp
```

The first fetch asks for your Garmin password in the terminal. It isn't stored
anywhere; only the session token is saved, in `~/.garminconnect`. After that, Claude
can refresh the data itself with the `sync_garmin` tool (at most every 15 minutes).

Everything is written to `data/`, which is gitignored.

### Try it with synthetic data

No Garmin account needed. This builds a database for a made-up runner (six months,
about 110 runs, an illness week, a half marathon at the end):

```bash
uv run garmin-demo
claude mcp add garmin-demo -e GARMIN_SYNTHETIC=1 -- uv --directory "$PWD" run garmin-mcp
```

Then ask Claude something like "how did my half marathon go?".

## What Claude gets

| Tool | Use |
|---|---|
| `data_status` | how up to date the data is |
| `list_activities` | find runs and strength sessions in a date range |
| `get_run` | one run in detail: km splits, laps, HR zones, weather |
| `daily_metrics` | sleep, resting HR, HRV, stress and steps per day |
| `compare_periods` | this block vs the last one |
| `query` | read-only SQL for everything else |
| `sync_garmin` | fetch new data from Garmin |

## Evaluation

Unit tests show the code does what I wrote. They don't show whether Claude, with these
tools, answers questions correctly. That depends on the tool descriptions and the
server instructions as much as on the code, so there's a separate eval.

It asks 15 questions about the synthetic runner, where the right answers are known
([`evals/questions.toml`](evals/questions.toml)). Claude gets the real server over
stdio, the same way Claude Code runs it. Each answer is checked twice: by code (does it
contain the right numbers, did it call the right tools, did it avoid running a sync
nobody asked for) and by a grader model against a short rubric, which catches things
like overclaiming or unrequested training advice.

```bash
export ANTHROPIC_API_KEY=...
uv run --group eval python -m evals.run --label "shorter query description"
```

A run takes about a minute and costs around $0.40 with Sonnet. Every run is committed:
a row in [`evals/HISTORY.md`](evals/HISTORY.md) and the full transcripts in
`evals/results/`. The first run scored 14/15: asking for "running" activities left out
trail and treadmill runs, so Claude counted 17 runs in May instead of 20. After fixing
the filter it's 15/15.

## Privacy and safety

- The database lives on my machine and never goes into git: CSVs, databases and tokens
  are gitignored, and a pre-commit hook (gitleaks plus a 500 KB file limit) is a second
  line of defence.
- Claude only sees what its tool calls return: the runs, days or query results it asks
  for, which go to Anthropic as part of the conversation. It never gets the database
  file itself, and results are capped in size.
- Every tool except `sync_garmin` uses a read-only connection.
- SQL from Claude goes through an allowlist parser and a sandboxed DuckDB connection
  with no file or network access.
- GPS coordinates never leave the database.
- Every tool call is logged (arguments, not results) to `data/audit.duckdb`.

## More detail

- [docs/pipeline.md](docs/pipeline.md): fetch commands, caching, date ranges, tables
- [docs/server.md](docs/server.md): every tool's inputs and outputs, safety, audit log
- [docs/schema.md](docs/schema.md): every column, its units, and Garmin's data quirks

## Development

```bash
uv run pytest                 # no network or real data needed
uv run pre-commit install     # gitleaks, file size limit, ruff
```
