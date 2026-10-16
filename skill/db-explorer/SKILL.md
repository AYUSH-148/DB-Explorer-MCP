---
name: db-explorer
description: Read-only SQL database analysis through the db-explorer MCP server. Use when the user asks about a database's tables, columns or relationships, wants data pulled with a SELECT, asks why a query is slow or which index to add, wants a schema health check, or is drafting a migration to check before running it. Works on PostgreSQL, MySQL and SQLite; it never changes data or schema.
license: MIT
compatibility: Requires the db-explorer MCP server (Python 3 and uv) connected to the agent.
---

# DB Explorer

Answer database questions with the db-explorer MCP server. Every tool is read-only:
queries must be one `SELECT`, run inside a read-only transaction with a timeout and a
row cap, and migration scripts are checked, never executed.

## Setup (once)

The server is open source (MIT): https://github.com/AYUSH-148/DB-Explorer-MCP.
It needs Python 3 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/AYUSH-148/DB-Explorer-MCP
claude mcp add -s user db-explorer --env DATABASE_URL="postgresql+psycopg2://user:pass@host:5432/db" -- uv --directory "/path/to/DB-Explorer-MCP" run server.py
```

Then run `/mcp` and confirm 7 tools are listed. Other clients: see the repo README.
Point `DATABASE_URL` at a database user with read-only grants.

The server may be registered under any name, so its tools can appear as
`mcp__<server>__explore_schema`. Look for the tool names (`explore_schema`,
`execute_query`, `explain_query`, `validate_schema`, `suggest_index`,
`migration_context`, `validate_migration`) under any server. Only if none are
available, tell the user to set the server up. Don't write SQL for them to run by hand.

## Workflow

1. **Find the tables.** `explore_schema()` lists tables, views and the SQL dialect.
   Skip it if you already know the table names.
2. **Read the columns.** Call `explore_schema(detail=true)` once to cover every table
   the question touches. Add `name_pattern` only when those tables share a name
   fragment. On a big schema, page with `offset=next_offset`.
3. **Query.** `execute_query` with SQL in the reported dialect. If the result says
   `truncated: true`, say so. The rows shown are not the full result.
4. **Slow query?** `explain_query` for the plan. `suggest_index(query=...)` reports
   full table scans in the plan but writes no SQL, so write the `CREATE INDEX`
   yourself from the WHERE/JOIN columns. `suggest_index(table_name=...)` returns
   `CREATE INDEX` statements for unindexed foreign keys.
5. **Schema audit?** `validate_schema` reports missing primary keys, unindexed foreign
   keys, wide tables and tables with no indexes.
6. **Migration?** Read `migration_context`, draft `up`/`down` SQL, then check it with
   `validate_migration` before the user runs it. It checks statement types, not
   syntax, so don't call the script verified. Pass `up_sql`/`down_sql` with no SQL
   comments. The user runs it, not you.

## Rules

- Compute counts, totals and averages in SQL (`COUNT`, `SUM`, `GROUP BY`), never from
  returned rows. Results are capped at 100 rows by default. Use `COUNT(*)` for an
  exact row count: the `explore_schema` row count stops at 100,000
  (`row_count_capped: true`).
- Errors start with a `[code]` and end with a hint. Follow the hint. Don't retry the same call unchanged.
- Don't try to write data through these tools. Writes are rejected by design. For a
  change, draft the SQL and hand it to the user.
- Base answers on query results. Don't guess at row counts or values.
