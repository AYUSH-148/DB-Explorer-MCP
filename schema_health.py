from typing import Any

from sqlalchemy import Engine, inspect

from errors import table_not_found
from indexes import reflect_coverage

# Reflection is keyed by (schema, table); every lookup uses the default schema.
_DEFAULT_SCHEMA: str | None = None


def validate_schema(
    engine: Engine,
    table_name: str | None = None,
) -> dict[str, Any]:
    """Report objective schema issues for one table or the whole database."""
    database_inspector = inspect(engine)
    tables = database_inspector.get_table_names()
    if table_name and table_name not in tables:
        raise table_not_found(table_name, tables)

    selected_tables = [table_name] if table_name else tables
    issues: list[dict[str, Any]] = []

    # One query per kind for every selected table, rather than one per kind per
    # table: a 500-table audit costs four queries instead of two thousand.
    filter_names = list(selected_tables)
    all_columns = (
        database_inspector.get_multi_columns(filter_names=filter_names)
        if filter_names
        else {}
    )
    all_foreign_keys = (
        database_inspector.get_multi_foreign_keys(filter_names=filter_names)
        if filter_names
        else {}
    )
    coverage = reflect_coverage(database_inspector, selected_tables)

    for current_table in selected_tables:
        columns = all_columns.get((_DEFAULT_SCHEMA, current_table), [])
        foreign_keys = all_foreign_keys.get((_DEFAULT_SCHEMA, current_table), [])
        table_coverage = coverage[current_table]

        if not table_coverage.primary_key:
            issues.append(
                {
                    "severity": "warning",
                    "code": "missing_primary_key",
                    "table": current_table,
                    "message": f"Table '{current_table}' has no primary key.",
                    "suggestion": "Add a primary key for reliable row identity.",
                }
            )

        if len(columns) >= 50:
            issues.append(
                {
                    "severity": "info",
                    "code": "wide_table",
                    "table": current_table,
                    "message": (
                        f"Table '{current_table}' has {len(columns)} columns."
                    ),
                    "suggestion": "Review whether some fields belong in a related table.",
                }
            )

        for foreign_key in foreign_keys:
            constrained_columns = foreign_key.get("constrained_columns", [])
            if not constrained_columns:
                continue
            if not table_coverage.covers(constrained_columns):
                columns_text = ", ".join(constrained_columns)
                issues.append(
                    {
                        "severity": "warning",
                        "code": "unindexed_foreign_key",
                        "table": current_table,
                        "columns": constrained_columns,
                        "message": (
                            f"Foreign key column(s) '{columns_text}' on "
                            f"'{current_table}' are not indexed."
                        ),
                        "suggestion": (
                            f"Create an index on {current_table}({columns_text})."
                        ),
                    }
                )

        if not table_coverage.has_any_index:
            issues.append(
                {
                    "severity": "info",
                    "code": "no_indexes",
                    "table": current_table,
                    "message": f"Table '{current_table}' has no indexes.",
                    "suggestion": "Add indexes for frequent filters and joins.",
                }
            )

    return {
        "table": table_name,
        "tables_checked": selected_tables,
        "issue_count": len(issues),
        "issues": issues,
    }
