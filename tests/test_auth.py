import asyncio

import pytest

from auth import MINIMUM_TOKEN_LENGTH, SharedSecretVerifier, build_auth_provider


VALID_TOKEN = "t" * MINIMUM_TOKEN_LENGTH


def test_stdio_needs_no_auth_provider():
    # The client owns the process; there is no network surface to guard.
    assert build_auth_provider("stdio", "", False) is None


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
