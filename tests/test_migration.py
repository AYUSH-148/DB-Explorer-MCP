from pathlib import Path
from types import SimpleNamespace

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
    database_path = tmp_path / "sample.db"
    create_sample_database(database_path)
    engine = create_engine(f"sqlite:///{database_path}")
    before = get_migration_context(engine)["tables"]

    result = validate_migration(
        engine,
        "ALTER TABLE users ADD COLUMN active BOOLEAN DEFAULT FALSE; DROP TABLE orders;",
        "ALTER TABLE users DROP COLUMN active;",
    )

    assert "valid" not in result
    assert result["up"]["statement_types"] == ["ALTER", "DROP"]
    assert result["down"]["statement_types"] == ["ALTER"]
    assert result["execution_note"] == "Not executed. Review and run manually."
    assert get_migration_context(engine)["tables"] == before


def test_validate_migration_rejects_select(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sample.db'}")

    with pytest.raises(ValueError, match="must not contain SELECT"):
        validate_migration(engine, "SELECT * FROM users", "DROP TABLE users")


@pytest.mark.parametrize(
    "script",
    [
        "ALTER TABLE users ADD COLUMN note TEXT DEFAULT 'a--b'",
        "ALTER TABLE users ADD COLUMN note TEXT DEFAULT '/* x */'",
        "ALTER TABLE users ADD COLUMN note TEXT DEFAULT '--'",
        'ALTER TABLE users ADD COLUMN "a--b" TEXT',
    ],
)
def test_validate_migration_allows_comment_markers_inside_literals(tmp_path: Path, script: str):
    engine = create_engine(f"sqlite:///{tmp_path / 'sample.db'}")

    assert validate_migration(engine, script, "DROP TABLE t")["up"]["statement_types"]


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


@pytest.mark.parametrize("up_sql, down_sql", [(None, "DROP TABLE t"), ("DROP TABLE t", None)])
def test_validate_migration_reports_missing_script(tmp_path: Path, up_sql, down_sql):
    engine = create_engine(f"sqlite:///{tmp_path / 'sample.db'}")

    with pytest.raises(ValueError, match="SQL is required"):
        validate_migration(engine, up_sql, down_sql)


@pytest.mark.parametrize("script", ["hello world", ";", " ; ; "])
def test_validate_migration_rejects_non_sql(tmp_path: Path, script: str):
    engine = create_engine(f"sqlite:///{tmp_path / 'sample.db'}")

    with pytest.raises(ValueError, match="could not be parsed|does not start with a SQL keyword"):
        validate_migration(engine, script, "DROP TABLE t")


def test_validate_migration_rejects_parenthesised_select(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sample.db'}")

    with pytest.raises(ValueError, match="does not start with a SQL keyword"):
        validate_migration(engine, "(SELECT * FROM users)", "DROP TABLE t")


@pytest.mark.parametrize(
    "script",
    [
        "DROP TABLE t;;",
        "COMMENT ON COLUMN t.a IS 'x'",
        "RENAME TABLE a TO b",
        "UPDATE t SET a = 1",
        "CREATE TABLE #temp (a INT)",
    ],
)
def test_validate_migration_accepts_real_statements(tmp_path: Path, script: str):
    engine = create_engine(f"sqlite:///{tmp_path / 'sample.db'}")

    assert validate_migration(engine, script, "DROP TABLE t")["up"]["statement_types"]


def test_validate_migration_rejects_mysql_hash_comment():
    # Only the dialect name is read, so no MySQL driver or server is needed.
    engine = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))

    with pytest.raises(ValueError, match="comments are not allowed"):
        validate_migration(engine, "DROP TABLE t #hi", "DROP TABLE t")


def test_migration_context_is_paged(tmp_path: Path):
    database_path = tmp_path / "sample.db"
    create_sample_database(database_path)
    engine = create_engine(f"sqlite:///{database_path}")

    result = get_migration_context(engine, limit=1)

    assert [table["name"] for table in result["tables"]] == ["orders"]
    assert result["has_more"] is True
    assert result["next_offset"] == 1
    assert get_migration_context(engine, name_pattern="user")["tables"][0]["name"] == "users"
