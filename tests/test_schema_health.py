from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from schema_health import validate_schema
from tests.seed_test_db import create_sample_database


def test_sample_schema_has_no_health_issues(tmp_path: Path):
    database_path = tmp_path / "sample.db"
    create_sample_database(database_path)
    engine = create_engine(f"sqlite:///{database_path}")

    result = validate_schema(engine)

    assert result["issue_count"] == 0


def test_validator_finds_missing_pk_and_unindexed_fk(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'unhealthy.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE parent (id INTEGER PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE child "
                "(parent_id INTEGER REFERENCES parent(id), value TEXT)"
            )
        )

    result = validate_schema(engine, "child")
    codes = {issue["code"] for issue in result["issues"]}

    assert result["issue_count"] == 3
    assert codes == {"missing_primary_key", "unindexed_foreign_key", "no_indexes"}


# One table per way a foreign key can already be indexed, and per way it can
# look indexed without being so.
_COVERAGE_SCHEMA = (
    "CREATE TABLE users (id INTEGER PRIMARY KEY)",
    # The primary key is the foreign key: the one-to-one child table.
    "CREATE TABLE profile (user_id INTEGER PRIMARY KEY REFERENCES users(id))",
    # A UNIQUE constraint is backed by an index get_indexes() does not report.
    "CREATE TABLE badge (id INTEGER PRIMARY KEY, "
    "user_id INTEGER UNIQUE REFERENCES users(id))",
    # A composite primary key led by the foreign key covers it.
    "CREATE TABLE tagged (user_id INTEGER REFERENCES users(id), tag TEXT, "
    "PRIMARY KEY (user_id, tag))",
    # One where the foreign key is second does not.
    "CREATE TABLE member (team_id INTEGER, user_id INTEGER REFERENCES users(id), "
    "PRIMARY KEY (team_id, user_id))",
    # Nothing at all.
    "CREATE TABLE orders (id INTEGER PRIMARY KEY, "
    "user_id INTEGER REFERENCES users(id))",
)


@pytest.fixture
def coverage_engine(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'coverage.db'}")
    with engine.begin() as connection:
        for statement in _COVERAGE_SCHEMA:
            connection.execute(text(statement))
    return engine


@pytest.mark.parametrize(
    "table_name, flagged",
    [
        ("profile", False),
        ("badge", False),
        ("tagged", False),
        ("member", True),
        ("orders", True),
    ],
)
def test_implicit_indexes_count_as_covering_a_foreign_key(
    coverage_engine, table_name, flagged
):
    result = validate_schema(coverage_engine, table_name)
    codes = [issue["code"] for issue in result["issues"]]

    assert ("unindexed_foreign_key" in codes) is flagged


def test_a_unique_constraint_alone_is_an_index(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'unique.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE tokens (value TEXT UNIQUE)"))

    codes = {issue["code"] for issue in validate_schema(engine, "tokens")["issues"]}

    assert codes == {"missing_primary_key"}


def test_validator_rejects_unknown_table(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sample.db'}")

    with pytest.raises(ValueError, match="Table not found: missing"):
        validate_schema(engine, "missing")
