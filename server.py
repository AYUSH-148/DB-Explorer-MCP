from typing import Any

from fastmcp import FastMCP
from sqlalchemy import create_engine

from config import DATABASE_URL, MCP_HOST, MCP_PORT, MCP_TRANSPORT
from explain import explain_safe
from inspector import get_all_tables, get_table_detail
from index_suggest import suggest_indexes
from safety import execute_safe
from schema_health import validate_schema as validate_schema_data


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


@mcp.tool
def validate_schema(table_name: str | None = None) -> dict[str, Any]:
    """Check tables for missing primary keys and unindexed foreign keys."""
    return validate_schema_data(engine, table_name)


@mcp.tool
def suggest_index(
    query: str | None = None,
    table_name: str | None = None,
) -> dict[str, Any]:
    """Suggest indexes from a query plan or table foreign-key metadata."""
    return suggest_indexes(engine, query, table_name)


def run_server() -> None:
    if MCP_TRANSPORT == "stdio":
        mcp.run(transport="stdio")
        return
    if MCP_TRANSPORT not in {"streamable-http", "sse"}:
        raise ValueError("MCP_TRANSPORT must be stdio, streamable-http, or sse")
    mcp.run(
        transport=MCP_TRANSPORT,
        host=MCP_HOST,
        port=MCP_PORT,
    )


if __name__ == "__main__":
    run_server()
