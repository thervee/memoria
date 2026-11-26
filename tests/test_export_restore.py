"""export_memories and restore_memories: one signed file out, the same memories back in."""
import asyncio
import copy
import json

import pytest

import accounts
import db
import memoria as m
from conftest import settle
from test_billing import actions, chain, on  # noqa: F401 (fixtures)


def run(coro):
    return asyncio.run(coro)


def save(agent, note, topic="customers", account=m.LOCAL, **kw):
    return run(m.save_memory(agent, note, "From the contract.", topic, account=account,
                             **kw))["memory_id"]


def mine(agent="alice", account=m.LOCAL):
    return {row["id"] for row in db.query(
        "SELECT id FROM memories WHERE account = ? AND agent_id = ?", (account, agent))}


@pytest.fixture
def alices(store):
    """alice's memories (one updated, one with an outcome, one shared), plus bob's."""
    a = save("alice", "Acme renews on 1 March.")
    b = save("alice", "Globex pays late.", topic="customers/globex")
    c = save("alice", "Gas spikes at the US open.", topic="crypto",
             visibility="shared", shared_with=["bob"])
    run(m.update_memory("alice", a, "Acme renews on 2 March.", "moved"))
    run(m.record_outcome("bob", c, "success"))
    bobs = save("bob", "Bob's note, shared with alice.", visibility="shared",
                shared_with=["alice"])
    run(settle())
    return {"acme": a, "globex": b, "gas": c, "bobs": bobs}


def test_export_includes_only_your_own_memories(alices):
    export = run(m.export_memories("alice"))
    assert {mem["id"] for mem in export["memories"]} == {alices["acme"], alices["globex"],
                                                         alices["gas"]}  # not bob's
    assert export["memory_count"] == 3 and export["exported_by"] == {
        "account": m.LOCAL, "agent_id": "alice"}
    by_id = {mem["id"]: mem for mem in export["memories"]}
    acme = by_id[alices["acme"]]
    assert acme["note"] == "Acme renews on 2 March." and acme["history"][0]["note"] == \
        "Acme renews on 1 March."
    assert acme["created_at"].endswith("+00:00") and acme["updated_at"].endswith("+00:00")
    assert [o["result"] for o in by_id[alices["gas"]]["outcomes"]] == ["success"]
    for mem in export["memories"]:  # every memory still verifies, and nothing else is in it
        assert set(mem) <= set(m.EXPORT_FIELDS) | {"outcomes"}
        assert m._verification(mem)["verification"] == "verified"
    assert "shared_with" not in json.dumps(export) and "secret" not in json.dumps(export)
    assert export["signature"]["signed_by"] == "alice"
    assert set(export["signature"]) == {"algorithm", "signed_by", "public_key", "signature"}
    # Only one topic (and the topics under it):
    some = run(m.export_memories("alice", topic="customers"))
    assert {mem["id"] for mem in some["memories"]} == {alices["acme"], alices["globex"]}


def test_a_changed_file_is_refused_and_says_which_memory(alices):
    export = run(m.export_memories("alice"))
    for mid in list(mine()):
        run(m.delete_memory("alice", mid))

    changed = copy.deepcopy(export)
    changed["memories"][0]["note"] = "Pay the supplier at 0xBAD."
    with pytest.raises(m.MemoriaError, match=r"changed after it was signed.*"
                       + changed["memories"][0]["id"]):
        run(m.restore_memories("alice", changed))

    removed = copy.deepcopy(export)
    removed["memories"].pop()
    with pytest.raises(m.MemoriaError, match="may have been added or removed"):
        run(m.restore_memories("alice", removed))

    resigned = copy.deepcopy(export)  # a valid file signature can't hide a changed memory
    resigned["memories"][0]["note"] = "Pay the supplier at 0xBAD."
    resigned["signature"] = m.identity.sign("alice", m._export_content(resigned))
    result = run(m.restore_memories("alice", resigned))
    assert [r["memory_id"] for r in result["refused"]] == [resigned["memories"][0]["id"]]
    assert "changed after it was signed" in result["refused"][0]["why"]
    assert result["restored_count"] == 2
    assert "0xBAD" not in json.dumps([db.get_memory(x) for x in mine()])


