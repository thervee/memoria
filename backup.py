"""Automatic backups of memory_store.

While Memoria runs it makes a backup when it starts (if the newest one is more than
a day old) and then once a day. Each backup is one zip file, named by date and time,
e.g. memoria-backup-2026-11-16_1030.zip. The newest 14 are kept; older ones are deleted.

What's in a backup: everything in memory_store (the memories, their history and
signatures, the agent list, the activity log and Cognee's databases), except things
that can be re-created: the downloaded search model, logs and lock files.

Settings (optional, in .env):
  MEMORIA_BACKUP_DIR   where backups go (default: the "backups" folder next to this file).
                       Point it at a USB drive or another disk to survive a disk failure.
  MEMORIA_BACKUP_KEEP  how many backups to keep (default 14).

To restore one, stop Memoria and double-click restore_backup.bat.
"""
import asyncio
import json
import os
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PREFIX = "memoria-backup-"
EVERY = timedelta(days=1)
# Left out of backups because they can be re-created (or would be stale if restored).
SKIP_TOP = {"models", "server.log", "server.old.log", "watchdog.lock", "watchdog.pid"}
SKIP_PARTS = {"pipeline_run_locks", "__pycache__"}
SKIP_SUFFIXES = {".tmp", ".partial"}


class BackupError(RuntimeError):
    """Something went wrong with a backup or restore, explained in plain words."""


def backup_dir() -> Path:
    return Path(os.environ.get("MEMORIA_BACKUP_DIR") or HERE / "backups")


def keep_count() -> int:
    try:
        return max(1, int(os.environ.get("MEMORIA_BACKUP_KEEP", "14")))
    except ValueError:
        return 14


def _included(relative: Path) -> bool:
    if relative.parts[0] in SKIP_TOP or SKIP_PARTS & set(relative.parts):
        return False
    return relative.suffix not in SKIP_SUFFIXES


def list_backups(folder: Path | None = None) -> list[Path]:
    """Every backup in the folder, newest first."""
    folder = folder or backup_dir()
    if not folder.exists():
        return []
    return sorted(folder.glob(f"{PREFIX}*.zip"), reverse=True)


def made_at(backup: Path) -> datetime | None:
    """When a backup was made (from its name), or None if the name isn't one of ours."""
    try:
        return datetime.strptime(backup.stem.removeprefix(PREFIX), "%Y-%m-%d_%H%M%S") \
            .replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_due(folder: Path | None = None, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    times = [t for t in (made_at(b) for b in list_backups(folder)) if t]
    return not times or now - max(times) >= EVERY


def make_backup(store: Path, folder: Path | None = None, now: datetime | None = None) -> Path:
    """Zip memory_store into the backup folder. Returns the new backup's path.

    Call it while nothing is changing the store (see memoria.paused()).
    """
    folder = folder or backup_dir()
    now = now or datetime.now(timezone.utc)
    if not store.exists():
        raise BackupError(f"There is no memory store at {store} to back up.")
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / f"{PREFIX}{now:%Y-%m-%d_%H%M%S}.zip"
    partial = final.with_suffix(".zip.partial")  # only renamed once complete

    files = sorted(p for p in store.rglob("*")
                   if p.is_file() and _included(p.relative_to(store)))
    index_file = store / "memories.json"
    memories = json.loads(index_file.read_text(encoding="utf-8")) if index_file.exists() else {}
    manifest = {
        "made_at": now.isoformat(timespec="seconds"),
        "memories": len(memories),
        "files": len(files),
        "made_from": str(store),
    }
    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr("backup_info.json", json.dumps(manifest, indent=2))
            for path in files:
                zf.write(path, Path("memory_store") / path.relative_to(store))
        with zipfile.ZipFile(partial) as zf:  # read it back to be sure it's sound
            if zf.testzip() is not None:
                raise BackupError("The backup file came out damaged.")
        partial.replace(final)
    finally:
        partial.unlink(missing_ok=True)
    return final


def prune(folder: Path | None = None, keep: int | None = None) -> list[Path]:
    """Delete all but the newest `keep` backups. Returns the ones deleted."""
    keep = keep or keep_count()
    old = list_backups(folder)[keep:]
    for backup in old:
        backup.unlink()
    return old


def info(backup: Path) -> dict:
    """What's inside a backup (from its backup_info.json)."""
    with zipfile.ZipFile(backup) as zf:
        return json.loads(zf.read("backup_info.json"))


# ---------- running inside Memoria ----------

async def back_up_now() -> Path:
    """Make one backup while Memoria briefly holds all changes, then tidy old ones."""
    import memoria  # here, so restore_backup.py can use this file without loading Cognee
    async with memoria.paused():
        path = await asyncio.to_thread(make_backup, memoria.STORE)
    removed = prune()
    details = info(path)
    memoria._log("memoria", "backup", file=path.name, memories=details["memories"],
                 size_kb=round(path.stat().st_size / 1024), removed=[p.name for p in removed])
    return path


async def backup_loop() -> None:
    """Back up when due (at start-up if the newest is over a day old), then once a day."""
    import memoria
    while True:
        try:
            if is_due():
                path = await back_up_now()
                print(f"Memoria backed up to {path}", file=sys.stderr)
        except Exception as e:  # never let a failed backup stop Memoria
            print(f"Memoria backup failed: {type(e).__name__}: {e}", file=sys.stderr)
            memoria._log("memoria", "backup_error", error=f"{type(e).__name__}: {e}")
            await asyncio.sleep(3600)  # try again in an hour
            continue
        await asyncio.sleep(_seconds_until_due())


def _seconds_until_due() -> float:
    times = [t for t in (made_at(b) for b in list_backups()) if t]
    if not times:
        return 60
    due = max(times) + EVERY - datetime.now(timezone.utc)
    return max(60, due.total_seconds())


_task: asyncio.Task | None = None


def start() -> None:
    """Called when Memoria starts."""
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(backup_loop())
