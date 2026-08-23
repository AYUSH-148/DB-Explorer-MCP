import pytest
from sqlalchemy.exc import OperationalError

from errors import (
    ToolInputError,
    from_database_error,
    table_not_found,
    unsafe_query,
)


def test_str_is_the_bare_message():
    # The whole error layer rests on this: callers and tests that matched the
    # plain message keep working, so adding codes and hints breaks nothing.
    error = ToolInputError(code="c", message="Table not found: missing", hint="h")

    assert str(error) == "Table not found: missing"
    assert isinstance(error, ValueError)


def test_as_text_carries_the_code_details_and_hint():
    error = ToolInputError(
        code="invalid_argument",
        message="offset must not be negative",
        hint="Start at offset 0.",
        received=-1,
    )

    assert error.as_text() == (
        "[invalid_argument] offset must not be negative. "
        "Received: -1. Hint: Start at offset 0."
    )


def test_as_dict_is_machine_readable():
    error = ToolInputError(code="table_not_found", message="m", hint="h", extra=["a"])

    assert error.as_dict() == {
        "error": {
            "code": "table_not_found",
            "message": "m",
            "hint": "h",
            "extra": ["a"],
        }
    }


def test_empty_details_are_dropped():
    error = ToolInputError(code="c", message="m", did_you_mean=[])

    assert error.details == {}
    assert "Did you mean" not in error.as_text()


def test_table_not_found_suggests_a_near_name():
    error = table_not_found("usrs", ["users", "orders"])

    assert error.code == "table_not_found"
    assert error.details["did_you_mean"] == ["users"]
    assert "Did you mean: users" in error.as_text()


def test_table_not_found_suggests_nothing_when_no_name_is_close():
    error = table_not_found("zzzzz", ["users", "orders"])

    assert "did_you_mean" not in error.details


@pytest.mark.parametrize(
    "reason, expected",
    [
        ("SQL comments are not allowed", "Remove the SQL comments"),
        ("Only SELECT queries are allowed. Got: DELETE", "read-only"),
        ("Blocked keyword detected: INTO", "plain SELECT"),
        ("Exactly one SQL statement is required", "one call per statement"),
    ],
)
def test_unsafe_query_hint_names_the_fix_for_the_rule_that_fired(reason, expected):
    error = unsafe_query(reason)

    assert error.code == "unsafe_query"
    assert expected in error.hint


def _operational_error(driver_message: str) -> OperationalError:
    return OperationalError(
        "SELECT * FROM (SELECT 1) AS limited_query LIMIT 100",
        {},
        Exception(driver_message),
    )


@pytest.mark.parametrize(
    "driver_message",
    ["interrupted", "canceling statement due to statement timeout"],
)
def test_a_given_up_statement_is_reported_as_a_timeout(driver_message):
    error = from_database_error(_operational_error(driver_message), timeout_seconds=15)

    assert error.code == "query_timeout"
    assert "15s" in error.message
    assert "WHERE" in error.hint


def test_a_missing_column_points_at_explore_schema():
    error = from_database_error(_operational_error("no such column: nope"))

    assert error.code == "sql_error"
    assert "explore_schema(table_name=...)" in error.hint


def test_the_row_limit_wrapper_is_not_echoed_back_to_the_caller():
    # str() on a SQLAlchemy error appends the statement it sent. Reporting that
    # would show the caller a query it never wrote.
    error = from_database_error(_operational_error("no such column: nope"))

    assert error.message == "The database rejected the query: no such column: nope"
    assert "limited_query" not in error.as_text()
