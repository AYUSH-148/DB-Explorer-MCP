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
from sqlalchemy.pool import QueuePool

from config import (
    DB_MAX_OVERFLOW,
    DB_POOL_SIZE,
    DB_POOL_TIMEOUT_SECONDS,
    QUERY_TIMEOUT_SECONDS,
)
from errors import ToolInputError

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


def _install_request_deadline(engine: Engine) -> None:
    """Refuse to start a statement once the call's whole budget is spent.

    Postgres and MySQL bound each statement, not the call, so a call that runs one
    statement per table could otherwise take the timeout once per table.
    ponytail: checked between statements only, so the statement that crosses the
    deadline still runs to its own limit: a call can take up to twice the
    timeout. Lower the server-side limit per statement if that matters.
    """

    @event.listens_for(engine, "before_cursor_execute")
    def _refuse_if_expired(*_args: Any) -> None:
        deadline = getattr(_deadline, "value", None)
        if deadline is not None and time.monotonic() > deadline:
            raise ToolInputError(
                code="query_timeout",
                message=(
                    "The call ran past its time budget of "
                    f"{engine_timeout_seconds(engine)}s"
                ),
                hint=(
                    "Ask for less in one call: a smaller limit, a name_pattern, "
                    "or no row counts. The budget is set by QUERY_TIMEOUT_SECONDS."
                ),
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
    """Return the driver arguments that bound connect and statement time for a backend."""
    # connect_timeout bounds a host that drops packets instead of refusing them;
    # without it every connect, and every pool_pre_ping, waits on the OS TCP timeout.
    if backend == "postgresql":
        # Enforced by the server: Postgres cancels any statement that exceeds it.
        return {
            "connect_timeout": timeout_seconds,
            "options": f"-c statement_timeout={timeout_seconds * 1000}",
        }
    if backend == "mysql":
        # The server-side max_execution_time set in _install_mysql_timeout is what
        # cancels a slow query. A socket timeout only abandons it, leaving the
        # server still running it, so keep it well above as a backstop for a
        # server that ignores the session variable or stops answering.
        backstop = timeout_seconds * 2
        return {
            "connect_timeout": timeout_seconds,
            "read_timeout": backstop,
            "write_timeout": backstop,
        }
    # SQLite is handled by a progress handler; other backends get no bound here.
    return {}


def create_configured_engine(
    url: str,
    timeout_seconds: int = QUERY_TIMEOUT_SECONDS,
    pool_size: int = DB_POOL_SIZE,
    max_overflow: int = DB_MAX_OVERFLOW,
    pool_timeout: int = DB_POOL_TIMEOUT_SECONDS,
) -> Engine:
    """Build an engine with a statement timeout, a bounded pool, and liveness checking."""
    parsed = make_url(url)
    backend = parsed.get_backend_name()
    connect_args = timeout_connect_args(backend, timeout_seconds)
    # In-memory SQLite uses a pool that has no overflow or wait to configure, and
    # rejects these arguments.
    pool_class = parsed.get_dialect().get_pool_class(parsed)
    pool_options = (
        {
            "pool_size": pool_size,
            "max_overflow": max_overflow,
            "pool_timeout": pool_timeout,
        }
        if issubclass(pool_class, QueuePool)
        else {}
    )
    engine = create_engine(
        url,
        **pool_options,
        pool_pre_ping=True,
        connect_args=connect_args,
        execution_options={TIMEOUT_EXECUTION_OPTION: timeout_seconds},
    )

    _install_request_deadline(engine)
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
