# DB Explorer MCP

[![M8ven Score](https://m8ven.ai/badge/mcp/ayush-148-db-explorer-mcp-1x2fhb)](https://m8ven.ai/mcp/ayush-148-db-explorer-mcp-1x2fhb)


A [Model Context Protocol](https://modelcontextprotocol.io) server that lets an AI coding assistant explore, query, and audit a relational database **without ever being able to write to it**.

Point your MCP client at a database and ask questions in plain language. The client's LLM writes the SQL; this server parses it, refuses anything that is not a single read-only `SELECT`, executes it with a row cap, and returns structured results. Schema inspection, execution plans, index recommendations, and migration review come along with it.

```text
MCP client LLM  ->  FastMCP tools  ->  safety layer  ->  SQLAlchemy  ->  database
   (writes SQL)       (7 tools)       (rejects writes)   (any dialect)
```

The server makes no LLM API calls of its own, so there is **no API key to configure** — reasoning happens in whichever client you connect. Works with SQLite, PostgreSQL, and MySQL through SQLAlchemy.

## Why

Giving an assistant raw database credentials means one confused or prompt-injected turn can drop a table. Handing it a read-only replica loses schema context and plan analysis. This server takes the middle path: full introspection and query power, with mutation made structurally impossible at the parser level rather than by asking the model to behave.

## Architecture

```text
┌──────────────────────────────────────────────────────────────┐
│  MCP client  (Claude Code / Claude Desktop / Inspector)      │
│  owns the LLM: reads schema, authors SQL, interprets results │
└───────────────────────────┬──────────────────────────────────┘
                            │  MCP  ·  stdio (local)
                            │        ·  streamable HTTP + OAuth 2.0 (remote)
┌───────────────────────────▼──────────────────────────────────┐
│ server.py  —  FastMCP instance + one shared SQLAlchemy engine│
│                                                              │
│   explore_schema   execute_query    explain_query            │
│   validate_schema  suggest_index    migration_context        │
│   validate_migration                                         │
└──────┬───────────────────────┬───────────────────┬───────────┘
       │                       │                   │
       │  read path            │  metadata path    │  review path
       │                       │                   │
┌──────▼────────────┐  ┌───────▼─────────┐  ┌──────▼──────────┐
│ safety.py         │  │ inspector.py    │  │ migration.py    │
│ ── trust boundary │  │ schema_health.py│  │ parses up/down, │
│ sqlparse AST      │  │ index_suggest.py│  │ never executes  │
│ SELECT-only       │  │ explain.py      │  │                 │
│ 1 stmt · no cmnts │  │                 │  │                 │
│ denylist · LIMIT  │  │                 │  │                 │
└──────┬────────────┘  └───────┬─────────┘  └─────────────────┘
       │                       │
       └───────────┬───────────┘
                   │  SQLAlchemy Core (text() + inspect())
┌──────────────────▼───────────────────────────────────────────┐
│  Target database   ·   PostgreSQL  /  MySQL  /  SQLite       │
└──────────────────────────────────────────────────────────────┘
```

**The LLM lives in the client, not the server.** Most NL-to-SQL designs put a model call inside the server; this one does not. The client already has a capable model, so the server ships zero LLM dependencies, zero API keys, and zero per-call inference cost — and stays usable from any MCP client, not just Claude.

That split defines the trust boundary: the SQL arriving at [safety.py](safety.py) is model-authored and therefore untrusted, so it is parsed rather than pattern-matched, and a rejected query never reaches the driver.

### Request lifecycle

A typical `execute_query` call:

1. **Client** turns the user's question into SQL, using schema it fetched earlier via `explore_schema`.
2. **FastMCP** deserializes the tool call and validates arguments against the tool's type hints.
3. **safety.py** parses the SQL with `sqlparse` — one statement, type `SELECT`, no comments, no blocked keywords. Failure raises before any connection is opened.
4. **Row cap** applied: if the query has no `LIMIT`, it is wrapped in `SELECT * FROM (…) AS limited_query LIMIT row_limit`.
5. **SQLAlchemy** executes it on a pooled connection and the rows are serialized to plain dicts.
6. **Client** receives `{columns, rows, count}` as structured JSON and explains it in natural language.

Errors travel the same path in reverse: a raised `ValueError` becomes an MCP tool error, which the client surfaces to the user while the server keeps serving.

### Module responsibilities

| Module | Role |
| --- | --- |
| [server.py](server.py) | Tool surface only — thin `@mcp.tool` wrappers over plain functions, plus transport selection |
| [safety.py](safety.py) | The trust boundary: AST validation and row-limited execution |
| [inspector.py](inspector.py) | Reflection via SQLAlchemy `inspect()` — columns, PK, FKs, indexes, row counts, samples |
| [explain.py](explain.py) | Dialect-aware plans (`EXPLAIN QUERY PLAN` on SQLite, `EXPLAIN` elsewhere) |
| [index_suggest.py](index_suggest.py) | Recommendations from a live plan or from FK metadata |
| [schema_health.py](schema_health.py) | Objective schema audit, no heuristics about naming or style |
| [migration.py](migration.py) | Schema context out, script validation in — never executes DDL |
| [config.py](config.py) | Environment resolution with fail-fast checks |

Each tool body delegates to a module-level function that takes an `Engine` argument, so the whole system is testable against a temporary SQLite database with no MCP client and no network involved.

### Transports

| Mode | Transport | Auth | Use |
| --- | --- | --- | --- |
| Local | stdio | process-level | development; client spawns the server |
| Remote | streamable HTTP | OAuth 2.0 (DCR + PKCE) at the platform edge | shared deployment; many clients, one database |

Both modes run identical tool code — only `MCP_TRANSPORT` changes.

## Tools

| Tool | Arguments | Returns |
| --- | --- | --- |
| `explore_schema` | `table_name?`, `include_sample_data=false` | All tables, or one table's columns, PK, FKs, indexes, row count, and up to 3 sample rows |
| `execute_query` | `sql`, `row_limit=100` | `columns`, `rows`, `count` for one validated `SELECT` |
| `explain_query` | `sql` | Native execution plan plus the resolved `dialect` |
| `validate_schema` | `table_name?` | Schema issues with `severity`, `code`, `message`, `suggestion` |
| `suggest_index` | `query?` **xor** `table_name?` | `CREATE INDEX` recommendations with reasons |
| `migration_context` | — | Dialect and full schema, for client-side migration drafting |
| `validate_migration` | `up_sql`, `down_sql` | Parsed statement types per script; **never executed** |

`validate_schema` reports four codes: `missing_primary_key`, `unindexed_foreign_key`, `wide_table` (50+ columns), and `no_indexes`.

## Safety model

Every `execute_query`, `explain_query`, and `suggest_index` call routes through [safety.py](safety.py) before touching the database. A query is rejected unless it satisfies all of:

- **Single statement.** `SELECT 1; DROP TABLE users` → `Exactly one SQL statement is required`
- **`SELECT` only**, determined from the parsed statement type rather than a string prefix → `Only SELECT queries are allowed. Got: DELETE`
- **No SQL comments.** `--`, `/*`, `*/` are refused outright, closing the classic comment-smuggling route
- **No blocked keywords** anywhere in the token stream: `ALTER`, `CREATE`, `DELETE`, `DROP`, `EXEC`, `EXECUTE`, `GRANT`, `INSERT`, `INTO`, `REVOKE`, `SET`, `TRUNCATE`, `UPDATE`

Every query that passes is wrapped as `SELECT * FROM (<your query>) AS limited_query LIMIT <row_limit>`, so an unbounded scan cannot flood the client's context. The wrap is unconditional: a `LIMIT` in your own query narrows the inner result, but `row_limit` still caps what comes back, so `LIMIT 500` with the default `row_limit` returns 100 rows.

Validation is only the first of three layers, because a keyword blocklist cannot see a query that is syntactically fine and still harmful:

- **A statement timeout.** `SELECT pg_sleep(600)` passes every check above, so time is bounded independently of syntax: `statement_timeout` on PostgreSQL, `max_execution_time` on MySQL, and a progress-handler deadline on SQLite. Configured by `QUERY_TIMEOUT_SECONDS`, applied in [db.py](db.py).
- **A read-only transaction.** Reads run through `BEGIN READ ONLY` on PostgreSQL, `SET SESSION TRANSACTION READ ONLY` on MySQL, and `PRAGMA query_only` on SQLite. The database refuses the write itself, which is a guarantee the blocklist cannot make.
- **Privileges.** Still the outermost boundary — see [.env.example](.env.example). A `SELECT`-only user is what stops server-side file reads like `pg_read_file()` that no keyword check reliably catches.

`validate_migration` is deliberately the inverse: it rejects `SELECT` statements, and it never runs either script. You get the parsed statement types back and run the DDL yourself.

## Quickstart

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```powershell
uv sync
uv run python tests/seed_test_db.py   # creates sample.db
uv run pytest                         # 32 tests, no external database needed
uv run server.py                      # stdio transport
```

If `uv` is not on `PATH`, prefix with `py -m` (`py -m uv sync`).

The default database is `sqlite:///sample.db`. Point at your own with `DATABASE_URL`:

```powershell
$env:DATABASE_URL = "postgresql+psycopg2://user:password@localhost:5432/example"
$env:DATABASE_URL = "mysql+pymysql://user:password@localhost:3306/example"
$env:DATABASE_URL = "sqlite:///C:/data/example.db"
```

Percent-encode special characters in passwords (`@` → `%40`, `#` → `%23`, `/` → `%2F`).

## Connect a client

### Claude Code — local

```powershell
claude mcp add db-explorer --env DATABASE_URL="postgresql+psycopg2://user:pass@localhost:5432/example" -- uv --directory "C:/path/to/DB-Explorer-MCP" run server.py
```

Then run `/mcp` in a session to confirm the 7 tools are listed. Add `-s user` to make it available in every project.

### Claude Code — remote

```powershell
claude mcp add --transport http db-explorer https://your-deployment.fastmcp.app/mcp
```

Run `/mcp` → **Authenticate** for the OAuth flow; tokens are cached and refreshed automatically.

### Claude Desktop

Local, in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "db-explorer": {
      "command": "uv",
      "args": ["--directory", "C:/path/to/DB-Explorer-MCP", "run", "server.py"],
      "env": { "DATABASE_URL": "postgresql+psycopg2://user:pass@localhost:5432/example" }
    }
  }
}
```

To reach a remote deployment without a custom connector, proxy it over stdio:

```json
{
  "mcpServers": {
    "db-explorer": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://your-deployment.fastmcp.app/mcp"]
    }
  }
}
```

### VS Code

[.vscode/mcp.json](.vscode/mcp.json) is checked in and starts the server over stdio — no extra setup for anyone who clones the repo.

### MCP Inspector

```powershell
npx @modelcontextprotocol/inspector
```

Use transport `Streamable HTTP` with your `/mcp` URL, or stdio with `uv run server.py`. The Inspector shows raw tool responses and unparaphrased errors, which makes it the fastest way to tell a server problem from a client problem.

### Python

```python
import asyncio
from fastmcp import Client

