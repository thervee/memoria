"""Memoria's database (db.py): storage, importing the old JSON files, and safety."""
import asyncio
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import accounts
import backup
import db
import identity
import memoria as m
from conftest import index, settle

HERE = Path(__file__).resolve().parent.parent


def run(coro):
    return asyncio.run(coro)


def save(agent="alice", note="Penguins huddle.", **kw):
    kw.setdefault("reason", "r")
    kw.setdefault("topic", "test/animals")
    return run(m.save_memory(agent, note, **kw))


# ---------- the basics ----------

def test_database_is_set_up_with_safe_settings(store):
    save()
    conn = db.connection()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.DB_FILE.exists()


def test_memories_survive_a_restart(store):
    mid = save(visibility="shared", shared_with=["bob"])["memory_id"]
    run(m.update_memory("alice", mid, "Penguins huddle in groups.", "more"))
    before = db.get_memory(mid)
    db.close()  # like Memoria stopping
    after = db.get_memory(mid)
    assert after == before
    assert after["shared_with"] == ["bob"] and len(after["history"]) == 1
    assert m.all_memories()[0]["verification"] == "verified"


def test_changes_happen_all_together_or_not_at_all(store):
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.queue_forget("data-1", "set-1", "mem-1")
            raise RuntimeError("something went wrong halfway")
    assert db.forget_queue() == []


def test_sharing_matches_exact_agent_names(store):
    mid = save(visibility="shared", shared_with=["bobby"])["memory_id"]
    mem = db.get_memory(mid)
    mem["shared_with"].append("b%")  # (Memoria refuses such names; plant one to be sure)
    db.put_memory(mem)
    assert [x["id"] for x in db.visible_memories("local", "bobby")] == [mid]
    assert db.visible_memories("local", "bob") == []   # not "bob" inside "bobby"
    assert db.visible_memories("local", "b") == []     # nor a wildcard


def test_daily_write_count_is_exact(store):
    assert [db.use_write("acme", "2026-11-16", 2) for _ in range(3)] == [True, True, False]
    assert db.writes_today("acme", "2026-11-16") == 2
    assert db.use_write("acme", "2026-11-17", 2)  # a new day starts again from zero
    assert db.writes_today("acme", "2026-11-17") == 1


