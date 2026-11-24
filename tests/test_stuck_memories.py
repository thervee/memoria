"""Memories that keep failing to reach Cognee must not hold up anyone else's."""
import asyncio

import db
import memoria as m
from conftest import index, settle


def failing_for(store, dataset, call):
    """Make one of the fake Cognee's calls always fail for one account's dataset."""
    real = getattr(store, call)

    async def fake(*args, **kwargs):
        wanted = kwargs.get("dataset_name") or kwargs.get("datasets")
        if wanted == dataset or (isinstance(wanted, list) and dataset in wanted):
            store.calls.append(call)
            raise RuntimeError(f"pretend {call} failed for {dataset}")
        return await real(*args, **kwargs)
    setattr(store, call, fake)


def attempts(memory_id):
    return db.query("SELECT attempts, last_error FROM memories WHERE id = ?", (memory_id,))[0]


def test_failing_memories_in_one_account_dont_block_another(store, devs):
    failing_for(store, m.dataset_for("acme"), "add")

    async def scenario():
        # More stuck memories than one background run takes (RUN_SIZE)...
        stuck = [await m.save_memory("trader", f"Stuck note {i}.", "r", "t", account="acme")
                 for i in range(m.RUN_SIZE + 1)]
        # ...saved before another account's memory.
        other = await m.save_memory("trader", "Penguins huddle.", "r", "t", account="globex")
        await settle()  # ends only because Memoria gives up on the stuck ones
        return stuck, other
    stuck, other = asyncio.run(scenario())

    done = index()[other["memory_id"]]
    assert done["needs_sync"] is False and done["processed"] is True
    for mem in stuck:
        row = attempts(mem["memory_id"])
        assert row["attempts"] == db.SYNC_GIVE_UP
        assert "pretend add failed" in row["last_error"]
    assert not db.has_work() and db.waiting_memories() == []


def test_a_failing_graph_run_for_one_account_doesnt_block_another(store, devs):
    failing_for(store, m.dataset_for("acme"), "cognify")

    async def scenario():
        bad = await m.save_memory("trader", "Owls hunt at night.", "r", "t", account="acme")
        good = await m.save_memory("trader", "Penguins huddle.", "r", "t", account="globex")
        await settle()
        return bad, good
    bad, good = asyncio.run(scenario())

    assert index()[good["memory_id"]]["processed"] is True  # in its own run, so it worked
    assert index()[bad["memory_id"]]["needs_sync"] is True
    assert attempts(bad["memory_id"])["attempts"] == db.SYNC_GIVE_UP
    assert store.count("add") == 2  # each was added only once (no re-adding on retries)


def test_updating_a_memory_memoria_gave_up_on_gives_it_fresh_tries(store, devs):
    real_add = store.add
    failing_for(store, m.dataset_for("acme"), "add")

    async def scenario():
        mem = await m.save_memory("trader", "Owls hunt at night.", "r", "t", account="acme")
        await settle()
        assert attempts(mem["memory_id"])["attempts"] == db.SYNC_GIVE_UP
        store.add = real_add  # Cognee works again
        await m.update_memory("trader", mem["memory_id"], "Owls hunt at dusk.", "r",
                              account="acme")
        await settle()
        return mem
    mem = asyncio.run(scenario())
    assert index()[mem["memory_id"]]["processed"] is True
    assert attempts(mem["memory_id"])["attempts"] == 0


def test_older_databases_get_the_new_columns(tmp_path, monkeypatch):
    import sqlite3
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as conn:  # a memories table from before version 7
        conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, account TEXT NOT NULL "
                     "DEFAULT 'local', agent_id TEXT NOT NULL, created_at TEXT NOT NULL, "
                     "topic TEXT NOT NULL, confidence TEXT NOT NULL, note TEXT NOT NULL, "
                     "reason TEXT NOT NULL, source TEXT, updated_at TEXT, updated_by TEXT, "
                     "visibility TEXT NOT NULL DEFAULT 'private', shared_with TEXT NOT NULL "
                     "DEFAULT '[]', signature TEXT, history TEXT NOT NULL DEFAULT '[]', "
                     "needs_sync INTEGER NOT NULL DEFAULT 0, sync_version INTEGER NOT NULL "
                     "DEFAULT 0, processed INTEGER, added_version INTEGER, data_id TEXT, "
                     "dataset_id TEXT)")
        conn.execute("INSERT INTO memories (id, agent_id, created_at, topic, confidence, note, "
                     "reason, needs_sync) VALUES ('m1', 'alice', '2026', 't', 'low', 'n', 'r', 1)")
    db.close()
    monkeypatch.setattr(db, "DB_FILE", old)
    assert [mem["id"] for mem in db.waiting_memories()] == ["m1"]
    assert attempts("m1")["attempts"] == 0 and attempts("m1")["last_error"] is None
