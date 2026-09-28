from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from index_suggest import suggest_indexes
from tests.seed_test_db import create_sample_database


@pytest.fixture
def engine(tmp_path: Path):
    database_path = tmp_path / "sample.db"
    create_sample_database(database_path)
    return create_engine(f"sqlite:///{database_path}")


def test_table_mode_returns_no_suggestion_for_indexed_foreign_key(engine):
    result = suggest_indexes(engine, table_name="orders")

    assert result["recommendations"] == []


@pytest.fixture
def coverage_engine(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'coverage.db'}")
    with engine.begin() as connection:
        for statement in (
            "CREATE TABLE users (id INTEGER PRIMARY KEY)",
            "CREATE TABLE profile (user_id INTEGER PRIMARY KEY REFERENCES users(id))",
            "CREATE TABLE badge (id INTEGER PRIMARY KEY, "
            "user_id INTEGER UNIQUE REFERENCES users(id))",
            "CREATE TABLE member (team_id INTEGER, "
            "user_id INTEGER REFERENCES users(id), PRIMARY KEY (team_id, user_id))",
        ):
            connection.execute(text(statement))
    return engine


@pytest.mark.parametrize("table_name", ["profile", "badge"])
def test_table_mode_does_not_recommend_an_index_that_already_exists(
    coverage_engine, table_name
):
    # Each foreign key here is indexed by its PRIMARY KEY or UNIQUE constraint,
    # so a CREATE INDEX would only duplicate it.
    result = suggest_indexes(coverage_engine, table_name=table_name)

    assert result["recommendations"] == []


def test_table_mode_still_recommends_when_the_key_is_not_leading(coverage_engine):
    result = suggest_indexes(coverage_engine, table_name="member")

    assert [rec["columns"] for rec in result["recommendations"]] == [["user_id"]]


def test_query_mode_reports_full_scan(engine):
    result = suggest_indexes(engine, query="SELECT name FROM users")

    assert result["recommendations"][0]["reason"] == "Execution plan contains a full scan: SCAN users"


def test_suggest_indexes_requires_one_input(engine):
    with pytest.raises(ValueError, match="Provide a query or table_name"):
        suggest_indexes(engine)

    with pytest.raises(ValueError, match="not both"):
        suggest_indexes(engine, query="SELECT 1", table_name="users")
