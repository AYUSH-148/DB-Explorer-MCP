from typing import Any

from sqlalchemy import Engine, inspect, text


def _column_info(column: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": column["name"],
        "type": str(column["type"]),
        "nullable": column["nullable"],
        "default": column.get("default"),
    }


def _table_info(engine: Engine, table_name: str) -> dict[str, Any]:
    database_inspector = inspect(engine)
    primary_key = database_inspector.get_pk_constraint(table_name)
    foreign_keys = database_inspector.get_foreign_keys(table_name)
    indexes = database_inspector.get_indexes(table_name)

    with engine.connect() as connection:
        row_count = connection.execute(
            text(f'SELECT COUNT(*) FROM "{table_name}"')
        ).scalar_one()

    return {
        "name": table_name,
        "row_count": row_count,
        "columns": [
            _column_info(column)
            for column in database_inspector.get_columns(table_name)
        ],
        "primary_key": primary_key.get("constrained_columns", []),
        "foreign_keys": [
            {
                "columns": foreign_key.get("constrained_columns", []),
                "referred_table": foreign_key.get("referred_table"),
                "referred_columns": foreign_key.get("referred_columns", []),
            }
            for foreign_key in foreign_keys
        ],
        "indexes": [
            {
                "name": index["name"],
                "columns": index.get("column_names", []),
                "unique": index.get("unique", False),
            }
            for index in indexes
        ],
    }


def get_all_tables(engine: Engine) -> list[dict[str, Any]]:
    """Return a serializable summary of every table in the database."""
    return [
        _table_info(engine, table_name)
        for table_name in inspect(engine).get_table_names()
    ]


def get_table_detail(
    engine: Engine,
    table_name: str,
    include_sample_data: bool = False,
) -> dict[str, Any]:
    """Return details for one table, optionally including three sample rows."""
    available_tables = inspect(engine).get_table_names()
    if table_name not in available_tables:
        raise ValueError(f"Table not found: {table_name}")

    details = _table_info(engine, table_name)
    if include_sample_data:
        with engine.connect() as connection:
            result = connection.execute(
                text(f'SELECT * FROM "{table_name}" LIMIT 3')
            )
            details["sample_rows"] = [dict(row) for row in result.mappings()]
    return details
