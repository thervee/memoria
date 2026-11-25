"""Outcomes: agents record whether a memory actually helped (record_outcome, get_outcomes)."""
import asyncio
import hashlib
import itertools
import json
import secrets
import zipfile

import pytest

import db
import demo
import identity
import memoria as m
from memoria_client import Memoria


@pytest.fixture
def clock(store, monkeypatch):
    """Each _now() call is one second later, so "latest" is always clear."""
    ticks = itertools.count()
    monkeypatch.setattr(m, "_now", lambda: _tick(ticks))
    return ticks


def _tick(ticks) -> str:
    n = next(ticks)
    return f"2026-11-23T{10 + n // 3600:02d}:{n // 60 % 60:02d}:{n % 60:02d}+00:00"


def run(coro):
    return asyncio.run(coro)


def save(agent="alice", note="Token X looked safe.", topic="crypto/token-x", **extra):
    return run(m.save_memory(agent, note, "checked the contract", topic, **extra))["memory_id"]


def record(agent, mid, result, note="", account="local"):
    return run(m.record_outcome(agent, mid, result, note, account=account))


def outcomes(agent, mid, account="local"):
    return run(m.get_outcomes(agent, mid, account=account))


def logged(action):
    return [json.loads(line) for line in m.ACTIVITY_FILE.read_text().splitlines()
            if json.loads(line)["action"] == action]


# ---------- recording ----------

def test_author_records_an_outcome(clock):
    mid = save()
    done = record("alice", mid, "failure", "rugged")
    assert done["recorded"]["result"] == "failure" and done["recorded"]["note"] == "rugged"
    assert done["recorded"]["verification"] == "verified" and done["replaced"] is None
    summary = done["outcomes"]
    assert (summary["success"], summary["failure"], summary["mixed"]) == (0, 1, 0)
    assert summary["latest"]["agent"] == "alice" and summary["signatures"] == "all verified"


def test_recording_again_replaces_your_earlier_outcome(clock):
    mid = save()
    record("alice", mid, "success", "looked fine")
    again = record("alice", mid, "failure", "rugged")
    assert again["replaced"] == "success"
    got = outcomes("alice", mid)
    assert [(o["agent"], o["result"], o["note"]) for o in got["outcomes"]] == \
        [("alice", "failure", "rugged")]
    assert [e["result"] for e in logged("outcome")] == ["success", "failure"]
    assert logged("outcome")[1]["replaced"] == "success"  # the change is in the activity log


def test_different_agents_each_record_their_own(clock):
    mid = save(visibility="shared", shared_with=["bob"])
    record("alice", mid, "success")
    record("bob", mid, "mixed", "half of it worked")
    got = outcomes("alice", mid)
    assert [(o["agent"], o["result"]) for o in got["outcomes"]] == [("bob", "mixed"),
                                                                   ("alice", "success")]
    assert got["summary"]["success"] == 1 and got["summary"]["mixed"] == 1
    assert got["summary"]["latest"]["agent"] == "bob"


@pytest.mark.parametrize("result, note, error", [
    ("great", "", "result must be"),
    ("success", "x" * 501, "500 characters"),
])
def test_bad_results_and_long_notes_are_refused(clock, result, note, error):
    mid = save()
    with pytest.raises(m.MemoriaError, match=error):
        record("alice", mid, result, note)


# ---------- who may record and read outcomes ----------

def test_a_shared_agent_may_record_and_read(clock):
    mid = save(visibility="shared", shared_with=["bob"])
    record("bob", mid, "success")
    assert outcomes("bob", mid)["summary"]["success"] == 1


def test_strangers_are_not_allowed_and_cant_tell_if_it_exists(clock):
    mid = save()  # private to alice
    for agent, memory in (("carol", mid), ("carol", "mem-" + "0" * 32)):
        with pytest.raises(m.MemoriaError) as refused:
            record(agent, memory, "failure")
        assert str(refused.value) == f"You're not allowed to record an outcome for memory " \
                                     f"'{memory}'."
        with pytest.raises(m.MemoriaError, match="not allowed to see the outcomes"):
            outcomes(agent, memory)
    assert db.outcomes_for([mid]) == {}


