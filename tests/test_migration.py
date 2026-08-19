from pathlib import Path

import pytest
from sqlalchemy import create_engine

from migration import get_migration_context, validate_migration
from tests.seed_test_db import create_sample_database


def test_migration_context_returns_dialect_and_schema(tmp_path: Path):
    database_path = tmp_path / "sample.db"
    create_sample_database(database_path)
    engine = create_engine(f"sqlite:///{database_path}")

    result = get_migration_context(engine)

    assert result["dialect"] == "sqlite"
    assert {table["name"] for table in result["tables"]} == {"users", "orders"}


def test_validate_migration_never_executes_scripts(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sample.db'}")

    result = validate_migration(
        engine,
        "ALTER TABLE users ADD COLUMN active BOOLEAN DEFAULT FALSE;",
        "ALTER TABLE users DROP COLUMN active;",
    )

    assert result["valid"] is True
    assert result["execution_note"] == "Not executed. Review and run manually."


def test_validate_migration_rejects_select(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sample.db'}")

    with pytest.raises(ValueError, match="must not contain SELECT"):
        validate_migration(engine, "SELECT * FROM users", "DROP TABLE users")
