import asyncio

import pytest

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from starlette.testclient import TestClient

from auth import (
    DEFAULT_TOKEN_NAME,
    MINIMUM_TOKEN_LENGTH,
    NamedTokenVerifier,
    RejectEveryToken,
    TokenEntry,
    build_auth_provider,
    parse_token_entries,
    token_entries,
)


VALID_TOKEN = "t" * MINIMUM_TOKEN_LENGTH
OTHER_TOKEN = "o" * MINIMUM_TOKEN_LENGTH
ENTRY = TokenEntry(DEFAULT_TOKEN_NAME, None, VALID_TOKEN)


def _call_tool(server: FastMCP, tool: str, token: str | None):
    """POST one tools/call over HTTP, as a remote client would."""
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": {}},
    }
    app = server.http_app(stateless_http=True, json_response=True)
    with TestClient(app) as client:
        return client.post("/mcp", json=request, headers=headers)


def test_stdio_without_a_token_refuses_http_requests():
    # stdio ignores the provider. A host that loads server.py:mcp and serves it
    # over HTTP never sets MCP_TRANSPORT, so it must not come up unguarded.
    assert isinstance(build_auth_provider("stdio", [], False), RejectEveryToken)


def test_stdio_with_a_token_checks_it_when_served_over_http():
    provider = build_auth_provider("stdio", [ENTRY], False)

    assert isinstance(provider, NamedTokenVerifier)


def test_stdio_opt_out_serves_unauthenticated():
    assert build_auth_provider("stdio", [], True) is None


def test_a_hosted_http_app_without_a_token_answers_401():
    server = FastMCP("hosted", auth=build_auth_provider("stdio", [], False))

    assert _call_tool(server, "anything", VALID_TOKEN).status_code == 401


def test_http_without_a_token_refuses_to_start():
    with pytest.raises(RuntimeError, match="MCP_AUTH_TOKEN or MCP_AUTH_TOKENS is required"):
        build_auth_provider("streamable-http", [], False)


def test_http_with_a_token_returns_a_verifier():
    provider = build_auth_provider("streamable-http", [ENTRY], False)

    assert isinstance(provider, NamedTokenVerifier)


def test_unauthenticated_http_requires_an_explicit_opt_in():
    assert build_auth_provider("streamable-http", [], True) is None


def test_a_single_token_is_audited_under_the_default_name():
    assert token_entries(VALID_TOKEN, "") == [ENTRY]


def test_no_token_configured_means_no_entries():
    assert token_entries("", "") == []


def test_setting_both_token_variables_is_refused():
    with pytest.raises(RuntimeError, match="not both"):
        token_entries(VALID_TOKEN, f"alice:{OTHER_TOKEN}")


def test_short_single_token_is_rejected():
    with pytest.raises(RuntimeError, match="at least"):
        token_entries("short", "")


def test_named_tokens_parse_with_and_without_a_group():
    raw = f" alice:analyst:{VALID_TOKEN} , ci-bot:{OTHER_TOKEN},"

    assert parse_token_entries(raw) == [
        TokenEntry("alice", "analyst", VALID_TOKEN),
        TokenEntry("ci-bot", None, OTHER_TOKEN),
    ]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (f"alice:a:b:{VALID_TOKEN}", "must be name:token or name:group:token"),
        (VALID_TOKEN, "must be name:token or name:group:token"),
        (f":{VALID_TOKEN}", "needs a name"),
        (f"alice:Analyst:{VALID_TOKEN}", "lowercase"),
        (f"alice:analyst-team:{VALID_TOKEN}", "lowercase"),
        ("alice:short", "at least"),
        # Written in the wrong order: the token sits where the name or group goes.
        (f"{VALID_TOKEN}:alice", "at least"),
        (f"alice:{VALID_TOKEN.upper()}:analyst", "lowercase"),
        (f"alice:{VALID_TOKEN},alice:{OTHER_TOKEN}", "a name repeats"),
        (f"alice:{VALID_TOKEN},bob:{VALID_TOKEN}", "same token two names"),
    ],
    ids=[
        "four_parts",
        "no_name",
        "empty_name",
        "uppercase_group",
        "dash_in_group",
        "short_token",
        "token_in_name_slot",
        "token_in_group_slot",
        "repeated_name",
        "repeated_token",
    ],
)
def test_bad_token_entries_stop_startup_without_echoing_the_token(raw, message):
    with pytest.raises(RuntimeError, match=message) as raised:
        parse_token_entries(raw)

    # The error lands in a deployment log; a token printed there is a leaked token.
    message = str(raised.value).lower()
    assert VALID_TOKEN not in message
    assert OTHER_TOKEN not in message


def test_verifier_names_the_caller_and_their_group():
    verifier = NamedTokenVerifier(
        [TokenEntry("alice", "analyst", VALID_TOKEN), TokenEntry("ci", None, OTHER_TOKEN)]
    )

    alice = asyncio.run(verifier.verify_token(VALID_TOKEN))
    ci = asyncio.run(verifier.verify_token(OTHER_TOKEN))

    assert (alice.client_id, alice.claims) == ("alice", {"group": "analyst"})
    assert (ci.client_id, ci.claims) == ("ci", {"group": None})


@pytest.mark.parametrize(
    "wrong",
    ["", "x" * MINIMUM_TOKEN_LENGTH, VALID_TOKEN + "x", "tokén"],
)
def test_verifier_rejects_anything_else(wrong):
    verifier = NamedTokenVerifier([ENTRY])

    assert asyncio.run(verifier.verify_token(wrong)) is None


def test_reject_every_token_rejects_any_token():
    assert asyncio.run(RejectEveryToken().verify_token(VALID_TOKEN)) is None


def test_a_tool_sees_the_caller_its_token_names():
    # End to end over HTTP: the group must survive from the verifier to the tool,
    # since server.current_engine picks the database login from it.
    server = FastMCP(
        "named",
        auth=NamedTokenVerifier([TokenEntry("alice", "analyst", VALID_TOKEN)]),
    )

    @server.tool
    def caller() -> str:
        token = get_access_token()
        return f"{token.client_id}/{token.claims['group']}"

    response = _call_tool(server, "caller", VALID_TOKEN)
    assert response.status_code == 200
    assert response.json()["result"]["content"][0]["text"] == "alice/analyst"

    assert _call_tool(server, "caller", OTHER_TOKEN).status_code == 401
    assert _call_tool(server, "caller", None).status_code == 401
