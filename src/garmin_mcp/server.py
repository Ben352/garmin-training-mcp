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
from garmin_mcp.tools.wellness import compare_periods, daily_metrics

mcp = MCPServer(
    name="garmin",
    instructions=(
        "Read-only access to the user's Garmin training data: runs and strength "
        "sessions. Use list_activities to find activities and their activity_id, "
        "then get_run for one run's splits, laps, HR zones and weather. Use "
        "daily_metrics for sleep, resting HR, stress and steps, compare_periods "
        "for period-over-period questions, and query (SQL; see schema://tables) "
        "for anything else. For questions about recent days, call data_status first "
        "to check the data covers them. Units are in field names (distance_km, "
        "pace_min_per_km, ...); every *_s_per_km pace has a matching *_min_per_km "
        "'m:ss' string. Dates are the local calendar date where the activity "
        "started. Only state facts based on the data, don't make assumptions, and "
        "use few tokens where possible."
    ),
)

# Every tool is read-only, touches only the local database, and is audited.
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
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
