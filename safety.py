from typing import Any

from sqlalchemy import Engine, text
from sqlparse import parse
from sqlparse.sql import Statement
from sqlparse.tokens import Comment, DDL, DML, Keyword, Literal
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
    "TRUNCATE",
    "UPDATE",
}

# SET is deliberately absent. It rejected `SELECT set FROM config`, because
# sqlparse types a bare `set` as a Keyword rather than a Name, and it protected
# nothing: every statement that changes session state -- SET search_path,
# SET ROLE, SET SESSION TRANSACTION READ WRITE, even SET x = (SELECT 1) -- parses
# as statement type UNKNOWN and is refused by the type check above, while the
# UPDATE ... SET form inside a data-modifying CTE is caught by UPDATE. Quoting
# the column (`SELECT "set"`) was the only workaround, which is a poor thing to
# ask of a caller for no gain in safety.

# Row locks are refused because a locking read is not a read: it blocks other
# transactions from writing those rows, so a tool that advertises itself as
# read-only could stall the writers around it.
#
# This is defense in depth, not a hole being closed. Verified against
# PostgreSQL 16: a read-only transaction refuses both forms itself, with
# "cannot execute SELECT FOR SHARE in a read-only transaction". What this check
# adds is a rejection before a connection is opened, an error naming the clause
# and the fix rather than a generic driver message, and consistency -- FOR UPDATE
# was previously refused only incidentally, because UPDATE happens to be on the
# denylist for data-modifying CTEs. MySQL's LOCK IN SHARE MODE under
# SET SESSION TRANSACTION READ ONLY is untested.
#
# Matched as a clause rather than as a keyword. `SHARE` alone is a legal column
# name -- sqlparse types the `share` in `SELECT share FROM cap_table` as a
# Keyword, so a BLOCKED_KEYWORDS entry would reject that real query. A flat set
# of words is the wrong shape for a rule about multi-word clauses.
_LOCK_MODIFIERS = frozenset({"NO", "KEY"})
_LOCK_TARGETS = frozenset({"UPDATE", "SHARE"})
_MYSQL_LOCK_CLAUSE = ("LOCK", "IN", "SHARE", "MODE")


def _locking_clause(keywords: list[str]) -> str | None:
    """Return the locking clause this keyword sequence contains, if any."""
    for index, keyword in enumerate(keywords):
        if keyword == "FOR":
            # FOR [NO] [KEY] UPDATE | SHARE. Dropping the optional modifiers
            # keeps all four Postgres spellings on one path, and a `FOR` that
            # belongs to something else (FOR XML, FOR SYSTEM_TIME) falls through
            # because its target is not a lock strength.
            rest = [
                word
                for word in keywords[index + 1 : index + 4]
                if word not in _LOCK_MODIFIERS
            ]
            if rest and rest[0] in _LOCK_TARGETS:
                return f"FOR {rest[0]}"
        if tuple(keywords[index : index + 4]) == _MYSQL_LOCK_CLAUSE:
            return " ".join(_MYSQL_LOCK_CLAUSE)
    return None


_COMMENT_MARKERS = ("--", "/*", "*/")
# A bare "#hi" lexes as a Name, not a Comment, yet MySQL treats it as one. Only
# the SELECT-only path adds it: in migrations "#" is legitimate (SQL Server #temp).
_QUERY_COMMENT_MARKERS = _COMMENT_MARKERS + ("#",)


def has_comment(statement: Statement, markers: tuple[str, ...] = _COMMENT_MARKERS) -> bool:
    """Detect comments on parsed tokens so a literal containing "--" is not one.

    Real comments lex as Comment tokens. An unterminated "/*" does not: it lexes
    as the separate tokens "/" and "*". So the non-literal tokens are also
    rejoined and searched for the markers. Literals and backtick names are
    masked with a space so their contents are never searched and the tokens
    around them cannot fuse into a marker.
    """
    parts = []
    for token in statement.flatten():
        if token.ttype in Comment:
            return True
        quoted = token.ttype in Literal or token.value.startswith("`")
        parts.append(" " if quoted else token.value)
    code = "".join(parts)
    return any(marker in code for marker in markers)


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

    if has_comment(statement, _QUERY_COMMENT_MARKERS):
        return False, "SQL comments are not allowed"

    # Keywords are collected in order so the clause check below can look at
    # sequences rather than single words.
    keywords: list[str] = []
    for token in statement.flatten():
        if token.ttype in (DML, DDL, Keyword):
            keywords.append(token.value.upper())

    # Checked before the denylist so that FOR UPDATE reports the clause that
    # actually rejected it rather than the bare UPDATE it happens to contain.
    clause = _locking_clause(keywords)
    if clause:
        return False, f"Locking clauses are not allowed: {clause}"

    for keyword in keywords:
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
    # One extra row is fetched so a capped result can be told apart from a
    # complete one; without it the caller would report a partial set as whole.
    inner_query = sql.strip().rstrip(";")
    query = f"SELECT * FROM ({inner_query}) AS limited_query LIMIT {row_limit + 1}"

    with read_only_connection(engine) as connection:
        result = connection.execute(text(query))
        rows = jsonable_rows(result.mappings())
        columns = list(result.keys())

    truncated = len(rows) > row_limit
    rows = rows[:row_limit]
    return {
        "columns": columns,
        "rows": rows,
        "count": len(rows),
        "truncated": truncated,
        "row_limit": row_limit,
    }
