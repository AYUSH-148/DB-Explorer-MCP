import asyncio

import pytest

from fastmcp import FastMCP
from starlette.testclient import TestClient

from auth import (
    MINIMUM_TOKEN_LENGTH,
    RejectEveryToken,
    SharedSecretVerifier,
    build_auth_provider,
)


VALID_TOKEN = "t" * MINIMUM_TOKEN_LENGTH


def test_stdio_without_a_token_refuses_http_requests():
    # stdio ignores the provider. A host that loads server.py:mcp and serves it
    # over HTTP never sets MCP_TRANSPORT, so it must not come up unguarded.
    assert isinstance(build_auth_provider("stdio", "", False), RejectEveryToken)


def test_stdio_with_a_token_checks_it_when_served_over_http():
    provider = build_auth_provider("stdio", VALID_TOKEN, False)

    assert isinstance(provider, SharedSecretVerifier)


def test_stdio_opt_out_serves_unauthenticated():
    assert build_auth_provider("stdio", "", True) is None


def test_a_hosted_http_app_without_a_token_answers_401():
    server = FastMCP("hosted", auth=build_auth_provider("stdio", "", False))
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }

    with TestClient(server.http_app()) as client:
        response = client.post(
            "/mcp",
            json=initialize,
            headers={"Accept": "application/json, text/event-stream"},
        )

    assert response.status_code == 401


def test_http_without_a_token_refuses_to_start():
    with pytest.raises(RuntimeError, match="MCP_AUTH_TOKEN is required"):
        build_auth_provider("streamable-http", "", False)


def test_http_with_a_token_returns_a_verifier():
    provider = build_auth_provider("streamable-http", VALID_TOKEN, False)

    assert isinstance(provider, SharedSecretVerifier)


def test_short_tokens_are_rejected():
    with pytest.raises(RuntimeError, match="at least"):
        build_auth_provider("streamable-http", "short", False)


def test_unauthenticated_http_requires_an_explicit_opt_in():
    assert build_auth_provider("streamable-http", "", True) is None


def test_verifier_accepts_the_configured_token():
    verifier = SharedSecretVerifier(VALID_TOKEN)

    access_token = asyncio.run(verifier.verify_token(VALID_TOKEN))

    assert access_token is not None
    assert access_token.client_id == "db-explorer"


@pytest.mark.parametrize(
    "wrong",
    ["", "x" * MINIMUM_TOKEN_LENGTH, VALID_TOKEN + "x", "tokén"],
)
def test_verifier_rejects_anything_else(wrong):
    verifier = SharedSecretVerifier(VALID_TOKEN)

    assert asyncio.run(verifier.verify_token(wrong)) is None


def test_reject_every_token_rejects_any_token():
    assert asyncio.run(RejectEveryToken().verify_token(VALID_TOKEN)) is None
