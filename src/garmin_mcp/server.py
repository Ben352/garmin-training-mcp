"""MCP server exposing Garmin data from DuckDB to LLM agents (stdio transport).

Usage:
    uv run garmin-mcp
"""

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from garmin_mcp.audit import audited
from garmin_mcp.resources import schema_tables
from garmin_mcp.tools.activities import list_activities
from garmin_mcp.tools.query import query
from garmin_mcp.tools.runs import get_run
from garmin_mcp.tools.status import data_status
from garmin_mcp.tools.sync import sync_garmin
from garmin_mcp.tools.wellness import compare_periods, daily_metrics

mcp = MCPServer(
    name="garmin",
    instructions=(
        "Read-only access to the user's own Garmin data: runs, strength sessions, "
        "daily wellness (sleep, resting HR, stress, steps) and VO2 max.\n\n"
        "Tools: data_status first for questions about recent days, to check the data "
        "covers them; if it doesn't, offer to run sync_garmin, which fetches new data "
        "from Garmin (at most once every 15 min). list_activities finds activities and their activity_id; "
        "get_run gives one run's splits, laps, HR zones and weather. daily_metrics "
        "for wellness over time, compare_periods for period-over-period questions, "
        "query (SQL; see schema://tables) for anything else.\n\n"
        "Units are in field names (distance_km, pace_min_per_km, ...); every "
        "*_s_per_km pace has a matching *_min_per_km 'm:ss' string. Dates are local.\n\n"
        "How to answer:\n"
        "- Answer the question asked, briefly: lead with the answer, then only the "
        "numbers that support it. No tables unless they're clearer than a sentence.\n"
        "- State what the data shows. Don't give training advice, plans or "
        "recommendations unless the user asks for them.\n"
        "- Give the number of days or runs behind any average or comparison.\n"
        "- When the data can't support a conclusion (few days, missing nights, low "
        "wear, data not synced), say so instead of drawing it."
    ),
)

# Every tool is audited. All but sync_garmin are read-only and local-only.
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
# sync_garmin writes new data and calls Garmin; re-running it is harmless.
SYNC = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
TOOLS = (
    data_status,
    list_activities,
    get_run,
    daily_metrics,
    compare_periods,
    query,
)

for tool in TOOLS:
    mcp.add_tool(audited(tool), annotations=READ_ONLY)
mcp.add_tool(audited(sync_garmin), annotations=SYNC)

mcp.resource(
    "schema://tables",
    name="tables",
    description="Tables, columns, types, keys and row counts for the query tool.",
    mime_type="text/markdown",
)(schema_tables)


def main() -> None:
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
