"""Browser-based MCP clients can reach /mcp (CORS)."""
import asyncio

import server


def send_through_login_check(method, headers=(), path="/mcp"):
    sent, reached = [], []

    async def inner_app(scope, receive, send):
        reached.append(scope)
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"mcp-session-id", b"abc")]})
        await send({"type": "http.response.body", "body": b"{}"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": path, "method": method, "headers": list(headers),
             "client": ("203.0.113.7", 5000)}
    asyncio.run(server.LoginCheck(inner_app)(scope, receive, send))
    return sent[0]["status"], dict(sent[0]["headers"]), reached


def test_preflight_is_answered_without_an_api_key(keys):
    status, headers, reached = send_through_login_check(
        "OPTIONS", [(b"origin", b"https://some-app.example"),
                    (b"access-control-request-method", b"POST")])
    assert status == 204 and reached == []  # answered here, never reaches the tools
    assert headers[b"access-control-allow-origin"] == b"*"
    assert headers[b"access-control-allow-methods"] == b"GET, POST, DELETE, OPTIONS"
    assert headers[b"access-control-allow-headers"] == \
        b"authorization, content-type, mcp-session-id, mcp-protocol-version"
    assert headers[b"access-control-expose-headers"] == b"mcp-session-id"


def test_normal_mcp_answers_carry_the_cors_headers_too(devs):
    status, headers, reached = send_through_login_check(
        "POST", [(b"authorization", f"Bearer {devs['acme/trader']}".encode())])
    assert status == 200 and len(reached) == 1
    assert headers[b"access-control-allow-origin"] == b"*"
    assert headers[b"access-control-expose-headers"] == b"mcp-session-id"
    assert headers[b"mcp-session-id"] == b"abc"


def test_refusals_carry_them_so_the_browser_can_read_the_error(keys):
    status, headers, reached = send_through_login_check("POST")  # no API key
    assert status == 401 and reached == []
    assert headers[b"access-control-allow-origin"] == b"*"


def test_other_addresses_are_unchanged(keys):
    status, headers, _ = send_through_login_check("OPTIONS", path="/other")
    assert status == 401 and b"access-control-allow-origin" not in headers
