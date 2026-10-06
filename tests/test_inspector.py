from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from inspector import (
    MAX_TABLE_LIMIT,
    _batched,
    get_all_tables,
    get_schema_page,
    get_table_detail,
)
from tests.seed_test_db import create_sample_database


@pytest.fixture
def engine(tmp_path: Path):
    database_path = tmp_path / "sample.db"
    create_sample_database(database_path)
    return create_engine(f"sqlite:///{database_path}")


def test_get_all_tables_returns_schema_summary(engine):
    tables = get_all_tables(engine)

    assert [table["name"] for table in tables] == ["orders", "users"]
    orders = next(table for table in tables if table["name"] == "orders")
    assert orders["row_count"] == 1
    assert orders["primary_key"] == ["id"]
    assert orders["foreign_keys"] == [
        {
            "columns": ["user_id"],
            "referred_table": "users",
            "referred_columns": ["id"],
        }
    ]


def test_get_table_detail_can_include_sample_rows(engine):
    details = get_table_detail(engine, "users", include_sample_data=True)

    assert details["columns"][0]["name"] == "id"
    assert details["sample_rows"] == [
        {"id": 1, "name": "Alice", "email": "alice@example.com"}
    ]


def test_get_table_detail_rejects_unknown_table(engine):
    with pytest.raises(ValueError, match="Table not found: missing"):
        get_table_detail(engine, "missing")


def test_get_table_detail_reports_row_count(engine):
    assert get_table_detail(engine, "users")["row_count"] == 1


def test_schema_page_summarises_without_counting_rows(engine):
    page = get_schema_page(engine)

    assert page["tables"] == [
        {"name": "orders", "kind": "table", "column_count": 3},
        {"name": "users", "kind": "table", "column_count": 3},
    ]
    assert page["total_matching_tables"] == 2
    assert page["has_more"] is False
    assert "next_offset" not in page
    # The listing exists to stay cheap; row counts are the expensive part.
    assert all("row_count" not in table for table in page["tables"])


def test_schema_page_matches_a_bare_pattern_as_a_substring(engine):
    page = get_schema_page(engine, name_pattern="SER")

    assert [table["name"] for table in page["tables"]] == ["users"]
    assert page["total_matching_tables"] == 1


def test_schema_page_matches_a_wildcard_pattern_as_a_glob(engine):
    matched = get_schema_page(engine, name_pattern="order*")
    assert [table["name"] for table in matched["tables"]] == ["orders"]

    # A glob is anchored, so a partial prefix does not match the way a bare
    # substring pattern would.
    assert get_schema_page(engine, name_pattern="rder*")["tables"] == []
    assert [
        table["name"] for table in get_schema_page(engine, name_pattern="rder")["tables"]
    ] == ["orders"]


def test_schema_page_paginates(engine):
    first = get_schema_page(engine, limit=1)

    assert [table["name"] for table in first["tables"]] == ["orders"]
    assert first["total_matching_tables"] == 2
    assert first["has_more"] is True
    assert first["next_offset"] == 1

    second = get_schema_page(engine, limit=1, offset=first["next_offset"])

    assert [table["name"] for table in second["tables"]] == ["users"]
    assert second["has_more"] is False


def test_schema_page_caps_an_oversized_limit(engine):
    assert get_schema_page(engine, limit=MAX_TABLE_LIMIT * 10)["limit"] == (
        MAX_TABLE_LIMIT
    )


def test_schema_page_rejects_impossible_bounds(engine):
    with pytest.raises(ValueError, match="limit must be at least 1"):
        get_schema_page(engine, limit=0)
    with pytest.raises(ValueError, match="offset must not be negative"):
        get_schema_page(engine, offset=-1)


def test_schema_page_offset_past_the_end_is_empty(engine):
    page = get_schema_page(engine, offset=99)

    assert page["tables"] == []
    assert page["total_matching_tables"] == 2
    assert page["has_more"] is False


def test_schema_page_detail_expands_the_page_without_row_counts(engine):
    page = get_schema_page(engine, name_pattern="orders", detail=True)

    orders = page["tables"][0]
    assert [column["name"] for column in orders["columns"]] == [
        "id",
        "user_id",
        "total",
    ]
    assert orders["primary_key"] == ["id"]
    assert "row_count" not in orders
    assert "detail_hint" not in page


def test_views_are_listed_and_described_like_tables(engine):
    """execute_query can select from a view, so the schema must show it too."""
    with engine.begin() as connection:
        connection.execute(text("CREATE VIEW user_view AS SELECT name FROM users"))

    page = get_schema_page(engine)
    assert {"name": "user_view", "kind": "view", "column_count": 1} in page["tables"]

    details = get_table_detail(engine, "user_view", include_sample_data=True)
    assert details["kind"] == "view"
    assert [column["name"] for column in details["columns"]] == ["name"]
    # A view's count would run the view's whole query.
    assert "row_count" not in details
    assert details["sample_rows"] == [{"name": "Alice"}]


def test_get_all_tables_can_skip_row_counts(engine):
    tables = get_all_tables(engine, include_row_counts=False)

    assert [table["name"] for table in tables] == ["orders", "users"]
    assert all("row_count" not in table for table in tables)


def _break_a_view(engine) -> None:
    """Leave a view whose base table no longer exists."""
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE base (a INTEGER)"))
        connection.execute(text("CREATE VIEW broken_view AS SELECT a FROM base"))
        connection.execute(text("DROP TABLE base"))


@pytest.mark.parametrize("detail", [False, True])
def test_one_broken_view_does_not_hide_the_other_tables(engine, detail):
    _break_a_view(engine)

    page = get_schema_page(engine, detail=detail)

    tables = {table["name"]: table for table in page["tables"]}
    assert tables["broken_view"]["kind"] == "view"
    assert "base table" in tables["broken_view"]["error"]
    assert tables["users"]["kind"] == "table"
    assert "error" not in tables["users"]
    assert "error" not in tables["orders"]


def test_a_broken_view_costs_log_n_fetches_not_n():
    names = [f"t{i}" for i in range(200)]
    calls = []

    def fetch(batch):
        calls.append(len(batch))
        if "t137" in batch:
            raise OperationalError("SELECT 1", {}, Exception("no such table: gone"))
        return {name: [] for name in batch}

    results = _batched(names, fetch)

    assert [name for name, value in results.items() if value is None] == ["t137"]
    assert len(results) == 200
    assert len(calls) < 30


def test_a_timeout_is_raised_not_blamed_on_every_relation():
    def fetch(batch):
        raise OperationalError("SELECT 1", {}, Exception("canceling statement"))

    with pytest.raises(OperationalError):
        _batched(["a", "b", "c"], fetch)


def test_a_broken_view_asked_for_by_name_reports_instead_of_raising(engine):
    _break_a_view(engine)

    details = get_table_detail(engine, "broken_view", include_sample_data=True)

    assert details["kind"] == "view"
    assert "error" in details
    assert "row_count" not in details
    assert "sample_rows" not in details


def test_row_counts_are_taken_for_tables_and_not_for_views(engine):
    with engine.begin() as connection:
        connection.execute(text("CREATE VIEW user_view AS SELECT name FROM users"))

    tables = {table["name"]: table for table in get_all_tables(engine)}

    assert tables["users"]["row_count"] == 1
    assert "row_count" not in tables["user_view"]