def test_other_accounts_are_not_allowed(clock, devs):
    mid = run(m.save_memory("trader", "Acme's note.", "r", "t", account="acme"))["memory_id"]
    with pytest.raises(m.MemoriaError, match="not allowed"):
        record("trader", mid, "failure", account="globex")


def test_sharing_shows_outcomes_and_unsharing_hides_them(clock):
    mid = save()
    record("alice", mid, "success")
    with pytest.raises(m.MemoriaError, match="not allowed"):
        outcomes("bob", mid)
    run(m.share_memory("alice", mid, "shared", ["bob"]))
    assert outcomes("bob", mid)["summary"]["success"] == 1
    run(m.share_memory("alice", mid, "private"))
    with pytest.raises(m.MemoriaError, match="not allowed"):
        outcomes("bob", mid)


# ---------- signatures ----------

def test_outcome_signatures_are_checked(clock):
    mid = save(visibility="shared", shared_with=["bob"])
    record("alice", mid, "success")
    record("bob", mid, "failure", "rugged")
    db.execute("UPDATE outcomes SET note = 'all good' WHERE agent_id = 'bob'")  # tampered
    got = outcomes("alice", mid)
    checks = {o["agent"]: o["verification"] for o in got["outcomes"]}
    assert checks == {"alice": "verified", "bob": "NOT verified"}
    assert got["summary"]["signatures"] == "1 NOT verified"


def test_an_outcome_relabelled_as_another_agents_is_not_verified(clock):
    mid = save(visibility="shared", shared_with=["bob"])
    record("bob", mid, "success")
    db.execute("UPDATE outcomes SET agent_id = 'alice' WHERE agent_id = 'bob'")
    assert outcomes("alice", mid)["outcomes"][0]["verification"] == "NOT verified"


# ---------- find_memory and list_memories ----------

def test_find_and_list_show_the_outcome_summary(clock):
    mid = save()
    record("alice", mid, "failure", "rugged")
    for got in (run(m.find_memory("alice", "Token X", topic="crypto")),
                run(m.list_memories("alice"))):
        summary = got[0]["outcomes"]
        assert summary["failure"] == 1 and summary["latest"]["note"] == "rugged"
        assert summary["latest"]["verification"] == "verified"
        assert "_verified" not in summary


def test_memories_without_outcomes_show_an_empty_summary(clock):
    save()
    summary = run(m.list_memories("alice"))[0]["outcomes"]
    assert summary == {"success": 0, "failure": 0, "mixed": 0, "total": 0, "latest": None,
                       "signatures": "none"}


def place(n: int) -> str:
    """Store memory n with created time n, so list_memories shows the highest n first."""
    mem = {"id": f"mem-{n:032x}", "account": "local", "agent_id": "alice",
           "created_at": f"2026-11-01T00:00:{n:02d}+00:00", "topic": "t", "confidence": "low",
           "note": f"note {n}", "reason": "r", "visibility": "shared",
           "shared_with": ["bob", "carol"]}
    m._sign(mem, "alice")
    db.put_memory(mem)
    return mem["id"]


def order(agent="alice", **filters):
    return [x["note"] for x in run(m.list_memories(agent, **filters))]


READERS = [f"reader{n}" for n in range(1, 7)]


def readers():
    """Six more agents that may read every stored memory (for many outcomes on one)."""
    for agent in READERS:
        identity.register_agent(agent)
    db.execute("UPDATE memories SET shared_with = ?", (json.dumps(["bob", "carol", *READERS]),))


