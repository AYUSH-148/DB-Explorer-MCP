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


def test_explore_schema_tool_defaults_to_a_summary(configured_engine, monkeypatch):
    monkeypatch.setattr(server, "engine", configured_engine)

    result = server.explore_schema_data()

    assert result["tables"][0] == {"name": "orders", "column_count": 3}
    assert result["limit"] == server.DEFAULT_TABLE_LIMIT
    assert result["detail_hint"]


def test_explore_schema_tool_filters_and_pages(configured_engine, monkeypatch):
    monkeypatch.setattr(server, "engine", configured_engine)

    result = server.explore_schema_data(name_pattern="user", limit=1)

    assert [table["name"] for table in result["tables"]] == ["users"]
    assert result["has_more"] is False


def test_explore_schema_tool_can_expand_the_page(configured_engine, monkeypatch):
    monkeypatch.setattr(server, "engine", configured_engine)

    result = server.explore_schema_data(detail=True)

    assert [column["name"] for column in result["tables"][0]["columns"]] == [
        "id",
        "user_id",
        "total",
    ]


def test_explore_schema_tool_returns_table_detail(configured_engine, monkeypatch):
    monkeypatch.setattr(server, "engine", configured_engine)

    result = server.explore_schema_data("users", include_sample_data=True)

    assert result["name"] == "users"
    assert result["sample_rows"][0]["email"] == "alice@example.com"


def test_execute_query_tool_returns_rows(configured_engine, monkeypatch):
    monkeypatch.setattr(server, "engine", configured_engine)

    result = server.execute_query_data("SELECT name FROM users")

    assert result == {
        "columns": ["name"],
        "rows": [{"name": "Alice"}],
        "count": 1,
    }


def test_explain_query_tool_returns_plan(configured_engine, monkeypatch):
    monkeypatch.setattr(server, "engine", configured_engine)

    result = server.explain_query("SELECT name FROM users")

    assert result["dialect"] == "sqlite"
    assert result["plan"]


def test_run_server_uses_stdio_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "MCP_TRANSPORT", "stdio")
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: calls.append(kwargs))

    server.run_server()

    assert calls == [{"transport": "stdio"}]


def test_run_server_rejects_unknown_transport(monkeypatch):
    monkeypatch.setattr(server, "MCP_TRANSPORT", "invalid")

    with pytest.raises(ValueError, match="MCP_TRANSPORT"):
        server.run_server()