async def main():
    async with Client("https://your-deployment.fastmcp.app/mcp", auth="oauth") as client:
        print([tool.name for tool in await client.list_tools()])
        print(await client.call_tool("explore_schema", {}))

asyncio.run(main())
```

## Try it

Once connected, prompts like these work directly:

- *"What tables exist, and which ones are missing primary keys?"*
- *"Show me 5 rows from `orders` with the highest total."*
- *"Why is this query slow? `SELECT * FROM orders WHERE customer_id = 42`"*
- *"Which foreign keys in this database lack indexes? Give me the `CREATE INDEX` statements."*
- *"Draft a migration adding a `status` column to `orders`, then validate the up and down scripts."*

To watch the guardrails work, ask it to run `DELETE FROM users`. The call fails with `Unsafe query blocked: Only SELECT queries are allowed. Got: DELETE` and the database is untouched.

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///sample.db` (stdio only) | **Required** when `MCP_TRANSPORT` is not `stdio`; startup fails loudly otherwise |
| `MCP_TRANSPORT` | `stdio` | `stdio`, `streamable-http`, or `sse` |
| `MCP_HOST` | `127.0.0.1` | HTTP transports only |
| `MCP_PORT` | `8000` | HTTP transports only |
| `QUERY_TIMEOUT_SECONDS` | `15` | Upper bound on any single statement; must be a positive integer |
| `MCP_AUTH_TOKEN` | — | **Required** when `MCP_TRANSPORT` is not `stdio`. Minimum 32 characters |
| `MCP_ALLOW_UNAUTHENTICATED` | `false` | Explicit opt-out of the token requirement, for trusted networks only |