def test_one_or_two_outcomes_move_a_memory_only_a_little(clock):
    ids = {n: place(n) for n in range(1, 5)}    # newest first: 4 3 2 1
    record("alice", ids[1], "success")          # the oldest, with one success
    record("alice", ids[4], "failure")          # the newest, with one failure
    assert order() == ["note 4", "note 3", "note 2", "note 1"]  # one outcome counts for nothing
    record("bob", ids[3], "success")
    record("carol", ids[3], "success")          # two successes alone aren't enough either
    assert order() == ["note 4", "note 3", "note 2", "note 1"]
    record("bob", ids[4], "failure")            # ...but next to one with two failures, they are
    assert order() == ["note 3", "note 4", "note 2", "note 1"]


def test_a_strong_record_moves_a_memory_further_but_not_far(clock):
    ids = {n: place(n) for n in range(1, 7)}    # newest first: 6 5 4 3 2 1
    readers()
    for agent in READERS:
        record(agent, ids[3], "success")        # 6 successes: up one place
    assert order() == ["note 6", "note 5", "note 3", "note 4", "note 2", "note 1"]
    for agent in READERS:
        record(agent, ids[5], "failure")        # 6 failures: down two places
    assert order() == ["note 6", "note 3", "note 4", "note 5", "note 2", "note 1"]
    for agent in ("alice", "bob", "carol"):
        record(agent, ids[1], "success")        # 9 outcomes in all: still not 2 places up
    for agent in READERS:
        record(agent, ids[1], "success")
    assert order()[-2:] == ["note 1", "note 2"]


def test_unverified_outcomes_dont_change_the_ranking(clock):
    ids = {n: place(n) for n in range(1, 4)}
    for agent in ("alice", "bob", "carol"):
        record(agent, ids[1], "success")
    db.execute("UPDATE outcomes SET note = 'forged' WHERE memory_id = ?", (ids[1],))
    assert order() == ["note 3", "note 2", "note 1"]


def test_find_memory_uses_the_same_ranking(clock, store):
    a = save(note="Penguins huddle in the cold.", visibility="shared", shared_with=["bob", "carol"])
    b = save(note="Penguins huddle for warmth.", visibility="shared", shared_with=["bob", "carol"])
    first = run(m.find_memory("alice", "penguins huddle", topic="crypto"))[0]["memory_id"]
    other = b if first == a else a
    for agent in ("alice", "bob", "carol"):
        record(agent, other, "success")
    for agent in ("alice", "bob"):
        record(agent, first, "failure")
    assert run(m.find_memory("alice", "penguins huddle", topic="crypto"))[0]["memory_id"] == other


# ---------- the outcome filter ----------

def test_outcome_filter_uses_the_latest_outcome(clock):
    ids = {n: place(n) for n in range(1, 5)}
    record("alice", ids[1], "success")
    record("bob", ids[1], "failure")           # latest for memory 1: failure
    record("alice", ids[2], "success")
    record("alice", ids[3], "mixed")
    assert order(outcome="failure") == ["note 1"]
    assert order(outcome="success") == ["note 2"]
    assert order(outcome="mixed") == ["note 3"]
    assert order(outcome="none") == ["note 4"]
    found = run(m.find_memory("alice", "note", topic="t", limit=20, outcome="success"))
    assert [x["note"] for x in found] == ["note 2"]
    with pytest.raises(m.MemoriaError, match="outcome must be"):
        run(m.list_memories("alice", outcome="great"))


# ---------- deleting ----------

def test_deleting_a_memory_deletes_its_outcomes(clock):
    mid = save(visibility="shared", shared_with=["bob"])
    record("alice", mid, "success")
    record("bob", mid, "failure")
    run(m.delete_memory("alice", mid))
    assert db.query("SELECT count(*) FROM outcomes")[0][0] == 0


# ---------- over MCP: the example, demo keys and billing ----------

