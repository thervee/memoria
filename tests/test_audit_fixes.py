"""Four audit fixes: exports split into parts that fit a request, locked writes that hold
the lock until their thread finishes, origin marks that survive export and restore, and
text that can't be stored refused before anything is charged."""
import asyncio
import json
import threading

import pytest

import accounts
import billing
import db
import limits
import memoria as m
import server
from conftest import settle
from test_billing import actions, chain, on  # noqa: F401 (fixtures)
from test_export_restore import forged_export


def run(coro):
    return asyncio.run(coro)


def ctx(account, agent):
    class Ctx:  # what MCP hands a tool: the caller, as proven by its key
        headers = {server.AGENT_HEADER: agent, server.ACCOUNT_HEADER: account}
    return Ctx()


def copies(account, agent):
    return db.query("SELECT count(*) FROM memories WHERE account = ? AND agent_id = ?",
                    (account, agent))[0][0]


def writes_today(account):
    rows = db.query("SELECT writes FROM usage WHERE account = ?", (account,))
    return rows[0]["writes"] if rows else 0


# ---------- 1. an export is split into parts that each fit in one request ----------

def save_many(count, agent="trader", account="acme"):
    async def go():
        for n in range(count):
            await m.save_memory(agent, f"Memory {n}: Acme's renewal moved to {n % 28 + 1} March "
                                f"after the pricing call; check before quoting.",
                                "From the account manager's notes.", f"customers/acme-{n % 7}",
                                account=account, source=f"crm-note-{n}")
        await settle()
    run(go())


def test_300_memories_export_in_parts_and_all_come_back(on, monkeypatch):
    monkeypatch.setattr(accounts, "limits", lambda account: None)  # no account limits here
    save_many(300)
    export = run(m.export_memories("trader", account="acme"))
    parts = export["parts"]
    assert export["memory_count"] == 300 and export["part_count"] == len(parts) > 1
    for number, part in enumerate(parts, 1):
        assert (part["part"], part["parts"], part["export_id"]) == (
            number, len(parts), export["export_id"])
        assert m._json_size(part) <= m.PART_BYTES  # fits in a request, with room to spare
        assert len(part["memories"]) <= m.MAX_RESTORE
    assert sum(len(part["memories"]) for part in parts) == 300

    restored = 0
    for part in parts:  # each part restores on its own, into another account
        restored += run(m.restore_memories("analyst", part, account="globex"))["restored_count"]
    assert restored == 300 and copies("globex", "analyst") == 300
    # Restoring every part again adds nothing.
    again = sum(run(m.restore_memories("analyst", part, account="globex"))["restored_count"]
                for part in parts)
    assert again == 0 and copies("globex", "analyst") == 300


def test_the_largest_part_fits_in_a_real_request(on, memoria_url, monkeypatch):
    """Sent over MCP, as an object and as JSON text, a full part stays under the limit."""
    from memoria_client import Memoria
    monkeypatch.setattr(accounts, "limits", lambda account: None)
    save_many(120)
    parts = run(m.export_memories("trader", account="acme"))["parts"]
    biggest = max(parts, key=m._json_size)
    billing.adjust("globex", 100, "test")  # enough actions for a whole part
    assert len(json.dumps(json.dumps(biggest))) < limits.MAX_REQUEST_BYTES - 2_000

    async def scenario():
        async with Memoria(api_key=on["globex/analyst"], url=memoria_url) as analyst:
            first = await analyst.restore(biggest)
            assert first["restored_count"] == len(biggest["memories"])
            assert (await analyst.restore(json.dumps(biggest)))["restored_count"] == 0
    run(scenario())


def test_a_whole_small_export_can_still_be_restored_in_one_call(store):
    mid = run(m.save_memory("alice", "Acme renews on 1 March.", "contract", "customers"))
    run(settle())
    export = run(m.export_memories("alice"))
    run(m.delete_memory("alice", mid["memory_id"]))
    result = run(m.restore_memories("alice", export))
    assert result["restored_count"] == 1 and result["parts_restored"] == 1


