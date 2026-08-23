from typing import Any

from sqlalchemy import Engine, text

from db import read_only_connection
from errors import unsafe_query
from safety import validate_query
from serialization import jsonable_rows


def explain_safe(engine: Engine, sql: str) -> dict[str, Any]:
    """Return the database execution plan for one safe SELECT query."""
    is_safe, reason = validate_query(sql)
    if not is_safe:
        raise unsafe_query(reason)

    query = sql.strip().rstrip(";")
    prefix = "EXPLAIN QUERY PLAN" if engine.dialect.name == "sqlite" else "EXPLAIN"
    explain_sql = f"{prefix} {query}"

    with read_only_connection(engine) as connection:
        result = connection.execute(text(explain_sql))
        rows = jsonable_rows(result.mappings())
        columns = list(result.keys())

    return {
        "query": query,
        "dialect": engine.dialect.name,
        "columns": columns,
        "plan": rows,
    }