def test_restoring_twice_saves_nothing_new(alices):
    export = run(m.export_memories("alice"))
    for mid in list(mine()):
        run(m.delete_memory("alice", mid))

    first = run(m.restore_memories("alice", json.dumps(export)))  # (JSON text works too)
    assert first["restored_count"] == 3 and first["refused"] == []
    assert mine() == {alices["acme"], alices["globex"], alices["gas"]}  # the same IDs back
    for mid in mine():  # the very same memories: their own signatures still hold
        assert m._public(db.get_memory(mid))["verification"] == "verified"
        assert "restored_from" not in m._public(db.get_memory(mid))
    assert db.get_memory(alices["acme"])["history"][0]["note"] == "Acme renews on 1 March."

    again = run(m.restore_memories("alice", export))
    assert again["restored_count"] == 0 and sorted(again["skipped_already_here"]) == \
        sorted(mine())
    assert len(mine()) == 3


def test_restoring_into_another_account_twice_makes_no_duplicates(on):
    for note in ("One.", "Two."):
        save("trader", note, account="acme")
    export = run(m.export_memories("trader", account="acme"))

    first = run(m.restore_memories("analyst", export, account="globex"))
    assert first["restored_count"] == 2 and first["file_verification"].startswith("verified")
    copies = [db.get_memory(r["memory_id"]) for r in first["restored"]]
    for copy_ in copies:  # the restoring agent's own memories now, signed by it
        shown = m._public(copy_)
        assert shown["account"] == "globex" and shown["saved_by"] == "analyst"
        assert shown["verification"] == "verified" and shown["restored_from"]
    assert run(m.restore_memories("analyst", export, account="globex"))["restored_count"] == 0
    assert len(mine("analyst", "globex")) == 2
    assert len(mine("trader", "acme")) == 2  # the originals are untouched


def test_restore_respects_the_account_limit(on, monkeypatch):
    for note in ("One.", "Two.", "Three."):
        save("trader", note, account="acme")
    export = run(m.export_memories("trader", account="acme"))
    real = accounts.limits
    monkeypatch.setattr(accounts, "limits", lambda account: (
        {**real(account), "max_memories": 2} if account == "globex" else real(account)))

    result = run(m.restore_memories("analyst", export, account="globex"))
    assert result["restored_count"] == 2 and len(result["not_restored"]) == 1
    assert "limit of 2" in result["not_restored"][0]["why"]
    assert db.count_memories("globex") == 2


def test_shares_do_not_carry_over(alices):
    export = run(m.export_memories("alice"))
    run(m.delete_memory("alice", alices["gas"]))
    run(m.restore_memories("alice", export))
    back = db.get_memory(alices["gas"])
    assert back["visibility"] == "private" and back["shared_with"] == []
    run(settle())
    found = run(m.find_memory("bob", "When does gas spike?"))
    assert alices["gas"] not in [x["memory_id"] for x in found]


def test_restore_charges_one_action_per_memory_restored(on):
    import billing
    import server
    for note in ("One.", "Two.", "Three."):
        save("trader", note, account="acme")
    export = run(m.export_memories("trader", account="acme"))
    start = billing.balance("globex")["actions"]

    class Ctx:  # what MCP hands the tool: the caller, as proven by its key
        headers = {server.AGENT_HEADER: "analyst", server.ACCOUNT_HEADER: "globex"}
    run(server.restore_memories(export, Ctx()))
    assert actions("globex") == start - 3 and db.count_memories("globex") == 3
    run(server.restore_memories(export, Ctx()))  # nothing new, so nothing charged
    assert actions("globex") == start - 3


def test_export_and_restore_over_mcp(memoria_url, devs):
    from memoria_client import Memoria

    async def scenario():
        async with Memoria(api_key=devs["acme/trader"], url=memoria_url) as trader:
            saved = await trader.save("Acme renews March 1.", reason="email", topic="customers")
            export = await trader.export()
            assert [mem["id"] for mem in export["memories"]] == [saved["memory_id"]]
            await trader.delete(saved["memory_id"])
            back = await trader.restore(export)
            assert back["restored_count"] == 1
            assert (await trader.restore(export))["restored_count"] == 0
    run(scenario())
