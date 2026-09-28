from typing import Any

from sqlalchemy import Engine, inspect

from errors import ToolInputError, table_not_found
from explain import explain_safe
from indexes import reflect_coverage


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
        database_inspector = inspect(engine)
        known_tables = database_inspector.get_table_names()
        if table_name not in known_tables:
            raise table_not_found(table_name, known_tables)

        foreign_keys = database_inspector.get_foreign_keys(table_name)
        # Coverage costs two more queries, and a table with no foreign keys has
        # nothing for it to answer, so it is only reflected when there is one.
        if foreign_keys:
            coverage = reflect_coverage(database_inspector, [table_name])[table_name]
            preparer = engine.dialect.identifier_preparer
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
        detail = str(plan_row.get("detail", ""))
        if "SCAN" in detail.upper() and "USING INDEX" not in detail.upper():
            recommendations.append(
                {
                    "sql": None,
                    "reason": f"Execution plan contains a full scan: {detail}",
                }
            )

    return {
        "mode": "query",
        "query": plan["query"],
        "plan": plan["plan"],
        "recommendations": recommendations,
    }
