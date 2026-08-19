import re
from typing import Any

from sqlalchemy import Engine, text
from sqlparse import parse
from sqlparse.tokens import DDL, DML, Keyword


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
    if "--" in sql or "/*" in sql or "*/" in sql:
        return False, "SQL comments are not allowed"

    statements = [statement for statement in parse(sql) if statement.tokens]
    if len(statements) != 1:
        return False, "Exactly one SQL statement is required"

    statement = statements[0]
    if statement.get_type() != "SELECT":
        statement_type = statement.get_type() or "UNKNOWN"
        return False, f"Only SELECT queries are allowed. Got: {statement_type}"

    for token in statement.flatten():
        if token.ttype in (DML, DDL, Keyword):
            keyword = token.value.upper()
            if keyword in BLOCKED_KEYWORDS:
                return False, f"Blocked keyword detected: {keyword}"

    return True, "Query is safe"


def _has_limit(sql: str) -> bool:
    return bool(re.search(r"\bLIMIT\b", sql, re.IGNORECASE))


def execute_safe(
    engine: Engine,
    sql: str,
    row_limit: int = 100,
) -> dict[str, Any]:
    """Validate and execute a read-only query with a maximum row count."""
    if row_limit < 1:
        raise ValueError("row_limit must be greater than zero")

    is_safe, reason = validate_query(sql)
    if not is_safe:
        raise ValueError(f"Unsafe query blocked: {reason}")

    query = sql.strip().rstrip(";")
    if not _has_limit(query):
        query = f"SELECT * FROM ({query}) AS limited_query LIMIT {row_limit}"

    with engine.connect() as connection:
        result = connection.execute(text(query))
        rows = [dict(row) for row in result.mappings()]

    return {
        "columns": list(result.keys()),
        "rows": rows,
        "count": len(rows),
    }
