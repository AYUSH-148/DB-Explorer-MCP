from typing import Any

from sqlalchemy import Engine, inspect
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.sql.compiler import IdentifierPreparer

from db import read_only_connection
from errors import ToolInputError, table_not_found
from explain import explain_safe
from indexes import reflect_coverage


def _paste_safe_preparer(dialect: Dialect) -> IdentifierPreparer:
    """Return the quoting for SQL that will be run somewhere else.

    A suggested CREATE INDEX is pasted into another client, not run here. On
    MySQL, SQLAlchemy quotes with double quotes when this connection has
    ANSI_QUOTES on, and in a session without it "order" is a string literal
    and the statement is a syntax error. Backticks quote an identifier in
    every MySQL session, whatever its sql_mode.
    """
    if dialect.name in {"mysql", "mariadb"}:
        return dialect.preparer(dialect, server_ansiquotes=False)
    return dialect.identifier_preparer


def _full_scan(dialect: str, plan_row: dict[str, Any]) -> str | None:
    """Return the plan row's text if it reads a whole table, else None."""
    if dialect == "postgresql":
        # One row per line of the text plan; "Parallel Seq Scan on" matches too.
        line = str(plan_row.get("QUERY PLAN", ""))
        return line.strip() if "Seq Scan on" in line else None
    if dialect == "mysql":
        # One row per table read; access type ALL is a full table scan.
        if plan_row.get("type") == "ALL":
            return f"table {plan_row.get('table')} (access type ALL)"
        return None
    detail = str(plan_row.get("detail", ""))
    if "SCAN" in detail.upper() and "USING INDEX" not in detail.upper():
        return detail
    return None


def suggest_indexes(
    engine: Engine,
    query: str | None = None,
    table_name: str | None = None,
) -> dict[str, Any]:
    """Suggest indexes from foreign-key metadata or a query execution plan."""
    if not query and not table_name:
        raise ToolInputError(
            code="missing_argument",
            message="Provide a query or table_name",
            hint=(
                "Pass query=... to analyse one statement's plan, or "
                "table_name=... to check a table's foreign keys."
            ),
        )
    if query and table_name:
        raise ToolInputError(
            code="conflicting_arguments",
            message="Provide query or table_name, not both",
            hint="Call the tool twice if you need both views.",
        )

    recommendations: list[dict[str, Any]] = []
    if table_name:
        with read_only_connection(engine) as connection:
            database_inspector = inspect(connection)
            known_tables = database_inspector.get_table_names()
            if table_name not in known_tables:
                raise table_not_found(table_name, known_tables)

            foreign_keys = database_inspector.get_foreign_keys(table_name)
            # Coverage costs two more queries, and a table with no foreign keys
            # has nothing for it to answer, so it is only reflected when there is one.
            coverage = (
                reflect_coverage(database_inspector, [table_name])[table_name]
                if foreign_keys
                else None
            )

        if coverage is not None:
            preparer = _paste_safe_preparer(engine.dialect)
            for foreign_key in foreign_keys:
                columns = foreign_key.get("constrained_columns", [])
                if not columns or coverage.covers(columns):
                    continue
                # Quoted, because this is SQL someone will paste and run: a table
                # called `order`, or a Postgres column called "UserId", breaks the
                # statement unquoted. quote() leaves an ordinary name alone.
                index_name = preparer.quote(f"idx_{table_name}_{'_'.join(columns)}")
                column_list = ", ".join(preparer.quote(column) for column in columns)
                recommendations.append(
                    {
                        "table": table_name,
                        "columns": columns,
                        "sql": (
                            f"CREATE INDEX {index_name} "
                            f"ON {preparer.quote(table_name)} ({column_list});"
                        ),
                        "reason": "Foreign-key columns are not covered by an index.",
                    }
                )

        return {
            "mode": "table",
            "table": table_name,
            "recommendations": recommendations,
        }

    plan = explain_safe(engine, query or "")
    for plan_row in plan["plan"]:
        scan = _full_scan(plan["dialect"], plan_row)
        if scan:
            recommendations.append(
                {
                    "sql": None,
                    "reason": f"Execution plan contains a full scan: {scan}",
                }
            )

    return {
        "mode": "query",
        "query": plan["query"],
        "plan": plan["plan"],
        "recommendations": recommendations,
    }
