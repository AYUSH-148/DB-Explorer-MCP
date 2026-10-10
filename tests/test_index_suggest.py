from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import index_suggest
from index_suggest import suggest_indexes
from tests.seed_test_db import create_sample_database


@pytest.fixture
def engine(tmp_path: Path):
    database_path = tmp_path / "sample.db"
    create_sample_database(database_path)
    return create_engine(f"sqlite:///{database_path}")


def test_table_mode_returns_no_suggestion_for_indexed_foreign_key(engine):
    result = suggest_indexes(engine, table_name="orders")

    assert result["recommendations"] == []


@pytest.fixture
def coverage_engine(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'coverage.db'}")
    with engine.begin() as connection:
        for statement in (
            "CREATE TABLE users (id INTEGER PRIMARY KEY)",
            "CREATE TABLE profile (user_id INTEGER PRIMARY KEY REFERENCES users(id))",
            "CREATE TABLE badge (id INTEGER PRIMARY KEY, "
            "user_id INTEGER UNIQUE REFERENCES users(id))",
            "CREATE TABLE member (team_id INTEGER, "
            "user_id INTEGER REFERENCES users(id), PRIMARY KEY (team_id, user_id))",
        ):
            connection.execute(text(statement))
    return engine


@pytest.mark.parametrize("table_name", ["profile", "badge"])
def test_table_mode_does_not_recommend_an_index_that_already_exists(
    coverage_engine, table_name
):
    # Each foreign key here is indexed by its PRIMARY KEY or UNIQUE constraint,
    # so a CREATE INDEX would only duplicate it.
    result = suggest_indexes(coverage_engine, table_name=table_name)

    assert result["recommendations"] == []


def test_table_mode_still_recommends_when_the_key_is_not_leading(coverage_engine):
    result = suggest_indexes(coverage_engine, table_name="member")

    assert [rec["columns"] for rec in result["recommendations"]] == [["user_id"]]


def test_suggested_sql_quotes_reserved_and_mixed_case_names(tmp_path: Path):
    # Unquoted, both CREATE INDEX statements below are syntax errors on
    # Postgres: `order` is reserved, and UserId would fold to userid.
    engine = create_engine(f"sqlite:///{tmp_path / 'names.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        connection.execute(
            text(
                'CREATE TABLE "order" (id INTEGER PRIMARY KEY, '
                "user_id INTEGER REFERENCES users(id))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE Ledger (id INTEGER PRIMARY KEY, "
                "UserId INTEGER REFERENCES users(id))"
            )
        )

    reserved = suggest_indexes(engine, table_name="order")["recommendations"][0]
    mixed = suggest_indexes(engine, table_name="Ledger")["recommendations"][0]

    assert reserved["sql"] == 'CREATE INDEX idx_order_user_id ON "order" (user_id);'
    assert mixed["sql"] == (
        'CREATE INDEX "idx_Ledger_UserId" ON "Ledger" ("UserId");'
    )
    # The statement it suggests is one the database accepts.
    with engine.begin() as connection:
        connection.execute(text(reserved["sql"]))
        connection.execute(text(mixed["sql"]))
    assert suggest_indexes(engine, table_name="order")["recommendations"] == []
    assert suggest_indexes(engine, table_name="Ledger")["recommendations"] == []


def _dialect(url: str):
    return make_url(url).get_dialect()()


@pytest.mark.parametrize("url", ["mysql+pymysql://", "mariadb+pymysql://"])
def test_mysql_suggestions_quote_with_backticks_even_under_ansi_quotes(url):
    # The state SQLAlchemy leaves a MySQL dialect in after connecting to a server
    # with ANSI_QUOTES on: its own preparer quotes with double quotes, which a
    # session without ANSI_QUOTES reads as a string.
    dialect = _dialect(url)
    dialect.identifier_preparer = dialect.preparer(dialect, server_ansiquotes=True)
    assert dialect.identifier_preparer.quote("order") == '"order"'

    preparer = index_suggest._paste_safe_preparer(dialect)

    assert preparer.quote("order") == "`order`"
    assert preparer.quote("UserId") == "`UserId`"
    assert preparer.quote("user_id") == "user_id"


def test_other_dialects_keep_their_own_quoting():
    dialect = _dialect("postgresql+psycopg2://")

    preparer = index_suggest._paste_safe_preparer(dialect)

    assert preparer is dialect.identifier_preparer
    assert preparer.quote("order") == '"order"'


def test_table_mode_skips_index_reflection_without_foreign_keys(engine, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("index coverage reflected for a table with no FKs")

    monkeypatch.setattr(index_suggest, "reflect_coverage", unexpected)

    assert suggest_indexes(engine, table_name="users")["recommendations"] == []


def test_query_mode_reports_full_scan(engine):
    result = suggest_indexes(engine, query="SELECT name FROM users")

    assert result["recommendations"][0]["reason"] == "Execution plan contains a full scan: SCAN users"


def test_suggest_indexes_requires_one_input(engine):
    with pytest.raises(ValueError, match="Provide a query or table_name"):
        suggest_indexes(engine)

    with pytest.raises(ValueError, match="not both"):
        suggest_indexes(engine, query="SELECT 1", table_name="users")


@pytest.mark.parametrize(
    "dialect, plan_row, flagged",
    [
        ("postgresql", {"QUERY PLAN": "Seq Scan on users  (cost=0.00..1.02)"}, True),
        ("postgresql", {"QUERY PLAN": "  ->  Parallel Seq Scan on users"}, True),
        ("postgresql", {"QUERY PLAN": "Index Scan using users_pkey on users"}, False),
        # A string literal in the query, not a plan node.
        ("postgresql", {"QUERY PLAN": "  Filter: (name <> 'Seq Scan on x'::text)"}, False),
        ("mysql", {"table": "users", "type": "ALL"}, True),
        ("mysql", {"table": "users", "type": "const"}, False),
        # The server's temporary tables: no index can serve them.
        ("mysql", {"table": "<derived2>", "type": "ALL"}, False),
        ("mysql", {"table": "<union1,2>", "type": "ALL"}, False),
        ("mariadb", {"table": "users", "type": "ALL"}, True),
    ],
)
def test_full_scan_reads_each_dialects_plan(dialect, plan_row, flagged):
    assert (index_suggest._full_scan(dialect, plan_row) is not None) is flagged
