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
    assert [table["name"] for table in result["tables"]] == ["orders", "users"]
    assert all("row_count" not in table for table in result["tables"])
    orders = result["tables"][0]
    assert orders["primary_key"] == ["id"]
    assert orders["foreign_keys"] == [
        {
            "columns": ["user_id"],
            "referred_table": "users",
            "referred_columns": ["id"],
        }
    ]


def test_migration_context_pages_like_explore_schema(tmp_path: Path):
    database_path = tmp_path / "sample.db"
    create_sample_database(database_path)
    engine = create_engine(f"sqlite:///{database_path}")

    first = get_migration_context(engine, limit=1)
    assert [table["name"] for table in first["tables"]] == ["orders"]
    assert first["has_more"] is True
    second = get_migration_context(engine, limit=1, offset=first["next_offset"])
    assert [table["name"] for table in second["tables"]] == ["users"]
    assert second["has_more"] is False
    matched = get_migration_context(engine, name_pattern="user")
    assert [table["name"] for table in matched["tables"]] == ["users"]


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


@pytest.mark.parametrize(
    "script",
    [
        "ALTER TABLE users ADD COLUMN note TEXT DEFAULT 'a--b'",
        "ALTER TABLE users ADD COLUMN note TEXT DEFAULT '/* x */'",
        'ALTER TABLE users ADD COLUMN "a--b" TEXT',
    ],
)
def test_validate_migration_allows_comment_markers_inside_literals(tmp_path: Path, script: str):
    engine = create_engine(f"sqlite:///{tmp_path / 'sample.db'}")

    assert validate_migration(engine, script, "DROP TABLE t")["valid"] is True


@pytest.mark.parametrize(
    "script",
    [
        "DROP TABLE t -- x",
        "DROP TABLE t /* x */",
        "DROP TABLE t /* open",
        "DROP TABLE t */",
        "ALTER TABLE t ADD COLUMN a TEXT DEFAULT 'x' -- 'y'",
    ],
)
def test_validate_migration_rejects_real_comments(tmp_path: Path, script: str):
    engine = create_engine(f"sqlite:///{tmp_path / 'sample.db'}")

    with pytest.raises(ValueError, match="comments are not allowed"):
        validate_migration(engine, script, "DROP TABLE t")
