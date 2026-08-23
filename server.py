from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from sqlalchemy.exc import SQLAlchemyError

from auth import build_auth_provider
from config import (
    DATABASE_URL,
    MCP_ALLOW_UNAUTHENTICATED,
    MCP_AUTH_TOKEN,
    MCP_HOST,
    MCP_PORT,
    MCP_TRANSPORT,
)
from db import create_configured_engine, engine_timeout_seconds
from errors import ToolInputError, from_database_error
from explain import explain_safe
from inspector import DEFAULT_TABLE_LIMIT, get_schema_page, get_table_detail
from index_suggest import suggest_indexes
from migration import get_migration_context, validate_migration as validate_migration_data
from safety import execute_safe
from schema_health import validate_schema as validate_schema_data


mcp = FastMCP(
    "db-explorer",
    auth=build_auth_provider(
        MCP_TRANSPORT, MCP_AUTH_TOKEN, MCP_ALLOW_UNAUTHENTICATED
    ),
)
engine = create_configured_engine(DATABASE_URL)

_Params = ParamSpec("_Params")
_Result = TypeVar("_Result")


def tool_errors(
    function: Callable[_Params, _Result],
) -> Callable[_Params, _Result]:
    """Turn a failure into a tool error that says what the caller should do next.
    """

    @wraps(function)
    def wrapper(*args: _Params.args, **kwargs: _Params.kwargs) -> _Result:
        try:
            return function(*args, **kwargs)
        except ToolInputError as error:
            raise ToolError(error.as_text()) from error
        except SQLAlchemyError as error:
            structured = from_database_error(error, engine_timeout_seconds(engine))
            raise ToolError(structured.as_text()) from error

    return wrapper


def explore_schema_data(
    table_name: str | None = None,
    include_sample_data: bool = False,
    name_pattern: str | None = None,
    detail: bool = False,
    limit: int = DEFAULT_TABLE_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    if table_name:
        return get_table_detail(engine, table_name, include_sample_data)
    return get_schema_page(
        engine,
        name_pattern=name_pattern,
        limit=limit,
        offset=offset,
        detail=detail,
    )


@mcp.tool
@tool_errors
def explore_schema(
    table_name: str | None = None,
    include_sample_data: bool = False,
    name_pattern: str | None = None,
    detail: bool = False,
    limit: int = DEFAULT_TABLE_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Explore database tables, columns, keys, indexes, and sample rows."""
    return explore_schema_data(
        table_name,
        include_sample_data,
        name_pattern=name_pattern,
        detail=detail,
        limit=limit,
        offset=offset,
    )


def execute_query_data(sql: str, row_limit: int = 100) -> dict[str, Any]:
    return execute_safe(engine, sql, row_limit)


@mcp.tool
@tool_errors
def execute_query(sql: str, row_limit: int = 100) -> dict[str, Any]:
    """Execute one validated, read-only SQL SELECT query."""
    return execute_query_data(sql, row_limit)


@mcp.tool
@tool_errors
def explain_query(sql: str) -> dict[str, Any]:
    """Return the database execution plan for one safe SELECT query."""
    return explain_safe(engine, sql)


@mcp.tool
@tool_errors
def validate_schema(table_name: str | None = None) -> dict[str, Any]:
    """Check tables for missing primary keys and unindexed foreign keys."""
    return validate_schema_data(engine, table_name)


@mcp.tool
@tool_errors
def suggest_index(
    query: str | None = None,
    table_name: str | None = None,
) -> dict[str, Any]:
    """Suggest indexes from a query plan or table foreign-key metadata."""
    return suggest_indexes(engine, query, table_name)


@mcp.tool
@tool_errors
def migration_context() -> dict[str, Any]:
    """Return schema context for client-side migration generation."""
    return get_migration_context(engine)


@mcp.tool
@tool_errors
def validate_migration(up_sql: str, down_sql: str) -> dict[str, Any]:
    """Validate migration scripts without executing them."""
    return validate_migration_data(engine, up_sql, down_sql)


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
