from typing import Any

from sqlalchemy import Engine, inspect


def _has_index_for_columns(
    indexes: list[dict[str, Any]],
    columns: list[str],
) -> bool:
    return any(
        index.get("column_names", [])[: len(columns)] == columns
        for index in indexes
    )


def validate_schema(
    engine: Engine,
    table_name: str | None = None,
) -> dict[str, Any]:
    """Report objective schema issues for one table or the whole database."""
    database_inspector = inspect(engine)
    tables = database_inspector.get_table_names()
    if table_name and table_name not in tables:
        raise ValueError(f"Table not found: {table_name}")

    selected_tables = [table_name] if table_name else tables
    issues: list[dict[str, Any]] = []

    for current_table in selected_tables:
        columns = database_inspector.get_columns(current_table)
        primary_key = database_inspector.get_pk_constraint(current_table)
        foreign_keys = database_inspector.get_foreign_keys(current_table)
        indexes = database_inspector.get_indexes(current_table)
        primary_key_columns = primary_key.get("constrained_columns", [])

        if not primary_key_columns:
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
            if not _has_index_for_columns(indexes, constrained_columns):
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

        if not indexes and not primary_key_columns:
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
