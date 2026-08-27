"""Errors a caller can act on.
An error is only useful to a model if it says what to do next, so each one
carries a code the client can branch on and a hint the caller can follow.
"""

from __future__ import annotations

from collections.abc import Sequence
from difflib import get_close_matches
from typing import Any


class ToolInputError(ValueError):
    """A caller-fixable error, carrying a code and a corrective hint."""

    def __init__(
        self,
        code: str,
        message: str,
        hint: str | None = None,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        # Empty details would render as noise, so they are dropped rather than
        # sent as an empty list the caller has to interpret.
        self.details = {key: value for key, value in details.items() if value}

    def as_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            error["hint"] = self.hint
        return {"error": {**error, **self.details}}

    def as_text(self) -> str:
        """Render one line a model can read and act on."""
        parts = [f"[{self.code}] {self.message}"]
        for key, value in self.details.items():
            rendered = (
                ", ".join(str(item) for item in value)
                if isinstance(value, (list, tuple))
                else value
            )
            parts.append(f"{key.replace('_', ' ').capitalize()}: {rendered}")
        if self.hint:
            parts.append(f"Hint: {self.hint}")
        return ". ".join(parts)


def table_not_found(table_name: str, known_tables: Sequence[str]) -> ToolInputError:
    """Report a missing table, with the nearest names the database does have."""
    return ToolInputError(
        code="table_not_found",
        message=f"Table not found: {table_name}",
        hint="Call explore_schema() to list the tables in this database.",
        did_you_mean=get_close_matches(table_name, list(known_tables), n=3, cutoff=0.6),
    )


# The rule that fired decides what the caller should do about it.
#
# Order matters: the first marker found in the reason wins, so a specific marker
# must precede any more general one it is a special case of. "Got: UNKNOWN" sits
# above "Only SELECT" for exactly that reason.
_UNSAFE_HINTS = {
    "comments": "Remove the SQL comments and resend the query.",
    "one SQL statement": "Send a single statement, one call per statement.",
    # A read whose statement type sqlparse could not determine -- not a write.
    # The generic read-only hint below actively misleads here, because it answers
    # a question the caller did not ask: it sent a SELECT and would be sent off
    # to validate_migration. Name the shapes that actually cause this instead.
    "Got: UNKNOWN": (
        "The statement type could not be determined, so it was refused rather "
        "than guessed at. Send a plain SELECT: unwrap a parenthesized query "
        "such as (SELECT 1), and rewrite VALUES or TABLE shorthand as "
        "SELECT ... FROM."
    ),
    "Only SELECT": (
        "This server is read-only. Use explore_schema for structure, and "
        "validate_migration to check DDL without running it."
    ),
    "Blocked keyword": "Rewrite the query as a plain SELECT without that keyword.",
    "Locking clauses": (
        "Drop the locking clause. A row lock blocks other writers, so this "
        "read-only server refuses it; a plain SELECT returns the same rows."
    ),
}


def unsafe_query(reason: str) -> ToolInputError:
    """Report a blocked query, naming the fix for the rule that rejected it."""
    return ToolInputError(
        code="unsafe_query",
        message=f"Unsafe query blocked: {reason}",
        hint=next(
            (hint for marker, hint in _UNSAFE_HINTS.items() if marker in reason),
            "Send one read-only SELECT statement.",
        ),
    )


# A statement the database gave up on, per backend. SQLite's progress handler
# reports "interrupted"; Postgres cancels with "canceling statement"; MySQL
# reports "Query execution was interrupted".
_TIMEOUT_MARKERS = (
    "interrupted",
    "canceling statement",
    "statement timeout",
    "max_execution_time",
)

_MISSING_OBJECT_MARKERS = (
    "no such table",
    "no such column",
    "does not exist",
    "unknown column",
    "unknown table",
)


def from_database_error(
    error: Exception,
    timeout_seconds: int | None = None,
) -> ToolInputError:
    """Classify a driver error into something the caller can act on."""
    detail = str(getattr(error, "orig", None) or error).strip()
    lowered = detail.lower()

    if any(marker in lowered for marker in _TIMEOUT_MARKERS):
        bound = f" of {timeout_seconds}s" if timeout_seconds else ""
        return ToolInputError(
            code="query_timeout",
            message=f"The query exceeded the statement timeout{bound}: {detail}",
            hint=(
                "Add a WHERE clause, aggregate instead of scanning, or query a "
                "smaller table. The timeout is set by QUERY_TIMEOUT_SECONDS."
            ),
        )

    if any(marker in lowered for marker in _MISSING_OBJECT_MARKERS):
        hint = (
            "Call explore_schema(table_name=...) to confirm the table and "
            "column names before retrying."
        )
    else:
        hint = "Check the SQL against the schema returned by explore_schema."

    return ToolInputError(
        code="sql_error",
        message=f"The database rejected the query: {detail}",
        hint=hint,
    )
