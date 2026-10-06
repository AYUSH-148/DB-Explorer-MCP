from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine.reflection import Inspector

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


# One table per way a foreign key can already be indexed, and per way it can
# look indexed without being so.
_COVERAGE_SCHEMA = (
    "CREATE TABLE users (id INTEGER PRIMARY KEY)",
    # The primary key is the foreign key: the one-to-one child table.
    "CREATE TABLE profile (user_id INTEGER PRIMARY KEY REFERENCES users(id))",
    # A UNIQUE constraint is backed by an index get_indexes() does not report.
    "CREATE TABLE badge (id INTEGER PRIMARY KEY, "
    "user_id INTEGER UNIQUE REFERENCES users(id))",
    # A composite primary key led by the foreign key covers it.
    "CREATE TABLE tagged (user_id INTEGER REFERENCES users(id), tag TEXT, "
    "PRIMARY KEY (user_id, tag))",
    # One where the foreign key is second does not.
    "CREATE TABLE member (team_id INTEGER, user_id INTEGER REFERENCES users(id), "
    "PRIMARY KEY (team_id, user_id))",
    # Nothing at all.
    "CREATE TABLE orders (id INTEGER PRIMARY KEY, "
    "user_id INTEGER REFERENCES users(id))",
)


@pytest.fixture
def coverage_engine(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'coverage.db'}")
    with engine.begin() as connection:
        for statement in _COVERAGE_SCHEMA:
            connection.execute(text(statement))
    return engine


@pytest.mark.parametrize(
    "table_name, flagged",
    [
        ("profile", False),
        ("badge", False),
        ("tagged", False),
        ("member", True),
        ("orders", True),
    ],
)
def test_implicit_indexes_count_as_covering_a_foreign_key(
    coverage_engine, table_name, flagged
):
    result = validate_schema(coverage_engine, table_name)
    codes = [issue["code"] for issue in result["issues"]]

    assert ("unindexed_foreign_key" in codes) is flagged


def test_a_unique_constraint_alone_is_an_index(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'unique.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE tokens (value TEXT UNIQUE)"))

    codes = {issue["code"] for issue in validate_schema(engine, "tokens")["issues"]}

    assert codes == {"missing_primary_key"}


def test_a_unique_constraint_spelled_in_another_case_is_found(tmp_path: Path):
    # SQLAlchemy's SQLite get_unique_constraints() matches the constraint text
    # against the index case-sensitively, so it reports no constraint here. The
    # automatic index behind it carries the declared name, UserId.
    engine = create_engine(f"sqlite:///{tmp_path / 'case.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE badge (id INTEGER PRIMARY KEY, "
                "UserId INTEGER REFERENCES users(id), UNIQUE (userid))"
            )
        )

    assert validate_schema(engine, "badge")["issues"] == []


def test_a_partial_index_does_not_cover_a_foreign_key(tmp_path: Path):
    # The lookup a foreign key needs has no WHERE deleted = 0, so the planner
    # cannot use this index for it.
    engine = create_engine(f"sqlite:///{tmp_path / 'partial.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE posts (id INTEGER PRIMARY KEY, "
                "user_id INTEGER REFERENCES users(id), deleted INTEGER)"
            )
        )
        connection.execute(
            text("CREATE INDEX idx_live_posts ON posts (user_id) WHERE deleted = 0")
        )

    codes = [issue["code"] for issue in validate_schema(engine, "posts")["issues"]]

    assert codes == ["unindexed_foreign_key"]


def test_a_table_with_only_an_unusable_index_still_has_an_index(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'unusable.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE events (kind TEXT, deleted INTEGER)"))
        connection.execute(
            text("CREATE INDEX idx_live ON events (kind) WHERE deleted = 0")
        )

    codes = {issue["code"] for issue in validate_schema(engine, "events")["issues"]}

    assert codes == {"missing_primary_key"}


def test_unique_constraint_reflection_is_never_needed(coverage_engine, monkeypatch):
    # SQL Server and other dialects raise NotImplementedError from it.
    def unsupported(*args, **kwargs):
        raise NotImplementedError

    monkeypatch.setattr(Inspector, "get_unique_constraints", unsupported)
    monkeypatch.setattr(Inspector, "get_multi_unique_constraints", unsupported)

    result = validate_schema(coverage_engine)

    assert {issue["table"] for issue in result["issues"]} == {"member", "orders"}


def test_a_whole_database_audit_reflects_each_kind_once(coverage_engine, monkeypatch):
    calls: list[str] = []
    for method in (
        "get_multi_columns",
        "get_multi_foreign_keys",
        "get_multi_indexes",
        "get_multi_pk_constraint",
    ):
        original = getattr(Inspector, method)

        def spy(self, *args, _original=original, _method=method, **kwargs):
            calls.append(_method)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(Inspector, method, spy)

    validate_schema(coverage_engine)

    # Once for all six tables, not once per table.
    assert sorted(calls) == sorted(
        [
            "get_multi_columns",
            "get_multi_foreign_keys",
            "get_multi_indexes",
            "get_multi_pk_constraint",
        ]
    )


def test_validator_rejects_unknown_table(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sample.db'}")

    with pytest.raises(ValueError, match="Table not found: missing"):
        validate_schema(engine, "missing")


def test_whole_database_audit_is_paged(tmp_path: Path):
    database_path = tmp_path / "sample.db"
    create_sample_database(database_path)
    engine = create_engine(f"sqlite:///{database_path}")

    first = validate_schema(engine, limit=1)
    second = validate_schema(engine, limit=1, offset=first["next_offset"])

    assert first["tables_checked"] == ["orders"]
    assert first["total_matching_tables"] == 2
    assert first["has_more"] is True
    assert second["tables_checked"] == ["users"]
    assert second["has_more"] is False
