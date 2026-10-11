"""Validation for agent-written SQL: one read-only SELECT over known tables.

This is the first of two layers. The second is the connection the query runs on
(read-only, external access disabled and locked, GPS columns hidden by temp views),
so a query that slips past the parser still can't write, read files or see GPS.
"""

import sqlglot
from sqlglot import exp

ALLOWED_TABLES = frozenset(
    {
        "activities",
        "daily",
        "vo2max",
        "runs",
        "run_laps",
        "run_km_splits",
        "run_fetch_log",
        "sync_log",
    }
)

# Functions that run SQL from strings, expose settings/env, or read files.
# (File access is also blocked by the connection; this gives a clearer error.)
DENIED_FUNCTIONS = frozenset({"query", "query_table", "getenv", "current_setting"})
DENIED_PREFIXES = (
    "read_",
    "duckdb_",
    "pragma_",
    "glob",
    "sniff_csv",
    "parquet_",
    "iceberg_",
    "delta_",
)


class UnsafeQuery(ValueError):
    """The SQL isn't a single read-only SELECT over the allowed tables."""


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return node.name.lower()
    return node.sql_name().lower()


def validate(sql: str) -> None:
    """Raise UnsafeQuery with a message the model can act on, or return None."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except sqlglot.errors.ParseError as e:
        # str(e) embeds terminal colour codes; build a plain message instead.
        err = e.errors[0] if e.errors else {}
        where = f" (line {err.get('line')}, col {err.get('col')})" if err else ""
        raise UnsafeQuery(
            f"Could not parse SQL: {err.get('description', 'syntax error')}{where}."
        ) from None
    if len(statements) != 1:
        raise UnsafeQuery("Send exactly one SQL statement.")
    [tree] = statements
    if not isinstance(tree, exp.Query):
        raise UnsafeQuery(f"Only SELECT queries are allowed (got {tree.key.upper()}).")
    for node in tree.walk():
        if isinstance(node, exp.DML | exp.DDL):
            raise UnsafeQuery("Only SELECT queries are allowed.")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    allowed = ", ".join(sorted(ALLOWED_TABLES))
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise UnsafeQuery(
                f"Table functions aren't allowed in FROM. Query one of: {allowed}."
            )
        if table.db or table.catalog:
            raise UnsafeQuery(
                f"Use plain table names without a schema: {table.sql()} -> {table.name}."
            )
        name = table.name.lower()
        if name not in ALLOWED_TABLES and name not in cte_names:
            raise UnsafeQuery(f"Unknown table {table.name!r}. Valid: {allowed}.")

    for func in tree.find_all(exp.Func):
        name = _function_name(func)
        if name in DENIED_FUNCTIONS or name.startswith(DENIED_PREFIXES):
            raise UnsafeQuery(f"Function {name}() isn't allowed.")
