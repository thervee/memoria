"""Developer accounts and API keys, for hosting Memoria online.

Each developer has an ACCOUNT (e.g. "acme"). Each account can have several agents,
and every API key belongs to exactly one agent of one account, e.g. acme's "trader".
A request made with that key IS acme's trader: it can't act as another agent, and
it only ever sees acme's memories.

This laptop's own agents belong to the built-in account "local" and keep logging
in with login passes (identity.py), exactly as before.

API keys look like:  mem_3f9a1c2b7d4e_<43 random characters>
                     ^^^^ ^^^^^^^^^^^^ ^^^^^^^^^^^^^^^^^^^^^^^
                     kind    key ID           the secret
Only a hash (SHA-256) of the secret is stored, so a copy of accounts.json can't be
used to log in. The full key is shown once, when it's made.

Everything is kept in memory_store/accounts.json (so it's in the daily backups).
This file doesn't import Cognee, so manage_accounts.py starts quickly.
"""
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

import identity

HERE = Path(__file__).resolve().parent
ACCOUNTS_FILE = HERE / "memory_store" / "accounts.json"
LOCAL = identity.LOCAL
KEY_PREFIX = "mem_"

# Spam protection: what each developer account may do, unless changed per account.
DEFAULT_LIMITS = {
    "requests_per_minute": 60,     # per API key
    "writes_per_day": 200,         # saves + updates (each one costs AI processing)
    "max_memories": 2000,          # memories kept at once
    "max_agents": 10,              # agents (and so API keys in use) per account
}


