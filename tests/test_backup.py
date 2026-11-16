"""Automatic backups (backup.py) and restoring them (restore_backup.py)."""
import asyncio
import json
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

import backup
import memoria as m
import restore_backup
from conftest import settle

NOW = datetime(2026, 11, 16, 10, 30, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def backups(tmp_path, monkeypatch):
    """A temporary backups folder, used by default."""
    folder = tmp_path / "backups"
    monkeypatch.setenv("MEMORIA_BACKUP_DIR", str(folder))
    monkeypatch.delenv("MEMORIA_BACKUP_KEEP", raising=False)
    return folder


def fill_store(store):
    """A small memory store with the kinds of files a real one has."""
    files = {
        "agents.json.imported": "{}",
        "activity.log": "{}\n",
        "data/abc/text_1.txt": "a memory",
        "system/databases/cognee_db": "sqlite bytes",
        "system/databases/abc/graph.lbug": "graph bytes",
        "system/databases/abc/x.lance.db/data/part.lance": "vector bytes",
        # these should be left out:
        "models/bge-small/model.onnx": "big model",
        "server.log": "log",
        "watchdog.pid": "123",
        "watchdog.lock": "",
        "system/pipeline_run_locks/run.lock": "",
        "memories.tmp": "half written",
    }
    store.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(store / "memoria.db")  # Memoria's database, with 2 memories
    database.execute("CREATE TABLE memories (id TEXT)")
    database.executemany("INSERT INTO memories VALUES (?)", [("mem-1",), ("mem-2",)])
    database.commit()
    database.close()
    (store / "memoria.db-wal").write_text("")  # copied via SQLite, never as a plain file
    for name, text in files.items():
        path = store / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return store


def names(zip_path):
    with zipfile.ZipFile(zip_path) as zf:
        return set(zf.namelist())


# ---------- making backups ----------

def test_backup_keeps_memories_and_databases_but_not_re_creatable_files(tmp_path, backups):
    store = fill_store(tmp_path / "memory_store")
    path = backup.make_backup(store, now=NOW)
    assert path.name == "memoria-backup-2026-11-16_103000.zip"
    assert names(path) == {
        "backup_info.json",
        "memory_store/memoria.db", "memory_store/agents.json.imported",
        "memory_store/activity.log",
        "memory_store/data/abc/text_1.txt",
        "memory_store/system/databases/cognee_db",
        "memory_store/system/databases/abc/graph.lbug",
        "memory_store/system/databases/abc/x.lance.db/data/part.lance",
    }
    assert backup.info(path) == {"made_at": "2026-11-16T10:30:00+00:00", "memories": 2,
                                 "files": 7, "made_from": str(store)}
    assert list(backups.glob("*.partial")) == []


def test_backup_of_missing_store_fails_clearly(tmp_path, backups):
    with pytest.raises(backup.BackupError, match="no memory store"):
        backup.make_backup(tmp_path / "nothing-here")


def test_only_the_newest_backups_are_kept(tmp_path, backups):
    store = fill_store(tmp_path / "memory_store")
    for day in range(5):
        backup.make_backup(store, now=NOW + timedelta(days=day))
    removed = backup.prune(keep=3)
    assert [p.name[15:25] for p in removed] == ["2026-11-17", "2026-11-16"]
    assert [p.name[15:25] for p in backup.list_backups()] == ["2026-11-20", "2026-11-19",
                                                              "2026-11-18"]


def test_settings_come_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORIA_BACKUP_DIR", str(tmp_path / "usb"))
    monkeypatch.setenv("MEMORIA_BACKUP_KEEP", "30")
    assert backup.backup_dir() == tmp_path / "usb" and backup.keep_count() == 30
    monkeypatch.setenv("MEMORIA_BACKUP_KEEP", "lots")
    assert backup.keep_count() == 14


def test_backup_is_due_once_a_day(tmp_path, backups):
    store = fill_store(tmp_path / "memory_store")
    assert backup.is_due(now=NOW)  # no backups yet
    backup.make_backup(store, now=NOW)
    assert not backup.is_due(now=NOW + timedelta(hours=23))
    assert backup.is_due(now=NOW + timedelta(hours=24))


def test_files_that_arent_backups_are_ignored(backups):
    backups.mkdir()
    (backups / "notes.txt").write_text("hi")
    (backups / "memoria-backup-garbage.zip").write_text("hi")
    assert backup.made_at(backups / "memoria-backup-garbage.zip") is None
    assert backup.is_due(now=NOW)


# ---------- backing up while Memoria runs ----------

def test_backup_waits_for_the_worker_and_holds_saves(store, backups):
    async def scenario():
        await m.save_memory("alice", "Penguins huddle.", "r", "test/animals")
        await settle()
        await m._cognee_lock.acquire()  # pretend the worker is mid-way through a pass
        job = asyncio.create_task(backup.back_up_now())
        await asyncio.sleep(0.05)
        assert not job.done()  # the backup waits for the worker
        m._cognee_lock.release()
        await job
        assert len(backup.list_backups()) == 1
    run(scenario())
    log = [json.loads(line) for line in m.ACTIVITY_FILE.read_text().splitlines()]
    assert log[-1]["action"] == "backup" and log[-1]["memories"] == 1


def test_save_during_a_backup_waits_until_it_is_done(store, backups, monkeypatch):
    order = []
    real_make = backup.make_backup

    def slow_make(*args, **kw):
        order.append("backup started")
        import time
        time.sleep(0.2)
        result = real_make(*args, **kw)
        order.append("backup finished")
        return result
    monkeypatch.setattr(backup, "make_backup", slow_make)

    async def scenario():
        job = asyncio.create_task(backup.back_up_now())
        await asyncio.sleep(0.05)
        await m.save_memory("alice", "Saved during a backup.", "r", "test/x")
        order.append("saved")
        await job
    run(scenario())
    assert order == ["backup started", "backup finished", "saved"]


def test_failed_backup_is_logged_and_memoria_carries_on(store, backups, monkeypatch):
    def broken(*args, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(backup, "make_backup", broken)

    class Stop(Exception):
        pass

    async def stop_after_first_wait(seconds):
        raise Stop
    monkeypatch.setattr(asyncio, "sleep", stop_after_first_wait)
    with pytest.raises(Stop):
        run(backup.backup_loop())
    log = [json.loads(line) for line in m.ACTIVITY_FILE.read_text().splitlines()]
    assert log[-1]["action"] == "backup_error" and "disk full" in log[-1]["error"]


# ---------- restoring ----------

def test_restore_brings_memories_back_and_keeps_the_old_store(store, backups):
    async def scenario():
        first = await m.save_memory("alice", "Keep me.", "r", "test/x")
        await settle()
        await backup.back_up_now()
        await m.delete_memory("alice", first["memory_id"])
        await settle()
        return first["memory_id"]
    mid = run(scenario())
    (m.STORE / "models").mkdir()
    (m.STORE / "models" / "model.onnx").write_text("big model")
    assert m.all_memories() == []

    kept = restore_backup.restore(backup.list_backups()[0], store=m.STORE,
                                  now=datetime(2026, 11, 17, 9, 0))
    assert kept.name == "memory_store.old-20261117-090000"
    restored = m.all_memories()
    assert [x["memory_id"] for x in restored] == [mid]
    assert restored[0]["verification"] == "verified"  # signatures survive the round trip
    assert (m.STORE / "models" / "model.onnx").exists()  # model moved across, not re-downloaded
    old = sqlite3.connect(kept / "memoria.db")  # the old store is kept as it was
    assert old.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
    old.close()


def test_restore_refuses_files_that_would_land_outside_the_store(tmp_path):
    evil = tmp_path / "memoria-backup-2026-11-16_103000.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("backup_info.json", '{"memories": 0}')
        zf.writestr("memory_store/../../evil.txt", "gotcha")
    with pytest.raises(backup.BackupError, match="unexpected file"):
        restore_backup.restore(evil, store=tmp_path / "memory_store")
    assert not (tmp_path.parent / "evil.txt").exists()


def test_restore_refuses_a_file_that_isnt_a_backup(tmp_path):
    fake = tmp_path / "memoria-backup-2026-11-16_103000.zip"
    fake.write_text("not a zip")
    with pytest.raises(backup.BackupError, match="isn't a Memoria backup"):
        restore_backup.restore(fake, store=tmp_path / "memory_store")


def test_restore_is_refused_while_memoria_runs(monkeypatch):
    monkeypatch.setattr(restore_backup, "memoria_running", lambda: True)
    monkeypatch.setattr("sys.argv", ["restore_backup.py"])
    with pytest.raises(SystemExit, match="Stop it first"):
        restore_backup.main()


def test_restore_lists_backups_and_can_be_cancelled(tmp_path, backups, monkeypatch, capsys):
    backup.make_backup(fill_store(tmp_path / "memory_store"), now=NOW)
    monkeypatch.setattr(restore_backup, "memoria_running", lambda: False)
    monkeypatch.setattr(restore_backup, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["restore_backup.py"])
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    restore_backup.main()
    out = capsys.readouterr().out
    assert "1. 16 Nov 2026" in out and "2 memories" in out and "Cancelled" in out
