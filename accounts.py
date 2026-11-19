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
Only a hash (SHA-256) of the secret is stored, so a copy of the database can't be
used to log in. The full key is shown once, when it's made.

Everything is kept in Memoria's database (db.py: the accounts, api_keys and
invites tables), so it's in the daily backups, and manage_accounts.py can change
it safely while Memoria is running. This file doesn't import Cognee, so
manage_accounts.py starts quickly.
"""
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone

import db
import identity

LOCAL = identity.LOCAL
KEY_PREFIX = "mem_"

# Spam protection: what each developer account may do, unless changed per account.
DEFAULT_LIMITS = {
    "requests_per_minute": 60,     # per agent
    "account_requests_per_minute": 120,  # all of the account's agents together
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


def _check_name(value: str, what: str) -> str:
    value = (value or "").strip().lower()
    if not identity.AGENT_ID_PATTERN.match(value):
        raise AccountError(f"{what} must be 2-40 lowercase letters, numbers and dashes.")
    return value


def _account(row) -> dict:
    info = dict(row)
    info["limits"] = json.loads(info["limits"])
    return info


# ---------- accounts ----------

def create_account(account: str, name: str = "", email: str = "") -> dict:
    account = _check_name(account, "The account ID")
    if account == LOCAL:
        raise AccountError("'local' is reserved for this laptop's own agents.")
    with db.transaction() as conn:
        if conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account,)).fetchone():
            raise AccountError(f"There is already an account called '{account}'.")
        conn.execute("INSERT INTO accounts (id, name, email, created_at) VALUES (?, ?, ?, ?)",
                     (account, name.strip()[:100], email.strip()[:200], _now()))
    return get_account(account)


def get_account(account: str) -> dict | None:
    rows = db.query("SELECT * FROM accounts WHERE id = ?", (account,))
    return _account(rows[0]) if rows else None


def list_accounts() -> list[dict]:
    return [_account(row) for row in db.query("SELECT * FROM accounts ORDER BY id")]


def _require_account(account: str) -> None:
    if get_account(account) is None:
        raise AccountError(f"No account called '{account}'.")


def set_status(account: str, status: str) -> None:
    """Turn an account off ("disabled") or back on ("active"). Its memories stay."""
    _require_account(account)
    db.execute("UPDATE accounts SET status = ? WHERE id = ?", (status, account))


def set_limit(account: str, name: str, value: int | None) -> None:
    """Change one limit for one account (None goes back to the default)."""
    if name not in DEFAULT_LIMITS:
        raise AccountError(f"Unknown limit '{name}'. Limits: {', '.join(DEFAULT_LIMITS)}.")
    with db.transaction() as conn:
        row = conn.execute("SELECT limits FROM accounts WHERE id = ?", (account,)).fetchone()
        if row is None:
            raise AccountError(f"No account called '{account}'.")
        own = json.loads(row["limits"])
        if value is None:
            own.pop(name, None)
        else:
            own[name] = int(value)
        conn.execute("UPDATE accounts SET limits = ? WHERE id = ?", (json.dumps(own), account))


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
    _require_account(account)
    most = limits(account)["max_agents"]
    agents = {row["agent"] for row in db.query(
        "SELECT DISTINCT agent FROM api_keys WHERE account = ? AND revoked_at IS NULL",
        (account,))}
    if agent not in agents and len(agents) >= most:
        raise AccountError(f"'{account}' already has the most agents allowed ({most}).")
    # The agent's signing key, so its memories are signed like everyone else's.
    who = identity.principal(account, agent)
    if identity.public_key_of(who) is None:
        identity.register_agent(who)
    key_id = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    db.execute("INSERT INTO api_keys (key_id, account, agent, hash, created_at) "
               "VALUES (?, ?, ?, ?, ?)", (key_id, account, agent, _hash(secret), _now()))
    return f"{KEY_PREFIX}{key_id}_{secret}"


def revoke_key(key_id: str) -> dict:
    """Stop an API key working, for good."""
    db.execute("UPDATE api_keys SET revoked_at = coalesce(revoked_at, ?) WHERE key_id = ?",
               (_now(), key_id))
    rows = db.query("SELECT * FROM api_keys WHERE key_id = ?", (key_id,))
    if not rows:
        raise AccountError(f"No API key with ID '{key_id}'.")
    return dict(rows[0])


def check_key(api_key: str) -> tuple[str, str]:
    """Return (account, agent) for a valid API key, or raise AccountError."""
    parts = (api_key or "").strip().split("_", 2)  # the secret itself may contain "_"
    key_id, secret = (parts[1], parts[2]) if len(parts) == 3 and parts[0] == "mem" \
        else (None, None)
    rows = db.query("SELECT k.*, a.status FROM api_keys k JOIN accounts a ON a.id = k.account "
                    "WHERE k.key_id = ?", (key_id,)) if key_id else []
    key = rows[0] if rows else None
    # Compare hashes in constant time, and do the same work even when the ID is unknown,
    # so timing doesn't reveal which key IDs exist.
    expected = key["hash"] if key else _hash("no such key")
    if not hmac.compare_digest(_hash(secret or ""), expected) or key is None:
        raise AccountError("Invalid API key.")
    if key["revoked_at"]:
        raise AccountError("This API key has been revoked.")
    if key["status"] != "active":
        raise AccountError("This account is disabled.")
    return key["account"], key["agent"]


def keys_of(account: str) -> list[dict]:
    return [{k: row[k] for k in ("key_id", "account", "agent", "created_at", "revoked_at")}
            for row in db.query("SELECT * FROM api_keys WHERE account = ? ORDER BY created_at",
                                (account,))]


# ---------- invite codes (for sign-up, see signup.py) ----------

def create_invite() -> str:
    code = "inv_" + secrets.token_urlsafe(12)
    db.execute("INSERT INTO invites (hash, created_at) VALUES (?, ?)", (_hash(code), _now()))
    return code


def check_invite(code: str) -> None:
    """Raise AccountError if an invite code can't be used (without using it up)."""
    rows = db.query("SELECT used_by FROM invites WHERE hash = ?", (_hash((code or "").strip()),))
    if not rows:
        raise AccountError("That invite code isn't valid.")
    if rows[0]["used_by"]:
        raise AccountError("That invite code has already been used.")


