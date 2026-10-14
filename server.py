import inspect
import logging
import os
from collections import Counter
from collections.abc import Callable
from functools import wraps
from typing import Annotated, Any, ParamSpec, TypeVar

import fastmcp
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError, ValidationError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp.types import ToolAnnotations
from pydantic import Field
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request
from starlette.responses import JSONResponse

from auth import build_auth_provider, token_entries
from config import (
    DATABASE_URL,
    MCP_ALLOW_UNAUTHENTICATED,
    MCP_AUTH_TOKEN,
    MCP_AUTH_TOKENS,
    MAX_CONCURRENT_CALLS_PER_CLIENT,
    MCP_HOST,
    MCP_PORT,
    MCP_TRANSPORT,
    group_database_url,
)
from db import create_configured_engine, engine_timeout_seconds
from errors import ToolInputError, from_database_error
from explain import explain_safe
from inspector import (
    DEFAULT_TABLE_LIMIT,
    MAX_TABLE_LIMIT,
    get_schema_page,
    get_table_detail,
)
from index_suggest import suggest_indexes
from migration import get_migration_context, validate_migration as validate_migration_data
from safety import MAX_ROW_LIMIT, execute_safe
from schema_health import validate_schema as validate_schema_data


# FastMCP leaves Host and Origin checking off by default, so a web page the user
# visits could reach a server on localhost through DNS rebinding. "auto" checks
# loopback binds only, so a public deployment on its own domain is unaffected. Set
# here rather than in run_server(), which a hosted entrypoint never calls.
if "FASTMCP_HTTP_HOST_ORIGIN_PROTECTION" not in os.environ:
    fastmcp.settings.http_host_origin_protection = "auto"

# By default the HTTP transport keeps each client's session in this process's
# memory, so behind a load balancer a request that reaches another replica gets
# 404 "Session not found". No tool streams progress or calls back to the client,
# and the bearer token is checked on every request anyway, so the session holds
# nothing this server needs. SSE has no stateless mode, so it keeps sessions.
if "FASTMCP_STATELESS_HTTP" not in os.environ and MCP_TRANSPORT != "sse":
    fastmcp.settings.stateless_http = True

# Sent to the client once, when it connects. The model reads this and the tool
# descriptions, never the README, so the workflow has to be stated here.
INSTRUCTIONS = """\
Read-only access to one SQL database. Nothing here can change data or schema:
queries must be a single SELECT, and migration scripts are checked, never run.

Typical flow: explore_schema() lists the tables and the SQL dialect. For the
columns of every table a question touches, call explore_schema(detail=true)
once, with name_pattern to narrow it, rather than one table_name call per table.
Then execute_query. Skip the listing when you already know the table names.
For a slow query, use explain_query or suggest_index(query=...).
To draft a migration, read migration_context, then check the script with
validate_migration before the user runs it.

Errors start with a [code] and end with a hint saying what to change. Follow the
hint rather than retrying the same call unchanged."""

# Every tool only reads, so a client may run any of them without asking first,
# and repeat a call safely.
READ_ONLY = ToolAnnotations(
    readOnlyHint=True, idempotentHint=True, openWorldHint=False
)

# Parameters shared by the tools that page through tables or take a SELECT.
NamePattern = Annotated[
    str | None,
    Field(
        description=(
            "Filter tables by name, case-insensitive. Plain text matches a "
            "substring ('order'); *, ? and [] make it a glob ('order_*')."
        )
    ),
]
Limit = Annotated[int, Field(description=f"Tables per page, 1 to {MAX_TABLE_LIMIT}.")]
Offset = Annotated[
    int, Field(description="Tables to skip. Pass the previous page's next_offset.")
]
SelectSql = Annotated[
    str,
    Field(
        description=(
            "One SELECT statement (WITH ... SELECT is fine), in the dialect "
            "explore_schema reports. No comments, no second statement, no "
            "FOR UPDATE or FOR SHARE."
        )
    ),
]

TOKEN_ENTRIES = token_entries(MCP_AUTH_TOKEN, MCP_AUTH_TOKENS)
mcp = FastMCP(
    "db-explorer",
    instructions=INSTRUCTIONS,
    auth=build_auth_provider(
        MCP_TRANSPORT, TOKEN_ENTRIES, MCP_ALLOW_UNAUTHENTICATED
    ),
)
engine = create_configured_engine(DATABASE_URL)
# One engine per token group, each logging in as that group's database user, so
# the database's own grants decide what a group's callers can read. Built at
# import, so a group with no DATABASE_URL_<GROUP> stops the server from starting.
# ponytail: one pool per group; lower DB_POOL_SIZE when there are many groups.
group_engines = {
    group: create_configured_engine(group_database_url(group))
    for group in sorted({entry.group for entry in TOKEN_ENTRIES if entry.group})
}
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


