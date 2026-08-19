from pathlib import Path

import pytest
from sqlalchemy import create_engine

from inspector import get_all_tables, get_table_detail
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
