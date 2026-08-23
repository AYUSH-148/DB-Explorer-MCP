from pathlib import Path

import pytest
from sqlalchemy import create_engine

from inspector import (
    MAX_TABLE_LIMIT,
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
        {"name": "orders", "column_count": 3},
        {"name": "users", "column_count": 3},
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


def test_get_all_tables_can_skip_row_counts(engine):
    tables = get_all_tables(engine, include_row_counts=False)

    assert [table["name"] for table in tables] == ["orders", "users"]
    assert all("row_count" not in table for table in tables)