def current_engine() -> Engine:
    """Return the engine for the caller's token group, or the default engine.

    stdio and tokens without a group have no group, and use DATABASE_URL. A
    group with no engine is refused rather than run on DATABASE_URL: it can only
    come from an auth provider other than MCP_AUTH_TOKENS, and quietly giving it
    the default login could grant more than that group was meant to see.
    """
    token = get_access_token()
    group = token.claims.get("group") if token else None
    if not group:
        return engine
    if isinstance(group, str) and group in group_engines:
        return group_engines[group]
    raise ToolInputError(
        code="unknown_group",
        message="No database login is configured for this caller's group",
        hint=(
            "The server builds a database login only for the groups named in "
            "MCP_AUTH_TOKENS. Ask the operator to add the group there and set "
            "its DATABASE_URL_<GROUP>."
        ),
    )


def _audit_arguments(arguments: dict[str, Any]) -> str:
    rendered = repr(arguments)
    if len(rendered) > AUDIT_ARGUMENT_LIMIT:
        rendered = f"{rendered[:AUDIT_ARGUMENT_LIMIT]}... ({len(rendered)} chars)"
    return rendered


def _caller() -> str:
    token = get_access_token()
    if token is None:
        return ""
    return f" user={token.client_id} group={token.claims.get('group') or '-'}"


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
            arguments = _audit_arguments({**dict(zip(parameter_names, args)), **kwargs})
            audit_log.info(
                "%s %s%s %s", function.__name__, outcome, _caller(), arguments
            )

    return wrapper


