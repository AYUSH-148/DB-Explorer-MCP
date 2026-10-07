import logging
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from sqlalchemy import create_engine

import server
from errors import ToolInputError
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

    assert result["tables"][0] == {"name": "orders", "kind": "table", "column_count": 3}
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
        "truncated": False,
        "row_limit": 100,
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


def test_a_caller_fixable_error_reaches_the_client_with_a_hint(
    configured_engine, monkeypatch
):
    # ToolError rather than the original ValueError, because only ToolError
    # survives a server configured with mask_error_details=True.
    monkeypatch.setattr(server, "engine", configured_engine)

    with pytest.raises(ToolError) as raised:
        server.explore_schema(table_name="usrs")

    message = str(raised.value)
    assert "[table_not_found]" in message
    assert "Did you mean: users" in message
    assert "explore_schema()" in message


def test_a_database_error_is_classified_at_the_boundary(
    configured_engine, monkeypatch
):
    monkeypatch.setattr(server, "engine", configured_engine)

    with pytest.raises(ToolError) as raised:
        server.execute_query("SELECT nosuchcol FROM users")

    message = str(raised.value)
    assert "[sql_error]" in message
    assert "no such column: nosuchcol" in message
    # The caller wrote the inner query, not the row-limit wrapper around it.
    assert "limited_query" not in message


def test_a_blocked_query_explains_what_to_send_instead(
    configured_engine, monkeypatch
):
    monkeypatch.setattr(server, "engine", configured_engine)

    with pytest.raises(ToolError) as raised:
        server.execute_query("DELETE FROM users")

    assert "[unsafe_query]" in str(raised.value)
    assert "read-only" in str(raised.value)


def test_the_undecorated_helpers_still_raise_for_python_callers(
    configured_engine, monkeypatch
):
    # The *_data functions are the testable seam; they stay exception-based so
    # internal composition keeps working.
    monkeypatch.setattr(server, "engine", configured_engine)

    with pytest.raises(ToolInputError, match="Table not found: usrs"):
        server.explore_schema_data(table_name="usrs")


def test_an_unexpected_error_is_logged_but_not_shown_to_the_caller(
    monkeypatch, caplog
):
    def explode(*_args, **_kwargs):
        raise TypeError("secret driver detail at /srv/db.sock")

    monkeypatch.setattr(server, "explain_safe", explode)

    with pytest.raises(ToolError) as raised:
        server.explain_query("SELECT 1")

    message = str(raised.value)
    assert "[internal_error]" in message
    assert "secret" not in message
    assert "secret driver detail" in caplog.text


def test_every_tool_call_writes_one_audit_line(
    configured_engine, monkeypatch, caplog
):
    # Enabled at import, not by run_server(): a hosted entrypoint loads
    # server.py:mcp and never calls run_server().
    assert server.audit_log.isEnabledFor(logging.INFO)
    assert server.audit_log.handlers
    monkeypatch.setattr(server, "engine", configured_engine)

    with caplog.at_level(logging.INFO, logger="db_explorer.audit"):
        server.execute_query("SELECT name FROM users")
        for sql in ("DELETE FROM users", "SELECT nosuchcol FROM users"):
            with pytest.raises(ToolError):
                server.execute_query(sql)
        server.suggest_index(query="SELECT * FROM orders WHERE user_id = 1")
        server.explain_query("SELECT '" + "x" * 5000 + "'")

    lines = [record.getMessage() for record in caplog.records]
    assert lines[:4] == [
        "execute_query ok rows=1 truncated=False {'sql': 'SELECT name FROM users'}",
        "execute_query unsafe_query {'sql': 'DELETE FROM users'}",
        "execute_query sql_error {'sql': 'SELECT nosuchcol FROM users'}",
        "suggest_index ok {'query': 'SELECT * FROM orders WHERE user_id = 1'}",
    ]
    assert lines[4].startswith("explain_query ok {'sql': \"SELECT 'xxx")
    assert len(lines[4]) < server.AUDIT_ARGUMENT_LIMIT + 100
    assert len(lines) == 5
