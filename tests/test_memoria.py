"""The memory engine (memoria.py): rules, privacy, signatures and history."""
import asyncio
import json

import pytest

import memoria as m
import db
from conftest import index, save_index, settle


def run(coro):
    return asyncio.run(coro)


def save(agent="alice", note="Penguins huddle to survive the Antarctic winter.", **kw):
    kw.setdefault("reason", "test")
    kw.setdefault("topic", "test/animals")
    return run(m.save_memory(agent, note, **kw))


# ---------- saving ----------

def test_save_returns_signed_private_memory(store):
    mem = save(confidence="high")
    assert mem["verification"] == "verified"
    assert mem["signed_by"] == "alice" and mem["saved_by"] == "alice"
    assert mem["visibility"] == "private" and mem["shared_with"] == []
    assert mem["confidence"] == "high"
    assert mem["memory_id"].startswith("mem-")


@pytest.mark.parametrize("kw, error", [
    ({"confidence": "very"}, "confidence"),
    ({"note": "  "}, "note"),
    ({"reason": ""}, "reason"),
    ({"topic": ""}, "topic"),
    ({"visibility": "public"}, "visibility"),
    ({"visibility": "shared", "shared_with": []}, "at least one"),
    ({"visibility": "shared", "shared_with": ["alice"]}, "at least one"),  # only yourself
    ({"visibility": "shared", "shared_with": ["Not Valid"]}, "not a valid agent"),
])
def test_save_rejects_bad_input(store, kw, error):
    with pytest.raises(m.MemoriaError, match=error):
        save(**kw)


def test_bad_agent_id_is_rejected(store):
    with pytest.raises(m.MemoriaError, match="agent_id"):
        save(agent="Bad Agent")


def test_shared_with_accepts_comma_text_and_drops_owner_and_repeats(store):
    mem = save(visibility="shared", shared_with="bob, alice, bob,carol")
    assert mem["shared_with"] == ["bob", "carol"]


def test_activity_and_agents_are_recorded(store):
    save()
    run(m.find_memory("bob", "penguins"))
    actions = [json.loads(line)["action"] for line in m.ACTIVITY_FILE.read_text().splitlines()]
    assert actions[:2] == ["save", "find"]
    assert {row["principal"] for row in db.query("SELECT principal FROM agents")} == {"alice", "bob"}


# ---------- privacy and sharing ----------

def test_private_memory_is_invisible_to_others(store):
    mem = save()
    mid = mem["memory_id"]

    async def scenario():
        await settle()
        assert [x["memory_id"] for x in await m.find_memory("alice", "penguins")] == [mid]
        assert await m.find_memory("bob", "penguins") == []
        for action in (m.update_memory("bob", mid, "new", "why"),
                       m.share_memory("bob", mid, "private"),
                       m.delete_memory("bob", mid)):
            with pytest.raises(m.MemoriaError, match="No memory with ID"):
                await action
    run(scenario())


def test_shared_reader_can_find_and_update_but_not_share_or_delete(store):
    mem = save(visibility="shared", shared_with=["bob"])
    mid = mem["memory_id"]

    async def scenario():
        await settle()
        assert [x["memory_id"] for x in await m.find_memory("bob", "penguins")] == [mid]
        assert await m.find_memory("carol", "penguins") == []
        updated = await m.update_memory("bob", mid, "Penguins huddle and take turns.", "more detail")
        assert updated["updated_by"] == "bob" and updated["verification"] == "verified"
        with pytest.raises(m.MemoriaError, match="Only the agent that wrote"):
            await m.share_memory("bob", mid, "shared", ["carol"])
        with pytest.raises(m.MemoriaError, match="Only the agent that wrote"):
            await m.delete_memory("bob", mid)
    run(scenario())


def test_owner_can_change_sharing(store):
    mid = save()["memory_id"]

    async def scenario():
        await settle()
        shared = await m.share_memory("alice", mid, "shared", ["carol"])
        assert shared["shared_with"] == ["carol"]
        assert [x["memory_id"] for x in await m.find_memory("carol", "penguins")] == [mid]
        await m.share_memory("alice", mid, "private")
        assert await m.find_memory("carol", "penguins") == []
    run(scenario())


def test_find_filters_by_topic_and_writer(store):
    async def scenario():
        await m.save_memory("alice", "Penguins live in the south.", "r", "test/animals")
        await m.save_memory("bob", "Penguins also live in zoos.", "r", "test/zoos",
                            visibility="shared", shared_with=["alice"])
        await settle()
        assert {x["saved_by"] for x in await m.find_memory("alice", "penguins")} == {"alice", "bob"}
        assert [x["saved_by"] for x in await m.find_memory("alice", "penguins", saved_by="bob")] == ["bob"]
        assert [x["topic"] for x in await m.find_memory("alice", "penguins", topic="zoos")] == ["test/zoos"]
    run(scenario())


def test_topic_search_tops_up_with_newest_on_that_topic(store):
    async def scenario():
        await m.save_memory("alice", "Octopuses have three hearts.", "r", "test/sea")
        await settle()
        found = await m.find_memory("alice", "completely unrelated words", topic="sea")
        assert [x["topic"] for x in found] == ["test/sea"]
    run(scenario())


def test_limit_is_respected(store):
    async def scenario():
        for i in range(8):
            await m.save_memory("alice", f"Penguin fact number {i}.", "r", "test/animals")
        await settle()
        assert len(await m.find_memory("alice", "penguin", limit=3)) == 3
    run(scenario())


# ---------- updates, history and signatures ----------

def test_update_keeps_history_and_resigns(store):
    mid = save(confidence="low")["memory_id"]

    async def scenario():
        out = await m.update_memory("alice", mid, "Penguins huddle in groups of thousands.",
                                    "better source", confidence="high", topic="test/birds")
        assert out["previous_note"].startswith("Penguins huddle to survive")
        assert out["times_updated"] == 1 and out["confidence"] == "high"
        assert out["topic"] == "test/birds" and out["verification"] == "verified"
        versions = m.memory_versions(mid)["versions"]
        assert [v["version"] for v in versions] == [2, 1]
        assert versions[0]["current"] and versions[1]["confidence"] == "low"
    run(scenario())


def test_editing_the_file_breaks_the_signature(store):
    mid = save()["memory_id"]
    data = index()
    data[mid]["note"] = "Someone quietly changed this."
    save_index(data)
    shown = next(x for x in m.all_memories() if x["memory_id"] == mid)
    assert shown["verification"] == "NOT verified"
    assert "changed after it was signed" in shown["verification_detail"]


def test_changing_sharing_does_not_break_the_signature(store):
    mid = save()["memory_id"]
    out = run(m.share_memory("alice", mid, "shared", ["bob"]))
    assert out["verification"] == "verified"


def test_unsigned_memory_is_reported(store):
    mid = save()["memory_id"]
    data = index()
    del data[mid]["signature"]
    save_index(data)
    shown = m.all_memories()[0]
    assert shown["verification"] == "NOT verified" and "no signature" in shown["verification_detail"]
