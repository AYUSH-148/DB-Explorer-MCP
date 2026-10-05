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


def _resolve_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer. Got: {raw!r}") from error
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}. Got: {value}")
    return value


def _resolve_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


DATABASE_URL = _resolve_database_url()
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))
QUERY_TIMEOUT_SECONDS = _resolve_int("QUERY_TIMEOUT_SECONDS", 15)
# Tool calls run on up to 40 worker threads, but each holds a connection only while
# it queries. A call that cannot get one within DB_POOL_TIMEOUT_SECONDS fails fast
# with server_busy instead of hanging for SQLAlchemy's default 30 seconds.
DB_POOL_SIZE = _resolve_int("DB_POOL_SIZE", 5)
DB_MAX_OVERFLOW = _resolve_int("DB_MAX_OVERFLOW", 10, minimum=0)
DB_POOL_TIMEOUT_SECONDS = _resolve_int("DB_POOL_TIMEOUT_SECONDS", 5)
MCP_AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN", "").strip()
MCP_ALLOW_UNAUTHENTICATED = _resolve_flag("MCP_ALLOW_UNAUTHENTICATED")
