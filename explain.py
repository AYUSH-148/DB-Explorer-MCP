from typing import Any

from sqlalchemy import Engine, text

from safety import validate_query


def explain_safe(engine: Engine, sql: str) -> dict[str, Any]:
    """Return the database execution plan for one safe SELECT query."""
    is_safe, reason = validate_query(sql)
    if not is_safe:
        raise ValueError(f"Unsafe query blocked: {reason}")

    query = sql.strip().rstrip(";")
    prefix = "EXPLAIN QUERY PLAN" if engine.dialect.name == "sqlite" else "EXPLAIN"
    explain_sql = f"{prefix} {query}"

    with engine.connect() as connection:
        result = connection.execute(text(explain_sql))
        rows = [dict(row) for row in result.mappings()]
        columns = list(result.keys())

    return {
        "query": query,
        "dialect": engine.dialect.name,
        "columns": columns,
        "plan": rows,
    }
