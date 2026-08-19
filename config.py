import os


def _resolve_database_url() -> str:
    """Return the configured database URL, allowing sqlite only for local stdio."""
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    if os.getenv("MCP_TRANSPORT", "stdio") == "stdio":
        return "sqlite:///sample.db"
    raise RuntimeError("DATABASE_URL must be set when serving over HTTP")


DATABASE_URL = _resolve_database_url()
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
