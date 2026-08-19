# Remote Deployment

## Before deployment

1. Push the project to GitHub.
2. Use PostgreSQL or MySQL for a shared remote database. Do not use the local `sample.db` for production data.
3. Keep `DATABASE_URL` in the hosting platform's secret settings.
4. Enable platform authentication before sharing the endpoint.

## FastMCP Horizon

FastMCP Cloud is now presented as Prefect Horizon in the current FastMCP documentation.

1. Sign in to Horizon with GitHub.
2. Select the repository and the `main` branch.
3. Set the entrypoint to:

   ```text
   server.py:mcp
   ```

4. Add this secret environment variable:

   ```text
   DATABASE_URL=<remote database connection string>
   ```

5. Enable authentication.
6. Deploy the server.
7. Use the generated `/mcp` URL with an MCP client or Inspector.

The platform installs dependencies from `pyproject.toml`. The `if __name__ == "__main__"` block is used for local execution and is not required by the hosted entrypoint.

## Local HTTP check

To run the same HTTP transport locally:

```powershell
$env:MCP_TRANSPORT = "streamable-http"
$env:MCP_HOST = "127.0.0.1"
$env:MCP_PORT = "8000"
uv run server.py
```

Use an MCP client against the platform-specific MCP endpoint. Do not expose an unauthenticated database server publicly.
