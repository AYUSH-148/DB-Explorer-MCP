"""Engine construction and read-only connection handling.

Two guarantees live here that a SQL keyword blocklist cannot provide: a statement
timeout, so one query cannot pin the server, and a transaction the database itself
refuses to write through.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.engine import make_url

from config import QUERY_TIMEOUT_SECONDS

_deadline = threading.local()

_SQLITE_PROGRESS_INSTRUCTIONS = 1_000

# Where create_configured_engine records the timeout it built the engine with.
TIMEOUT_EXECUTION_OPTION = "db_explorer_timeout_seconds"


def engine_timeout_seconds(engine: Engine) -> int:
    """Return the timeout an engine was configured with."""
    return engine.get_execution_options().get(
        TIMEOUT_EXECUTION_OPTION, QUERY_TIMEOUT_SECONDS
    )


def _install_sqlite_deadline(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _register_progress_handler(dbapi_connection: Any, _record: Any) -> None:
        def _abort_if_expired() -> int:
            deadline = getattr(_deadline, "value", None)
            # A non-zero return aborts the running statement.
            return 1 if deadline is not None and time.monotonic() > deadline else 0

        dbapi_connection.set_progress_handler(
            _abort_if_expired, _SQLITE_PROGRESS_INSTRUCTIONS
        )


def _install_mysql_timeout(engine: Engine, timeout_seconds: int) -> None:
    milliseconds = timeout_seconds * 1000

    @event.listens_for(engine, "connect")
    def _set_session_timeout(dbapi_connection: Any, _record: Any) -> None:
        # max_execution_time is MySQL 5.7.8+; MariaDB spells it max_statement_time
        # and measures seconds.
        for statement in (
            f"SET SESSION max_execution_time = {milliseconds}",
            f"SET SESSION max_statement_time = {timeout_seconds}",
        ):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(statement)
            except Exception:
                continue
            finally:
                cursor.close()
            return


def timeout_connect_args(backend: str, timeout_seconds: int) -> dict[str, Any]:
    """Return the driver arguments that bound statement time for a backend."""
    if backend == "postgresql":
        # Enforced by the server: Postgres cancels any statement that exceeds it.
        return {"options": f"-c statement_timeout={timeout_seconds * 1000}"}
    if backend == "mysql":
        return {"read_timeout": timeout_seconds, "write_timeout": timeout_seconds}
    # SQLite is handled by a progress handler; other backends get no bound here.
    return {}


def create_configured_engine(
    url: str,
    timeout_seconds: int = QUERY_TIMEOUT_SECONDS,
) -> Engine:
    """Build an engine with a statement timeout and liveness checking."""
    backend = make_url(url).get_backend_name()
    connect_args = timeout_connect_args(backend, timeout_seconds)
    engine = create_engine(
        url,
        pool_pre_ping=True,
        connect_args=connect_args,
        execution_options={TIMEOUT_EXECUTION_OPTION: timeout_seconds},
    )

    if backend == "sqlite":
        _install_sqlite_deadline(engine)
    elif backend == "mysql":
        _install_mysql_timeout(engine, timeout_seconds)
    return engine


def _begin_read_only(connection: Connection) -> None:
    """Put the connection into a mode the database enforces, where one exists."""
    backend = connection.engine.dialect.name
    if backend == "postgresql":
        # Emits BEGIN READ ONLY. Must be set before the transaction starts.
        connection.execution_options(postgresql_readonly=True)
    elif backend == "sqlite":
        connection.execute(text("PRAGMA query_only = ON"))
    elif backend == "mysql":
        connection.execute(text("SET SESSION TRANSACTION READ ONLY"))
        # The access mode applies to the next transaction, so end the implicit one
        # the statement above opened.
        connection.rollback()


def _end_read_only(connection: Connection) -> None:
    if connection.engine.dialect.name != "sqlite":
        return
    try:
        # query_only lives on the connection, which is going back to the pool.
        connection.exec_driver_sql("PRAGMA query_only = OFF")
    except Exception:
        # An aborted statement can leave the connection unusable; the pool will
        # discard it. Failing to reset a pragma must not mask the real error.
        pass


@contextmanager
def read_only_connection(
    engine: Engine,
    timeout_seconds: int | None = None,
) -> Iterator[Connection]:
    """Yield a connection that is time-bounded and, where supported, read-only."""
    if timeout_seconds is None:
        timeout_seconds = engine_timeout_seconds(engine)
    with engine.connect() as connection:
        _begin_read_only(connection)
        _deadline.value = time.monotonic() + timeout_seconds
        try:
            yield connection
        finally:
            _deadline.value = None
            _end_read_only(connection)
            connection.rollback()
