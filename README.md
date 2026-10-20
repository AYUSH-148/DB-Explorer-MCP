# DB Explorer MCP

[![CI](https://github.com/AYUSH-148/DB-Explorer-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/AYUSH-148/DB-Explorer-MCP/actions/workflows/ci.yml)
[![M8ven Score](https://m8ven.ai/badge/mcp/ayush-148-db-explorer-mcp-1x2fhb)](https://m8ven.ai/mcp/ayush-148-db-explorer-mcp-1x2fhb)

A read-only [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server for PostgreSQL, MySQL, and SQLite that lets an AI coding assistant explore, query, and audit a relational database **without ever being able to write to it**.

<!-- mcp-name: io.github.AYUSH-148/db-explorer-mcp-server -->

Point your MCP client at a database and ask questions in plain language. The client's LLM writes the SQL; this server parses it, refuses anything that is not a single read-only `SELECT`, executes it with a row cap, and returns structured results. Schema inspection, execution plans, index recommendations, and migration review come along with it.

```text
MCP client LLM  ->  FastMCP tools  ->  safety layer  ->  SQLAlchemy  ->  database
   (writes SQL)       (7 tools)       (rejects writes)   (any dialect)
```

The server makes no LLM API calls of its own, so there is **no API key to configure** — reasoning happens in whichever client you connect. Works with SQLite, PostgreSQL, and MySQL through SQLAlchemy.

**Tested and evaluated.** 336 tests run on every push and pull request, plus 26 more against real PostgreSQL and MySQL in CI ([Tests](#tests)). In an agent evaluation, Claude completed **14 of 14** realistic database tasks through these tools with **no failed tool calls**, an average of 2 calls per task, and a mean independent judge score of **4.6 / 5** ([Agent evals](#agent-evals)).

## Why

Giving an assistant raw database credentials means one confused or prompt-injected turn can drop a table. Handing it a read-only replica loses schema context and plan analysis. This server takes the middle path: full introspection and query power, with mutation made structurally impossible at the parser level rather than by asking the model to behave.

## Architecture

```text
┌──────────────────────────────────────────────────────────────┐
│  MCP client  (Claude Code / Claude Desktop / Inspector)      │
│  owns the LLM: reads schema, authors SQL, interprets results │
└───────────────────────────┬──────────────────────────────────┘
                            │  MCP  ·  stdio (local)
                            │        ·  streamable HTTP + bearer token (remote)
┌───────────────────────────▼──────────────────────────────────┐
│ server.py  —  FastMCP instance + one engine per token group  │
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
                   │  SQLAlchemy Core
┌──────────────────▼───────────────────────────────────────────┐
│  Target database   ·   PostgreSQL  /  MySQL  /  SQLite       │
└──────────────────────────────────────────────────────────────┘
```

The SQL arriving at [safety.py](safety.py) is written by the client's model and therefore untrusted, so it is parsed rather than pattern-matched, and a rejected query never reaches the database.

| Mode | Transport | Auth |
| --- | --- | --- |
| Local | stdio | none needed; the client spawns the server |
| Remote | streamable HTTP | bearer tokens, or OAuth at the platform edge on Prefect Horizon |

Both modes run identical tool code — only `MCP_TRANSPORT` changes.

## Quickstart

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

### From PyPI

No clone needed. `uvx` downloads the package and runs it in its own environment:

```bash
claude mcp add db-explorer --env DATABASE_URL="postgresql+psycopg2://user:pass@localhost:5432/example" -- uvx db-explorer-mcp-server
```

Leave out `--env` to try it against the bundled demo database first. In any other client's config, use `"command": "uvx"` and `"args": ["db-explorer-mcp-server"]`.

### From source

```bash
git clone https://github.com/AYUSH-148/DB-Explorer-MCP
cd DB-Explorer-MCP
uv sync
uv run python seed_demo_db.py   # creates sample.db, a small shop with deliberate schema flaws
uv run pytest                   # 336 tests on SQLite; 26 more with TEST_*_URL set
```

On Windows, if `uv` is not on `PATH`, prefix with `py -m` (`py -m uv sync`).

You don't start the server yourself: your MCP client launches it over stdio, so go on to [Connect a client](#connect-a-client). Run by hand, `uv run server.py` prints nothing and waits for a client on stdin; stop it with Ctrl+C.

The default database is `sample.db`. Point at your own with `DATABASE_URL`, either in the client config below or in a `.env` file copied from [.env.example](.env.example):

```text
postgresql+psycopg2://user:password@localhost:5432/example
mysql+pymysql://user:password@localhost:3306/example
sqlite:///C:/data/example.db
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

For a server you host yourself with a bearer token, send it as a header. Read it from an environment variable, so a `.mcp.json` committed for the team holds no secret:

```json
{
  "mcpServers": {
    "db-explorer": {
      "type": "http",
      "url": "https://dbx.example.com/mcp",
      "headers": { "Authorization": "Bearer ${DBX_TOKEN}" }
    }
  }
}
```

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

[.vscode/mcp.json](.vscode/mcp.json) is checked in and starts the server over stdio through the Windows `py` launcher. On macOS or Linux, change its `command` to `uv` and its `args` to `["run", "server.py"]`.

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

Once connected to the demo `sample.db` from [Quickstart](#quickstart), prompts like these work directly:

- *"What tables exist, and which ones are missing primary keys?"*
- *"Show me 5 rows from `orders` with the highest total."*
- *"Why is this query slow? `SELECT * FROM orders WHERE created_at >= '2026-01-01'`"*
- *"Which foreign keys in this database lack indexes? Give me the `CREATE INDEX` statements."*
- *"Draft a migration adding a `shipped_at` column to `orders`, then validate the up and down scripts."*

To watch the guardrails work, ask it to run `DELETE FROM users`. The call fails with `Unsafe query blocked: Only SELECT queries are allowed. Got: DELETE` and the database is untouched.

## Agent skill

[skill/db-explorer/SKILL.md](skill/db-explorer/SKILL.md) is an optional agent skill that teaches the model to use these tools well: read the schema once before querying, compute totals in SQL rather than from returned rows, say when a result is `truncated`, and follow error hints instead of retrying. It guides the model only; the server must still be connected.

For Claude Code, copy the `skill/db-explorer` folder to `~/.claude/skills/db-explorer/` for every project, or to `.claude/skills/db-explorer/` inside one project.

## Tools

| Tool | Arguments | Returns |
| --- | --- | --- |
| `explore_schema` | `table_name?`, `include_sample_data=false`, `name_pattern?`, `detail=false`, `limit=200`, `offset=0` | The `dialect` to write SQL in, plus a listing of tables and views (each with a `kind` and column count), or one relation's `kind`, columns, PK, FKs, indexes, row count (tables only), and up to 3 sample rows. One relation, or a page with `detail=true`, also carries any table and column `comment` the database stores |
| `execute_query` | `sql`, `row_limit=100` (max 1000) | `columns`, `rows`, `count`, `truncated`, and the effective `row_limit` for one validated `SELECT`; plus `duplicate_columns` and a `note` when a result repeats a column name (the repeats are labelled `id:1`, which is not valid SQL), and a `note` when rows were cut to stay under the size budget |
| `explain_query` | `sql` | Native execution plan plus the resolved `dialect` |
| `validate_schema` | `table_name?`, `name_pattern?`, `limit=200`, `offset=0` | Schema issues with `severity`, `code`, `message`, `suggestion`; without `table_name`, one page of tables (views skipped), paged like `explore_schema` |
| `suggest_index` | `query?` **xor** `table_name?` | `CREATE INDEX` recommendations with reasons |
| `migration_context` | `name_pattern?`, `limit=200`, `offset=0` | Dialect plus one page of columns, keys, and indexes (no row counts), for client-side migration drafting; pages like `explore_schema` |
| `validate_migration` | `up_sql`, `down_sql` | Parsed statement types per script; rejects `SELECT`, comments and unbalanced parentheses. Not a syntax check, and **never executed** |

`validate_schema` reports four codes: `missing_primary_key`, `unindexed_foreign_key`, `wide_table` (50+ columns), and `no_indexes`. A foreign key counts as indexed when any index, the primary key, or a unique constraint starts with its columns; partial, GIN/GiST/BRIN and FULLTEXT/SPATIAL indexes do not count.

`explore_schema` is cheap by default: with no arguments it returns table names and column counts. Row counts arrive only with `table_name` (tables only, capped at 100,000 with `row_count_capped: true`). Narrow a large schema with `name_pattern` (`order` matches any name containing it, `order_*` is a glob), page with `limit`/`offset`, and use `detail=true` for the columns, keys and indexes of a whole page in one call. Views and materialized views are listed alongside tables, each with a `kind`.

## Safety model

Every `execute_query`, `explain_query`, and `suggest_index` call routes through [safety.py](safety.py) before touching the database. A query is rejected unless it satisfies all of:

- **Single statement.** `SELECT 1; DROP TABLE users` → `Exactly one SQL statement is required`
- **`SELECT` only**, determined from the parsed statement type rather than a string prefix → `Only SELECT queries are allowed. Got: DELETE`
- **No SQL comments.** `--`, `/*`, `*/` are refused outside string literals and quoted names, closing the comment-smuggling route. `#` is refused on MySQL and MariaDB only, where it starts a comment.
- **No blocked keywords** anywhere in the token stream: `ALTER`, `CREATE`, `DELETE`, `DROP`, `EXEC`, `EXECUTE`, `GRANT`, `INSERT`, `INTO`, `REVOKE`, `TRUNCATE`, `UPDATE`. Matched as whole tokens, so `SELECT grant_date FROM permissions` passes. Four entries are load-bearing: `INSERT`, `UPDATE` and `DELETE` can hide in a PostgreSQL data-modifying CTE whose statement type is still `SELECT`, and `SELECT * INTO archive FROM users` creates a table. The rest cannot appear in a `SELECT`-typed statement and stay as a second line of defense if `sqlparse` type detection regresses.
- **No locking clause.** `FOR UPDATE`, `FOR NO KEY UPDATE`, `FOR SHARE`, `FOR KEY SHARE` and MySQL's `LOCK IN SHARE MODE` are refused, because a locking read blocks other writers. Matched as a clause, not a keyword, so a column named `share` still works.

Every query that passes is wrapped as `SELECT * FROM (<your query>) AS limited_query LIMIT <row_limit + 1>`, so an unbounded scan cannot flood the client's context. A `LIMIT` in your own query narrows the inner result, but `row_limit` (clamped to 1000) still caps what comes back. If the extra probe row comes back it is dropped and the result carries `"truncated": true`, so a capped result is never mistaken for a complete one.

Validation is only the first of three layers, because a keyword blocklist cannot see a query that is syntactically fine and still harmful:

- **A statement timeout.** `SELECT pg_sleep(600)` passes every check above, so time is bounded independently of syntax: `statement_timeout` on PostgreSQL (set per transaction, so it works behind PgBouncer, Supabase and Neon poolers), `max_execution_time` on MySQL, and a progress-handler deadline on SQLite. Configured by `QUERY_TIMEOUT_SECONDS`, applied in [db.py](db.py).
- **A read-only transaction.** Reads run through `BEGIN READ ONLY` on PostgreSQL, `START TRANSACTION READ ONLY` on MySQL, and `PRAGMA query_only` on SQLite. The database refuses the write itself, which is a guarantee the blocklist cannot make.
- **Privileges.** Still the outermost boundary — see [.env.example](.env.example). A `SELECT`-only user is what stops server-side file reads like `pg_read_file()` that no keyword check reliably catches.

  Privileges are also where to restrict *what* can be read. A token reads whatever its group's database user can `SELECT`, so give each group its own user (see [Named tokens and groups](#named-tokens-and-groups)). Hide credentials and PII with grants, not with a server-side table or column blocklist: a parser cannot see the columns behind `SELECT *`, `SELECT u FROM users u`, or `row_to_json(u)`, but the database enforces a grant on all of them.

  ```sql
  -- PostgreSQL: whole tables, minus one
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO explorer;
  REVOKE SELECT ON users FROM explorer;
  -- then only the safe columns of it
  GRANT SELECT (id, name, created_at) ON users TO explorer;
  ```

  MySQL takes the same column list: `GRANT SELECT (id, name) ON app.users TO 'explorer'@'%';`. A query that touches a revoked column then fails with `sql_error` (`permission denied`).

Every tool call writes one line to the `db_explorer.audit` logger on stderr: the tool, how it ended (`ok` or the error code), the caller (`user=<token name> group=<group>` over HTTP), and its arguments, capped at 2000 characters. Arguments are logged verbatim, so treat the log as being as sensitive as the data.

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
| `query_timeout` | The query ran past `QUERY_TIMEOUT_SECONDS` and was cancelled |
| `database_unavailable` | The server could not reach the database, or the link dropped mid-query. Details go to the server log only |
| `unsafe_query` | Blocked by [safety.py](safety.py). The hint names the specific rule that fired |
| `query_too_complex` | The SQL is over 50,000 characters, or too long or deeply nested for the parser (e.g. an `IN (...)` of about 3,000 values) |
| `too_many_concurrent_calls` | The caller's token already has `MAX_CONCURRENT_CALLS_PER_CLIENT` calls running |
| `unknown_group` | The caller's token names a group the server has no database login for |
| `invalid_argument` | An argument out of range, such as `row_limit` below 1, or arguments that don't fit the tool's input schema: a missing required one, a wrong type, an unknown name |
| `missing_argument` / `conflicting_arguments` | `suggest_index` needs exactly one of `query` or `table_name` |
| `comments_not_allowed` / `unparsable_sql` / `select_in_migration` | `validate_migration` rejected a script |
| `server_busy` | Every database connection was in use for `DB_POOL_TIMEOUT_SECONDS`; no query ran. Retry unchanged |
| `internal_error` | A bug in the server. The traceback goes to the server log; the caller sees only the code |

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `DATABASE_URL` | `sample.db` next to `config.py`, read-only (stdio only) | **Required** when `MCP_TRANSPORT` is not `stdio`; startup fails loudly otherwise |
| `MCP_TRANSPORT` | `stdio` | `stdio`, `streamable-http`, or `sse` |
| `MCP_HOST` | `127.0.0.1` | HTTP transports only |
| `MCP_PORT` | `8000` | HTTP transports only |
| `QUERY_TIMEOUT_SECONDS` | `15` | Upper bound on any single statement, and the budget for a whole call: once spent, no further statement starts. Must be a positive integer |
| `DB_POOL_SIZE` | `5` | Connections kept open to the database |
| `DB_MAX_OVERFLOW` | `10` | Extra connections opened under load, closed when returned |
| `DB_POOL_TIMEOUT_SECONDS` | `5` | How long a call waits for a free connection before failing with `server_busy` |
| `MAX_CONCURRENT_CALLS_PER_CLIENT` | `5` | Tool calls one token can have running at once (per server process); stdio is not limited |
| `MCP_AUTH_TOKEN` | — | One shared token. This or `MCP_AUTH_TOKENS` is **required** when serving over HTTP, including a host that loads `server.py:mcp` itself. Minimum 32 characters |
| `MCP_AUTH_TOKENS` | — | Named tokens, `name:token` or `name:group:token`, comma-separated. Use instead of `MCP_AUTH_TOKEN`, not with it. See [Named tokens and groups](#named-tokens-and-groups) |
| `DATABASE_URL_<GROUP>` | — | The database URL for one token group, for example `DATABASE_URL_ANALYST`. **Required** for every group `MCP_AUTH_TOKENS` names |
| `MCP_ALLOW_UNAUTHENTICATED` | `false` | Explicit opt-out of the token requirement, for trusted networks or a host whose own authentication sits in front (Horizon) |

At most `DB_POOL_SIZE + DB_MAX_OVERFLOW` (15 by default) calls hold a connection at once; a call past that waits `DB_POOL_TIMEOUT_SECONDS`, then fails with `server_busy`. Keep the pool under the database's `max_connections`, counted across every server instance.

The SQLite fallback is for local development only: `sample.db` next to [config.py](config.py), opened read-only. `.env` is loaded at startup (the file next to `config.py` first, then one in the working directory); real environment variables win over both.

## Serve over HTTP

```powershell
$env:MCP_TRANSPORT = "streamable-http"
$env:MCP_HOST = "0.0.0.0"
$env:MCP_PORT = "8000"
$env:DATABASE_URL = "postgresql+psycopg2://user:password@host:5432/example"
$env:MCP_AUTH_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
uv run server.py
```

The same on macOS or Linux:

```bash
export MCP_TRANSPORT=streamable-http MCP_HOST=0.0.0.0 MCP_PORT=8000
export DATABASE_URL="postgresql+psycopg2://user:password@host:5432/example"
export MCP_AUTH_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run server.py
```

An HTTP endpoint publishes SELECT on the configured database to anyone who can reach the port, so the server **fails to start** without `MCP_AUTH_TOKEN` or `MCP_AUTH_TOKENS` rather than coming up unprotected. Clients send a token as `Authorization: Bearer <token>`; it is compared in constant time in [auth.py](auth.py). Set `MCP_ALLOW_UNAUTHENTICATED=true` to override on a genuinely trusted network — the server then warns on stderr at every startup.

A host that loads `server.py:mcp` directly (Horizon, `fastmcp run`) answers every HTTP request `401` unless a token or `MCP_ALLOW_UNAUTHENTICATED=true` is set.

On a loopback bind, foreign `Host` and `Origin` headers are refused, so a web page cannot reach a local server through DNS rebinding. The transport is stateless, so any replica behind a load balancer can answer any request. `GET /health` answers `{"status": "ok"}` without a token, for load-balancer probes; it never touches the database.

### Named tokens and groups

Give each person or service its own token, and optionally a group whose database user decides what it can read:

```text
MCP_AUTH_TOKENS=alice:analyst:<token>,carol:analyst:<token>,fran:finance:<token>,ci:<token>
DATABASE_URL=postgresql+psycopg2://dbx_readonly:...@db/app          # tokens with no group (ci)
DATABASE_URL_ANALYST=postgresql+psycopg2://dbx_analyst:...@db/app
DATABASE_URL_FINANCE=postgresql+psycopg2://dbx_finance:...@db/app
```

Each entry is `name:token` or `name:group:token`. A token's name goes into every audit line; its group picks the database login the call runs on, so the database's own grants (and PostgreSQL row-level security) decide what that caller can read. A lone `MCP_AUTH_TOKEN` behaves as one token named `default` with no group.

The server refuses to start on a malformed entry, a repeated name or token, a token under 32 characters, a group with no `DATABASE_URL_<GROUP>`, or both `MCP_AUTH_TOKEN` and `MCP_AUTH_TOKENS` set. Tokens are compared in constant time and read at startup: restart after changing them. Each group has its own connection pool.

On PostgreSQL, `explore_schema` lists tables a group cannot `SELECT` (the catalog is readable by everyone), but their rows stay protected. [DEPLOYMENT.md](DEPLOYMENT.md) has the `GRANT` statements for both databases, OAuth on Horizon, and a VM setup with HTTPS.

**Hosted Supabase note:** direct connections (`db.<ref>.supabase.co`) are IPv6-only, which fails from IPv4-only containers with an empty-looking `psycopg2.OperationalError`. Use the pooler host from the dashboard's *Connect* panel, and note that the username becomes `postgres.<project-ref>`.

## Tests

```powershell
uv run pytest
```

336 tests covering the safety layer, serialization, read-only enforcement and timeouts, HTTP auth and token groups, every tool, and error reporting. Each uses a temporary SQLite database, so the suite needs no credentials and no running server.

[tests/test_live_databases.py](tests/test_live_databases.py) covers what SQLite cannot — the read-only transaction, the statement timeout, `NUMERIC`/`TIMESTAMP` values, catalog reflection — against real PostgreSQL and MySQL. Set `TEST_POSTGRES_URL` and/or `TEST_MYSQL_URL` to a throwaway database to run it (it creates and drops `dbx_live_*` tables). CI runs both.

## Agent evals

The tests check each tool; the evals check whether a model uses the tools well. [evals/run.py](evals/run.py) runs each task in [evals/scenarios.py](evals/scenarios.py) through Claude Code in headless mode (`claude -p`), with this server as its only tool source, against a seeded `evals/eval.db`. Each run is scored on tool choice, call budget and a correct answer read from the database, and a second `claude -p` call grades the whole run from 1 to 5.

```powershell
uv run python evals/seed_eval_db.py      # build evals/eval.db (the runner does this if it is missing)
uv run python evals/run.py               # every scenario, on Sonnet
uv run python evals/run.py --only top_customers --model opus
```

It needs the `claude` CLI on `PATH`, logged in; runs count toward that login's usage limits, and no API key is involved. Results vary between runs, so the evals are not part of CI. Reports are written to `evals/results/`.

### Latest results

Claude Sonnet 5.5 as agent and judge, 2026-10-14. Full report, including every tool call and its result: [evals/reports/2026-10-14-sonnet.json](evals/reports/2026-10-14-sonnet.json).

**14/14 passed · 0 tool errors · 0 repeated calls · 28 tool calls in total · mean judge score 4.64 / 5**

| Task | Tools the agent chose | Calls / budget | Judge |
|---|---|---|---|
| List the tables and views | `explore_schema` | 1/3 | 5/5 |
| Orders per status | `explore_schema`, `execute_query` | 2/4 | 5/5 |
| Top 3 customers by delivered spend | `explore_schema`, `execute_query` | 3/5 | 4/5 |
| Category with the most revenue | `explore_schema`, `execute_query` | 2/5 | 5/5 |
| Category with the best average rating | `explore_schema`, `execute_query` | 2/5 | 5/5 |
| Users who never ordered | `explore_schema`, `execute_query` | 2/4 | 5/5 |
| 5 most recent orders with customer names | `explore_schema`, `execute_query` | 2/4 | 5/5 |
| Row count of "the user table" (real name: `users`) | `explore_schema`, `execute_query` | 2/4 | 5/5 |
| Why a date-filtered query is slow | `explore_schema`, `explain_query` | 2/4 | 5/5 |
| Audit the schema for structural problems | `validate_schema`, `explore_schema` | 2/4 | 4/5 |
| Which foreign keys on `reviews` need an index | `suggest_index`, `explore_schema` | 2/3 | 4/5 |
| Migration adding a `phone` column | `migration_context`, `validate_migration` | 2/4 | 5/5 |
| "Delete all cancelled orders" (must refuse: read-only) | `explore_schema`, `execute_query` | 2/5 | 4/5 |
| Users on the pro plan | `explore_schema`, `execute_query` | 2/3 | 4/5 |

Every answer that has a single right value was checked against the database, not just judged. Scores of 4 were for one call more than needed, or extra commentary beyond the question.

Known limitation: `explore_schema` does not yet report UNIQUE or CHECK constraints, so in a schema audit the model can claim one is missing when it exists.

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
seed_demo_db.py   builds the demo sample.db used in Quickstart
skill/            optional agent skill for clients that load SKILL.md files
tests/            pytest suite over temporary SQLite databases
evals/            agent evals: a Claude model works scripted tasks through the tools
```

## Design notes and limits

- **Query-mode `suggest_index` flags full table scans, not missing indexes in general.** It names the scan rather than writing a `CREATE INDEX`, because which column to index depends on the query. A scan on a tiny table is expected, not a problem.
- **Only the default schema is inspected**: the first schema on PostgreSQL's `search_path` (usually `public`), or the database named in `DATABASE_URL` on MySQL. `execute_query` still reads `billing.invoices` when the SQL names the schema. To inspect another PostgreSQL schema, put it first: `ALTER ROLE explorer SET search_path = billing, public;`.
- **A few read-only shapes are refused.** `sqlparse` cannot type a parenthesized `(SELECT 1)`, `VALUES (1)` or `TABLE users`, so they are rejected with a hint to write a plain `SELECT`.
- **SQL is capped at 50,000 characters**, and past `sqlparse`'s token or nesting limits the call fails with `query_too_complex`.
- **The row cap guards context, not cost.** A heavy aggregate still runs in full; `QUERY_TIMEOUT_SECONDS` bounds that.
- **Long text and large results are cut.** A text value over 2000 characters arrives truncated with its full length noted. A query result stops at 500,000 bytes with `truncated` set, and a schema page stops there too and continues at `next_offset`.
- **MariaDB is not tested.** It shares the MySQL code path, but may drop an `ORDER BY` inside the row-cap subquery; add your own `LIMIT` to an ordered query there.
- **Binary values up to 256 bytes arrive hex-encoded**; larger ones are reported as a size only.
- **Wide `NUMERIC` values arrive as strings**, so they keep their exact digits rather than being rounded.

## License

MIT. See [LICENSE](LICENSE).
