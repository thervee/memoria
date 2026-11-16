"""Restore memory_store from one of Memoria's automatic backups.

Usage:  .venv\\Scripts\\python.exe restore_backup.py            (choose from a list)
        .venv\\Scripts\\python.exe restore_backup.py <file.zip>  (restore that file)
Or double-click restore_backup.bat.

Memoria must be stopped first (stop_memoria.bat). Nothing is thrown away: the
current memory_store is renamed to memory_store.old-<date> and kept.
"""
import shutil
import socket
import sys
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from dotenv import load_dotenv

import backup

HERE = Path(__file__).resolve().parent
STORE = HERE / "memory_store"
PORT = 8765


def memoria_running() -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def check_backup(path: Path) -> dict:
    """Make sure a file is a sound Memoria backup. Returns its details."""
    try:
        with zipfile.ZipFile(path) as zf:
            if zf.testzip() is not None:
                raise backup.BackupError(f"{path.name} is damaged.")
            for name in zf.namelist():
                parts = PurePosixPath(name).parts
                # Every file must land inside memory_store (never "..\\something").
                if name != "backup_info.json" and (parts[0] != "memory_store" or ".." in parts
                                                   or name.startswith("/") or ":" in name):
                    raise backup.BackupError(f"{path.name} contains an unexpected file: {name}")
            details = backup.info(path)
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        raise backup.BackupError(f"{path.name} isn't a Memoria backup ({e}).") from None
    return details


def restore(path: Path, store: Path = STORE, now: datetime | None = None) -> Path:
    """Put the backup in place of the store. Returns where the old store was kept."""
    check_backup(path)
    now = now or datetime.now()
    # Short folder names on purpose: Cognee's database paths are long, and Windows
    # refuses paths over 260 characters.
    restored = store.parent / "memory_store.new"
    kept = store.parent / f"memory_store.old-{now:%Y%m%d-%H%M%S}"
    if restored.exists():
        raise backup.BackupError(f"{restored} is left over from an earlier restore; "
                                 "delete it and try again.")
    prefix = "memory_store/"
    with zipfile.ZipFile(path) as zf:
        for member in zf.infolist():
            if member.is_dir() or not member.filename.startswith(prefix):
                continue
            target = restored / member.filename[len(prefix):]
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as source, open(target, "wb") as out:
                shutil.copyfileobj(source, out)
    if store.exists():
        store.rename(kept)
        # Bring the downloaded search model across, so it doesn't download again.
        if (kept / "models").exists():
            (kept / "models").rename(restored / "models")
    restored.rename(store)
    return kept


def _choose(backups: list[Path]) -> Path | None:
    print("Backups, newest first:\n")
    for number, path in enumerate(backups, 1):
        try:
            details = backup.info(path)
            when = datetime.fromisoformat(details["made_at"]).astimezone()
            text = f"{when:%d %b %Y, %H:%M}  -  {details['memories']} memories"
        except Exception:
            text = "(can't read this one)"
        print(f"  {number:>2}. {text}  ({path.stat().st_size // 1024} KB)  {path.name}")
    answer = input("\nType the number to restore (or just press Enter to cancel): ").strip()
    if not answer:
        return None
    if not answer.isdigit() or not 1 <= int(answer) <= len(backups):
        sys.exit("That isn't one of the numbers. Nothing was changed.")
    return backups[int(answer) - 1]


def main() -> None:
    load_dotenv(HERE / ".env")  # for MEMORIA_BACKUP_DIR
    if memoria_running():
        sys.exit("Memoria is running. Stop it first with stop_memoria.bat, then try again.")
    if len(sys.argv) > 1:
        chosen = Path(sys.argv[1])
    else:
        backups = backup.list_backups()
        if not backups:
            sys.exit(f"There are no backups in {backup.backup_dir()} yet.")
        chosen = _choose(backups)
        if chosen is None:
            print("Cancelled. Nothing was changed.")
            return
    try:
        details = check_backup(chosen)
        kept = restore(chosen)
    except backup.BackupError as e:
        sys.exit(f"Not restored: {e}")
    print(f"\nRestored {details['memories']} memories from {chosen.name}.")
    print(f"Your previous memory store is kept at {kept}")
    print("  (delete that folder once you're happy with the restore).")
    print("Start Memoria again with start_memoria.bat.")


if __name__ == "__main__":
    main()