def test_max_restore_matches_what_fits_in_a_request(store):
    """MAX_RESTORE is about the most memories, even the smallest ones, a request can hold."""
    mid = run(m.save_memory("alice", "x", "y", "z"))["memory_id"]
    smallest = one = run(m.export_memories("alice"))["parts"][0]["memories"][0]
    assert one["id"] == mid
    fit = limits.MAX_REQUEST_BYTES // m._json_size(smallest)
    assert fit <= m.MAX_RESTORE <= fit * 1.2
    too_many = {"format": m.EXPORT_FORMAT, "exported_by": {"account": "local", "agent_id": "x"},
                "memories": [{}] * (m.MAX_RESTORE + 1)}
    with pytest.raises(m.MemoriaError, match=f"{m.MAX_RESTORE} memories at most.*each part"):
        run(m.restore_memories("alice", too_many))


# ---------- 2. a cancelled write keeps the lock until its thread has finished ----------

def test_cancel_a_restore_mid_write_then_retry_makes_one_copy_and_one_charge(on, monkeypatch):
    run(m.save_memory("trader", "Acme renews on 1 March.", "contract", "customers",
                      account="acme"))
    part = run(m.export_memories("trader", account="acme"))["parts"][0]
    start = billing.balance("globex")["actions"]

    started, second, release = threading.Event(), threading.Event(), threading.Event()
    real = db.add_memory

    def slow_add(mem):
        if not release.is_set():  # writes wait here, before writing anything
            (second if started.is_set() else started).set()
            release.wait(10)
        return real(mem)
    monkeypatch.setattr(db, "add_memory", slow_add)

    async def scenario():
        loop = asyncio.get_running_loop()
        first = asyncio.create_task(server.restore_memories(part, ctx("globex", "analyst")))
        await loop.run_in_executor(None, started.wait, 10)
        first.cancel()  # the client gives up mid-write...
        retry = asyncio.create_task(server.restore_memories(part, ctx("globex", "analyst")))
        # ...and retries at once, while the first write is still going. The retry must
        # wait for the first write's lock, so it never reaches its own write meanwhile.
        reached = await loop.run_in_executor(None, second.wait, 1.0)
        assert not reached
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        return await retry
    retried = run(scenario())
    assert retried["restored_count"] == 0 and len(retried["skipped_already_here"]) == 1
    assert copies("globex", "analyst") == 1  # exactly one copy
    assert actions("globex") == start - 1    # and exactly one charge


def test_a_cancelled_save_still_finishes_before_the_next_write_starts(store, monkeypatch):
    order = []
    started, second, release = threading.Event(), threading.Event(), threading.Event()
    real = db.add_memory

    def slow_add(mem):
        if mem["note"] == "First.":
            started.set()
            release.wait(10)
        else:
            second.set()
        order.append(mem["note"])
        return real(mem)
    monkeypatch.setattr(db, "add_memory", slow_add)

    async def scenario():
        first = asyncio.create_task(m.save_memory("alice", "First.", "r", "t"))
        await asyncio.get_running_loop().run_in_executor(None, started.wait, 10)
        first.cancel()
        then = asyncio.create_task(m.save_memory("alice", "Second.", "r", "t"))
        reached = await asyncio.get_running_loop().run_in_executor(None, second.wait, 1.0)
        assert not reached and order == []  # the second save waits for the first's lock
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await then
    run(scenario())
    assert order == ["First.", "Second."] and copies("local", "alice") == 2


# ---------- 3. origin marks survive export and restore ----------

def assert_unverified(mem):
    shown = m._public(mem)
    assert shown["verification"] == "NOT verified"
    assert shown["verification_detail"].startswith("unverified origin")
    assert shown["unverified_origin"]["signed_by"] == "evil/mallory"