def use_invite(code: str, account: str) -> None:
    """Mark an invite as used by an account. Raises AccountError if it can't be used."""
    with db.transaction() as conn:
        check_invite(code)
        conn.execute("UPDATE invites SET used_by = ?, used_at = ? WHERE hash = ?",
                     (account, _now(), _hash(code.strip())))


def unused_invites() -> int:
    return db.query("SELECT count(*) FROM invites WHERE used_by IS NULL")[0][0]


# ---------- sign-ups waiting for their email link (see signup.py) ----------

SIGNUP_LINK_HOURS = 24
SIGNUP_AGENT = "main"  # the agent a new account's first API key belongs to


def _later(hours: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc).replace(microsecond=0)
            + timedelta(hours=hours)).isoformat()


def start_signup(account: str, email: str, invite: str = "") -> str:
    """Reserve an account name for an email address until its link is clicked.

    Returns the secret for the email link. Nothing is created until finish_signup().
    One account per email address, counting sign-ups still waiting for their link.
    """
    account = _check_name(account, "The account name")
    if account == LOCAL:
        raise AccountError("'local' is reserved for this laptop's own agents.")
    email = (email or "").strip().lower()
    now = _now()
    with db.transaction() as conn:
        waiting = "used_at IS NULL AND expires_at > ?"
        if conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account,)).fetchone() or \
                conn.execute(f"SELECT 1 FROM signups WHERE account = ? AND {waiting}",
                             (account, now)).fetchone():
            raise AccountError(f"The name '{account}' is already taken. Please choose another.")
        if conn.execute("SELECT 1 FROM accounts WHERE lower(email) = ?", (email,)).fetchone() or \
                conn.execute(f"SELECT 1 FROM signups WHERE email = ? AND {waiting}",
                             (email, now)).fetchone():
            raise AccountError("This email address already has an account, or a sign-up "
                               "waiting for its email link to be clicked.")
        if signup_mode() == "invite":
            check_invite(invite)
            conn.execute("UPDATE invites SET used_by = ?, used_at = ? WHERE hash = ?",
                         (account, now, _hash(invite.strip())))
        secret = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO signups (hash, account, email, created_at, expires_at) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (_hash(secret), account, email, now, _later(SIGNUP_LINK_HOURS)))
    return secret


def cancel_signup(secret: str) -> None:
    """Forget a sign-up whose email couldn't be sent, so it can be tried again."""
    db.execute("DELETE FROM signups WHERE hash = ? AND used_at IS NULL", (_hash(secret or ""),))


def finish_signup(secret: str) -> tuple[str, str, str]:
    """The email link was clicked: create the account and its API key, once.

    Returns (account, agent, api_key). The key is shown only this once.
    """
    key_hash = _hash((secret or "").strip())
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM signups WHERE hash = ?", (key_hash,)).fetchone()
        if row is None:
            raise AccountError("This link isn't valid. Please check you copied all of it.")
        if row["used_at"]:
            raise AccountError("This link has already been used. The API key was shown only "
                               "once, when it was first opened.")
        if row["expires_at"] <= _now():
            raise AccountError("This link has expired. Please sign up again.")
        conn.execute("UPDATE signups SET used_at = ? WHERE hash = ?", (_now(), key_hash))
    account = row["account"]
    try:
        create_account(account, email=row["email"])
        api_key = create_key(account, SIGNUP_AGENT)
    except BaseException:
        # Undo, so the link can be used again once the problem is fixed.
        db.execute("DELETE FROM accounts WHERE id = ? AND NOT EXISTS "
                   "(SELECT 1 FROM api_keys WHERE account = ?)", (account, account))
        db.execute("UPDATE signups SET used_at = NULL WHERE hash = ?", (key_hash,))
        raise
    return account, SIGNUP_AGENT, api_key


