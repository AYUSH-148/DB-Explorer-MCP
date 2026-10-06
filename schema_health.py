from typing import Any

from sqlalchemy import Engine, inspect

from db import read_only_connection
from errors import table_not_found
from indexes import reflect_coverage
from inspector import DEFAULT_TABLE_LIMIT, select_names

# Reflection is keyed by (schema, table); every lookup uses the default schema.
_DEFAULT_SCHEMA: str | None = None


def validate_schema(
    engine: Engine,
    table_name: str | None = None,
    name_pattern: str | None = None,
    limit: int = DEFAULT_TABLE_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Report objective schema issues for one table or one page of tables.

    Views are skipped: they have no primary key or indexes of their own.
    """
    with read_only_connection(engine) as connection:
        database_inspector = inspect(connection)
        if table_name:
            tables = database_inspector.get_table_names()
            if table_name not in tables:
                raise table_not_found(table_name, tables)
            page = None
            selected_tables = [table_name]
        else:
            page = select_names(
                database_inspector, name_pattern, limit, offset, tables_only=True
            )
            selected_tables = page.names

        # One call per kind for every selected table. PostgreSQL and Oracle answer
        # each in one query; SQLAlchemy loops per table on the other dialects, which
        # is why the page is bounded.
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

    issues: list[dict[str, Any]] = []

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

    result: dict[str, Any] = {
        "table": table_name,
        "tables_checked": selected_tables,
        "issue_count": len(issues),
        "issues": issues,
    }
    if page is not None:
        result.update(
            total_matching_tables=page.total,
            offset=page.offset,
            limit=page.limit,
            has_more=page.has_more,
        )
        if page.has_more:
            result["next_offset"] = page.offset + len(page.names)
    return result
