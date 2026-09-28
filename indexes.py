"""Which column lists a table has an index on that can serve a foreign key.

A foreign key needs an index for joins and for the parent-side delete that looks
up its children. The reflected index list alone does not say whether one exists:
`get_indexes()` leaves out the index behind a PRIMARY KEY on every dialect, and on
SQLite it also leaves out the automatic index behind a UNIQUE constraint. Checking
that list alone reported `profile(user_id PRIMARY KEY REFERENCES users(id))` as
unindexed and recommended a CREATE INDEX that would duplicate the primary key.

Unique constraints are read from those automatic indexes rather than from
`get_unique_constraints()`. That call raises NotImplementedError on SQL Server,
and on SQLite it matches the constraint text against the index case-sensitively,
so `UserId INTEGER, UNIQUE (userid)` comes back as no constraint at all. The
other dialects already report the index behind a unique constraint in
`get_indexes()`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, NamedTuple

from sqlalchemy.engine.reflection import Inspector

# Reflection is keyed by (schema, table); every lookup uses the default schema,
# as in inspector.py.
_DEFAULT_SCHEMA: str | None = None

# Postgres access methods that answer `WHERE fk_column = $1`, which is the lookup
# a foreign key needs. BRIN narrows a scan to ranges of pages rather than finding
# rows, and GIN and GiST index containment and geometry.
_EQUALITY_ACCESS_METHODS = frozenset({"btree", "hash"})


def _serves_equality_lookup(index: dict[str, Any]) -> bool:
    """Return whether an index can find the rows matching one key value."""
    options = index.get("dialect_options") or {}
    # A partial index (postgresql_where, sqlite_where, mssql_where) holds only the
    # rows its predicate admits. The lookup a foreign key needs carries no such
    # predicate, so the planner cannot use it.
    if any(key.endswith("_where") and value is not None for key, value in options.items()):
        return False
    if options.get("postgresql_using", "btree") not in _EQUALITY_ACCESS_METHODS:
        return False
    # MySQL FULLTEXT and SPATIAL keys search text and geometry, not values.
    return not any(
        key.endswith("_prefix") and value in {"FULLTEXT", "SPATIAL"}
        for key, value in options.items()
    )


class IndexCoverage(NamedTuple):
    """The indexes one table has, from the point of view of its foreign keys."""

    primary_key: list[str]
    covering: list[list[str]]
    has_any_index: bool
    case_sensitive: bool

    def covers(self, columns: Sequence[str]) -> bool:
        """Return whether some index starts with exactly these columns, in order.

        Only a leading prefix helps: an index on (team_id, user_id) cannot find
        rows by user_id alone, just as a phone book sorted by surname cannot find
        everyone with a given first name.
        """
        wanted = [self._normalise(column) for column in columns]
        return bool(wanted) and any(
            [self._normalise(column) for column in existing[: len(wanted)]] == wanted
            for existing in self.covering
        )

    def _normalise(self, column: str) -> str:
        # Column names are case-insensitive on SQLite, MySQL and SQL Server, and
        # SQLite reflects the spelling each clause was written with. Postgres is
        # the exception: a quoted "UserId" and userid are different columns.
        return column if self.case_sensitive else column.casefold()


def covering_column_lists(
    indexes: Sequence[dict[str, Any]],
    primary_key: Sequence[str],
) -> list[list[str]]:
    """Return the leading columns of every index that can serve a foreign key."""
    column_lists = [
        list(index.get("column_names") or [])
        for index in indexes
        if _serves_equality_lookup(index)
    ]
    column_lists.append(list(primary_key))

    covering = []
    for columns in column_lists:
        # An expression such as lower(email) is reflected as None. It cannot serve
        # a lookup on the plain column, so the index covers only what precedes it.
        if None in columns:
            columns = columns[: columns.index(None)]
        if columns:
            covering.append(columns)
    return covering


def reflect_coverage(
    inspector: Inspector,
    table_names: Sequence[str],
) -> dict[str, IndexCoverage]:
    """Reflect index coverage for a set of tables in two queries, not two per table."""
    if not table_names:
        return {}

    filter_names = list(table_names)
    # Only SQLite hides the index behind a UNIQUE constraint, and only it accepts
    # the argument that shows it.
    extra = {"include_auto_indexes": True} if inspector.dialect.name == "sqlite" else {}
    indexes = inspector.get_multi_indexes(filter_names=filter_names, **extra)
    primary_keys = inspector.get_multi_pk_constraint(filter_names=filter_names)
    case_sensitive = inspector.dialect.name == "postgresql"

    coverage = {}
    for name in table_names:
        table_indexes = indexes.get((_DEFAULT_SCHEMA, name), [])
        primary_key = list(
            (primary_keys.get((_DEFAULT_SCHEMA, name)) or {}).get("constrained_columns")
            or []
        )
        coverage[name] = IndexCoverage(
            primary_key=primary_key,
            covering=covering_column_lists(table_indexes, primary_key),
            # Any index at all, including ones no foreign key can use: a table
            # with only a GIN or partial index still does not have "no indexes".
            has_any_index=bool(table_indexes or primary_key),
            case_sensitive=case_sensitive,
        )
    return coverage
