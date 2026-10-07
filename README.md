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
4. **Row cap** applied: the query is always wrapped in `SELECT * FROM (…) AS limited_query LIMIT row_limit + 1`.
5. **SQLAlchemy** executes it on a pooled connection and the rows are serialized to plain dicts. The probe row, if present, is dropped and sets `truncated`.
6. **Client** receives `{columns, rows, count, truncated, row_limit}` as structured JSON and explains it in natural language.

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
| [indexes.py](indexes.py) | Which columns a table already has indexed, including the implicit indexes behind PRIMARY KEY and UNIQUE |
| [migration.py](migration.py) | Schema context out, script validation in — never executes DDL |
| [errors.py](errors.py) | Coded, hinted errors and driver-error classification |
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
| `explore_schema` | `table_name?`, `include_sample_data=false`, `name_pattern?`, `detail=false`, `limit=200`, `offset=0` | A listing of tables and views (each with a `kind` and column count), or one relation's `kind`, columns, PK, FKs, indexes, row count, and up to 3 sample rows |
| `execute_query` | `sql`, `row_limit=100` (max 1000) | `columns`, `rows`, `count`, `truncated`, and the effective `row_limit` for one validated `SELECT`; plus `duplicate_columns` and a `note` when a result repeats a column name (the repeats are labelled `id:1`, which is not valid SQL) |
| `explain_query` | `sql` | Native execution plan plus the resolved `dialect` |
| `validate_schema` | `table_name?`, `name_pattern?`, `limit=200`, `offset=0` | Schema issues with `severity`, `code`, `message`, `suggestion`; without `table_name`, one page of tables (views skipped), paged like `explore_schema` |
| `suggest_index` | `query?` **xor** `table_name?` | `CREATE INDEX` recommendations with reasons |
| `migration_context` | `name_pattern?`, `limit=200`, `offset=0` | Dialect plus one page of columns, keys, and indexes (no row counts), for client-side migration drafting; pages like `explore_schema` |
| `validate_migration` | `up_sql`, `down_sql` | Parsed statement types per script; **never executed** |

`validate_schema` reports four codes: `missing_primary_key`, `unindexed_foreign_key`, `wide_table` (50+ columns), and `no_indexes`. A foreign key counts as indexed when any index, the primary key, or a unique constraint starts with its columns in order, so a one-to-one child keyed on its parent's id is not reported, and no `CREATE INDEX` is suggested that would duplicate an index the database already built. An index that cannot look rows up by value does not count: a partial index (`WHERE ...`), a PostgreSQL GIN, GiST or BRIN index, or a MySQL `FULLTEXT`/`SPATIAL` key. Suggested `CREATE INDEX` statements quote their names, so they run as written against a table called `order` or a PostgreSQL column called `"UserId"`. On MySQL the quotes are backticks, whatever the server's `sql_mode`, because a suggestion is pasted into another session and double quotes only name an identifier where `ANSI_QUOTES` is on.

`explore_schema` is cheap by default and expensive only on request. With no arguments it
returns table names and column counts — a handful of queries however wide the database
is, and small enough to read before picking a table. Row counts cost a scan, so they
arrive only with `table_name`, never for a view (its count would run the view's whole
query), and stop at 100,000 rows: past that, `row_count` is 100000 and
`row_count_capped` is `true`, so a huge table never times out the call.
Narrow a large schema with `name_pattern` (`order` matches any name containing it, `order_*` is a glob), page with `limit`/`offset` (capped
at 1000), and use `detail=true` to expand a whole page into columns, keys, and indexes.
Views and materialized views are listed alongside tables, since `execute_query` can read
them too; each entry's `kind` is `table`, `view`, or `materialized_view`. A view whose base table was dropped cannot be read; it is listed with an `error` instead of failing the whole call.

## Safety model

Every `execute_query`, `explain_query`, and `suggest_index` call routes through [safety.py](safety.py) before touching the database. A query is rejected unless it satisfies all of:

