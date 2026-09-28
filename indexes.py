"""Which column lists a table already has an index on.

A foreign key needs an index for joins and for the parent-side delete that looks
up its children. Asking the reflected index list is not enough to know whether
one exists: `get_indexes()` leaves out the indexes a database builds for itself
behind a PRIMARY KEY and, on SQLite, behind a UNIQUE constraint. Checking that
list alone reported `profile(user_id PRIMARY KEY REFERENCES users(id))` as
unindexed and recommended a CREATE INDEX that would duplicate the primary key.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def covering_column_lists(
    indexes: Sequence[dict[str, Any]],
    primary_key: dict[str, Any],
    unique_constraints: Sequence[dict[str, Any]],
) -> list[list[str]]:
    """Return the column list of every index the table has, implicit ones included.

    A unique constraint that the dialect also reports as an index appears twice,
    which is harmless: the lists are only ever searched.

    A partial index (Postgres `WHERE ...`) counts as covering. It serves only some
    rows, but treating it as absent would recommend a second index on the same
    columns, and a recommendation someone runs against production is worse when
    it is wrong than when it is missing.
    """
    column_lists = [list(index.get("column_names") or []) for index in indexes]
    column_lists.append(list(primary_key.get("constrained_columns") or []))
    column_lists.extend(
        list(constraint.get("column_names") or []) for constraint in unique_constraints
    )

    covering = []
    for columns in column_lists:
        # An expression such as lower(email) is reflected as None. It cannot serve
        # a lookup on the plain column, so the index covers only what precedes it.
        if None in columns:
            columns = columns[: columns.index(None)]
        if columns:
            covering.append(columns)
    return covering


def is_covered(columns: Sequence[str], covering: Sequence[Sequence[str]]) -> bool:
    """Return whether some index starts with exactly these columns, in this order.

    Only a leading prefix helps: an index on (team_id, user_id) cannot find rows
    by user_id alone, just as a phone book sorted by surname cannot find everyone
    with a given first name. Names are compared exactly, because both sides come
    from the same catalog and Postgres treats "UserId" and userid as different
    columns.
    """
    wanted = list(columns)
    return bool(wanted) and any(
        list(existing[: len(wanted)]) == wanted for existing in covering
    )
