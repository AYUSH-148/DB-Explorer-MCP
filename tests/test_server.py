from pathlib import Path

import pytest
from sqlalchemy import create_engine

import server
from tests.seed_test_db import create_sample_database


@pytest.fixture
def configured_engine(tmp_path: Path):
    database_path = tmp_path / "sample.db"
    create_sample_database(database_path)
    return create_engine(f"sqlite:///{database_path}")


def test_explore_schema_tool_returns_all_tables(configured_engine, monkeypatch):
    monkeypatch.setattr(server, "engine", configured_engine)

    result = server.explore_schema_data()

    assert [table["name"] for table in result["tables"]] == ["orders", "users"]


def test_explore_schema_tool_returns_table_detail(configured_engine, monkeypatch):
    monkeypatch.setattr(server, "engine", configured_engine)

    result = server.explore_schema_data("users", include_sample_data=True)

    assert result["name"] == "users"
    assert result["sample_rows"][0]["email"] == "alice@example.com"
