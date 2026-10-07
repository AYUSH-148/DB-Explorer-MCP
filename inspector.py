"""Schema reflection, batched so a wide database costs a few queries, not a few thousand.

Two rules keep this cheap. One connection and one Inspector serve an entire call,
and metadata for every table on the page arrives in one query per kind rather than
one query per table. Row counts are the exception: they scan rows, so they are
computed only for a single named table, and stop at ROW_COUNT_CAP.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Sequence
from typing import Any, NamedTuple

from sqlalchemy import Connection, Engine, inspect, text
from sqlalchemy.engine.reflection import Inspector, ObjectKind
from sqlalchemy.exc import SQLAlchemyError

from db import audit_log, read_only_connection
from errors import ToolInputError, from_database_error, table_not_found
from serialization import jsonable, jsonable_rows

# A summary row is small, but a warehouse has thousands of tables. Bound the page so
# one call cannot spend a whole context window on a listing.
DEFAULT_TABLE_LIMIT = 200
MAX_TABLE_LIMIT = 1000

# Past this many rows, row_count reports the cap and row_count_capped is true.
ROW_COUNT_CAP = 100_000

_WILDCARDS = "*?["

# Reflection is keyed by (schema, table). Multi-schema support is not wired up yet,
# so every lookup uses the default schema.
_DEFAULT_SCHEMA: str | None = None

_UNREADABLE = (
    "Could not read this relation's columns. A view whose base table was "
    "dropped or renamed does this."
)


class _Page(NamedTuple):
    """One page of table names, plus the bounds that produced it."""

    names: list[str]
    kinds: dict[str, str]
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


def _relation_kinds(inspector: Inspector) -> dict[str, str]:
    """Map every relation a query can select from to its kind.

    Views are listed because execute_query reads them like tables; leaving them
    out showed the caller a schema narrower than the one it could query.
    """
    kinds = {name: "view" for name in inspector.get_view_names()}
    try:
        kinds.update(
            (name, "materialized_view")
            for name in inspector.get_materialized_view_names()
        )
    except NotImplementedError:
        pass  # Only some dialects (PostgreSQL, Oracle) have them.
    kinds.update((name, "table") for name in inspector.get_table_names())
    return kinds


def select_names(
    inspector: Inspector,
    name_pattern: str | None,
    limit: int | None,
    offset: int,
    tables_only: bool = False,
) -> _Page:
    """Return the page of relation names a call should reflect.

    tables_only leaves out views, for callers whose checks only make sense on a
    table, such as a missing primary key.
    """
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

    if tables_only:
        kinds = {name: "table" for name in inspector.get_table_names()}
    else:
        kinds = _relation_kinds(inspector)
    names = sorted(kinds)
    if name_pattern:
        names = [name for name in names if _matches(name, name_pattern)]

    end = None if limit is None else offset + limit
    return _Page(names[offset:end], kinds, len(names), limit, offset)


def _aborted_transaction(error: SQLAlchemyError) -> bool:
    """Whether Postgres refused a statement because an earlier one failed (25P02)."""
    orig = getattr(error, "orig", None)
    return (getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)) == "25P02"


def _batched(
    names: Sequence[str],
    fetch: Callable[[Sequence[str]], dict[str, Any]],
) -> dict[str, Any]:
    """Fetch metadata for many relations in one go, or by halving if that fails.

    One unreadable relation, such as a view whose base table was dropped, fails
    the whole batched query and would take every healthy relation's listing with
    it. Halving the batch until the failure is down to one name confines it to the
    relation that has it, which maps to None. One broken relation costs about
    log2(n) extra fetches; if every relation is broken it costs up to 2n - 1,
    more than retrying singly would. `fetch` must return an entry for every name
    it is given.
    ponytail: no savepoint, so on Postgres a failed statement aborts the
    transaction. The next fetch then raises "transaction aborted", which is
    re-raised rather than blamed on a relation. Postgres refuses to drop a table
    that a view depends on, so a broken view should not occur there.
    """
    try:
        return fetch(names)
    except SQLAlchemyError as error:
        # A timeout or lost connection fails every relation alike; splitting the
        # batch cannot help and would blame each relation for it. The same goes
        # for a Postgres transaction already aborted by an earlier failure.
        if _aborted_transaction(error) or (
            from_database_error(error, log=False).code != "sql_error"
        ):
            raise
    if len(names) == 1:
        return {names[0]: None}
    middle = len(names) // 2
    return {**_batched(names[:middle], fetch), **_batched(names[middle:], fetch)}


def _column_lists(
    inspector: Inspector,
    names: Sequence[str],
) -> dict[str, list[dict[str, Any]] | None]:
    def fetch(batch: Sequence[str]) -> dict[str, Any]:
        found = inspector.get_multi_columns(
            filter_names=list(batch), kind=ObjectKind.ANY
        )
        return {name: found.get((_DEFAULT_SCHEMA, name), []) for name in batch}

    return _batched(names, fetch) if names else {}


def _reflect(
    inspector: Inspector,
    table_names: Sequence[str],
) -> dict[str, dict[str, Any] | None]:
    """Reflect a set of tables, mapping any that cannot be read to None."""
    if not table_names:
        return {}
    return _batched(table_names, lambda names: _reflect_batch(inspector, names))


def _reflect_batch(
    inspector: Inspector,
    table_names: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Reflect a set of tables in four queries rather than four per table.

    ObjectKind.ANY includes views; the default kind silently drops them.
    """
    options = {"filter_names": list(table_names), "kind": ObjectKind.ANY}
    columns = inspector.get_multi_columns(**options)
    primary_keys = inspector.get_multi_pk_constraint(**options)
    foreign_keys = inspector.get_multi_foreign_keys(**options)
    indexes = inspector.get_multi_indexes(**options)

    return {
        name: {
            "columns": columns.get((_DEFAULT_SCHEMA, name), []),
            "primary_key": primary_keys.get((_DEFAULT_SCHEMA, name)) or {},
            "foreign_keys": foreign_keys.get((_DEFAULT_SCHEMA, name), []),
            "indexes": indexes.get((_DEFAULT_SCHEMA, name), []),
        }
        for name in table_names
    }


