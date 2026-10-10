"""Bearer-token authentication for the HTTP transports.

stdio needs none: the client owns the process and there is no network surface. Any
HTTP transport publishes SELECT on the configured database to whoever holds the
URL, so it fails closed instead.

FastMCP applies the provider only to its HTTP app, so stdio ignores whatever is
returned here. That is why one is returned even when MCP_TRANSPORT is stdio: a host
that loads server.py:mcp and serves it over HTTP (Horizon, `fastmcp run
--transport http`) never sets MCP_TRANSPORT, and would otherwise serve with none.
"""

from __future__ import annotations

import hmac
import sys

from fastmcp.server.auth import AuthProvider, TokenVerifier
from mcp.server.auth.provider import AccessToken


# Short enough to type, long enough that guessing is not a strategy.
MINIMUM_TOKEN_LENGTH = 32


class SharedSecretVerifier(TokenVerifier):
    """Verify a bearer token against one shared secret, in constant time."""

    def __init__(self, token: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._token = token.encode()

    async def verify_token(self, token: str) -> AccessToken | None:
        # compare_digest rather than ==, so a wrong token cannot be recovered one
        # character at a time from response timing.
        if not hmac.compare_digest(token.encode(), self._token):
            return None
        return AccessToken(token=token, client_id="db-explorer", scopes=[])


class RejectEveryToken(TokenVerifier):
    """Refuse every HTTP request: no token was configured to check against."""

    async def verify_token(self, token: str) -> AccessToken | None:
        return None


def build_auth_provider(
    transport: str,
    token: str,
    allow_unauthenticated: bool,
) -> AuthProvider | None:
    """Return the auth provider for a transport, refusing to expose HTTP unguarded."""
    if token:
        if len(token) < MINIMUM_TOKEN_LENGTH:
            raise RuntimeError(
                f"MCP_AUTH_TOKEN must be at least {MINIMUM_TOKEN_LENGTH} characters. "
                "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )
        return SharedSecretVerifier(token)

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
        # answers 401 until MCP_AUTH_TOKEN or MCP_ALLOW_UNAUTHENTICATED is set.
        return RejectEveryToken()

    raise RuntimeError(
        f"MCP_AUTH_TOKEN is required when MCP_TRANSPORT is {transport!r}, because an "
        "HTTP endpoint exposes the database to anyone who can reach it. Generate a "
        'token with: python -c "import secrets; print(secrets.token_urlsafe(32))"\n'
        "To serve without authentication anyway (trusted network only), set "
        "MCP_ALLOW_UNAUTHENTICATED=true."
    )
