import time
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

from db import (
    create_configured_engine,
    engine_timeout_seconds,
    read_only_connection,
    timeout_connect_args,
)
from tests.seed_test_db import create_sample_database


# A recursive CTE with no termination: the only way out is the deadline.
UNBOUNDED_QUERY = (
    "WITH RECURSIVE counter(x) AS ("
    "  SELECT 1 UNION ALL SELECT x + 1 FROM counter"
    ") SELECT COUNT(*) FROM counter"
)


@pytest.fixture
def engine(tmp_path: Path):
    database_path = tmp_path / "sample.db"
    create_sample_database(database_path)
    return create_configured_engine(f"sqlite:///{database_path}")


def test_reads_still_work(engine):
    with read_only_connection(engine) as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM users")).scalar_one() == 1


def test_writes_are_refused_by_the_database(engine):
    # Not by a keyword blocklist -- by SQLite itself, via PRAGMA query_only.
    with read_only_connection(engine) as connection:
        with pytest.raises(OperationalError, match="readonly database"):
            connection.execute(text("INSERT INTO users (name, email) VALUES ('x', 'y')"))


def test_ddl_is_refused_by_the_database(engine):
    with read_only_connection(engine) as connection:
        with pytest.raises(OperationalError, match="readonly database"):
            connection.execute(text("DROP TABLE users"))


def test_read_only_mode_does_not_leak_to_the_next_checkout(engine):
    with read_only_connection(engine):
        pass

    # The pooled connection must come back writable, or seeding and migrations
    # through the same engine would break.
    with engine.connect() as connection:
        connection.execute(text("CREATE TABLE scratch (a INTEGER)"))
        connection.rollback()


def test_an_unbounded_query_is_aborted_by_the_timeout(tmp_path: Path):
    database_path = tmp_path / "sample.db"
    create_sample_database(database_path)
    engine = create_configured_engine(f"sqlite:///{database_path}", timeout_seconds=1)

    started = time.monotonic()
    with read_only_connection(engine, timeout_seconds=1) as connection:
        with pytest.raises(DBAPIError):
            connection.execute(text(UNBOUNDED_QUERY))
    elapsed = time.monotonic() - started

    # Without the deadline this query never returns at all.
    assert elapsed < 10


def test_postgres_gets_a_server_side_statement_timeout():
    assert timeout_connect_args("postgresql", 7) == {
        "options": "-c statement_timeout=7000"
    }


def test_mysql_gets_driver_level_timeouts():
    assert timeout_connect_args("mysql", 7) == {
        "read_timeout": 7,
        "write_timeout": 7,
    }


def test_sqlite_takes_no_connect_args():
    # Its bound comes from the progress handler instead.
    assert timeout_connect_args("sqlite", 7) == {}


def test_configured_engine_checks_connection_liveness(engine):
    assert engine.pool._pre_ping is True


def test_the_engines_timeout_is_used_when_none_is_passed(tmp_path: Path):
    database_path = tmp_path / "sample.db"
    create_sample_database(database_path)
    engine = create_configured_engine(f"sqlite:///{database_path}", timeout_seconds=2)

    assert engine_timeout_seconds(engine) == 2

    # The deadline must follow the engine, not a module-level default -- otherwise a
    # tool built on a 2s engine would still wait for the global timeout.
    started = time.monotonic()
    with read_only_connection(engine) as connection:
        with pytest.raises(DBAPIError):
            connection.execute(text(UNBOUNDED_QUERY))
    assert time.monotonic() - started < 8
