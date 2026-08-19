from pathlib import Path

import pytest
from sqlalchemy import create_engine

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


def test_query_mode_reports_full_scan(engine):
    result = suggest_indexes(engine, query="SELECT name FROM users")

    assert result["recommendations"][0]["reason"] == "Execution plan contains a full scan: SCAN users"


def test_suggest_indexes_requires_one_input(engine):
    with pytest.raises(ValueError, match="Provide a query or table_name"):
        suggest_indexes(engine)

    with pytest.raises(ValueError, match="not both"):
        suggest_indexes(engine, query="SELECT 1", table_name="users")