- **Single statement.** `SELECT 1; DROP TABLE users` → `Exactly one SQL statement is required`
- **`SELECT` only**, determined from the parsed statement type rather than a string prefix → `Only SELECT queries are allowed. Got: DELETE`
- **No SQL comments.** `--`, `/*`, `*/` are refused outright (outside string literals, quoted names and `[bracket]` names, so `'a--b'` still passes), closing the classic comment-smuggling route. `#` is refused too, but only on MySQL and MariaDB, where it starts a comment; elsewhere it is legal SQL (`#>>` on PostgreSQL, `#temp` on SQL Server)
- **No blocked keywords** anywhere in the token stream: `ALTER`, `CREATE`, `DELETE`, `DROP`, `EXEC`, `EXECUTE`, `GRANT`, `INSERT`, `INTO`, `REVOKE`, `TRUNCATE`, `UPDATE`
- **No locking clause.** `FOR UPDATE`, `FOR NO KEY UPDATE`, `FOR SHARE`, `FOR KEY SHARE` and MySQL's `LOCK IN SHARE MODE` are refused, because a locking read is not a read: it blocks other transactions from writing those rows. This one is defense in depth rather than a hole being closed — PostgreSQL 16 refuses both forms itself inside a read-only transaction (`cannot execute SELECT FOR SHARE in a read-only transaction`, verified). What the check adds is a rejection before a connection is opened, an error naming the clause and the fix instead of a generic driver message, and consistency: `FOR UPDATE` used to be refused only incidentally, because `UPDATE` is on the denylist for data-modifying CTEs.

  This is matched as a **clause**, not as a keyword, and the distinction is the point. `SHARE` alone is a legal column name — `sqlparse` types the `share` in `SELECT share FROM cap_table` as a `Keyword` — so adding `SHARE` to the denylist above would reject a real query. A flat set of words is the wrong shape for a rule about multi-word clauses, so [safety.py](safety.py) collects the keyword sequence and matches `FOR [NO] [KEY] UPDATE|SHARE` against it. A `FOR` belonging to something else (`FOR XML`, `FOR JSON`, `FOR SYSTEM_TIME`) falls through, because its target is not a lock strength.

Every query that passes is wrapped as `SELECT * FROM (<your query>) AS limited_query LIMIT <row_limit>`, so an unbounded scan cannot flood the client's context. The wrap is unconditional: a `LIMIT` in your own query narrows the inner result, but `row_limit` still caps what comes back, so `LIMIT 500` with the default `row_limit` returns 100 rows. `row_limit` is itself clamped to 1000, so raising it cannot defeat the guard. Your query sits on its own line between the parentheses, so a comment the database sees but the validator did not (the two disagree about where a string ends after a backslash-quote) stops at the line break instead of swallowing the closing paren and the `LIMIT`.

The cap is never silent. The wrapper actually asks for `row_limit + 1` rows; if the extra one comes back it is dropped and the result carries `"truncated": true`, so the client can tell "that is all the data" from "there is more" instead of reporting a capped set as complete. The result also returns the effective `row_limit`, which shows when a request above 1000 was clamped.

Validation is only the first of three layers, because a keyword blocklist cannot see a query that is syntactically fine and still harmful:

- **A statement timeout.** `SELECT pg_sleep(600)` passes every check above, so time is bounded independently of syntax: `statement_timeout` on PostgreSQL, `max_execution_time` on MySQL, and a progress-handler deadline on SQLite. Configured by `QUERY_TIMEOUT_SECONDS`, applied in [db.py](db.py).
- **A read-only transaction.** Reads run through `BEGIN READ ONLY` on PostgreSQL, `SET SESSION TRANSACTION READ ONLY` on MySQL, and `PRAGMA query_only` on SQLite. The database refuses the write itself, which is a guarantee the blocklist cannot make.
- **Privileges.** Still the outermost boundary — see [.env.example](.env.example). A `SELECT`-only user is what stops server-side file reads like `pg_read_file()` that no keyword check reliably catches.

  Privileges are also where to restrict *what* can be read. The token is all-or-nothing, so anything the database user can `SELECT`, every token holder can read. Hide credentials and PII with grants, not with a server-side table or column blocklist: a parser cannot see the columns behind `SELECT *`, `SELECT u FROM users u`, or `row_to_json(u)`, but the database enforces a grant on all of them.

  ```sql
  -- PostgreSQL: whole tables, minus one
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO explorer;
  REVOKE SELECT ON users FROM explorer;
  -- then only the safe columns of it
  GRANT SELECT (id, name, created_at) ON users TO explorer;
  ```

  MySQL takes the same column list: `GRANT SELECT (id, name) ON app.users TO 'explorer'@'%';`. A query that touches a revoked column then fails with `sql_error` (`permission denied`). On PostgreSQL, `explore_schema` still lists tables the user cannot read, because reflection reads the catalog, not the data.

