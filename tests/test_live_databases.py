"""The same guarantees as the SQLite suite, checked against real PostgreSQL and MySQL.

SQLite cannot exercise the code paths that differ per dialect: BEGIN READ ONLY,
SET SESSION TRANSACTION READ ONLY, statement_timeout, max_execution_time, NUMERIC
values, or catalog reflection. Each backend runs only when its URL is set:

    TEST_POSTGRES_URL=postgresql+psycopg2://user:pass@host:5432/db
    TEST_MYSQL_URL=mysql+pymysql://user:pass@host:3306/db

The tests create and drop their own `dbx_live_*` tables, so point them at a
throwaway database.
"""

import os
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from db import create_configured_engine, read_only_connection
from explain import explain_safe
from index_suggest import suggest_indexes
from inspector import get_table_detail
from safety import execute_safe
from schema_health import validate_schema

BACKENDS = {
    "postgresql": "TEST_POSTGRES_URL",
    "mysql": "TEST_MYSQL_URL",
}

SCHEMA = [
    "CREATE TABLE dbx_live_users ("
    " id INTEGER PRIMARY KEY,"
    " name VARCHAR(50) NOT NULL,"
    " balance NUMERIC(12, 2) NOT NULL,"
    " created_at TIMESTAMP NOT NULL)",
    # Table-level FOREIGN KEY: MySQL parses an inline REFERENCES and ignores it.
    "CREATE TABLE dbx_live_orders ("
    " id INTEGER PRIMARY KEY,"
    " user_id INTEGER NOT NULL,"
    " FOREIGN KEY (user_id) REFERENCES dbx_live_users (id))",
    "INSERT INTO dbx_live_users VALUES"
    " (1, 'Alice', 1234.50, '2026-01-02 03:04:05'),"
    " (2, 'Bob', 0.10, '2026-01-03 00:00:00')",
    "INSERT INTO dbx_live_orders VALUES (1, 1)",
]

# Enough rows, with statistics, that PostgreSQL's planner uses the primary key for
# an equality lookup instead of scanning, as it rightly does on a two-row table.
# MySQL reads a primary-key equality as access type const at any size.
EXTRA_SETUP = {
    "postgresql": [
        "INSERT INTO dbx_live_users"
        " SELECT g, 'User' || g, 0, TIMESTAMP '2026-01-01' FROM generate_series(3, 2000) g",
        "ANALYZE dbx_live_users",
    ],
}

# The server's own "statement cancelled" error, as opposed to a client-side read
# timeout, which raises the same exception class.
TIMEOUT_ERROR_CODE = {"postgresql": "57014", "mysql": 3024}

# Runs far past a one-second budget unless the server cancels it. MySQL's SLEEP()
# returns early without an error when interrupted, so it gets real work instead.
SLOW_QUERY = {
    "postgresql": "SELECT pg_sleep(30)",
    "mysql": (
        "SELECT COUNT(*) FROM information_schema.columns a"
        " CROSS JOIN information_schema.columns b"
        " CROSS JOIN information_schema.columns c"
    ),
}


DROP_TABLES = [
    "DROP TABLE IF EXISTS dbx_live_orders",
    "DROP TABLE IF EXISTS dbx_live_users",
]


def _run(url: str, statements: list[str]) -> None:
    # Disposed straight away: an engine left to the garbage collector holds its
    # pooled connection open, and small hosted plans cap connections.
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
    finally:
        engine.dispose()


@pytest.fixture(scope="module", params=list(BACKENDS))
def url(request):
    url = os.getenv(BACKENDS[request.param])
    if not url:
        pytest.skip(f"{BACKENDS[request.param]} is not set")

    _run(url, DROP_TABLES + SCHEMA + EXTRA_SETUP.get(request.param, []))
    yield url
    _run(url, DROP_TABLES)


@pytest.fixture
def engine(url):
    # Generous: a remote database spends most of a call on network round trips.
    engine = create_configured_engine(url, timeout_seconds=10)
    yield engine
    engine.dispose()


def test_writes_are_refused_by_the_database(engine):
    # Raw SQL straight to the connection: the parser never sees it, so only the
    # read-only transaction can stop it.
    with read_only_connection(engine) as connection:
        with pytest.raises(DBAPIError):
            connection.execute(text("DELETE FROM dbx_live_orders"))


def test_read_only_mode_does_not_leak_to_the_next_checkout(engine):
    # A statement inside, as every tool runs, so the mode is undone with a
    # transaction open.
    with read_only_connection(engine) as connection:
        connection.execute(text("SELECT 1"))

    # The pool hands back the same connection; it must be writable again.
    with engine.connect() as connection:
        connection.execute(text("UPDATE dbx_live_users SET name = 'Al' WHERE id = 1"))
        connection.rollback()


def test_a_slow_statement_is_cancelled_by_the_server(url):
    engine = create_configured_engine(url, timeout_seconds=1)
    started = time.monotonic()
    try:
        with read_only_connection(engine) as connection:
            with pytest.raises(DBAPIError) as caught:
                connection.execute(text(SLOW_QUERY[engine.dialect.name]))
    finally:
        engine.dispose()

    error = caught.value.orig
    code = getattr(error, "pgcode", None) or error.args[0]
    assert code == TIMEOUT_ERROR_CODE[engine.dialect.name]
    # The query alone would run 10s or more; the server stopped it at 1s.
    assert time.monotonic() - started < 8


def test_driver_values_are_serialized_and_the_row_cap_holds(engine):
    result = execute_safe(
        engine,
        "SELECT id, balance, created_at FROM dbx_live_users ORDER BY id",
        row_limit=1,
    )

    # NUMERIC arrives as Decimal and TIMESTAMP as datetime; neither is JSON.
    assert result["rows"] == [
        {"id": 1, "balance": 1234.5, "created_at": "2026-01-02T03:04:05"}
    ]
    assert result["truncated"] is True


def test_explain_returns_a_plan(engine):
    assert explain_safe(engine, "SELECT name FROM dbx_live_users")["plan"]


def test_table_detail_reflects_keys(engine):
    details = get_table_detail(engine, "dbx_live_orders")

    assert details["primary_key"] == ["id"]
    assert details["foreign_keys"] == [
        {
            "columns": ["user_id"],
            "referred_table": "dbx_live_users",
            "referred_columns": ["id"],
        }
    ]


def test_unindexed_foreign_key_is_reported_where_one_exists(engine):
    issues = validate_schema(engine, table_name="dbx_live_orders")["issues"]
    codes = {issue["code"] for issue in issues}

    # MySQL builds an index for every foreign key itself; PostgreSQL does not.
    expected = engine.dialect.name == "postgresql"
    assert ("unindexed_foreign_key" in codes) is expected


def test_query_mode_flags_a_full_table_scan(engine):
    # No index on name, so both databases read the whole table to filter on it.
    result = suggest_indexes(
        engine, query="SELECT id FROM dbx_live_users WHERE name = 'Alice'"
    )

    assert len(result["recommendations"]) == 1
    assert "dbx_live_users" in result["recommendations"][0]["reason"]


def test_query_mode_stays_quiet_on_a_primary_key_lookup(engine):
    result = suggest_indexes(
        engine, query="SELECT name FROM dbx_live_users WHERE id = 1"
    )

    assert result["recommendations"] == []
