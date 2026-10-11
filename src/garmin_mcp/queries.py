"""Load SQL from src/garmin_mcp/queries/<name>.sql (shipped inside the package).

Queries with a fixed shape live there. SQL whose columns are chosen at call time
(daily_metrics, compare_periods' daily averages) is built in server.py.
"""

from functools import cache
from importlib.resources import files


@cache
def sql(name: str) -> str:
    return files("garmin_mcp").joinpath("queries", f"{name}.sql").read_text()
