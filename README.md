# DB Explorer MCP

A [Model Context Protocol](https://modelcontextprotocol.io) server that lets an AI coding assistant explore, query, and audit a relational database **without ever being able to write to it**.

Point your MCP client at a database and ask questions in plain language. The client's LLM writes the SQL; this server parses it, refuses anything that is not a single read-only `SELECT`, executes it with a row cap, and returns structured results. Schema inspection, execution plans, index recommendations, and migration review come along with it.

```text
MCP client LLM  ->  FastMCP tools  ->  safety layer  ->  SQLAlchemy  ->  database
   (writes SQL)       (7 tools)       (rejects writes)   (any dialect)
```

The server makes no LLM API calls of its own, so there is **no API key to configure** — reasoning happens in whichever client you connect. Works with SQLite, PostgreSQL, and MySQL through SQLAlchemy.

## Why

Giving an assistant raw database credentials means one confused or prompt-injected turn can drop a table. Handing it a read-only replica loses schema context and plan analysis. This server takes the middle path: full introspection and query power, with mutation made structurally impossible at the parser level rather than by asking the model to behave.

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

Queries that pass and contain no `LIMIT` are wrapped as `SELECT * FROM (<your query>) AS limited_query LIMIT <row_limit>`, so an unbounded scan cannot flood the client's context. A `LIMIT` you write yourself is respected as-is.

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

The sqlite fallback exists for local development only. [config.py](config.py) raises `RuntimeError: DATABASE_URL must be set when serving over HTTP` rather than silently serving an empty local file from a deployment — a failure mode that otherwise surfaces much later as a confusing `unable to open database file`.

Nothing in this project reads `.env` files; `.env.example` is documentation. Supply real values through your shell or your host's secret store, and keep credentials out of the repo.

## Serve over HTTP

```powershell
$env:MCP_TRANSPORT = "streamable-http"
$env:MCP_HOST = "0.0.0.0"
$env:MCP_PORT = "8000"
$env:DATABASE_URL = "postgresql+psycopg2://user:password@host:5432/example"
uv run server.py
```

Never expose this endpoint without authentication — read-only still means readable, and every row is reachable. See [DEPLOYMENT.md](DEPLOYMENT.md) for FastMCP Cloud / Prefect Horizon deployment, where OAuth 2.0 with dynamic client registration and PKCE is handled by the platform.

**Hosted Supabase note:** direct connections (`db.<ref>.supabase.co`) are IPv6-only, which fails from IPv4-only containers with an empty-looking `psycopg2.OperationalError`. Use the pooler host from the dashboard's *Connect* panel, and note that the username becomes `postgres.<project-ref>`.

## Tests

```powershell
uv run pytest
```

32 tests covering the safety layer, inspector, explain, index suggestions, schema health, migration validation, and the tool wrappers. Each uses a temporary SQLite database, so the suite needs no credentials and no running server.

## Project layout

```text
server.py         FastMCP instance, engine, and the 7 tool definitions
safety.py         query validation and row-limited execution
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
- **The row cap is a context guard, not a performance guard.** A heavy aggregate still runs in full on the database before its output is limited.
