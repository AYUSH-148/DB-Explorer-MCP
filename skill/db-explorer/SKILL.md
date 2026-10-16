---
name: db-explorer
description: Read-only SQL database analysis through the db-explorer MCP server. Use when the user asks about a database's tables, columns or relationships, wants data pulled with a SELECT, asks why a query is slow or which index to add, wants a schema health check, or is drafting a migration to check before running it. Works on PostgreSQL, MySQL and SQLite; it never changes data or schema.
---

# DB Explorer

Answer database questions with the `db-explorer` MCP server. Every tool is read-only:
queries must be one `SELECT`, run inside a read-only transaction with a timeout and a
row cap, and migration scripts are checked, never executed.

## Setup (once)

The server is open source (MIT): https://github.com/AYUSH-148/DB-Explorer-MCP

```bash
git clone https://github.com/AYUSH-148/DB-Explorer-MCP
claude mcp add db-explorer --env DATABASE_URL="postgresql+psycopg2://user:pass@host:5432/db" -- uv --directory "/path/to/DB-Explorer-MCP" run server.py
```

Then run `/mcp` and confirm 7 tools are listed. Other clients: see the repo README.
Point `DATABASE_URL` at a database user with read-only grants.

If the tools are missing, tell the user to set the server up. Don't write SQL for them to run by hand.

## Workflow

1. **Find the tables.** `explore_schema()` lists tables, views and the SQL dialect.
   Skip it if you already know the table names.
2. **Read the columns.** Call `explore_schema(detail=true, name_pattern=...)` once for
   every table the question touches. Don't make one call per table.
3. **Query.** `execute_query` with SQL in the reported dialect. If the result says
   `truncated: true`, say so. The rows shown are not the full result.
4. **Slow query?** `explain_query` for the plan, then `suggest_index(query=...)`.
5. **Schema audit?** `validate_schema` reports missing primary keys, unindexed foreign
   keys, wide tables and tables with no indexes.
6. **Migration?** Read `migration_context`, draft `up`/`down` SQL, then check it with
   `validate_migration` before the user runs it. The user runs it, not you.

## Rules

- Errors start with a `[code]` and end with a hint. Follow the hint. Don't retry the same call unchanged.
- Don't try to write data through these tools. Writes are rejected by design. For a
  change, draft the SQL and hand it to the user.
- Base answers on query results. Don't guess at row counts or values.
