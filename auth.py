"""Bearer-token authentication for the HTTP transports.

stdio needs none: the client owns the process and there is no network surface. Any
HTTP transport publishes SELECT on the configured database to whoever holds the
URL, so it fails closed instead.

Each token has a name, written to the audit line, and optionally a group, which
picks the database login its calls run as (see server.current_engine). The
database's own grants for that login then decide what the caller can read.

FastMCP applies the provider only to its HTTP app, so stdio ignores whatever is
returned here. That is why one is returned even when MCP_TRANSPORT is stdio: a host
that loads server.py:mcp and serves it over HTTP (Horizon, `fastmcp run
--transport http`) never sets MCP_TRANSPORT, and would otherwise serve with none.
"""

from __future__ import annotations

import hmac
import re
import sys
from typing import NamedTuple

from fastmcp.server.auth import AccessToken, AuthProvider, TokenVerifier


# Short enough to type, long enough that guessing is not a strategy.
MINIMUM_TOKEN_LENGTH = 32

# The name a lone MCP_AUTH_TOKEN is audited under.
DEFAULT_TOKEN_NAME = "default"

_NAME = re.compile(r"[\w.@-]+")
# A group names the DATABASE_URL_<GROUP> variable, so it must be valid in one.
_GROUP = re.compile(r"[a-z0-9_]+")

_GENERATE_HINT = (
    'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
)


class TokenEntry(NamedTuple):
    name: str
    group: str | None
    token: str


def _check_token(token: str, owner: str) -> None:
    if len(token) < MINIMUM_TOKEN_LENGTH:
        raise RuntimeError(
            f"{owner} must be at least {MINIMUM_TOKEN_LENGTH} characters. {_GENERATE_HINT}"
        )


def parse_token_entries(raw: str) -> list[TokenEntry]:
    """Parse MCP_AUTH_TOKENS: comma-separated name:token or name:group:token.

    Errors name the entry by position only: in an entry written in the wrong
    order, the "name" or "group" is the token, and errors land in deploy logs.
    """
    entries: list[TokenEntry] = []
    for position, item in enumerate(raw.split(","), start=1):
        parts = [part.strip() for part in item.split(":")]
        if parts == [""]:
            continue
        label = f"MCP_AUTH_TOKENS entry {position}"
        if len(parts) == 2:
            name, token = parts
            group = None
        elif len(parts) == 3:
            name, group, token = parts
        else:
            raise RuntimeError(f"{label} must be name:token or name:group:token")
        if not _NAME.fullmatch(name):
            raise RuntimeError(
                f"{label} needs a name of letters, digits, '_', '.', '@' or '-'"
            )
        if group is not None and not _GROUP.fullmatch(group):
            raise RuntimeError(
                f"{label} needs a group of lowercase letters, digits and '_'"
            )
        _check_token(token, f"The token in {label}")
        entries.append(TokenEntry(name, group, token))

    names = [entry.name for entry in entries]
    if len(set(names)) != len(names):
        raise RuntimeError("MCP_AUTH_TOKENS names each token once; a name repeats")
    tokens = [entry.token for entry in entries]
    if len(set(tokens)) != len(tokens):
        # One token under two names would be audited as whichever matched last.
        raise RuntimeError("MCP_AUTH_TOKENS gives the same token two names")
    return entries


def token_entries(token: str, tokens: str) -> list[TokenEntry]:
    """Return the configured tokens from MCP_AUTH_TOKEN or MCP_AUTH_TOKENS."""
    if token and tokens:
        raise RuntimeError(
            "Set MCP_AUTH_TOKEN or MCP_AUTH_TOKENS, not both. To keep the single "
            f"token, add it to MCP_AUTH_TOKENS as {DEFAULT_TOKEN_NAME}:<token>."
        )
    if token:
        _check_token(token, "MCP_AUTH_TOKEN")
        return [TokenEntry(DEFAULT_TOKEN_NAME, None, token)]
    return parse_token_entries(tokens)


class NamedTokenVerifier(TokenVerifier):
    """Verify a bearer token against the configured tokens, in constant time."""

    def __init__(self, entries: list[TokenEntry], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._entries = [(entry, entry.token.encode()) for entry in entries]

    async def verify_token(self, token: str) -> AccessToken | None:
        presented = token.encode()
        match = None
        # compare_digest rather than ==, and every entry rather than stopping at
        # the first match, so response timing reveals neither a token's
        # characters nor which entry it matched.
        for entry, expected in self._entries:
            if hmac.compare_digest(presented, expected):
                match = entry
        if match is None:
            return None
        return AccessToken(
            token=token,
            client_id=match.name,
            scopes=[],
            claims={"group": match.group},
        )


class RejectEveryToken(TokenVerifier):
    """Refuse every HTTP request: no token was configured to check against."""

    async def verify_token(self, token: str) -> AccessToken | None:
        return None


def build_auth_provider(
    transport: str,
    entries: list[TokenEntry],
    allow_unauthenticated: bool,
) -> AuthProvider | None:
    """Return the auth provider for a transport, refusing to expose HTTP unguarded."""
    if entries:
        return NamedTokenVerifier(entries)

    if allow_unauthenticated:
        if transport == "stdio":
            return None
        print(
            f"WARNING: serving {transport} with no authentication. Every client that "
            "can reach this port has SELECT on the configured database.",
            file=sys.stderr,
        )
        return None

    if transport == "stdio":
        # Local stdio runs as before. An HTTP host that imported server.py:mcp
        # answers 401 until a token or MCP_ALLOW_UNAUTHENTICATED is set.
        return RejectEveryToken()

    raise RuntimeError(
        f"MCP_AUTH_TOKEN or MCP_AUTH_TOKENS is required when MCP_TRANSPORT is "
        f"{transport!r}, because an HTTP endpoint exposes the database to anyone who "
        f"can reach it. {_GENERATE_HINT}\n"
        "To serve without authentication anyway (trusted network only), set "
        "MCP_ALLOW_UNAUTHENTICATED=true."
    )