# ---------- keys for more agents, by email link (see signup.py, POST /keys) ----------

def start_key_request(account: str, email: str, agent: str) -> tuple[str, str] | None:
    """A request for a NEW agent's API key in an existing account.

    Returns None if no active account has this name and email (the caller answers the
    same either way, so nobody learns who has an account). Otherwise returns
    ("link", secret) for the email link, or ("problem", explanation) when the agent
    name is taken or the account has all the agents it may have.
    Keys are only ever made for new agents: a key for an existing agent would let
    whoever asked act as that agent.
    """
    account = (account or "").strip().lower()
    email = (email or "").strip().lower()
    agent = _check_name(agent, "The agent name")
    info = get_account(account)
    if not info or info["status"] != "active" or not email \
            or (info.get("email") or "").strip().lower() != email:
        return None
    now = _now()
    with db.transaction() as conn:
        taken = conn.execute("SELECT 1 FROM api_keys WHERE account = ? AND agent = ?",
                             (account, agent)).fetchone() or conn.execute(
            "SELECT 1 FROM key_requests WHERE account = ? AND agent = ? AND used_at IS NULL "
            "AND expires_at > ?", (account, agent, now)).fetchone()
        if taken:
            return "problem", (f"Your account '{account}' already has an agent called "
                               f"'{agent}', or a request for it waiting. Please choose a "
                               "different name for the new agent.")
        most = limits(account)["max_agents"]
        agents = {row["agent"] for row in conn.execute(
            "SELECT DISTINCT agent FROM api_keys WHERE account = ? AND revoked_at IS NULL",
            (account,))}
        if len(agents) >= most:
            return "problem", (f"Your account '{account}' already has the most agents "
                               f"allowed ({most}), so no key was made for '{agent}'.")
        secret = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO key_requests (hash, account, agent, created_at, expires_at) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (_hash(secret), account, agent, now, _later(SIGNUP_LINK_HOURS)))
    return "link", secret


def cancel_key_request(secret: str) -> None:
    db.execute("DELETE FROM key_requests WHERE hash = ? AND used_at IS NULL",
               (_hash(secret or ""),))


def link_kind(secret: str) -> str | None:
    """Which kind of email link this is: "signup", "agent" or None (unknown)."""
    key_hash = _hash((secret or "").strip())
    if db.query("SELECT 1 FROM signups WHERE hash = ?", (key_hash,)):
        return "signup"
    if db.query("SELECT 1 FROM key_requests WHERE hash = ?", (key_hash,)):
        return "agent"
    return None


def finish_key_request(secret: str) -> tuple[str, str, str]:
    """The email link was clicked: make the new agent's API key, once.

    Returns (account, agent, api_key). The key is shown only this once.
    """
    key_hash = _hash((secret or "").strip())
    with db.transaction() as conn:
        row = conn.execute("SELECT r.*, a.status FROM key_requests r JOIN accounts a "
                           "ON a.id = r.account WHERE r.hash = ?", (key_hash,)).fetchone()
        if row is None:
            raise AccountError("This link isn't valid. Please check you copied all of it.")
        if row["used_at"]:
            raise AccountError("This link has already been used. The API key was shown only "
                               "once, when it was first opened.")
        if row["expires_at"] <= _now():
            raise AccountError("This link has expired. Please ask for the key again.")
        if row["status"] != "active":
            raise AccountError("This account is disabled.")
        if conn.execute("SELECT 1 FROM api_keys WHERE account = ? AND agent = ?",
                        (row["account"], row["agent"])).fetchone():
            raise AccountError(f"The agent '{row['agent']}' already has a key.")
        conn.execute("UPDATE key_requests SET used_at = ? WHERE hash = ?", (_now(), key_hash))
    try:
        api_key = create_key(row["account"], row["agent"])
    except BaseException:
        db.execute("UPDATE key_requests SET used_at = NULL WHERE hash = ?", (key_hash,))
        raise
    return row["account"], row["agent"], api_key


def signup_mode() -> str:
    """'off' (the default), 'invite' (needs an invite code) or 'open' (anyone)."""
    mode = os.environ.get("MEMORIA_SIGNUP", "off").strip().lower()
    return mode if mode in ("off", "invite", "open") else "off"
