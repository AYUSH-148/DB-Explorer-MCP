import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")
load_dotenv()


def _resolve_database_url() -> str:
    """Return the configured database URL, allowing sqlite only for local stdio."""
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    if os.getenv("MCP_TRANSPORT", "stdio") == "stdio":
        return "sqlite:///sample.db"
    raise RuntimeError("DATABASE_URL must be set when serving over HTTP")


def _resolve_query_timeout() -> int:
    raw = os.getenv("QUERY_TIMEOUT_SECONDS", "15")
    try:
        seconds = int(raw)
    except ValueError as error:
        raise RuntimeError(
            f"QUERY_TIMEOUT_SECONDS must be an integer. Got: {raw!r}"
        ) from error
    if seconds < 1:
        raise RuntimeError("QUERY_TIMEOUT_SECONDS must be greater than zero")
    return seconds


def _resolve_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


DATABASE_URL = _resolve_database_url()
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
QUERY_TIMEOUT_SECONDS = _resolve_query_timeout()
MCP_AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN", "").strip()
MCP_ALLOW_UNAUTHENTICATED = _resolve_flag("MCP_ALLOW_UNAUTHENTICATED")
