"""Where Memoria keeps its data, and whether it's running online.

On this laptop nothing needs setting: data stays where it always was, next to
the code (memory_store, keys, backups), and Memoria listens on 127.0.0.1:8765.

Online (e.g. on Railway) two environment variables change that:
  MEMORIA_ONLINE=1          listen on 0.0.0.0 and the PORT the host gives, turn the
                            dashboard off, and read visitors' real addresses from
                            the host's proxy (for the spam limits)
  MEMORIA_DATA_DIR=/data    keep ALL data in this one folder (mount a volume there):
                              /data/memory_store   database, Cognee's data, logs
                              /data/keys           agents' signing keys (locked)
                              /data/backups        the daily backups
The Dockerfile sets both, so on Railway they're already right.

Secrets (the Gemini key, MEMORIA_SECRET_KEY) are always read from environment
variables. On this laptop they come from the .env file, which is never copied
into the Docker image or uploaded to GitHub.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")  # this laptop's settings; real environment variables win


def _is_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


ONLINE = _is_on("MEMORIA_ONLINE")
_data = os.environ.get("MEMORIA_DATA_DIR", "").strip()
DATA_DIR = Path(_data).resolve() if _data else None

STORE = DATA_DIR / "memory_store" if DATA_DIR else HERE / "memory_store"
KEYS = DATA_DIR / "keys" if DATA_DIR else HERE / "keys"
BACKUPS = DATA_DIR / "backups" if DATA_DIR else HERE / "backups"

HOST = os.environ.get("MEMORIA_HOST", "").strip() or ("0.0.0.0" if ONLINE else "127.0.0.1")
PORT = int((os.environ.get("PORT", "").strip() if ONLINE else "")
           or os.environ.get("MEMORIA_PORT", "").strip() or 8765)


SECRET_NAME = "MEMORIA_SECRET_KEY"


def server_secret() -> str:
    """MEMORIA_SECRET_KEY, cleaned up from the usual ways of pasting it.

    In Railway's Variables tab it's easy to paste the whole line
    (MEMORIA_SECRET_KEY=abc...=) into the Value box, or to add quotes or spaces.
    All of these give the same secret here.
    """
    value = os.environ.get(SECRET_NAME, "").strip()
    for _ in range(2):  # quotes may be around the value or around the whole line
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1].strip()
        name, sign, rest = value.partition("=")
        if sign and name.strip().upper() == SECRET_NAME:
            value = rest.strip()
    return value


def online_problems() -> list[str]:
    """Settings that are missing or wrong for running online. Empty means all good."""
    if not ONLINE:
        return []
    problems = []
    if not os.environ.get("LLM_API_KEY", "").strip():
        problems.append("LLM_API_KEY (your Gemini API key) isn't set.")
    secret = server_secret()
    if not secret:
        problems.append("MEMORIA_SECRET_KEY isn't set. Make one on your laptop with "
                        "'python manage_accounts.py make-secret'.")
    else:
        from cryptography.fernet import Fernet
        try:
            Fernet(secret)
        except ValueError:
            problems.append(f"MEMORIA_SECRET_KEY isn't a valid secret (it's {len(secret)} "
                            "characters; a secret is 44, ending in '='). Paste exactly the "
                            "Value that 'python manage_accounts.py make-secret' printed.")
    if DATA_DIR is None:
        problems.append("MEMORIA_DATA_DIR isn't set, so data would be lost on every deploy. "
                        "Set it to the folder where the volume is mounted, e.g. /data.")
    on_railway = any(os.environ.get(n) for n in ("RAILWAY_ENVIRONMENT_NAME", "RAILWAY_PROJECT_ID"))
    volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if on_railway and not volume:
        problems.append("No Railway volume is attached, so data would be lost on every deploy. "
                        "Add a volume to this service, mounted at /data.")
    elif volume and DATA_DIR is not None:
        mount = Path(volume).resolve()
        if DATA_DIR != mount and mount not in DATA_DIR.parents:
            problems.append(f"MEMORIA_DATA_DIR ({DATA_DIR}) isn't on the Railway volume "
                            f"({mount}). Mount the volume at {DATA_DIR} or change one of them.")
    return problems