def test_lookups_use_indexes_not_a_full_scan(store):
    """With thousands of memories, finding one must not mean reading them all."""
    rows = [(f"mem-{i:08x}", "acme" if i % 2 else "globex", f"agent-{i % 7}",
             "2026-11-16T00:00:00+00:00", "t", "low", "n", "r", f"data-{i}") for i in range(5000)]
    db.connection().executemany(
        "INSERT INTO memories (id, account, agent_id, created_at, topic, confidence, note, "
        "reason, data_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def plan(sql, params):
        return " ".join(row[-1] for row in db.query("EXPLAIN QUERY PLAN " + sql, params))

    assert "USING INDEX sqlite_autoindex_memories_1" in plan(
        "SELECT * FROM memories WHERE id = ?", ("mem-00000001",))
    assert "USING INDEX memories_by_owner" in plan(
        "SELECT * FROM memories WHERE account = ? AND agent_id = ?", ("acme", "agent-1"))
    assert "USING INDEX memories_by_data_id" in plan(
        "SELECT * FROM memories WHERE account = ? AND data_id IN (?, ?)",
        ("acme", "data-1", "data-3"))
    assert "USING INDEX memories_waiting" in plan(
        "SELECT * FROM memories WHERE needs_sync = 1", ())
    found = db.memories_by_data_id("acme", [f"data-{i}" for i in range(1200)])  # > 500 at once
    assert len(found) == 600 and all(mem["account"] == "acme" for mem in found.values())


# ---------- importing the old JSON files ----------

def legacy_files(folder: Path) -> dict:
    """Memoria's files from before the database, like the ones on this laptop."""
    folder.mkdir(parents=True, exist_ok=True)
    mem = {"id": "mem-0ld00001", "agent_id": "alice", "created_at": "2026-11-16T05:00:00+00:00",
           "topic": "crypto/chainlink", "confidence": "high", "note": "An old note.",
           "reason": "Saved before the database.", "history": [], "visibility": "shared",
           "shared_with": ["bob"]}
    mem["signature"] = identity.sign("alice", m._signed_content(mem))  # signed, like the real one
    unsigned = {"id": "mem-0ld00002", "agent_id": "bob", "created_at": "2026-11-15T05:00:00+00:00",
                "topic": "t", "confidence": "low", "note": "Older still.", "reason": "r",
                "history": [{"note": "first", "reason": "r", "confidence": "low", "topic": "t",
                             "replaced_at": "2026-11-15T06:00:00+00:00", "replaced_by": "bob"}],
                "updated_at": "2026-11-15T06:00:00+00:00", "updated_by": "bob",
                "needs_sync": True, "sync_version": 2, "data_id": "d-2", "dataset_id": "s-1",
                "processed": False, "added_version": 2}
    files = {
        "memories.json": {mem["id"]: mem, unsigned["id"]: unsigned},
        "agents.json": {"alice": {"first_seen": "2026-11-16T05:00:00+00:00",
                                  "last_seen": "2026-11-16T06:00:00+00:00"}},
        "usage.json": {"acme": {"day": "2026-11-16", "writes": 3}},
        "pending_forget.json": [{"data_id": "d-9", "dataset_id": "s-1",
                                 "memory_id": "mem-gone0001", "attempts": 1}],
        "accounts.json": {
            "accounts": {"acme": {"name": "Acme", "email": "dev@acme.example",
                                  "created_at": "2026-11-16T05:00:00+00:00", "status": "active",
                                  "limits": {"writes_per_day": 9}}},
            "keys": {"abcdef123456": {"account": "acme", "agent": "trader",
                                      "hash": accounts._hash("the-secret"),
                                      "created_at": "2026-11-16T05:00:00+00:00",
                                      "revoked_at": None}},
            "invites": {accounts._hash("inv_old"): {"created_at": "2026-11-16T05:00:00+00:00",
                                                    "used_by": None, "used_at": None}}},
    }
    for name, value in files.items():
        (folder / name).write_text(json.dumps(value), encoding="utf-8")
    return files


def test_old_json_files_are_imported_once_and_kept(store):
    db.close()
    legacy_files(db.DB_FILE.parent)
    stored = index()  # opening the database imports them
    assert set(stored) == {"mem-0ld00001", "mem-0ld00002"}
    old = m.all_memories()
    assert [x["memory_id"] for x in old] == ["mem-0ld00001", "mem-0ld00002"]  # newest first
    assert old[0]["verification"] == "verified" and old[0]["account"] == "local"
    assert old[0]["shared_with"] == ["bob"] and old[1]["times_updated"] == 1
    waiting = db.get_memory("mem-0ld00002")
    assert waiting["needs_sync"] is True and waiting["processed"] is False  # work carries on
    assert db.forget_queue()[0]["attempts"] == 1
    assert db.writes_today("acme", "2026-11-16") == 3
    assert accounts.check_key("mem_abcdef123456_the-secret") == ("acme", "trader")
    assert accounts.limits("acme")["writes_per_day"] == 9
    assert accounts.unused_invites() == 1
    folder = db.DB_FILE.parent
    assert not (folder / "memories.json").exists() and (folder / "memories.json.imported").exists()

    # Opening again doesn't import anything twice, even if an old file reappears.
    db.close()
    (folder / "memories.json.imported").replace(folder / "memories.json")
    assert len(index()) == 2


def test_import_is_all_or_nothing(store):
    db.close()
    folder = db.DB_FILE.parent
    legacy_files(folder)
    broken = json.loads((folder / "accounts.json").read_text())
    broken["keys"]["abcdef123456"]["account"] = "no-such-account"  # breaks a database rule
    (folder / "accounts.json").write_text(json.dumps(broken))
    with pytest.raises(sqlite3.IntegrityError):
        db.connection()
    db.close()
    assert (folder / "memories.json").exists()  # nothing renamed: it can be fixed and retried
    plain = sqlite3.connect(db.DB_FILE)
    assert plain.execute("SELECT count(*) FROM memories").fetchone()[0] == 0  # nothing half-done
    plain.close()


def test_memories_imported_halfway_through_processing_are_finished(store):
    db.close()
    folder = db.DB_FILE.parent
    files = legacy_files(folder)
    del files["memories.json"]["mem-0ld00002"]  # only the finished one has no work left
    mem = files["memories.json"]["mem-0ld00001"]
    mem.update(needs_sync=True, sync_version=1)
    (folder / "memories.json").write_text(json.dumps(files["memories.json"]))
    (folder / "pending_forget.json").write_text("[]")
    run(settle())
    assert db.get_memory("mem-0ld00001")["needs_sync"] is False
    assert len(store.docs) == 1


# ---------- two programs at once ----------

def test_another_program_can_change_accounts_while_memoria_runs(store):
    save()  # Memoria has the database open
    script = (
        "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]);"
        "import db, accounts; db.DB_FILE = Path(sys.argv[2]);"
        "accounts.create_account('fromoutside', email='x@y.example')"
    )
    subprocess.run([sys.executable, "-c", script, str(HERE), str(db.DB_FILE)], check=True,
                   timeout=60, capture_output=True)
    assert accounts.get_account("fromoutside")["email"] == "x@y.example"
    save(note="Still saving fine afterwards.")
    assert len(index()) == 2


def test_backup_never_catches_a_half_finished_change(store, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORIA_BACKUP_DIR", str(tmp_path / "backups"))
    save()
    other_program = sqlite3.connect(db.DB_FILE, isolation_level=None)
    other_program.execute("BEGIN IMMEDIATE")
    other_program.execute("INSERT INTO memories (id, agent_id, created_at, topic, confidence, "
                          "note, reason) VALUES ('mem-halfdone', 'x', 'now', 't', 'low', 'n', 'r')")
    path = backup.make_backup(m.STORE)  # taken while that change isn't finished
    other_program.execute("ROLLBACK")
    other_program.close()
    assert backup.info(path)["memories"] == 1


def test_visibility_lookup_never_crosses_accounts(store, devs):
    run(m.save_memory("trader", "Acme's own.", "r", "t", account="acme"))
    run(m.save_memory("trader", "Globex's own.", "r", "t", account="globex",
                      visibility="shared", shared_with=["analyst"]))
    assert [x["note"] for x in db.visible_memories("acme", "trader")] == ["Acme's own."]
    assert db.visible_memories("acme", "analyst") == []
    assert [x["note"] for x in db.visible_memories("globex", "analyst")] == ["Globex's own."]
    assert db.visible_memories("local", "trader") == []