The sqlite fallback exists for local development only. [config.py](config.py) raises `RuntimeError: DATABASE_URL must be set when serving over HTTP` rather than silently serving an empty local file from a deployment — a failure mode that otherwise surfaces much later as a confusing `unable to open database file`.

`.env` is loaded at startup by [config.py](config.py), so copying `.env.example` to `.env` works as that file instructs. The file next to `config.py` is read first and a `.env` in the working directory second, because an MCP client launches this server with a working directory you do not control. Real environment variables always win over both, so a host's secret store overrides the file without editing it. `.env` stays gitignored.

## Serve over HTTP

```powershell
$env:MCP_TRANSPORT = "streamable-http"
$env:MCP_HOST = "0.0.0.0"
$env:MCP_PORT = "8000"
$env:DATABASE_URL = "postgresql+psycopg2://user:password@host:5432/example"
$env:MCP_AUTH_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
uv run server.py
```

An HTTP endpoint publishes SELECT on the configured database to anyone who can reach the port, so the server **fails to start** without `MCP_AUTH_TOKEN` rather than coming up unprotected. Clients send it as `Authorization: Bearer <token>`; it is compared in constant time in [auth.py](auth.py). Set `MCP_ALLOW_UNAUTHENTICATED=true` to override on a genuinely trusted network — the server then warns on stderr at every startup.

