"""Connecting agents: the Python helper, the bridge and connect_agent.py.

These start a real Memoria server (with the fake Cognee) on a spare port and
talk to it the way a real agent would.
"""
import asyncio
import json
import time

import pytest
from mcp import types

import connect_agent
import identity
import memoria_bridge
import memoria_client
from conftest import free_port
from memoria_client import Memoria


def run(coro):
    return asyncio.run(coro)


# ---------- the Python helper ----------

def test_python_helper_does_everything(memoria_url):
    async def scenario():
        async with Memoria("alice", memoria_url) as mem:
            saved = await mem.save("Octopuses have three hearts.", reason="test", topic="test/sea",
                                   visibility="shared", shared_with=["bob"])
            assert saved["saved_by"] == "alice" and saved["verification"] == "verified"
            found = await mem.find("octopuses hearts")
            assert [x["memory_id"] for x in found] == [saved["memory_id"]]
            updated = await mem.update(saved["memory_id"], "Octopuses have blue blood.", "more")
            assert updated["times_updated"] == 1
        async with Memoria("bob", memoria_url) as mem:
            assert len(await mem.find("octopuses")) == 1
            with pytest.raises(RuntimeError, match="Only the agent that wrote"):
                await mem.delete(saved["memory_id"])
        async with Memoria("alice", memoria_url) as mem:
            assert (await mem.share(saved["memory_id"], "private"))["visibility"] == "private"
            assert await mem.delete(saved["memory_id"]) == {"deleted": saved["memory_id"]}
            assert await mem.find("octopuses") == []
    run(scenario())


def test_python_helper_says_clearly_when_memoria_is_not_running(keys):
    async def scenario():
        async with Memoria("alice", f"http://127.0.0.1:{free_port()}/mcp"):
            pass
    with pytest.raises(memoria_client.MemoriaNotRunning, match="start_memoria.bat"):
        run(scenario())


def test_python_helper_refuses_unregistered_agent(keys):
    with pytest.raises(identity.IdentityError, match="not registered"):
        run(Memoria("dave").__aenter__())


def test_login_pass_is_renewed_before_it_expires(keys, monkeypatch):
    auth = memoria_client.FreshPass("alice")
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    first = auth.token()
    assert auth.token() == first  # reused while fresh
    monkeypatch.setattr(time, "time", lambda: now + memoria_client.REFRESH_AFTER + 1)
    second = auth.token()
    assert second != first and identity.check_token(second) == "alice"


# ---------- the bridge ----------

def test_bridge_passes_tools_and_calls_through(memoria_url):
    list_tools, call_tool = memoria_bridge.make_handlers("alice", memoria_url)

    async def scenario():
        tools = await list_tools(None, None)
        assert {t.name for t in tools.tools} == {"save_memory", "find_memory", "update_memory",
                                                 "share_memory", "delete_memory"}
        result = await call_tool(None, types.CallToolRequestParams(
            name="save_memory",
            arguments={"note": "Owls hunt at night.", "reason": "test", "topic": "test/birds"}))
        saved = memoria_client._result(result)
        assert saved["saved_by"] == "alice"  # logged in as the bridge's agent
    run(scenario())


def test_bridge_reports_when_memoria_is_not_running(keys):
    list_tools, call_tool = memoria_bridge.make_handlers(
        "alice", f"http://127.0.0.1:{free_port()}/mcp")

    async def scenario():
        assert (await list_tools(None, None)).tools == []
        result = await call_tool(None, types.CallToolRequestParams(
            name="find_memory", arguments={"query": "x"}))
        assert result.is_error and "start_memoria.bat" in result.content[0].text
    run(scenario())


# ---------- connect_agent.py ----------

def test_connect_agent_registers_and_prints_setup(keys, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["connect_agent.py", "Dave"])
    connect_agent.main()
    out = capsys.readouterr().out
    assert "Registered 'dave'" in out and identity.public_key_of("dave")
    assert "claude mcp add memoria-dave -s user" in out
    start = out.index('{\n  "mcpServers"')
    config, _ = json.JSONDecoder().raw_decode(out[start:])
    assert config["mcpServers"]["memoria"]["args"][-1] == "dave"
    assert config["mcpServers"]["memoria"]["command"].endswith("python.exe")


def test_connect_agent_reuses_existing_registration(keys, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["connect_agent.py", "alice"])
    key_before = identity.public_key_of("alice")
    connect_agent.main()
    assert "already registered" in capsys.readouterr().out
    assert identity.public_key_of("alice") == key_before


def test_connect_agent_rejects_bad_id(keys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["connect_agent.py", "Not Valid!"])
    with pytest.raises(SystemExit, match="Can't connect"):
        connect_agent.main()