class ConcurrentCallLimit(Middleware):
    """Refuse a token's call while it already has the maximum number running.

    The pool is shared, so without this one token firing calls in parallel could
    hold every connection and leave other callers with server_busy. Refusing at
    once, before a worker thread or connection is taken, tells the caller to wait
    for its own calls instead of queueing behind them. Calls with no token (stdio,
    or MCP_ALLOW_UNAUTHENTICATED) are not limited: there is no one to tell apart.
    ponytail: counted per process, so N replicas allow N times the limit.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        # Only touched on the event loop, between awaits, so it needs no lock.
        self.running: Counter[str] = Counter()

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        token = get_access_token()
        if token is None:
            return await call_next(context)
        client = token.client_id
        if self.running[client] >= self.limit:
            audit_log.info(
                "%s too_many_concurrent_calls%s", context.message.name, _caller()
            )
            refused = ToolInputError(
                code="too_many_concurrent_calls",
                message=(
                    f"This caller already has {self.limit} tool calls running; "
                    "no query was run"
                ),
                hint=(
                    "Wait for a running call to finish, then retry this one "
                    "unchanged. Run fewer calls in parallel."
                ),
            )
            raise ToolError(refused.as_text())
        self.running[client] += 1
        try:
            return await call_next(context)
        finally:
            self.running[client] -= 1


class ArgumentErrors(Middleware):
    """Give a call whose arguments don't fit the input schema a [code] and hint.

    FastMCP checks argument names and types before the tool runs, so tool_errors
    never sees these calls: without this they reach the model as raw pydantic text
    and leave no audit line.
    """

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        try:
            return await call_next(context)
        except ValidationError as error:
            cause = error.__cause__
            problems = (
                "; ".join(
                    f"{'.'.join(str(part) for part in problem['loc'])}: {problem['msg']}"
                    for problem in cause.errors(include_url=False)
                )
                if isinstance(cause, PydanticValidationError)
                else str(error)
            )
            name = context.message.name
            audit_log.info(
                "%s invalid_argument%s %s",
                name,
                _caller(),
                _audit_arguments(context.message.arguments or {}),
            )
            refused = ToolInputError(
                code="invalid_argument",
                message=f"The arguments do not match {name}'s input schema: {problems}",
                hint=(
                    "Use only the parameter names and types the tool lists, and "
                    "include every required one; no query was run."
                ),
            )
            raise ToolError(refused.as_text()) from error


mcp.add_middleware(ArgumentErrors())
mcp.add_middleware(ConcurrentCallLimit(MAX_CONCURRENT_CALLS_PER_CLIENT))


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    """Liveness only: answers without a token, so it reveals nothing.

    It does not touch the database. A database outage would otherwise fail every
    replica's check at once and restart them all, which fixes nothing; callers
    already see database errors through the tools.
    """
    return JSONResponse({"status": "ok"})


def explore_schema_data(
    table_name: str | None = None,
    include_sample_data: bool = False,
    name_pattern: str | None = None,
    detail: bool = False,
    limit: int = DEFAULT_TABLE_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    if table_name:
        return get_table_detail(current_engine(), table_name, include_sample_data)
    return get_schema_page(
        current_engine(),
        name_pattern=name_pattern,
        limit=limit,
        offset=offset,
        detail=detail,
    )


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def explore_schema(
    table_name: Annotated[
        str | None,
        Field(
            description=(
                "Exact name of one table or view. Returns its columns, keys and "
                "indexes, plus a row count for a table; the listing parameters "
                "are ignored."
            )
        ),
    ] = None,
    include_sample_data: Annotated[
        bool, Field(description="With table_name, also return 3 sample rows.")
    ] = False,
    name_pattern: NamePattern = None,
    detail: Annotated[
        bool,
        Field(
            description=(
                "List each table with its columns, keys and indexes instead of "
                "a column count. Use this, not one table_name call per table, "
                "when a question touches several tables. Pages get larger, so "
                "narrow a big schema with name_pattern."
            )
        ),
    ] = False,
    limit: Limit = DEFAULT_TABLE_LIMIT,
    offset: Offset = 0,
) -> dict[str, Any]:
    """List the database's tables and views, or describe one in detail.

    Every result names the SQL dialect to write queries in. Table and column
    comments stored in the database come with table_name or detail=true, not
    with the default listing. CHECK constraints, triggers and view definitions
    are never included, so their absence here says nothing about the database.

    A listing comes in pages. A page can hold fewer tables than limit when it
    reaches the size cap, so continue with offset=next_offset, never offset+limit.
    """
    return explore_schema_data(
        table_name,
        include_sample_data,
        name_pattern=name_pattern,
        detail=detail,
        limit=limit,
        offset=offset,
    )


def execute_query_data(sql: str, row_limit: int = 100) -> dict[str, Any]:
    return execute_safe(current_engine(), sql, row_limit)


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def execute_query(
    sql: SelectSql,
    row_limit: Annotated[
        int,
        Field(
            description=(
                f"Most rows to return, at least 1. Values above {MAX_ROW_LIMIT} "
                f"are treated as {MAX_ROW_LIMIT}."
            )
        ),
    ] = 100,
) -> dict[str, Any]:
    """Run one read-only SELECT and return its rows.

    The result is capped by row_limit and by size. truncated=true means more rows
    exist: filter with WHERE or aggregate rather than raising row_limit. Repeated
    column names come back labelled name:1, name:2; alias them in the query.
    """
    return execute_query_data(sql, row_limit)


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def explain_query(sql: SelectSql) -> dict[str, Any]:
    """Return the database's execution plan for one SELECT, without running it."""
    return explain_safe(current_engine(), sql)


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def validate_schema(
    table_name: Annotated[
        str | None,
        Field(
            description=(
                "One table to check; a view is refused as not found. Omit it to "
                "check a page of tables."
            )
        ),
    ] = None,
    name_pattern: NamePattern = None,
    limit: Limit = DEFAULT_TABLE_LIMIT,
    offset: Offset = 0,
) -> dict[str, Any]:
    """Check tables for missing primary keys, unindexed foreign keys, very wide
    tables and tables with no indexes. A page of tables leaves views out.
    """
    return validate_schema_data(current_engine(), table_name, name_pattern, limit, offset)


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def suggest_index(
    query: Annotated[
        str | None,
        Field(
            description=(
                "One SELECT whose plan to check for full table scans, under the "
                "same rules as execute_query. Pass this or table_name, not both."
            )
        ),
    ] = None,
    table_name: Annotated[
        str | None,
        Field(
            description=(
                "A table whose foreign keys to check for a covering index. Pass "
                "this or query, not both."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Suggest indexes for one table or one query.

    With table_name, returns a CREATE INDEX for each foreign key no index covers.
    With query, reports the full table scans in its plan, without generated SQL.
    Nothing is created: the caller decides whether to run a suggestion.
    """
    return suggest_indexes(current_engine(), query, table_name)


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def migration_context(
    name_pattern: NamePattern = None,
    limit: Limit = DEFAULT_TABLE_LIMIT,
    offset: Offset = 0,
) -> dict[str, Any]:
    """Return dialect and one page of table detail for client-side migration drafting.

    A page can hold fewer tables than limit when it reaches the size cap, so
    continue with offset=next_offset, never offset+limit.
    """
    return get_migration_context(current_engine(), name_pattern, limit, offset)


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def validate_migration(
    up_sql: Annotated[
        str,
        Field(
            description=(
                "Statements that apply the change, separated by semicolons, "
                "with no comments."
            )
        ),
    ],
    down_sql: Annotated[
        str, Field(description="Statements that undo up_sql, in the same form.")
    ],
) -> dict[str, Any]:
    """Check migration scripts without executing them.

    Reports each statement's type and rejects SELECT, comments and unbalanced
    parentheses. It does not check syntax: the database reports other errors when
    the script is run.
    """
    return validate_migration_data(current_engine(), up_sql, down_sql)


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
