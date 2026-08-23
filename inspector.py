"""Schema reflection, batched so a wide database costs a few queries, not a few thousand.

Two rules keep this cheap. One connection and one Inspector serve an entire call,
and metadata for every table on the page arrives in one query per kind rather than
one query per table. Row counts are the exception: COUNT(*) scans the whole table,
so they are computed for a single named table, or when a caller opts in.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from typing import Any, NamedTuple

from sqlalchemy import Connection, Engine, inspect, text
from sqlalchemy.engine.reflection import Inspector

from db import read_only_connection
from errors import ToolInputError, table_not_found
from serialization import jsonable, jsonable_rows

# A summary row is small, but a warehouse has thousands of tables. Bound the page so
# one call cannot spend a whole context window on a listing.
DEFAULT_TABLE_LIMIT = 200
MAX_TABLE_LIMIT = 1000

_WILDCARDS = "*?["

# Reflection is keyed by (schema, table). Multi-schema support is not wired up yet,
# so every lookup uses the default schema.
_DEFAULT_SCHEMA: str | None = None


class _Page(NamedTuple):
    """One page of table names, plus the bounds that produced it."""

    names: list[str]
    total: int
    limit: int | None
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.names) < self.total


def _column_info(column: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": column["name"],
        "type": str(column["type"]),
        "nullable": column["nullable"],
        "default": jsonable(column.get("default")),
    }


def _matches(table_name: str, pattern: str) -> bool:
    """Match case-insensitively. A pattern with no wildcard matches a substring."""
    lowered = pattern.lower()
    if not any(wildcard in lowered for wildcard in _WILDCARDS):
        lowered = f"*{lowered}*"
    return fnmatch.fnmatchcase(table_name.lower(), lowered)


def _select_names(
    inspector: Inspector,
    name_pattern: str | None,
    limit: int | None,
    offset: int,
) -> _Page:
    """Return the page of table names a call should reflect."""
    if limit is not None:
        if limit < 1:
            raise ToolInputError(
                code="invalid_argument",
                message="limit must be at least 1",
                hint=f"Pass a limit between 1 and {MAX_TABLE_LIMIT}, or omit it.",
                received=limit,
            )
        limit = min(limit, MAX_TABLE_LIMIT)
    if offset < 0:
        raise ToolInputError(
            code="invalid_argument",
            message="offset must not be negative",
            hint="Start at offset 0 and page forward with next_offset.",
            received=offset,
        )

    names = sorted(inspector.get_table_names())
    if name_pattern:
        names = [name for name in names if _matches(name, name_pattern)]

    end = None if limit is None else offset + limit
    return _Page(names[offset:end], len(names), limit, offset)


def _reflect(
    inspector: Inspector,
    table_names: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Reflect a set of tables in four queries rather than four per table."""
    if not table_names:
        return {}

    filter_names = list(table_names)
    columns = inspector.get_multi_columns(filter_names=filter_names)
    primary_keys = inspector.get_multi_pk_constraint(filter_names=filter_names)
    foreign_keys = inspector.get_multi_foreign_keys(filter_names=filter_names)
    indexes = inspector.get_multi_indexes(filter_names=filter_names)

    return {
        name: {
            "columns": columns.get((_DEFAULT_SCHEMA, name), []),
            "primary_key": primary_keys.get((_DEFAULT_SCHEMA, name)) or {},
            "foreign_keys": foreign_keys.get((_DEFAULT_SCHEMA, name), []),
            "indexes": indexes.get((_DEFAULT_SCHEMA, name), []),
        }
        for name in table_names
    }


def _table_payload(table_name: str, reflected: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": table_name,
        "columns": [_column_info(column) for column in reflected["columns"]],
        "primary_key": reflected["primary_key"].get("constrained_columns", []),
        "foreign_keys": [
            {
                "columns": foreign_key.get("constrained_columns", []),
                "referred_table": foreign_key.get("referred_table"),
                "referred_columns": foreign_key.get("referred_columns", []),
            }
            for foreign_key in reflected["foreign_keys"]
        ],
        "indexes": [
            {
                "name": index["name"],
                "columns": index.get("column_names", []),
                "unique": index.get("unique", False),
            }
            for index in reflected["indexes"]
        ],
    }


def _row_count(connection: Connection, table_name: str) -> int:
    quoted = connection.dialect.identifier_preparer.quote(table_name)
    return connection.execute(text(f"SELECT COUNT(*) FROM {quoted}")).scalar_one()


def get_schema_page(
    engine: Engine,
    name_pattern: str | None = None,
    limit: int | None = DEFAULT_TABLE_LIMIT,
    offset: int = 0,
    detail: bool = False,
    include_row_counts: bool = False,
) -> dict[str, Any]:
    """Return one page of tables, as a compact listing or with full detail."""
    with read_only_connection(engine) as connection:
        inspector = inspect(connection)
        page = _select_names(inspector, name_pattern, limit, offset)

        if detail:
            reflected = _reflect(inspector, page.names)
            tables = [_table_payload(name, reflected[name]) for name in page.names]
        else:
            columns = (
                inspector.get_multi_columns(filter_names=list(page.names))
                if page.names
                else {}
            )
            tables = [
                {
                    "name": name,
                    "column_count": len(columns.get((_DEFAULT_SCHEMA, name), [])),
                }
                for name in page.names
            ]

        if include_row_counts:
            for table in tables:
                table["row_count"] = _row_count(connection, table["name"])

    result: dict[str, Any] = {
        "tables": tables,
        "total_matching_tables": page.total,
        "returned": len(tables),
        "offset": page.offset,
        "limit": page.limit,
        "has_more": page.has_more,
    }
    if page.has_more:
        result["next_offset"] = page.offset + len(tables)
    if not detail:
        result["detail_hint"] = (
            "Call explore_schema(table_name=...) for columns, keys, indexes, "
            "and row count."
        )
    return result


def get_all_tables(
    engine: Engine,
    include_row_counts: bool = True,
) -> list[dict[str, Any]]:
    """Return full detail for every table in the database."""
    return get_schema_page(
        engine,
        limit=None,
        detail=True,
        include_row_counts=include_row_counts,
    )["tables"]


def get_table_detail(
    engine: Engine,
    table_name: str,
    include_sample_data: bool = False,
) -> dict[str, Any]:
    """Return details for one table, optionally including three sample rows."""
    with read_only_connection(engine) as connection:
        inspector = inspect(connection)
        known_tables = inspector.get_table_names()
        if table_name not in known_tables:
            raise table_not_found(table_name, known_tables)

        reflected = _reflect(inspector, [table_name])[table_name]
        details = _table_payload(table_name, reflected)
        details["row_count"] = _row_count(connection, table_name)

        if include_sample_data:
            quoted = connection.dialect.identifier_preparer.quote(table_name)
            result = connection.execute(text(f"SELECT * FROM {quoted} LIMIT 3"))
            details["sample_rows"] = jsonable_rows(result.mappings())
    return details
