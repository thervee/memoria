"""Memoria's tools never touch the database on the event loop's thread.

The server answers every request on one event loop. A database call made on it holds
up every other request until it finishes, so all of them run in worker threads instead.
These tests call every tool over MCP, like a real agent, and record any database call
made on the event loop's thread.
"""
import asyncio
import threading
import time
import traceback

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

import billing
import db
import memoria as m
import server
from conftest import free_port
from memoria_client import Memoria
from test_billing import UNIT, chain, on, transfer, tx_hash  # noqa: F401 (fixtures)


@pytest.fixture
def on_the_loop(monkeypatch):
    """Every database call made on a thread that runs an event loop, with where it came from."""
    seen = []
    real = db.connection

    def watched():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # a worker thread: fine
        else:
            seen.append("".join(traceback.format_stack(limit=8)))
        return real()
    monkeypatch.setattr(db, "connection", watched)
    # The background worker (Cognee bookkeeping) isn't a tool: keep it out of this test.
    monkeypatch.setattr(m, "_kick", lambda: None)
    return seen


@pytest.fixture
def every_tool_url(on):
    """A Memoria server offering every tool, the payment tools included."""
    import uvicorn
    from mcp.server.mcpserver import MCPServer
    mcp = MCPServer("memoria-test")
    for tool in (server.save_memory, server.find_memory, server.list_memories,
                 server.record_outcome, server.get_outcomes, server.update_memory,
                 server.share_memory, server.delete_memory, server.export_memories,
                 server.restore_memories):
        mcp.tool()(tool)
    server.add_billing_tools(mcp)
    port = free_port()
    web = uvicorn.Server(uvicorn.Config(server.LoginCheck(mcp.streamable_http_app(
        host="127.0.0.1")), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=web.run, daemon=True)
    thread.start()
    while not web.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    web.should_exit = True
    thread.join(timeout=10)


def test_no_tool_touches_the_database_on_the_event_loop(on, chain, every_tool_url, on_the_loop):
    wallet = Account.create()

    async def every_tool():
        async with Memoria(api_key=on["acme/trader"], url=every_tool_url) as mem:
            saved = await mem.save("Acme renews March 1.", reason="email", topic="customers")
            await mem.find("When does Acme renew?")
            await mem.list()
            await mem.record_outcome(saved["memory_id"], "success")
            await mem.outcomes(saved["memory_id"])
            await mem.update(saved["memory_id"], "Acme renews March 2.", reason="moved")
            await mem.share(saved["memory_id"], "shared", ["analyst"])
            export = await mem.export()
            await mem.restore(export)
            message = (await mem.wallet_challenge())["message"]
            signature = Account.sign_message(encode_defunct(text=message),
                                             wallet.key).signature.hex()
            await mem.link_wallet(wallet.address, signature)
            chain.add(tx_hash(40), [transfer(wallet.address, amount=UNIT)])
            await mem.claim_payment(tx_hash(40))
            await mem.balance()
            await mem.delete(saved["memory_id"])
    asyncio.run(every_tool())
    assert on_the_loop == [], "database calls on the event loop:\n\n" + "\n\n".join(on_the_loop)


def test_a_balance_email_still_goes_out_in_the_background(on, monkeypatch):
    """charge() runs in a worker thread now; its "balance low" email must still be sent
    on the event loop, without making the tool call wait for it."""
    sent = threading.Event()
    release = asyncio.Event()

    async def slow_alert(account, kind, left):
        await release.wait()  # an email that takes as long as we like
        sent.set()
    monkeypatch.setattr(billing, "_send_alert", slow_alert)

    async def scenario():
        await billing.in_thread(billing.charge, "acme", "trader", "find_memory")  # starter credits
        db.execute("UPDATE billing SET actions = 1 WHERE account = 'acme'")
        await billing.in_thread(billing.charge, "acme", "trader", "find_memory")  # -> 0: email
        assert not sent.is_set()  # the charge didn't wait for the email
        release.set()
        for _ in range(100):
            if sent.is_set():
                break
            await asyncio.sleep(0.01)
    asyncio.run(scenario())
    assert sent.is_set()
