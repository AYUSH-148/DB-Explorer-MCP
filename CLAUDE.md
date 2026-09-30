# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```powershell
uv sync                                # install deps
uv run python tests/seed_test_db.py    # (re)create sample.db
uv run pytest                          # run all 212 tests
uv run pytest tests/test_safety.py     # run one test file
uv run pytest tests/test_safety.py::test_name -v   # run a single test
uv run server.py                       # start server, stdio transport
```

No lint/format command is configured in `pyproject.toml`.

To run over HTTP locally, set `MCP_TRANSPORT=streamable-http` (plus `MCP_HOST`/`MCP_PORT`/`MCP_AUTH_TOKEN`) before `uv run server.py` — see README "Serve over HTTP".

If `uv` isn't on `PATH`, prefix commands with `py -m`.

## Architecture

This is an MCP server (`server.py`, FastMCP) exposing 7 read-only database tools (`explore_schema`, `execute_query`, `explain_query`, `validate_schema`, `suggest_index`, `migration_context`, `validate_migration`) over one shared SQLAlchemy engine. It makes no LLM calls itself — the MCP client's model writes the SQL; this server's only job is to make mutation structurally impossible before it reaches the driver.

**Trust boundary is `safety.py`.** Every query-executing tool routes through it first. It parses SQL with `sqlparse` (never string-matches) and rejects anything that isn't exactly one `SELECT` statement, contains SQL comments, contains a blocked keyword, or contains a locking clause (`FOR UPDATE`/`SHARE` etc., matched as a multi-word clause sequence, not a keyword set — plain columns named `share` must still pass). A query that passes is unconditionally wrapped as `SELECT * FROM (<query>) AS limited_query LIMIT <row_limit + 1>`, so a `LIMIT` inside the original query narrows the inner result but never removes the outer cap. The extra probe row is dropped and sets `truncated: true` in the result, so a capped result is never mistaken for a complete one. `row_limit` is clamped to 1000. See the README's "Safety model" section for the full denylist rationale (which entries are load-bearing vs. defense-in-depth) before touching the keyword list.

Safety validation is layer one of three — `db.py` adds a statement timeout (`QUERY_TIMEOUT_SECONDS`, dialect-specific: `statement_timeout` on Postgres, `max_execution_time` on MySQL, progress-handler on SQLite) and runs reads inside a read-only transaction (`BEGIN READ ONLY` / `SET SESSION TRANSACTION READ ONLY` / `PRAGMA query_only`) so the database itself refuses writes, independent of the parser.

**Module split** (each tool in `server.py` is a thin `@mcp.tool` wrapper delegating to a plain function that takes an `Engine`, so everything is testable against a temp SQLite DB with no MCP client involved):
- `safety.py` — the trust boundary described above
- `db.py` — engine construction, timeouts, read-only transaction setup
- `inspector.py` — schema reflection (columns, PK, FKs, indexes, row counts, samples) via SQLAlchemy `inspect()`
- `explain.py` — dialect-aware `EXPLAIN` / `EXPLAIN QUERY PLAN`
- `index_suggest.py` — index recommendations from a live plan (SQLite-tuned) or FK metadata (works on every dialect)
- `schema_health.py` — objective schema audit (`missing_primary_key`, `unindexed_foreign_key`, `wide_table`, `no_indexes`)
- `indexes.py` — which indexes can serve a foreign key, shared by `schema_health.py` and `index_suggest.py`. Counts the implicit PRIMARY KEY and UNIQUE indexes that `get_indexes()` omits; ignores partial, GIN/GiST/BRIN and FULLTEXT/SPATIAL indexes. Reads UNIQUE through SQLite's auto-indexes rather than `get_unique_constraints()`, which raises `NotImplementedError` on SQL Server and drops constraints whose case differs on SQLite.
- `migration.py` — returns schema context and validates `up`/`down` SQL statement types; **never executes DDL**
- `errors.py` — `ToolInputError` (a `ValueError` subclass) internally; converted to FastMCP `ToolError` only at the `server.py` boundary, because `ToolError` is the one error type that survives `mask_error_details=True`. Every error carries a `code` and a corrective hint.
- `serialization.py` — driver values (Decimal, datetime, UUID, binary) to JSON-safe primitives
- `auth.py` — constant-time bearer-token check for HTTP transports
- `config.py` — loads `.env` (local file next to `config.py` first, then CWD `.env`; real env vars win over both), fails fast if `DATABASE_URL`/`MCP_AUTH_TOKEN` are missing under an HTTP transport

Transports (stdio vs. streamable-http) run identical tool code; only `MCP_TRANSPORT` changes. HTTP transport requires `MCP_AUTH_TOKEN` (min 32 chars) unless `MCP_ALLOW_UNAUTHENTICATED=true` is explicitly set.

## Testing notes

All 212 tests run against a temporary SQLite database — no credentials, no running server, no network. SQLite can't produce the driver types that matter for correctness (no `NUMERIC`, returns `str`/`int` for nearly everything), so `tests/test_serialization.py` exercises `Decimal`/`datetime`/`UUID`/binary directly rather than through a query. There is currently no PostgreSQL/MySQL test path.
