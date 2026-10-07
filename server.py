import inspect
import logging
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
logger = logging.getLogger(__name__)

# One line per tool call: which tool, its arguments, and how it ended. Set up at
# import rather than in run_server(), because a hosted entrypoint loads
# server.py:mcp and never calls run_server(). It has its own stderr handler (the
# stdio transport owns stdout) and does not propagate, so it neither depends on
# nor duplicates whatever logging the host configures.
audit_log = logging.getLogger("db_explorer.audit")
audit_log.setLevel(logging.INFO)
audit_log.propagate = False
_audit_handler = logging.StreamHandler()
_audit_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
audit_log.addHandler(_audit_handler)

# The arguments are caller-supplied, so a single call cannot grow the log by more
# than this.
AUDIT_ARGUMENT_LIMIT = 2000

_Params = ParamSpec("_Params")
_Result = TypeVar("_Result")


def tool_errors(
    function: Callable[_Params, _Result],
) -> Callable[_Params, _Result]:
    """Turn a failure into a tool error that says what the caller should do next.

    Every call, whatever its outcome, also writes one audit line: a refused,
    denied or timed-out query is the one an operator most needs to see.
    """
    parameter_names = list(inspect.signature(function).parameters)

    @wraps(function)
    def wrapper(*args: _Params.args, **kwargs: _Params.kwargs) -> _Result:
        outcome = "internal_error"
        try:
            result = function(*args, **kwargs)
            outcome = "ok"
            if isinstance(result, dict) and "count" in result:
                outcome += f" rows={result['count']} truncated={result['truncated']}"
            return result
        except ToolInputError as error:
            outcome = error.code
            raise ToolError(error.as_text()) from error
        except SQLAlchemyError as error:
            structured = from_database_error(error, engine_timeout_seconds(engine))
            outcome = structured.code
            raise ToolError(structured.as_text()) from error
        except Exception as error:
            # Anything else is a bug, and its text can carry driver or file
            # details, so the traceback goes to the server log only.
            logger.exception("Unexpected error in tool %s", function.__name__)
            unexpected = ToolInputError(
                code="internal_error",
                message="The server failed unexpectedly while handling this call",
                hint=(
                    "The cause is in the server log. Retrying the same call "
                    "will likely fail the same way; try different arguments."
                ),
            )
            raise ToolError(unexpected.as_text()) from error
        finally:
            arguments = repr({**dict(zip(parameter_names, args)), **kwargs})
            if len(arguments) > AUDIT_ARGUMENT_LIMIT:
                arguments = (
                    f"{arguments[:AUDIT_ARGUMENT_LIMIT]}... "
                    f"({len(arguments)} chars)"
                )
            audit_log.info("%s %s %s", function.__name__, outcome, arguments)

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
def validate_schema(
    table_name: str | None = None,
    name_pattern: str | None = None,
    limit: int = DEFAULT_TABLE_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Check one table, or one page of tables, for schema issues."""
    return validate_schema_data(engine, table_name, name_pattern, limit, offset)


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
def migration_context(
    name_pattern: str | None = None,
    limit: int = DEFAULT_TABLE_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Return dialect and one page of table detail for client-side migration drafting."""
    return get_migration_context(engine, name_pattern, limit, offset)


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
