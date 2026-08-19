from typing import Any

from fastmcp import FastMCP
from sqlalchemy import create_engine

from config import DATABASE_URL
from explain import explain_safe
from inspector import get_all_tables, get_table_detail
from safety import execute_safe


mcp = FastMCP("db-explorer")
engine = create_engine(DATABASE_URL)


def explore_schema_data(
    table_name: str | None = None,
    include_sample_data: bool = False,
) -> dict[str, Any]:
    if table_name:
        return get_table_detail(engine, table_name, include_sample_data)
    return {"tables": get_all_tables(engine)}


@mcp.tool
def explore_schema(
    table_name: str | None = None,
    include_sample_data: bool = False,
) -> dict[str, Any]:
    """Explore database tables, columns, keys, indexes, and sample rows."""
    return explore_schema_data(table_name, include_sample_data)


def execute_query_data(sql: str, row_limit: int = 100) -> dict[str, Any]:
    return execute_safe(engine, sql, row_limit)


@mcp.tool
def execute_query(sql: str, row_limit: int = 100) -> dict[str, Any]:
    """Execute one validated, read-only SQL SELECT query."""
    return execute_query_data(sql, row_limit)


@mcp.tool
def explain_query(sql: str) -> dict[str, Any]:
    """Return the database execution plan for one safe SELECT query."""
    return explain_safe(engine, sql)


if __name__ == "__main__":
    mcp.run()