def _table_payload(
    table_name: str, kind: str, reflected: dict[str, Any] | None
) -> dict[str, Any]:
    if reflected is None:
        return {"name": table_name, "kind": kind, "error": _UNREADABLE}
    return {
        "name": table_name,
        "kind": kind,
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


def _row_count(connection: Connection, table_name: str) -> tuple[int, bool]:
    """Count rows up to ROW_COUNT_CAP; return (count, capped).

    A bare COUNT(*) scans the whole table, so a billion-row table hit the statement
    timeout and the call returned nothing, not even the columns already reflected.
    Counting a LIMITed subquery stops the scan at the cap on every dialect.
    """
    quoted = connection.dialect.identifier_preparer.quote(table_name)
    count = connection.execute(
        text(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM {quoted} "
            f"LIMIT {ROW_COUNT_CAP + 1}) AS capped"
        )
    ).scalar_one()
    return min(count, ROW_COUNT_CAP), count > ROW_COUNT_CAP


def get_schema_page(
    engine: Engine,
    name_pattern: str | None = None,
    limit: int | None = DEFAULT_TABLE_LIMIT,
    offset: int = 0,
    detail: bool = False,
) -> dict[str, Any]:
    """Return one page of tables, as a compact listing or with full detail."""
    with read_only_connection(engine) as connection:
        inspector = inspect(connection)
        page = select_names(inspector, name_pattern, limit, offset)

        if detail:
            reflected = _reflect(inspector, page.names)
            tables = [
                _table_payload(name, page.kinds[name], reflected[name])
                for name in page.names
            ]
        else:
            columns = _column_lists(inspector, page.names)
            tables = [
                {"name": name, "kind": page.kinds[name], "error": _UNREADABLE}
                if columns[name] is None
                else {
                    "name": name,
                    "kind": page.kinds[name],
                    "column_count": len(columns[name]),
                }
                for name in page.names
            ]

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


def get_table_detail(
    engine: Engine,
    table_name: str,
    include_sample_data: bool = False,
) -> dict[str, Any]:
    """Return details for one table, optionally including three sample rows."""
    with read_only_connection(engine) as connection:
        inspector = inspect(connection)
        kinds = _relation_kinds(inspector)
        if table_name not in kinds:
            raise table_not_found(table_name, list(kinds))

        reflected = _reflect(inspector, [table_name])[table_name]
        details = _table_payload(table_name, kinds[table_name], reflected)
        if reflected is None:
            return details
        # No count for a view: it would run the view's whole query and could time
        # out the one call that exists to show the structure.
        if kinds[table_name] == "table":
            details["row_count"], details["row_count_capped"] = _row_count(
                connection, table_name
            )

        if include_sample_data:
            quoted = connection.dialect.identifier_preparer.quote(table_name)
            result = connection.execute(text(f"SELECT * FROM {quoted} LIMIT 3"))
            details["sample_rows"] = jsonable_rows(result.mappings())
            audit_log.info("explore_schema sample_rows table=%r", table_name)
    return details
