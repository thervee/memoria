"""A save or update that is cancelled after its write is still charged.

The database write runs in a worker thread and finishes even when the request is
cancelled, so giving the action back then would make the save free. Only a save or
update that Memoria refused (nothing written) is given back.
"""
import asyncio
import threading

import pytest

import db
import memoria as m
import server
from test_billing import actions, chain, on  # noqa: F401 (fixtures)


def cancel_after_write(monkeypatch, db_call, tool, work):
    """Run `work` through _charged, and cancel the request just after `db_call` wrote.
    Returns the balance right after the charge and the balance at the end."""
    written, release = threading.Event(), threading.Event()
    real = getattr(db, db_call)

    def write_then_wait(*args):
        result = real(*args)  # the memory really is written...
        written.set()
        release.wait(10)      # ...and the request is cancelled before it hears back
        return result
    monkeypatch.setattr(db, db_call, write_then_wait)

    async def scenario():
        task = asyncio.create_task(server._charged("acme", "trader", tool, work))
        await asyncio.get_running_loop().run_in_executor(None, written.wait, 10)
        charged = actions()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return charged
    charged = asyncio.run(scenario())
    return charged, actions()


def test_a_save_cancelled_after_the_write_is_still_charged(on, monkeypatch):
    def work():
        return m.save_memory("trader", "Acme renews on 1 March.", "contract", "customers",
                             account="acme")
    charged, end = cancel_after_write(monkeypatch, "add_memory", "save_memory", work)
    assert end == charged  # not given back
    saved = db.query("SELECT note FROM memories WHERE account = 'acme'")
    assert [row["note"] for row in saved] == ["Acme renews on 1 March."]


def test_an_update_cancelled_after_the_write_is_still_charged(on, monkeypatch):
    mid = asyncio.run(m.save_memory("trader", "Acme renews on 1 March.", "contract",
                                    "customers", account="acme"))["memory_id"]

    def work():
        return m.update_memory("trader", mid, "Acme renews on 2 March.", "moved",
                               account="acme")
    charged, end = cancel_after_write(monkeypatch, "put_memory", "update_memory", work)
    assert end == charged  # not given back
    assert db.get_memory(mid)["note"] == "Acme renews on 2 March."


def test_a_refused_save_is_still_given_back(on):
    async def scenario():
        await server._charged("acme", "trader", "find_memory", lambda: m.find_memory(
            "trader", "anything", account="acme"))  # starts the account's billing
        before = actions()
        with pytest.raises(server.ToolError, match="note can't be empty"):
            await server._charged("acme", "trader", "save_memory", lambda: m.save_memory(
                "trader", "  ", "r", "t", account="acme"))
        return before
    before = asyncio.run(scenario())
    assert actions() == before  # nothing was written, so the action came back