def test_the_example_over_mcp(memoria_url, devs):
    async def scenario():
        async with Memoria(api_key=devs["acme/trader"], url=memoria_url) as trader:
            saved = await trader.save("Token X looked safe.", reason="contract checked",
                                      topic="crypto/token-x", visibility="shared",
                                      shared_with=["analyst"])
        async with Memoria(api_key=devs["acme/analyst"], url=memoria_url) as analyst:
            await analyst.record_outcome(saved["memory_id"], "failure", "rugged")
            found = await analyst.find("Token X", topic="crypto", outcome="failure")
            full = await analyst.outcomes(saved["memory_id"])
        async with Memoria(api_key=devs["globex/trader"], url=memoria_url) as stranger:
            with pytest.raises(Exception, match="not allowed"):
                await stranger.record_outcome(saved["memory_id"], "success")
        return found, full
    found, full = run(scenario())
    assert found[0]["outcomes"]["latest"]["note"] == "rugged"
    assert full["outcomes"][0]["agent"] == "analyst"
    assert full["outcomes"][0]["verification"] == "verified"


def test_demo_keys_cant_record_or_read_outcomes(store, memoria_url, monkeypatch):
    key_id, secret = secrets.token_hex(6), secrets.token_urlsafe(32)
    monkeypatch.setattr(demo, "KEYS", {key_id: (demo.AGENT_A, hashlib.sha256(
        secret.encode()).hexdigest())})
    run(demo.ensure())
    mid = db.query("SELECT id FROM memories WHERE account = 'demo' AND agent_id = ?",
                   (demo.AGENT_A,))[0][0]

    async def scenario():
        async with Memoria(api_key=f"mem_{key_id}_{secret}", url=memoria_url) as mem:
            with pytest.raises(Exception, match="read-only"):
                await mem.record_outcome(mid, "success")
            with pytest.raises(Exception, match="can only use find_memory"):
                await mem.outcomes(mid)
            return await mem.find("Memoria", topic="memoria", limit=20)
    found = run(scenario())
    assert found and all(x["outcomes"]["total"] == 0 for x in found)  # find still works
    assert db.query("SELECT count(*) FROM outcomes")[0][0] == 0


def test_recording_outcomes_is_free_reading_them_costs_one(memoria_url, devs, monkeypatch,
                                                            store):
    monkeypatch.setenv("MEMORIA_BILLING", "on")

    def actions():
        return db.query("SELECT actions FROM billing WHERE account = 'acme'")[0][0]

    async def scenario():
        async with Memoria(api_key=devs["acme/trader"], url=memoria_url) as mem:
            saved = await mem.save("Token X looked safe.", reason="r", topic="crypto")
            before = actions()
            await mem.record_outcome(saved["memory_id"], "failure", "rugged")
            await mem.record_outcome(saved["memory_id"], "mixed")
            after_recording = actions()
            await mem.outcomes(saved["memory_id"])
            return before, after_recording, actions()
    before, after_recording, after_reading = run(scenario())
    assert before == after_recording == 20 - 1  # recording outcomes is free
    assert after_reading == 20 - 2             # get_outcomes uses 1 action


# ---------- database ----------

def test_outcomes_are_in_backups_and_old_databases_upgrade(clock, tmp_path, monkeypatch):
    import sqlite3

    import backup
    mid = save()
    record("alice", mid, "success")
    made = backup.make_backup(db.DB_FILE.parent, folder=tmp_path / "backups")
    with zipfile.ZipFile(made) as zf:
        zf.extract("memory_store/memoria.db", tmp_path / "restored")
    copy = sqlite3.connect(tmp_path / "restored" / "memory_store" / "memoria.db")
    assert copy.execute("SELECT result FROM outcomes").fetchall() == [("success",)]
    copy.close()

    old = tmp_path / "old" / "memoria.db"
    old.parent.mkdir()
    sqlite3.connect(old).close()
    db.close()
    monkeypatch.setattr(db, "DB_FILE", old)
    tables = {r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "outcomes" in tables
