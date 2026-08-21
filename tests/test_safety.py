from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from safety import execute_safe, validate_query
from tests.seed_test_db import create_sample_database


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM users",
        "SELECT name FROM users WHERE id = 1;",
        "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id",
    ],
)
def test_select_queries_are_safe(query):
    assert validate_query(query) == (True, "Query is safe")


@pytest.mark.parametrize(
    "query, keyword",
    [
        ("INSERT INTO users (name, email) VALUES ('X', 'x@example.com')", "INSERT"),
        ("UPDATE users SET name = 'X'", "UPDATE"),
        ("DELETE FROM users", "DELETE"),
        ("DROP TABLE users", "DROP"),
        ("SELECT name INTO archived_users FROM users", "INTO"),
    ],
)
def test_mutating_queries_are_blocked(query, keyword):
    is_safe, reason = validate_query(query)

    assert not is_safe
    assert keyword in reason


def test_multiple_statements_are_blocked():
    is_safe, reason = validate_query("SELECT * FROM users; DELETE FROM users")

    assert not is_safe
    assert reason == "Exactly one SQL statement is required"


def test_comments_are_blocked():
    is_safe, reason = validate_query("SELECT * FROM users -- expose data")

    assert not is_safe
    assert reason == "SQL comments are not allowed"


def test_execute_safe_applies_row_limit(tmp_path: Path):
    database_path = tmp_path / "sample.db"
    create_sample_database(database_path)
    engine = create_engine(f"sqlite:///{database_path}")

    result = execute_safe(engine, "SELECT * FROM users", row_limit=1)

    assert result["columns"] == ["id", "name", "email"]
    assert result["count"] == 1
    assert result["rows"][0]["name"] == "Alice"


def test_execute_safe_rejects_unsafe_query(tmp_path: Path):
    database_path = tmp_path / "sample.db"
    create_sample_database(database_path)
    engine = create_engine(f"sqlite:///{database_path}")

    with pytest.raises(ValueError, match="Unsafe query blocked"):
        execute_safe(engine, "DROP TABLE users")


def test_string_literals_containing_dashes_are_allowed():
    assert validate_query("SELECT '--sale' AS label FROM users") == (
        True,
        "Query is safe",
    )


def test_block_comments_are_blocked():
    is_safe, reason = validate_query("SELECT /* hidden */ * FROM users")

    assert not is_safe
    assert reason == "SQL comments are not allowed"


def test_union_select_is_allowed():
    """UNION stays permitted: it is read-only, and this server exposes every
    table through explore_schema anyway, so it reaches nothing a plain SELECT
    could not. See issue #1."""
    assert validate_query("SELECT name FROM users UNION SELECT email FROM users") == (
        True,
        "Query is safe",
    )


def test_subquery_limit_does_not_bypass_row_limit(tmp_path: Path):
    database_path = tmp_path / "sample.db"
    create_sample_database(database_path)
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        for index in range(2, 7):
            connection.execute(
                text("INSERT INTO users (name, email) VALUES (:name, :email)"),
                {"name": f"User{index}", "email": f"user{index}@example.com"},
            )

    # The LIMIT belongs to the subquery, so the outer row cap must still apply.
    result = execute_safe(
        engine,
        "SELECT * FROM users WHERE id IN (SELECT id FROM users LIMIT 5)",
        row_limit=2,
    )

    assert result["count"] == 2