For real user identity rather than one shared secret, swap `SharedSecretVerifier` for one of FastMCP's OAuth providers. See [DEPLOYMENT.md](DEPLOYMENT.md) for FastMCP Cloud / Prefect Horizon deployment, where OAuth 2.0 with dynamic client registration and PKCE is handled by the platform.

**Hosted Supabase note:** direct connections (`db.<ref>.supabase.co`) are IPv6-only, which fails from IPv4-only containers with an empty-looking `psycopg2.OperationalError`. Use the pooler host from the dashboard's *Connect* panel, and note that the username becomes `postgres.<project-ref>`.

## Tests

```powershell
uv run pytest
```

80 tests covering the safety layer, value serialization, read-only enforcement and timeouts, HTTP authentication, inspector, explain, index suggestions, schema health, migration validation, and the tool wrappers. Each uses a temporary SQLite database, so the suite needs no credentials and no running server.

SQLite cannot produce the types that break a real driver -- it has no `NUMERIC` and returns `str`/`int` for nearly everything -- so [tests/test_serialization.py](tests/test_serialization.py) exercises `Decimal`, `datetime`, `UUID`, and binary values directly rather than through a query. A PostgreSQL and MySQL test path is the next gap worth closing.

## Project layout

```text
server.py         FastMCP instance, engine, and the 7 tool definitions
safety.py         query validation and row-limited execution
db.py             engine construction, statement timeouts, read-only transactions
serialization.py  driver values to JSON-safe primitives
auth.py           bearer-token verification for HTTP transports
inspector.py      schema reflection (columns, PK, FKs, indexes, samples)
explain.py        dialect-aware EXPLAIN
index_suggest.py  index recommendations from plans or FK metadata
schema_health.py  objective schema issue reporting
migration.py      migration context and non-executing script validation
config.py         environment configuration with fail-fast checks
tests/            pytest suite over temporary SQLite databases
```

## Design notes and limits

- **Migrations are never executed.** The server returns schema context and validates scripts; you run the DDL. That keeps the connection read-only in practice, not just by policy.
- **Query-mode `suggest_index` is tuned to SQLite plan output**, which exposes a `detail` column containing `SCAN`. On PostgreSQL and MySQL the plan is still returned in full, but automatic recommendations will usually be empty — use `table_name` mode there, which works from foreign-key metadata on every dialect.
- **`SET` and `INTO` are blocked keywords**, so a few legitimate `SELECT`s (for example `GROUPING SETS`) are rejected. Deliberate trade: a false rejection is cheap, a false acceptance is not.
- **The row cap is a context guard, not a performance guard.** A heavy aggregate still runs in full on the database before its output is limited. `QUERY_TIMEOUT_SECONDS` is what bounds the cost of that work.
- **Binary columns are summarised, not returned.** Values up to 256 bytes arrive hex-encoded, which suits `BINARY(16)` UUIDs and digests; anything larger is reported as a size only. Inlining a multi-megabyte blob would consume the context window it was sent to.
- **Wide `NUMERIC` values arrive as strings.** A decimal that fits a float is a JSON number so it sorts and compares correctly; one that does not keeps its exact digits rather than being silently rounded.
