from typing import Any

from sqlalchemy import Engine, text
from sqlparse import parse
from sqlparse.tokens import Comment, DDL, DML, Keyword
from db import read_only_connection
from errors import ToolInputError, unsafe_query
from serialization import jsonable_rows


# The row cap is a context guard, so the caller must not be able to raise it
# without bound. Mirrors MAX_TABLE_LIMIT in inspector.py.
MAX_ROW_LIMIT = 1000

BLOCKED_KEYWORDS = {
    "ALTER",
    "CREATE",
    "DELETE",
    "DROP",
    "EXEC",
    "EXECUTE",
    "GRANT",
    "INSERT",
    "INTO",
    "REVOKE",
    "SET",
    "TRUNCATE",
    "UPDATE",
}


def validate_query(sql: str) -> tuple[bool, str]:
    """Return whether SQL contains exactly one safe read-only statement."""
    if not isinstance(sql, str) or not sql.strip():
        return False, "SQL query is required"

    statements = [statement for statement in parse(sql) if statement.tokens]
    if len(statements) != 1:
        return False, "Exactly one SQL statement is required"

    statement = statements[0]
    if statement.get_type() != "SELECT":
        statement_type = statement.get_type() or "UNKNOWN"
        return False, f"Only SELECT queries are allowed. Got: {statement_type}"

    # Comments are detected on parsed tokens rather than as raw substrings so that
    # a string literal containing "--" is not mistaken for a comment.
    for token in statement.flatten():
        if token.ttype in Comment:
            return False, "SQL comments are not allowed"
        if token.ttype in (DML, DDL, Keyword):
            keyword = token.value.upper()
            if keyword in BLOCKED_KEYWORDS:
                return False, f"Blocked keyword detected: {keyword}"

    return True, "Query is safe"


def execute_safe(
    engine: Engine,
    sql: str,
    row_limit: int = 100,
) -> dict[str, Any]:
    """Validate and execute a read-only query with a maximum row count."""
    if row_limit < 1:
        raise ToolInputError(
            code="invalid_argument",
            message="row_limit must be greater than zero",
            hint="Pass the number of rows you want back, at least 1.",
            received=row_limit,
        )
    # Clamped rather than rejected: a caller asking for more rows than the cap
    # wants as many as it can have, and failing the query would tell it nothing
    # it can act on.
    row_limit = min(row_limit, MAX_ROW_LIMIT)

    is_safe, reason = validate_query(sql)
    if not is_safe:
        raise unsafe_query(reason)

    # The query is always wrapped. Looking for a LIMIT in the text instead would
    # accept one belonging to a subquery and leave the result set unbounded.
    inner_query = sql.strip().rstrip(";")
    query = f"SELECT * FROM ({inner_query}) AS limited_query LIMIT {row_limit}"

    with read_only_connection(engine) as connection:
        result = connection.execute(text(query))
        rows = jsonable_rows(result.mappings())
        columns = list(result.keys())

    return {
        "columns": columns,
        "rows": rows,
        "count": len(rows),
    }
