"""Memory IDs: long and random, and saving never overwrites an existing memory."""
import asyncio
import re

import pytest

import db
import memoria


def save(agent, account, note):
    return asyncio.run(memoria.save_memory(agent, note, "test", "audit", account=account))


def test_new_ids_are_128_random_bits():
    ids = {memoria._new_memory_id() for _ in range(2000)}
    assert len(ids) == 2000
    assert all(re.fullmatch(r"mem-[0-9a-f]{32}", i) for i in ids)


def test_saved_memories_get_the_long_ids(store, devs):
    saved = save("trader", "acme", "Acme's note.")
    assert re.fullmatch(r"mem-[0-9a-f]{32}", saved["memory_id"])


def test_cross_account_overwrite_is_blocked(store, devs, monkeypatch):
    """The audit's case: globex saves a memory whose new ID is one acme already uses."""
    acme = save("trader", "acme", "Acme's private plan.")
    taken = acme["memory_id"]
    before = db.get_memory(taken)

    # The old way of writing ("insert or replace") is what lost data: it would have
    # replaced acme's memory. The write used for saving now refuses instead.
    intruder = dict(before, account="globex", agent_id="trader", note="Globex's note.")
    assert db.add_memory(intruder) is False
    assert db.get_memory(taken) == before

    # Every new ID collides: the save fails rather than overwrite anything.
    monkeypatch.setattr(memoria, "_new_memory_id", lambda: taken)
    with pytest.raises(memoria.MemoriaError, match="Could not save"):
        save("trader", "globex", "Globex's note.")
    after = db.get_memory(taken)
    assert after == before and after["account"] == "acme"
    assert memoria._verification(after)["verification"] == "verified"
    assert db.count_memories("globex") == 0


def test_a_taken_id_just_gets_a_new_one(store, devs, monkeypatch):
    acme = save("trader", "acme", "Acme's private plan.")
    taken = acme["memory_id"]
    ids = iter([taken, "mem-" + "b" * 32])
    monkeypatch.setattr(memoria, "_new_memory_id", lambda: next(ids))
    globex = save("trader", "globex", "Globex's note.")
    assert globex["memory_id"] == "mem-" + "b" * 32
    assert globex["verification"] == "verified"  # signed with the ID it was saved under
    assert db.get_memory(taken)["note"] == "Acme's private plan."
    assert db.get_memory(taken)["account"] == "acme"


def test_memories_with_old_short_ids_still_work(store, devs, monkeypatch):
    real = memoria._new_memory_id
    monkeypatch.setattr(memoria, "_new_memory_id", lambda: "mem-1a2b3c4d")
    old = save("trader", "acme", "Saved before IDs got longer.")
    monkeypatch.setattr(memoria, "_new_memory_id", real)
    assert old["memory_id"] == "mem-1a2b3c4d" and old["verification"] == "verified"

    async def use_it():
        found = await memoria.find_memory("trader", "IDs longer", topic="audit",
                                          account="acme")
        updated = await memoria.update_memory("trader", "mem-1a2b3c4d", "Still here.",
                                              "check", account="acme")
        shared = await memoria.share_memory("trader", "mem-1a2b3c4d", "shared", ["analyst"],
                                            account="acme")
        deleted = await memoria.delete_memory("trader", "mem-1a2b3c4d", account="acme")
        return found, updated, shared, deleted
    found, updated, shared, deleted = asyncio.run(use_it())
    assert [m["memory_id"] for m in found] == ["mem-1a2b3c4d"]
    assert found[0]["verification"] == "verified"
    assert updated["verification"] == "verified" and shared["verification"] == "verified"
    assert deleted == {"deleted": "mem-1a2b3c4d"}
