# Remote Deployment

## Before deployment

1. Push the project to GitHub.
2. Use PostgreSQL or MySQL for a shared remote database. Do not use the local `sample.db` for production data.
3. Keep `DATABASE_URL` in the hosting platform's secret settings.
4. Enable platform authentication, or set tokens (see [VM or EC2](#vm-or-ec2)), before sharing the endpoint.

## FastMCP Horizon

FastMCP Cloud is now presented as Prefect Horizon in the current FastMCP documentation.

1. Sign in to Horizon with GitHub.
2. Select the repository and the `main` branch.
3. Set the entrypoint to:

   ```text
   server.py:mcp
   ```

4. Add these environment variables:

   ```text
   DATABASE_URL=<remote database connection string>
   MCP_ALLOW_UNAUTHENTICATED=true
   ```

   `MCP_ALLOW_UNAUTHENTICATED=true` hands authentication to the platform (step 5). Without it, or a token in `MCP_AUTH_TOKEN` or `MCP_AUTH_TOKENS`, the server answers `401` to every request rather than serving the database unprotected. Set it only together with step 5.

5. Enable authentication.
6. Deploy the server.
7. Use the generated `/mcp` URL with an MCP client or Inspector.

The platform installs dependencies from `pyproject.toml`. The `if __name__ == "__main__"` block is used for local execution and is not required by the hosted entrypoint.

## VM or EC2

On a machine you run yourself, the server checks bearer tokens. Each person or service gets a named token, and each token group logs in to the database as its own user, so the database's grants decide what each group can read.

### 1. Create one database user per group

PostgreSQL:

```sql
-- dbx_readonly is the DATABASE_URL login: tokens with no group use it.
CREATE ROLE dbx_readonly LOGIN PASSWORD '<password>';
CREATE ROLE dbx_analyst LOGIN PASSWORD '<password>';
CREATE ROLE dbx_finance LOGIN PASSWORD '<password>';
GRANT CONNECT ON DATABASE app TO dbx_readonly, dbx_analyst, dbx_finance;
GRANT USAGE ON SCHEMA public TO dbx_readonly, dbx_analyst, dbx_finance;
GRANT SELECT ON products TO dbx_readonly;
GRANT SELECT ON orders, products TO dbx_analyst;
GRANT SELECT ON orders, products, invoices, salaries TO dbx_finance;

-- Optional: some columns only
GRANT SELECT (id, name, email) ON users TO dbx_analyst;

-- Optional: some rows only
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
CREATE POLICY analyst_eu ON orders FOR SELECT TO dbx_analyst USING (region = 'EU');
CREATE POLICY finance_all ON orders FOR SELECT TO dbx_finance USING (true);
```

Once row-level security is on, a login with no policy on the table sees no rows and gets no error, so the table looks empty. Give every login that can read the table a policy, including the `DATABASE_URL` one if it has a grant there.

MySQL:

```sql
CREATE USER 'dbx_readonly'@'%' IDENTIFIED BY '<password>';
CREATE USER 'dbx_analyst'@'%' IDENTIFIED BY '<password>';
CREATE USER 'dbx_finance'@'%' IDENTIFIED BY '<password>';
GRANT SELECT ON app.products TO 'dbx_readonly'@'%';
GRANT SELECT ON app.orders TO 'dbx_analyst'@'%';
GRANT SELECT ON app.products TO 'dbx_analyst'@'%';
GRANT SELECT ON app.* TO 'dbx_finance'@'%';
GRANT SELECT (id, name, email) ON app.users TO 'dbx_analyst'@'%';
```

MySQL has no row-level security. To limit rows, grant a view such as `CREATE VIEW app.orders_eu AS SELECT * FROM app.orders WHERE region = 'EU'` and not the table behind it.

Make `DATABASE_URL` the narrowest login, because every token without a group, and stdio, uses it.

With a column grant, `explore_schema(table_name=..., include_sample_data=True)` fails on that table with `permission denied`, because the sample is `SELECT *`. Ask for the table's detail without sample data, or query the granted columns with `execute_query`.

Grant `SELECT` only. Never use the table owner, a superuser or a role with `BYPASSRLS`: row-level security does not apply to them. On PostgreSQL, `explore_schema` still lists a table a group cannot read, because every user can read the catalog; the rows stay protected. Move a table to a schema the group has no `USAGE` on to hide its name too.

### 2. Configure the server

Generate one token per person or service:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put the configuration in `/etc/db-explorer.env`, readable only by the service user (`chmod 600`):

```text
MCP_TRANSPORT=streamable-http
MCP_HOST=127.0.0.1
MCP_PORT=8000
MCP_AUTH_TOKENS=alice:analyst:<token>,fran:finance:<token>,ci:<token>
DATABASE_URL=postgresql+psycopg2://dbx_readonly:<password>@db.internal:5432/app
DATABASE_URL_ANALYST=postgresql+psycopg2://dbx_analyst:<password>@db.internal:5432/app
DATABASE_URL_FINANCE=postgresql+psycopg2://dbx_finance:<password>@db.internal:5432/app
```

`DATABASE_URL` serves tokens with no group (`ci` above). URL-encode a password that contains `@ : / #`, for example `@` as `%40`.

### 3. Run it as a service

Give the service its own user, and let it own the project folder: `uv run` creates `.venv` there and keeps a cache in the user's home.

```bash
sudo useradd --system --create-home --home-dir /var/lib/dbx dbx
sudo git clone https://github.com/<you>/DB-Explorer-MCP.git /opt/db-explorer
sudo chown -R dbx:dbx /opt/db-explorer
sudo -u dbx /usr/local/bin/uv sync --directory /opt/db-explorer
```

`/etc/systemd/system/db-explorer.service`:

```ini
[Unit]
Description=DB Explorer MCP
After=network-online.target

[Service]
User=dbx
WorkingDirectory=/opt/db-explorer
EnvironmentFile=/etc/db-explorer.env
ExecStart=/usr/local/bin/uv run server.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now db-explorer
journalctl -u db-explorer -f    # audit lines: execute_query ok ... user=alice group=analyst
```

### 4. Put HTTPS in front

A bearer token sent over plain HTTP can be read by anyone on the network path. Bind the server to `127.0.0.1` as above, and let a reverse proxy terminate TLS. With Caddy, which obtains a certificate itself, `/etc/caddy/Caddyfile` is:

```text
dbx.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Open only ports 80 and 443 in the security group or firewall, never 8000.

### 5. Connect a client

Each person keeps their own token in an environment variable, so a shared `.mcp.json` holds no secret:

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

### Rotate or revoke a token

Tokens are read at startup. To rotate one, add the new token under a new name, restart, switch the client to it, then remove the old entry and restart again. To revoke one, remove its entry and restart.

## Local HTTP check

To run the same HTTP transport locally:

```powershell
$env:MCP_TRANSPORT = "streamable-http"
$env:MCP_HOST = "127.0.0.1"
$env:MCP_PORT = "8000"
uv run server.py
```

Use an MCP client against the platform-specific MCP endpoint. Do not expose an unauthenticated database server publicly.
