from pathlib import Path

import pytest
from sqlalchemy import create_engine

from explain import explain_safe
from tests.seed_test_db import create_sample_database


@pytest.fixture
def engine(tmp_path: Path):
    database_path = tmp_path / "sample.db"
    create_sample_database(database_path)
    return create_engine(f"sqlite:///{database_path}")


def test_explain_safe_returns_sqlite_plan(engine):
    result = explain_safe(engine, "SELECT name FROM users")

    assert result["dialect"] == "sqlite"
    assert result["query"] == "SELECT name FROM users"
    assert result["plan"]
    assert "detail" in result["columns"]


def test_explain_safe_rejects_mutating_query(engine):
    with pytest.raises(ValueError, match="Unsafe query blocked"):
        explain_safe(engine, "DELETE FROM users")