Every tool call is logged on the `db_explorer.audit` logger, one line each, to stderr: the tool name, how it ended (`ok`, with `rows` and `truncated` for `execute_query`, or the error code such as `unsafe_query`, `sql_error` or `query_timeout`), and its arguments, capped at 2000 characters. Refused, denied and timed-out calls are logged too. The logger is set up when `server.py` is imported, so a hosted entrypoint (`server.py:mcp`) logs the same as `uv run server.py`. Arguments are logged verbatim, so literals in a `WHERE` clause land in the log; treat the log as being as sensitive as the data. With one shared token the log records what was asked and when, not who asked.

`validate_migration` is deliberately the inverse: it rejects `SELECT` statements, and it never runs either script. You get the parsed statement types back and run the DDL yourself.

## Errors

A tool error is only useful to a model if it says what to do next, so every failure carries a code and a corrective hint instead of a bare message:

```text
[table_not_found] Table not found: usrs. Did you mean: users.
Hint: Call explore_schema() to list the tables in this database.

[sql_error] The database rejected the query: no such column: totl.
Hint: Call explore_schema(table_name=...) to confirm the table and column names before retrying.

[query_timeout] The query exceeded the statement timeout of 15s: interrupted.
Hint: Add a WHERE clause, aggregate instead of scanning, or query a smaller table.
```

| Code | Cause |
| --- | --- |
| `table_not_found` | No such table. Carries the nearest matching names the database does have |
| `sql_error` | The database rejected the query — a missing column, a type mismatch, bad syntax |
| `query_timeout` | The statement hit `QUERY_TIMEOUT_SECONDS` and was cancelled, including a MySQL client-side read timeout (a backstop at twice the limit), or a call that runs many statements spent that budget in total |
| `database_unavailable` | The server could not reach the database, or the link dropped mid-query. The driver's text names the host and user, so it goes to the server log and not to the caller |
| `unsafe_query` | Blocked by [safety.py](safety.py). The hint names the specific rule that fired |
| `invalid_argument` | An argument out of range, such as `row_limit` below 1 |
| `missing_argument` / `conflicting_arguments` | `suggest_index` needs exactly one of `query` or `table_name` |
| `comments_not_allowed` / `unparsable_sql` / `select_in_migration` | `validate_migration` rejected a script |
| `internal_error` | A bug in the server. The traceback goes to the server log; the caller sees only the code |

Two details worth knowing. Errors are raised as FastMCP `ToolError`, which is the only error type that survives a server configured with `mask_error_details=True` — reasonable hardening for an HTTP deployment, and it would otherwise reduce every message above to `Error calling tool`. And `sql_error` reports the query *you* sent, not the row-limit wrapper [safety.py](safety.py) builds around it, so the SQL in the message is SQL you can act on.

Internally these stay Python exceptions. [errors.py](errors.py) defines `ToolInputError`, a `ValueError` subclass whose `str()` is the plain message, so composition between modules and direct Python use both keep working; conversion happens only at the tool boundary in [server.py](server.py).