def test_unverified_origin_survives_export_delete_restore_same_agent(store):
    first = run(m.restore_memories("alice", forged_export()))["restored"][0]["memory_id"]
    part = run(m.export_memories("alice"))["parts"][0]
    exported = part["memories"][0]
    assert exported["unverified_origin"]["signed_by"] == "evil/mallory"
    assert exported["restored_from"] == "mem-" + "ab" * 16
    run(m.delete_memory("alice", first))
    back = run(m.restore_memories("alice", part))
    assert back["restored_count"] == 1 and back["restored"][0]["memory_id"] == first
    assert_unverified(db.get_memory(first))


def test_unverified_origin_survives_restore_by_another_agent_and_account(on):
    first = run(m.restore_memories("trader", forged_export(), account="acme"))
    part = run(m.export_memories("trader", account="acme"))["parts"][0]
    # another agent of the same account (once the first copy is gone: an account never
    # gets a memory it already has twice)
    run(m.delete_memory("trader", first["restored"][0]["memory_id"], account="acme"))
    other = run(m.restore_memories("analyst", part, account="acme"))["restored"][0]
    assert_unverified(db.get_memory(other["memory_id"]))
    # another account
    elsewhere = run(m.restore_memories("analyst", part, account="globex"))["restored"][0]
    mem = db.get_memory(elsewhere["memory_id"])
    assert mem["account"] == "globex"
    assert_unverified(mem)


@pytest.mark.parametrize("field, value", [
    ("unverified_origin", "junk"),
    ("unverified_origin", {"signed_by": 7}),
    ("unverified_origin", {"signed_by": "x", "extra": "no"}),
    ("unverified_origin", {"signed_by": "x" * 500}),
    ("restored_from", "mem-x"),
    ("restored_from", 12),
])
def test_origin_fields_must_have_the_right_shape(store, field, value):
    run(m.save_memory("alice", "Acme renews on 1 March.", "contract", "customers"))
    part = run(m.export_memories("alice"))["parts"][0]
    part["memories"][0][field] = value
    part["signature"] = m.identity.sign("alice", m._export_content(part))  # a signed file
    run(m.delete_memory("alice", part["memories"][0]["id"]))
    result = run(m.restore_memories("alice", part))
    assert result["restored_count"] == 0
    assert field in result["refused"][0]["why"]


# ---------- 4. text that can't be stored never costs an action or a daily write ----------

LONE = "\ud800"  # a lone surrogate: not valid in UTF-8


@pytest.mark.parametrize("change", [
    {"note": f"Acme {LONE}"}, {"reason": LONE + "x"}, {"topic": f"t{LONE}"},
    {"source": f"crm{LONE}"}, {"visibility": "shared", "shared_with": [f"analyst{LONE}"]},
])
def test_a_save_with_unstorable_text_costs_nothing(on, change):
    billing.balance("acme")
    before = (actions("acme"), writes_today("acme"))
    args = {"note": "Acme renews.", "reason": "r", "topic": "t", **change}
    with pytest.raises(server.ToolError, match="can't be stored"):
        run(server.save_memory(ctx=ctx("acme", "trader"), **args))
    assert (actions("acme"), writes_today("acme")) == before
    assert copies("acme", "trader") == 0


@pytest.mark.parametrize("change, error", [
    ({"confidence": "certain"}, "confidence must be"),
    ({"new_note": f"Acme {LONE}"}, "can't be stored"),
    ({"topic": f"t{LONE}"}, "can't be stored"),
])
def test_a_refused_update_costs_nothing(on, change, error):
    mid = run(m.save_memory("trader", "Acme renews.", "r", "t", account="acme"))["memory_id"]
    billing.balance("acme")
    before = (actions("acme"), writes_today("acme"))
    args = {"memory_id": mid, "new_note": "Acme renews later.", "reason": "moved", **change}
    with pytest.raises(server.ToolError, match=error):
        run(server.update_memory(ctx=ctx("acme", "trader"), **args))
    assert (actions("acme"), writes_today("acme")) == before
    assert db.get_memory(mid)["note"] == "Acme renews."
