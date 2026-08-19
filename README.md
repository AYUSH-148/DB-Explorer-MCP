# Database Explorer MCP

A FastMCP server for exploring and safely querying relational databases. The local development database is SQLite; SQLAlchemy also supports PostgreSQL and MySQL through their drivers.

## Architecture

The MCP client performs natural-language reasoning and generates SQL. This server exposes database tools, validates read-only queries, executes them, and returns structured results.

```text
MCP client LLM -> FastMCP tools -> safety layer -> SQLAlchemy -> database
```

The server does not call Anthropic, so an Anthropic API key is not required.

## Setup

Install `uv`, then from this directory run:

```powershell
uv sync
uv run python tests/seed_test_db.py
uv run pytest
```

If `uv` is not on `PATH` yet, use:

```powershell
py -m uv sync
py -m uv run pytest
```

The default database is:

```text
sqlite:///sample.db
```

To use another database, set `DATABASE_URL` before starting the server. Examples:

```powershell
$env:DATABASE_URL = "sqlite:///C:/data/example.db"
$env:DATABASE_URL = "postgresql+psycopg2://user:password@localhost:5432/example"
$env:DATABASE_URL = "mysql+pymysql://user:password@localhost:3306/example"
```

## Run the MCP server

```powershell
uv run server.py
```

The shareable VS Code MCP configuration is in `.vscode/mcp.json`. It starts the server over stdio and can be used with MCP-compatible clients and Inspector.

For remote hosting, use Streamable HTTP:

```powershell
$env:MCP_TRANSPORT = "streamable-http"
$env:MCP_HOST = "0.0.0.0"
$env:MCP_PORT = "8000"
uv run server.py
```

The remote deployment must provide authentication and protect `DATABASE_URL` through its secret configuration.

## Tools

- `explore_schema`: list tables or inspect one table
- `execute_query`: execute one safe read-only `SELECT`
- `explain_query`: return the native execution plan
- `validate_schema`: report missing primary keys and unindexed foreign keys
- `suggest_index`: suggest indexes from table metadata or a query plan

Every query execution and explanation passes through the safety layer. Mutating statements, comments, and multiple statements are rejected. Query results default to a maximum of 100 rows.

## Tests

```powershell
uv run pytest
```

The test suite uses temporary SQLite databases and does not require external credentials or a running database server.
