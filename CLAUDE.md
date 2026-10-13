# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```powershell
uv sync                                # install deps
uv run python tests/seed_test_db.py    # (re)create sample.db
uv run pytest                          # 328 SQLite tests; 24 live tests skip unless TEST_*_URL is set
uv run pytest tests/test_safety.py     # run one test file
uv run pytest tests/test_safety.py::test_name -v   # run a single test
uv run server.py                       # start server, stdio transport
```

`uv run ruff check .` lints (bug-catching rules only, see `[tool.ruff.lint]` in `pyproject.toml`). CI (`.github/workflows/ci.yml`) runs ruff and pytest on every push to `main` and every PR.

To run over HTTP locally, set `MCP_TRANSPORT=streamable-http` (plus `MCP_HOST`/`MCP_PORT` and `MCP_AUTH_TOKEN` or `MCP_AUTH_TOKENS`) before `uv run server.py` — see README "Serve over HTTP".

If `uv` isn't on `PATH`, prefix commands with `py -m`.

## Architecture

This is an MCP server (`server.py`, FastMCP) exposing 7 read-only database tools (`explore_schema`, `execute_query`, `explain_query`, `validate_schema`, `suggest_index`, `migration_context`, `validate_migration`) over one SQLAlchemy engine per token group (`server.current_engine`; stdio and ungrouped tokens use `DATABASE_URL`). It makes no LLM calls itself — the MCP client's model writes the SQL; this server's only job is to make mutation structurally impossible before it reaches the driver.

**Trust boundary is `safety.py`.** Every query-executing tool routes through it first. It parses SQL with `sqlparse` (never string-matches) through `parse_sql`, which caps input at `MAX_SQL_CHARS` and turns `sqlparse`'s token/nesting limits into `query_too_complex`, and rejects anything that isn't exactly one `SELECT` statement, contains SQL comments, contains a blocked keyword, or contains a locking clause (`FOR UPDATE`/`SHARE` etc., matched as a multi-word clause sequence, not a keyword set — plain columns named `share` must still pass). A query that passes is unconditionally wrapped as `SELECT * FROM (\n<query>\n) AS limited_query LIMIT <row_limit + 1>` (the line breaks keep a comment the validator missed from swallowing the `LIMIT`), so a `LIMIT` inside the original query narrows the inner result but never removes the outer cap. The extra probe row is dropped and sets `truncated: true` in the result, so a capped result is never mistaken for a complete one. Rows are also cut once the result passes `MAX_RESULT_BYTES`, and `serialization.py` cuts any text value over `MAX_INLINE_TEXT_CHARS`. Queries run through `db.execute_raw` (`exec_driver_sql` with `no_parameters`), never `text()`, which reads `:word` inside a string literal as a bind parameter. `row_limit` is clamped to 1000. See the README's "Safety model" section for the full denylist rationale (which entries are load-bearing vs. defense-in-depth) before touching the keyword list.

Safety validation is layer one of three — `db.py` adds a statement timeout (`QUERY_TIMEOUT_SECONDS`, dialect-specific: `SET LOCAL statement_timeout` per transaction on Postgres, never the `options` startup parameter that PgBouncer-style poolers reject; `max_execution_time` on MySQL, progress-handler on SQLite) and runs reads inside a read-only transaction (`BEGIN READ ONLY` / `START TRANSACTION READ ONLY` / `PRAGMA query_only`) so the database itself refuses writes, independent of the parser.

**Module split** (each tool in `server.py` is a thin `@mcp.tool` wrapper delegating to a plain function that takes an `Engine`, so everything is testable against a temp SQLite DB with no MCP client involved):
- `safety.py` — the trust boundary described above
- `db.py` — engine construction, timeouts, read-only transaction setup
- `inspector.py` — schema reflection (columns, PK, FKs, indexes, row counts, samples) via SQLAlchemy `inspect()`. Lists views and materialized views alongside tables, each tagged with a `kind`. A schema page (`explore_schema` listing, `migration_context`) is cut at `MAX_RESULT_BYTES` like a query result and continues at `next_offset`; detail is reflected `DETAIL_BATCH` tables at a time so the cut stops the work too
- `explain.py` — dialect-aware `EXPLAIN` / `EXPLAIN QUERY PLAN`
- `index_suggest.py` — index recommendations from a live plan (full-scan detection for SQLite, PostgreSQL, MySQL) or FK metadata (works on every dialect)
- `schema_health.py` — objective schema audit (`missing_primary_key`, `unindexed_foreign_key`, `wide_table`, `no_indexes`)
- `indexes.py` — which indexes can serve a foreign key, shared by `schema_health.py` and `index_suggest.py`. Counts the implicit PRIMARY KEY and UNIQUE indexes that `get_indexes()` omits; ignores partial, GIN/GiST/BRIN and FULLTEXT/SPATIAL indexes. Reads UNIQUE through SQLite's auto-indexes rather than `get_unique_constraints()`, which raises `NotImplementedError` on SQL Server and drops constraints whose case differs on SQLite.
- `migration.py` — returns schema context and validates `up`/`down` SQL statement types; **never executes DDL**
- `errors.py` — `ToolInputError` (a `ValueError` subclass) internally; converted to FastMCP `ToolError` only at the `server.py` boundary, because `ToolError` is the one error type that survives `mask_error_details=True`. Every error carries a `code` and a corrective hint.
- `serialization.py` — driver values (Decimal, datetime, UUID, binary) to JSON-safe primitives
- `auth.py` — named bearer tokens for HTTP transports (`MCP_AUTH_TOKENS=name:group:token,...`, or a single `MCP_AUTH_TOKEN`), compared in constant time against every entry. A match returns an `AccessToken` with `client_id=<name>` and `claims={"group": ...}`; `server.current_engine` picks the group's engine (`DATABASE_URL_<GROUP>`) from that claim, refusing a group it has no engine for with `unknown_group` rather than falling back to `DATABASE_URL`, and the audit line records `user=`/`group=`. Access control per group is the database's grants, never server-side filtering
- `config.py` — loads `.env` (local file next to `config.py` first, then CWD `.env`; real env vars win over both), fails fast if `DATABASE_URL`/a token is missing under an HTTP transport, or a token group has no `DATABASE_URL_<GROUP>`. The stdio fallback is `sample.db` next to `config.py` (never CWD-relative), opened `mode=ro`

Transports (stdio vs. streamable-http) run identical tool code; only `MCP_TRANSPORT` changes. HTTP transport requires `MCP_AUTH_TOKEN` or `MCP_AUTH_TOKENS` (min 32 chars per token) unless `MCP_ALLOW_UNAUTHENTICATED=true` is explicitly set. That includes a host that imports `server.py:mcp` and serves HTTP without setting `MCP_TRANSPORT` (Horizon, `fastmcp run`): `auth.py` returns `RejectEveryToken` there, so it answers 401 rather than serving open; stdio ignores the provider. `server.py` also turns on FastMCP's Host/Origin check (off by default) against DNS rebinding, runs streamable-http stateless (no `mcp-session-id`, so replicas can share traffic), serves an unauthenticated liveness-only `GET /health`, and refuses a token's tool call with `too_many_concurrent_calls` once it has `MAX_CONCURRENT_CALLS_PER_CLIENT` running (`ConcurrentCallLimit` middleware, per process).

## Testing notes

Without `TEST_POSTGRES_URL` / `TEST_MYSQL_URL`, the 328 tests that run use a temporary SQLite database — no credentials, no running server, no network. SQLite can't produce the driver types that matter for correctness (no `NUMERIC`, returns `str`/`int` for nearly everything), so `tests/test_serialization.py` exercises `Decimal`/`datetime`/`UUID`/binary directly rather than through a query. `tests/test_live_databases.py` runs the dialect-specific paths (read-only transaction, statement timeout, NUMERIC/TIMESTAMP serialization, reflection) against real PostgreSQL and MySQL when `TEST_POSTGRES_URL` / `TEST_MYSQL_URL` are set, and skips otherwise. CI sets both via service containers. It creates and drops `dbx_live_*` tables, so only point it at a throwaway database.
