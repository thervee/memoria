"""Memoria stays quick as it grows: keys cached, database work off the main loop."""
import asyncio
import json
import time

import db
import identity
import memoria as m


def count_key_file_reads(monkeypatch):
    reads = {"n": 0}
    real = identity.json.loads

    def counting(text, *a, **k):
        reads["n"] += 1
        return real(text, *a, **k)
    monkeypatch.setattr(identity.json, "loads", counting)
    return reads


def test_public_keys_are_read_once_not_for_every_check(keys, monkeypatch):
    identity._registry_cache["stamp"] = None
    reads = count_key_file_reads(monkeypatch)
    for _ in range(50):
        assert identity.public_key_of("alice")
    assert reads["n"] == 1


def test_a_newly_registered_agent_is_seen_straight_away(keys, monkeypatch):
    assert identity.public_key_of("dave") is None
    identity.register_agent("dave")
    assert identity.public_key_of("dave") is not None


def test_a_key_added_by_another_program_is_seen(keys):
    assert identity.public_key_of("erin") is None
    data = json.loads(identity.REGISTRY.read_text(encoding="utf-8"))
    data["erin"] = {"public_key": "x" * 43, "registered_at": "2026-11-24T00:00:00+00:00"}
    identity.REGISTRY.write_text(json.dumps(data, indent=2), encoding="utf-8")  # like manage_accounts.py
    assert identity.public_key_of("erin") == "x" * 43


def test_slow_database_work_does_not_hold_up_other_requests(store, monkeypatch):
    asyncio.run(m.save_memory("alice", "Penguins huddle.", "r", "animals"))
    real = db.visible_memories

    def slow(*args, **kwargs):
        time.sleep(0.5)  # a slow query, or waiting for another program's lock
        return real(*args, **kwargs)
    monkeypatch.setattr(db, "visible_memories", slow)

    async def scenario():
        ticks = 0

        async def other_requests():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1
        other = asyncio.create_task(other_requests())
        listed = await m.list_memories("alice")
        other.cancel()
        return listed, ticks
    listed, ticks = asyncio.run(scenario())
    assert [x["note"] for x in listed] == ["Penguins huddle."]
    assert ticks >= 10  # the loop kept running while the database worked


def test_agent_last_seen_is_written_at_most_once_a_minute(store, monkeypatch):
    m._last_seen.clear()
    writes = []
    monkeypatch.setattr(db, "touch_agent", lambda principal, now: writes.append(principal))
    for _ in range(5):
        asyncio.run(m.list_memories("alice"))
    assert writes == ["alice"]
    m._last_seen["alice"] -= m.SEEN_EVERY  # a minute later
    asyncio.run(m.list_memories("alice"))
    assert writes == ["alice", "alice"]
