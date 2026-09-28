from typing import Any

from sqlalchemy import Engine, inspect

from errors import table_not_found
from indexes import covering_column_lists, is_covered


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

    for current_table in selected_tables:
        columns = database_inspector.get_columns(current_table)
        primary_key = database_inspector.get_pk_constraint(current_table)
        foreign_keys = database_inspector.get_foreign_keys(current_table)
        indexes = database_inspector.get_indexes(current_table)
        unique_constraints = database_inspector.get_unique_constraints(current_table)
        covering = covering_column_lists(indexes, primary_key, unique_constraints)
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
            if not is_covered(constrained_columns, covering):
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

        # Checked against what exists rather than against `covering`, which drops an
        # expression index: that index cannot serve a foreign key, but it is still
        # an index, and a table that has one does not have "no indexes".
        if not indexes and not primary_key_columns and not unique_constraints:
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
