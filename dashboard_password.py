"""The dashboard password: only a salted scrypt hash is ever stored.

Kept separate from dashboard.py so setting a password doesn't load Cognee.
The hash lives in keys/dashboard_password.json (git-ignored, your Windows account only).
"""
import hashlib
import hmac
import json
import secrets

import identity

PASSWORD_FILE = identity.KEYS_DIR / "dashboard_password.json"
MIN_PASSWORD_LENGTH = 8


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)


def is_set() -> bool:
    return PASSWORD_FILE.exists()


def set_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"The password must be at least {MIN_PASSWORD_LENGTH} characters.")
    identity.KEYS_DIR.mkdir(exist_ok=True)
    salt = secrets.token_bytes(16)
    PASSWORD_FILE.write_text(json.dumps({
        "scheme": "scrypt-n16384-r8-p1",
        "salt": salt.hex(),
        "hash": _hash(password, salt).hex(),
    }), encoding="utf-8")


def check(password: str) -> bool:
    if not PASSWORD_FILE.exists():
        return False
    saved = json.loads(PASSWORD_FILE.read_text(encoding="utf-8"))
    attempt = _hash(password, bytes.fromhex(saved["salt"]))
    return hmac.compare_digest(attempt, bytes.fromhex(saved["hash"]))
