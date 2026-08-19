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


def test_validator_rejects_unknown_table(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sample.db'}")

    with pytest.raises(ValueError, match="Table not found: missing"):
        validate_schema(engine, "missing")
