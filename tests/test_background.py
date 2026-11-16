"""The background worker that keeps Cognee in step with memories.json."""
import asyncio
import json

import memoria as m
from conftest import settle


def run(coro):
    return asyncio.run(coro)


def index():
    return json.loads(m.INDEX_FILE.read_text(encoding="utf-8"))


def errors():
    if not m.ACTIVITY_FILE.exists():
        return []
    return [json.loads(line) for line in m.ACTIVITY_FILE.read_text().splitlines()
            if json.loads(line)["action"].startswith("background")]


def pending_forget():
    return json.loads(m.FORGET_FILE.read_text()) if m.FORGET_FILE.exists() else []


def test_save_answers_before_cognee_is_touched(store):
    async def scenario():
        mem = await m.save_memory("alice", "Penguins huddle.", "r", "test/animals")
        assert mem["processing"] is True
        assert store.calls == []  # nothing sent to Cognee yet
        await settle()
        saved = index()[mem["memory_id"]]
        assert saved["needs_sync"] is False and saved["processed"] is True
        assert saved["data_id"] in store.docs
    run(scenario())


def test_memories_saved_together_are_processed_in_one_run(store):
    async def scenario():
        for note in ("Penguins huddle.", "Octopuses have three hearts.", "Owls hunt at night."):
            await m.save_memory("alice", note, "r", "test/animals")
        await settle()
        assert store.count("add") == 3 and store.count("cognify") == 1
        assert all(not mem["needs_sync"] for mem in index().values())
    run(scenario())


def test_unprocessed_memory_is_found_by_its_words(store):
    async def scenario():
        mem = await m.save_memory("alice", "Octopuses have three hearts.", "r", "test/sea")
        found = await m.find_memory("alice", "how many hearts does an octopus have?")
        assert [x["memory_id"] for x in found] == [mem["memory_id"]]
        assert found[0]["processing"] is True
        assert await m.find_memory("alice", "volcanoes") == []
    run(scenario())


def test_processed_memory_is_found_through_cognee(store):
    async def scenario():
        mem = await m.save_memory("alice", "Octopuses have three hearts.", "r", "test/sea")
        await settle()
        found = await m.find_memory("alice", "octopuses hearts")
        assert [x["memory_id"] for x in found] == [mem["memory_id"]]
        assert found[0]["processing"] is False and "search" in store.calls
    run(scenario())


def test_update_of_processed_memory_replaces_it_in_cognee(store):
    async def scenario():
        mem = await m.save_memory("alice", "Octopuses have three hearts.", "r", "test/sea")
        await settle()
        await m.update_memory("alice", mem["memory_id"], "Octopuses have blue blood.", "more")
        assert index()[mem["memory_id"]]["needs_sync"] is True
        await settle()
        data_id = index()[mem["memory_id"]]["data_id"]
        assert store.count("update") == 1 and "blue blood" in store.docs[data_id]["text"]
        assert index()[mem["memory_id"]]["needs_sync"] is False
    run(scenario())


def test_update_while_waiting_sends_the_latest_version(store):
    async def scenario():
        mem = await m.save_memory("alice", "Version one.", "r", "test/x")
        await m.update_memory("alice", mem["memory_id"], "Version two.", "changed")
        await settle()
        texts = [doc["text"] for doc in store.docs.values()]
        assert len(texts) == 1 and "Version two." in texts[0]
    run(scenario())


def test_delete_before_processing_never_reaches_cognee(store):
    async def scenario():
        mem = await m.save_memory("alice", "Short-lived.", "r", "test/x")
        await m.delete_memory("alice", mem["memory_id"])
        await settle()
        assert store.docs == {} and pending_forget() == [] and index() == {}
    run(scenario())


def test_delete_after_processing_removes_it_from_cognee(store):
    async def scenario():
        mem = await m.save_memory("alice", "Octopuses have three hearts.", "r", "test/sea")
        await settle()
        assert (await m.delete_memory("alice", mem["memory_id"])) == {"deleted": mem["memory_id"]}
        assert await m.find_memory("alice", "octopuses") == []  # gone straight away
        await settle()
        assert store.docs == {} and pending_forget() == []
    run(scenario())


def test_failed_graph_run_is_retried_without_adding_twice(store):
    store.fail["cognify"] = 1

    async def scenario():
        mem = await m.save_memory("alice", "Octopuses have three hearts.", "r", "test/sea")
        await settle()
        saved = index()[mem["memory_id"]]
        assert saved["needs_sync"] is False and saved["processed"] is True
        assert store.count("add") == 1 and store.count("cognify") == 2
        assert store.count("update") == 0  # the bug this guards against: it used to try update
        assert [e["what"] for e in errors()] == ["building the knowledge graph"]
    run(scenario())


def test_failed_add_is_retried(store):
    store.fail["add"] = 2

    async def scenario():
        mem = await m.save_memory("alice", "Octopuses have three hearts.", "r", "test/sea")
        await settle()
        assert index()[mem["memory_id"]]["needs_sync"] is False
        assert len(store.docs) == 1 and len(errors()) == 2
    run(scenario())


def test_one_failing_memory_does_not_block_the_others(store):
    async def scenario():
        first = await m.save_memory("alice", "First.", "r", "test/x")
        second = await m.save_memory("alice", "Second.", "r", "test/x")
        await settle()
        store.fail["update"] = 1  # the next update fails once
        await m.update_memory("alice", first["memory_id"], "First, changed.", "r")
        await m.update_memory("alice", second["memory_id"], "Second, changed.", "r")
        await settle()
        assert all(not mem["needs_sync"] for mem in index().values())
        assert store.count("update") == 3  # one failure, then both succeed
    run(scenario())


def test_removal_gives_up_after_repeated_failures(store):
    async def scenario():
        mem = await m.save_memory("alice", "Doomed.", "r", "test/x")
        await settle()
        store.fail["forget"] = 99
        await m.delete_memory("alice", mem["memory_id"])
        await settle()
        assert pending_forget() == []
        assert store.count("forget") == m.FORGET_GIVE_UP
        assert errors()[-1]["action"] == "background_gave_up"
    run(scenario())


def test_unfinished_work_is_resumed_on_start(store, monkeypatch):
    # Memoria "stops" right after saving: the worker never gets to run.
    with monkeypatch.context() as patch:
        patch.setattr(m, "_kick", lambda: None)
        run(m.save_memory("alice", "Saved just before a crash.", "r", "test/x"))
    assert next(iter(index().values()))["needs_sync"] is True and store.docs == {}

    async def restart():
        await settle()  # calls start_background_work(), as server start-up does
    run(restart())
    assert next(iter(index().values()))["needs_sync"] is False and len(store.docs) == 1


def test_old_memories_without_bookkeeping_fields_still_work(store):
    """Memories saved before the background worker existed have none of its fields."""
    async def scenario():
        mem = await m.save_memory("alice", "Old style.", "r", "test/x")
        await settle()
        data = index()
        for field in ("needs_sync", "sync_version", "processed", "added_version"):
            data[mem["memory_id"]].pop(field)
        m._write_json(m.INDEX_FILE, data)
        assert m.all_memories()[0]["processing"] is False
        await m.update_memory("alice", mem["memory_id"], "Old style, updated.", "r")
        await settle()
        assert store.count("update") == 1 and index()[mem["memory_id"]]["needs_sync"] is False
    run(scenario())
