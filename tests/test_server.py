"""The login check in front of the agents' server (server.py)."""
import asyncio
import json

import identity
import server


def call(headers):
    """Send one request through LoginCheck. Returns (what the inner app saw, responses sent)."""
    seen, sent = {}, []

    async def inner_app(scope, receive, send):
        seen["headers"] = dict(scope["headers"])

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": "/mcp", "method": "POST", "headers": headers}
    asyncio.run(server.LoginCheck(inner_app)(scope, receive, send))
    return seen.get("headers"), sent


def status(sent):
    return sent[0]["status"] if sent else None


def test_request_without_pass_is_refused(keys):
    seen, sent = call([])
    assert seen is None and status(sent) == 401
    assert json.loads(sent[1]["body"])["error"] == "unauthorized"


def test_request_with_bad_pass_is_refused(keys):
    seen, sent = call([(b"authorization", b"Bearer v1.alice.123.nonsense")])
    assert seen is None and status(sent) == 401


def test_valid_pass_lets_the_proven_agent_through(keys):
    token = identity.make_token("alice")
    seen, sent = call([(b"authorization", f"Bearer {token}".encode())])
    assert sent == [] and seen[b"x-memoria-agent"] == b"alice"


def test_agent_cant_claim_another_identity_with_a_header(keys):
    token = identity.make_token("alice")
    seen, _ = call([(b"authorization", f"Bearer {token}".encode()),
                    (b"x-memoria-agent", b"bob"), (b"X-Memoria-Agent", b"carol")])
    claimed = [v for k, v in seen.items() if k.lower() == b"x-memoria-agent"]
    assert claimed == [b"alice"]


def test_claimed_identity_without_pass_is_still_refused(keys):
    seen, sent = call([(b"x-memoria-agent", b"alice")])
    assert seen is None and status(sent) == 401


def test_all_ten_tools_are_offered():
    names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert names == {"save_memory", "find_memory", "list_memories", "update_memory",
                     "share_memory", "delete_memory", "record_outcome", "get_outcomes",
                     "export_memories", "restore_memories"}