## Quickstart

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```powershell
uv sync
uv run python tests/seed_test_db.py   # creates sample.db
uv run pytest                         # 258 tests, no external database needed
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
| `QUERY_TIMEOUT_SECONDS` | `15` | Upper bound on any single statement, and the budget for a whole call: once spent, no further statement starts. Must be a positive integer |
| `DB_POOL_SIZE` | `5` | Connections kept open to the database |
| `DB_MAX_OVERFLOW` | `10` | Extra connections opened under load, closed when returned |
| `DB_POOL_TIMEOUT_SECONDS` | `5` | How long a call waits for a free connection before failing with `server_busy` |
| `MCP_AUTH_TOKEN` | — | **Required** when `MCP_TRANSPORT` is not `stdio`. Minimum 32 characters |
| `MCP_ALLOW_UNAUTHENTICATED` | `false` | Explicit opt-out of the token requirement, for trusted networks only |

Tool calls run on up to 40 worker threads, but the pool allows at most `DB_POOL_SIZE + DB_MAX_OVERFLOW` (15 by default) at once. A call past that waits `DB_POOL_TIMEOUT_SECONDS`, then fails with `server_busy` so the client can retry. Raise the pool toward 40 for heavier HTTP traffic, but keep it under the database's connection limit (`max_connections`), counted across every server instance.

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

258 tests covering the safety layer, value serialization, read-only enforcement and timeouts, HTTP authentication, inspector, explain, index suggestions, schema health, migration validation, error reporting, and the tool wrappers. Each uses a temporary SQLite database, so the suite needs no credentials and no running server.

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
indexes.py        index coverage shared by schema_health and index_suggest
migration.py      migration context and non-executing script validation
errors.py         coded errors with hints, and driver-error classification
config.py         environment configuration with fail-fast checks
tests/            pytest suite over temporary SQLite databases
```

## Design notes and limits

- **Migrations are never executed.** The server returns schema context and validates scripts; you run the DDL. That keeps the connection read-only in practice, not just by policy.
- **Query-mode `suggest_index` is tuned to SQLite plan output**, which exposes a `detail` column containing `SCAN`. On PostgreSQL and MySQL the plan is still returned in full, but automatic recommendations will usually be empty — use `table_name` mode there, which works from foreign-key metadata on every dialect.
- **The keyword denylist matches whole tokens, not substrings**, so a keyword that merely contains a blocked word is unaffected: `GROUPING SETS` and `SELECT grant_date FROM permissions` both pass, where a naive `"SET" in sql` check would reject the first and `"GRANT" in sql` the second.
- **Each denylist entry has to earn its place.** `INTO` does: `SELECT * INTO archive FROM users` has statement type `SELECT` but creates a table, so only the keyword scan catches it. `SET` did not, and was removed — every statement that changes session state (`SET ROLE`, `SET search_path`, even `SET x = (SELECT 1)`) parses as type `UNKNOWN` and is refused by the type check, while `UPDATE ... SET` inside a data-modifying CTE is caught by `UPDATE`. All it added was rejecting `SELECT set FROM config`, since `sqlparse` types a bare `set` as a keyword rather than a column name.
- **Four of the twelve entries are load-bearing** — `INSERT`, `UPDATE`, `DELETE` and `INTO` are reachable in a statement whose type is `SELECT`, the first three through Postgres data-modifying CTEs. The rest are redundant, because a CTE accepts only `INSERT`, `UPDATE`, `DELETE` and `MERGE`, never DDL: no legal `SELECT`-typed statement can contain `DROP`. They stay as a second line if `sqlparse` type detection ever regresses.
- **A few read-only statements are rejected for lack of a statement type.** `sqlparse` reports `UNKNOWN` for a parenthesized `(SELECT 1)`, for `VALUES (1)` and for `TABLE users`, and the type check refuses anything that is not `SELECT`. All are harmless; none is currently accepted.

  This is left as a rejection rather than fixed by unwrapping, because the incidence is near zero — a caller writes the plain `SELECT` — and widening what the parser accepts to serve a query nobody sends is a poor trade against the risk. What *was* fixed is the explanation: these reasons and a genuine write both contain `Only SELECT`, so they used to collapse to the same hint, and a caller that sent a read was told the server is read-only and pointed at `validate_migration`. `Got: UNKNOWN` now carries its own hint naming the shapes that cause it. A rejection the caller can recover from in one turn is an acceptable cost; a rejection that misdiagnoses itself is not.
- **The row cap is a context guard, not a performance guard.** A heavy aggregate still runs in full on the database before its output is limited. `QUERY_TIMEOUT_SECONDS` is what bounds the cost of that work.
- **Binary columns are summarised, not returned.** Values up to 256 bytes arrive hex-encoded, which suits `BINARY(16)` UUIDs and digests; anything larger is reported as a size only. Inlining a multi-megabyte blob would consume the context window it was sent to.
- **Wide `NUMERIC` values arrive as strings.** A decimal that fits a float is a JSON number so it sorts and compares correctly; one that does not keeps its exact digits rather than being silently rounded.