class AccountError(ValueError):
    """Something about an account or API key is wrong, explained in plain words."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


# ---------- reading and writing accounts.json ----------

_cache = {"mtime": None, "data": None, "path": None}


def _empty() -> dict:
    return {"accounts": {}, "keys": {}, "invites": {}}


def load() -> dict:
    """The accounts file, re-read only when it has changed (it's checked on every request)."""
    try:
        mtime = ACCOUNTS_FILE.stat().st_mtime_ns
    except FileNotFoundError:
        return _empty()
    if _cache["mtime"] != mtime or _cache["path"] != ACCOUNTS_FILE:
        data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
        _cache.update(mtime=mtime, data=data, path=ACCOUNTS_FILE)
    return _cache["data"]


def _save(data: dict) -> None:
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACCOUNTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(ACCOUNTS_FILE)
    _cache["mtime"] = None


def _fresh() -> dict:
    """A copy to change and save (never change the cached copy in place)."""
    return json.loads(json.dumps(load()))


def _check_name(value: str, what: str) -> str:
    value = (value or "").strip().lower()
    if not identity.AGENT_ID_PATTERN.match(value):
        raise AccountError(f"{what} must be 2-40 lowercase letters, numbers and dashes.")
    return value


# ---------- accounts ----------

def create_account(account: str, name: str = "", email: str = "") -> dict:
    account = _check_name(account, "The account ID")
    if account == LOCAL:
        raise AccountError("'local' is reserved for this laptop's own agents.")
    data = _fresh()
    if account in data["accounts"]:
        raise AccountError(f"There is already an account called '{account}'.")
    data["accounts"][account] = {"name": name.strip()[:100], "email": email.strip()[:200],
                                 "created_at": _now(), "status": "active", "limits": {}}
    _save(data)
    return data["accounts"][account]


def get_account(account: str) -> dict | None:
    return load()["accounts"].get(account)


def set_status(account: str, status: str) -> None:
    """Turn an account off ("disabled") or back on ("active"). Its memories stay."""
    data = _fresh()
    if account not in data["accounts"]:
        raise AccountError(f"No account called '{account}'.")
    data["accounts"][account]["status"] = status
    _save(data)


def set_limit(account: str, name: str, value: int | None) -> None:
    """Change one limit for one account (None goes back to the default)."""
    if name not in DEFAULT_LIMITS:
        raise AccountError(f"Unknown limit '{name}'. Limits: {', '.join(DEFAULT_LIMITS)}.")
    data = _fresh()
    if account not in data["accounts"]:
        raise AccountError(f"No account called '{account}'.")
    limits = data["accounts"][account]["limits"]
    if value is None:
        limits.pop(name, None)
    else:
        limits[name] = int(value)
    _save(data)


def limits(account: str) -> dict | None:
    """The limits for an account, or None for this laptop's own agents (no limits)."""
    if account == LOCAL:
        return None
    info = get_account(account) or {}
    return {**DEFAULT_LIMITS, **info.get("limits", {})}


# ---------- API keys ----------

def create_key(account: str, agent: str) -> str:
    """Make a new API key for one agent of an account. Returns the full key (shown once)."""
    agent = _check_name(agent, "The agent ID")
    data = _fresh()
    info = data["accounts"].get(account)
    if info is None:
        raise AccountError(f"No account called '{account}'.")
    agents = {k["agent"] for k in data["keys"].values()
              if k["account"] == account and not k.get("revoked_at")}
    if agent not in agents and len(agents) >= limits(account)["max_agents"]:
        raise AccountError(f"'{account}' already has the most agents allowed "
                           f"({limits(account)['max_agents']}).")
    # The agent's signing key, so its memories are signed like everyone else's.
    who = identity.principal(account, agent)
    if identity.public_key_of(who) is None:
        identity.register_agent(who)
    key_id = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    data["keys"][key_id] = {"account": account, "agent": agent, "hash": _hash(secret),
                            "created_at": _now(), "revoked_at": None}
    _save(data)
    return f"{KEY_PREFIX}{key_id}_{secret}"


def revoke_key(key_id: str) -> dict:
    """Stop an API key working, for good."""
    data = _fresh()
    key = data["keys"].get(key_id)
    if key is None:
        raise AccountError(f"No API key with ID '{key_id}'.")
    key["revoked_at"] = key["revoked_at"] or _now()
    _save(data)
    return key


def check_key(api_key: str) -> tuple[str, str]:
    """Return (account, agent) for a valid API key, or raise AccountError."""
    parts = (api_key or "").strip().split("_", 2)  # the secret itself may contain "_"
    key_id, secret = (parts[1], parts[2]) if len(parts) == 3 and parts[0] == "mem" \
        else (None, None)
    data = load()
    key = data["keys"].get(key_id) if key_id else None
    # Compare hashes in constant time, and do the same work even when the ID is unknown,
    # so timing doesn't reveal which key IDs exist.
    expected = key["hash"] if key else _hash("no such key")
    if not hmac.compare_digest(_hash(secret or ""), expected) or key is None:
        raise AccountError("Invalid API key.")
    if key.get("revoked_at"):
        raise AccountError("This API key has been revoked.")
    account = data["accounts"].get(key["account"])
    if account is None or account["status"] != "active":
        raise AccountError("This account is disabled.")
    return key["account"], key["agent"]


def keys_of(account: str) -> list[dict]:
    return [{"key_id": key_id, **{k: v for k, v in key.items() if k != "hash"}}
            for key_id, key in load()["keys"].items() if key["account"] == account]


# ---------- invite codes (for sign-up, see signup.py) ----------

def create_invite() -> str:
    code = "inv_" + secrets.token_urlsafe(12)
    data = _fresh()
    data["invites"][_hash(code)] = {"created_at": _now(), "used_by": None, "used_at": None}
    _save(data)
    return code


def use_invite(code: str, account: str) -> None:
    """Mark an invite as used by an account. Raises AccountError if it can't be used."""
    data = _fresh()
    invite = data["invites"].get(_hash((code or "").strip()))
    if invite is None:
        raise AccountError("That invite code isn't valid.")
    if invite["used_by"]:
        raise AccountError("That invite code has already been used.")
    invite.update(used_by=account, used_at=_now())
    _save(data)


def signup_mode() -> str:
    """'off' (the default), 'invite' (needs an invite code) or 'open' (anyone)."""
    mode = os.environ.get("MEMORIA_SIGNUP", "off").strip().lower()
    return mode if mode in ("off", "invite", "open") else "off"
