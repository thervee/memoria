"""Watchdog: starts Memoria and restarts it if it ever crashes.

Started at Windows login by start_memoria_hidden.vbs. Stopped by stop_memoria.bat.
Everything it does is written to memory_store\\server.log.
"""
import msvcrt
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
STORE = HERE / "memory_store"
LOG = STORE / "server.log"
LOCK = STORE / "watchdog.lock"
PID_FILE = STORE / "watchdog.pid"
PYTHON = HERE / ".venv" / "Scripts" / "python.exe"
MAX_LOG_BYTES = 5_000_000  # start a fresh log when it passes about 5 MB


def note(message: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[watchdog {datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")


def main() -> None:
    STORE.mkdir(parents=True, exist_ok=True)

    # Only one watchdog at a time: hold a lock on a file for as long as we run.
    lock = open(LOCK, "w")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return  # another watchdog is already looking after Memoria
    PID_FILE.write_text(str(os.getpid()))

    if LOG.exists() and LOG.stat().st_size > MAX_LOG_BYTES:
        LOG.replace(LOG.with_suffix(".old.log"))
    note("watchdog started")

    wait = 5
    while True:
        started = time.time()
        with LOG.open("a", encoding="utf-8") as log:
            server = subprocess.Popen(
                [str(PYTHON), str(HERE / "server.py")],
                cwd=HERE, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            code = server.wait()

        if code == 0:
            # Clean exit: usually "already running" because someone started it by hand.
            note("Memoria exited normally (probably already running elsewhere); checking again in 60s")
            time.sleep(60)
            continue

        # A crash. If it had been running a while, restart quickly; if it keeps
        # crashing straight away, wait longer each time (up to 1 minute).
        wait = 5 if time.time() - started > 120 else min(wait * 2, 60)
        note(f"Memoria crashed (exit code {code}); restarting in {wait}s")
        time.sleep(wait)
        note("restarting Memoria")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never die silently
        note(f"watchdog error: {e!r}")
        sys.exit(1)
