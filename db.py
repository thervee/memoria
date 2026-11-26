"""Memoria's database: one SQLite file, memory_store/memoria.db.

SQLite is a full database kept in a single file, built into Python: nothing to
install or run, it works the same on this laptop and on a Linux server, and it
is safe when two programs use it at once (Memoria and manage_accounts.py).
Looking up one memory or one API key no longer means reading every memory.

What's in it:
  memories      every memory, its labels, signature, sharing and history
  agents        every agent that has used Memoria (first and last seen)
  usage         each developer account's saves and updates today (for its limits)
  forget_queue  deleted memories still waiting to be removed from Cognee
  accounts      developer accounts (see accounts.py)
  api_keys      their API keys (hashes only)
  invites       sign-up invite codes (hashes only)
  signups       sign-ups waiting for their email link to be clicked (link hashes only)
  key_requests  requests for a new agent's API key, waiting for their email link (hashes only)
  billing       each developer account's balance of actions and billing settings (billing.py)
  wallets       wallets linked to an account, to pay with $MEMORIA
  wallet_challenges  one-time messages a wallet signs to prove it belongs to an account
  payments      $MEMORIA payments already claimed (each transfer can be claimed only once)
  outcomes      whether a memory helped: one signed result per agent per memory
  wallet_proofs test payments the host asked for before linking a wallet by hand

The activity log stays a plain text file (activity.log), easy to read and search.

The first time it opens, it imports Memoria's older JSON files (memories.json,
agents.json, usage.json, pending_forget.json, accounts.json) and renames each to
<name>.imported, so nothing is lost and nothing is imported twice.

All of Memoria's SQL lives here and in accounts.py, so moving to a database
server (e.g. Postgres) later only means changing these two files.
"""
import json
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import paths

HERE = Path(__file__).resolve().parent
DB_FILE = paths.STORE / "memoria.db"
SCHEMA_VERSION = 10
# Stop sending a memory to Cognee after this many failed tries (it stays "processing"
# and is still found by its exact words). Saving a new version gives it fresh tries.
SYNC_GIVE_UP = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id            TEXT PRIMARY KEY,
    account       TEXT NOT NULL DEFAULT 'local',
    agent_id      TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    topic         TEXT NOT NULL,
    confidence    TEXT NOT NULL,
    note          TEXT NOT NULL,
    reason        TEXT NOT NULL,
    source        TEXT,                         -- where the claim came from (optional)
    updated_at    TEXT,
    updated_by    TEXT,
    visibility    TEXT NOT NULL DEFAULT 'private',
    shared_with   TEXT NOT NULL DEFAULT '[]',   -- JSON list of agent IDs
    share_expires_at TEXT,                      -- UTC time the sharing ends (none: never)
    signature     TEXT,                         -- JSON
    history       TEXT NOT NULL DEFAULT '[]',   -- JSON list of older versions
    needs_sync    INTEGER NOT NULL DEFAULT 0,   -- 1 while Cognee lacks the latest version
    sync_version  INTEGER NOT NULL DEFAULT 0,
    processed     INTEGER,
    added_version INTEGER,
    data_id       TEXT,                         -- where Cognee keeps it
    dataset_id    TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,   -- failed tries at sending this version to Cognee
    last_error    TEXT,                         -- why the last try failed
    restored_from TEXT,                         -- for a restored copy: the exported memory's ID
    unverified_origin TEXT                      -- JSON: restored from a file signed by an agent
                                                -- not on this server (what the file claimed)
);
CREATE INDEX IF NOT EXISTS memories_by_owner ON memories(account, agent_id);
CREATE INDEX IF NOT EXISTS memories_waiting ON memories(needs_sync) WHERE needs_sync = 1;
CREATE INDEX IF NOT EXISTS memories_by_data_id ON memories(account, data_id);

CREATE TABLE IF NOT EXISTS agents (
    principal  TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    account TEXT PRIMARY KEY,
    day     TEXT NOT NULL,
    writes  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS forget_queue (
    data_id    TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    memory_id  TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS accounts (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    email      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    limits     TEXT NOT NULL DEFAULT '{}'      -- JSON: this account's own limits
);
CREATE TABLE IF NOT EXISTS api_keys (
    key_id     TEXT PRIMARY KEY,
    account    TEXT NOT NULL REFERENCES accounts(id),
    agent      TEXT NOT NULL,
    hash       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS api_keys_by_account ON api_keys(account);
CREATE TABLE IF NOT EXISTS invites (
    hash       TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    used_by    TEXT,
    used_at    TEXT
);
CREATE TABLE IF NOT EXISTS signups (
    hash       TEXT PRIMARY KEY,               -- of the secret in the email link
    account    TEXT NOT NULL,
    email      TEXT NOT NULL,                  -- lowercase
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT
);
CREATE INDEX IF NOT EXISTS signups_by_account ON signups(account);
CREATE INDEX IF NOT EXISTS signups_by_email ON signups(email);
CREATE TABLE IF NOT EXISTS key_requests (
    hash       TEXT PRIMARY KEY,               -- of the secret in the email link
    account    TEXT NOT NULL,
    agent      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT
);
CREATE INDEX IF NOT EXISTS key_requests_by_account ON key_requests(account);
CREATE TABLE IF NOT EXISTS billing (
    account          TEXT PRIMARY KEY,
    actions          INTEGER NOT NULL DEFAULT 0,   -- 1 credit = MEMORIA_ACTIONS_PER_CREDIT actions
    starter_given    INTEGER NOT NULL DEFAULT 0,   -- 1 once the starter credits were added
    public_purchases INTEGER NOT NULL DEFAULT 0,   -- 1: /purchases/<account> is public
    low_alert_sent   INTEGER NOT NULL DEFAULT 0,   -- 1 once the "low balance" email went out
    empty_alert_sent INTEGER NOT NULL DEFAULT 0,   -- 1 once the "out of actions" email went out
    leftover         TEXT NOT NULL DEFAULT '0',    -- paid $MEMORIA units not yet a whole credit
    updated_at       TEXT
);
CREATE TABLE IF NOT EXISTS wallets (
    address   TEXT PRIMARY KEY,                    -- lowercase 0x...; one account per wallet
    account   TEXT NOT NULL,
    agent     TEXT NOT NULL,
    linked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS wallets_by_account ON wallets(account);
CREATE TABLE IF NOT EXISTS wallet_challenges (
    nonce      TEXT PRIMARY KEY,
    account    TEXT NOT NULL,
    agent      TEXT NOT NULL,
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT
);
CREATE INDEX IF NOT EXISTS wallet_challenges_by_owner ON wallet_challenges(account, agent);
CREATE TABLE IF NOT EXISTS payments (
    tx_hash      TEXT NOT NULL,                    -- lowercase 0x...
    log_index    INTEGER NOT NULL,
    account      TEXT NOT NULL,
    agent        TEXT NOT NULL,
    from_address TEXT NOT NULL,
    amount       TEXT NOT NULL,                    -- token units (with 18 decimals), as text
    credits      INTEGER NOT NULL,                 -- credits and actions of the whole claim are
    actions      INTEGER NOT NULL,                 -- kept on its first transfer, 0 on the others
    block_number INTEGER NOT NULL,
    claimed_at   TEXT NOT NULL,
    PRIMARY KEY (tx_hash, log_index)              -- so a payment can never be claimed twice
);
CREATE INDEX IF NOT EXISTS payments_by_account ON payments(account);
CREATE TABLE IF NOT EXISTS outcomes (
    memory_id   TEXT NOT NULL,
    account     TEXT NOT NULL,
    agent_id    TEXT NOT NULL,                     -- the agent that recorded it
    result      TEXT NOT NULL,                     -- success, failure or mixed
    note        TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL,
    signature   TEXT,                              -- JSON, like memories' signatures
    PRIMARY KEY (memory_id, agent_id)              -- one outcome per agent per memory
);
CREATE TABLE IF NOT EXISTS wallet_proofs (
    account    TEXT NOT NULL,
    address    TEXT NOT NULL,                     -- lowercase 0x...
    amount     TEXT NOT NULL,                     -- the exact test amount, in token units
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_tx    TEXT,                              -- the transaction that proved it
    PRIMARY KEY (account, address)
);
"""

MEMORY_FIELDS = ("id", "account", "agent_id", "created_at", "topic", "confidence", "note",
                 "reason", "source", "updated_at", "updated_by", "visibility", "shared_with", "share_expires_at",
                 "signature", "history", "restored_from", "unverified_origin", "needs_sync", "sync_version", "processed", "added_version",
                 "data_id", "dataset_id")
JSON_FIELDS = {"shared_with": [], "signature": None, "history": [], "unverified_origin": None}
TRUE_FALSE_FIELDS = ("needs_sync", "processed")

# One connection per process, used by one thread at a time.
_lock = threading.RLock()
_state = {"path": None, "conn": None}


# ---------- opening the database ----------

def connection() -> sqlite3.Connection:
    with _lock:
        if _state["conn"] is None or _state["path"] != DB_FILE:
            close()
            DB_FILE.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(DB_FILE, isolation_level=None, check_same_thread=False,
                                   timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")     # readers don't wait for writers
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")   # wait for another program's write
            conn.executescript(SCHEMA)
            # Version 2 added memories.source; older databases get the column, empty.
            if "source" not in {row[1] for row in conn.execute("PRAGMA table_info(memories)")}:
                conn.execute("ALTER TABLE memories ADD COLUMN source TEXT")
            # Version 5 added the balance alert flags; older databases get them, unset.
            billing_columns = {row[1] for row in conn.execute("PRAGMA table_info(billing)")}
            for column in ("low_alert_sent", "empty_alert_sent"):
                if column not in billing_columns:
                    conn.execute(f"ALTER TABLE billing ADD COLUMN {column} INTEGER NOT NULL "
                                 "DEFAULT 0")
            # Version 6 added leftover $MEMORIA (part of a credit) per account.
            if "leftover" not in billing_columns:
                conn.execute("ALTER TABLE billing ADD COLUMN leftover TEXT NOT NULL DEFAULT '0'")
            # Version 7 added failed tries per memory; older memories start with none.
            memory_columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
            if "attempts" not in memory_columns:
                conn.execute("ALTER TABLE memories ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
            if "last_error" not in memory_columns:
                conn.execute("ALTER TABLE memories ADD COLUMN last_error TEXT")
            # Version 8 added share expiry; older shares never expire.
            if "share_expires_at" not in memory_columns:
                conn.execute("ALTER TABLE memories ADD COLUMN share_expires_at TEXT")
            # Version 9 records where a restored memory came from (restore_memories).
            if "restored_from" not in memory_columns:
                conn.execute("ALTER TABLE memories ADD COLUMN restored_from TEXT")
            # Version 10 marks memories restored from files signed by agents not on this server.
            if "unverified_origin" not in memory_columns:
                conn.execute("ALTER TABLE memories ADD COLUMN unverified_origin TEXT")
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            _import_json_files(conn, DB_FILE.parent)
            _state.update(path=DB_FILE, conn=conn)
        return _state["conn"]


def close() -> None:
    with _lock:
        if _state["conn"] is not None:
            _state["conn"].close()
        _state.update(path=None, conn=None)


@contextmanager
def transaction():
    """Several changes that happen all together or not at all."""
    with _lock:
        conn = connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


def query(sql: str, params=()) -> list[sqlite3.Row]:
    with _lock:
        return connection().execute(sql, params).fetchall()


def execute(sql: str, params=()) -> int:
    """Run one change. Returns how many rows it changed."""
    with _lock:
        return connection().execute(sql, params).rowcount


# ---------- memories ----------

def _to_row(mem: dict) -> list:
    values = []
    for field in MEMORY_FIELDS:
        value = mem.get(field)
        if field in JSON_FIELDS:
            value = json.dumps(value if value is not None else JSON_FIELDS[field],
                               ensure_ascii=False)
        elif field == "needs_sync":
            value = int(bool(value))  # missing (older memories) means "nothing to do"
        elif field in TRUE_FALSE_FIELDS:
            value = None if value is None else int(bool(value))
        elif field == "account":
            value = value or "local"
        elif field == "visibility":
            value = value or "private"
        elif field == "sync_version":
            value = int(value or 0)
        values.append(value)
    return values


def _to_memory(row: sqlite3.Row | None) -> dict | None:
    """A database row as the memory dict Memoria works with (empty fields left out)."""
    if row is None:
        return None
    mem = {}
    for field in MEMORY_FIELDS:
        value = row[field]
        if field in JSON_FIELDS and value is not None:
            value = json.loads(value)
        elif field in TRUE_FALSE_FIELDS and value is not None:
            value = bool(value)
        if value is not None:
            mem[field] = value
    return mem


_UPSERT = (f"INSERT INTO memories ({', '.join(MEMORY_FIELDS)}) "
           f"VALUES ({', '.join('?' for _ in MEMORY_FIELDS)}) ON CONFLICT(id) DO UPDATE SET "
           + ", ".join(f"{f} = excluded.{f}" for f in MEMORY_FIELDS if f != "id")
           # A new version (e.g. an update) gets fresh tries at reaching Cognee.
           + ", attempts = CASE WHEN excluded.sync_version != memories.sync_version THEN 0 "
             "ELSE memories.attempts END")


_INSERT_NEW = (f"INSERT OR IGNORE INTO memories ({', '.join(MEMORY_FIELDS)}) "
               f"VALUES ({', '.join('?' for _ in MEMORY_FIELDS)})")


_INSERT_ONLY = (f"INSERT INTO memories ({', '.join(MEMORY_FIELDS)}) "
                f"VALUES ({', '.join('?' for _ in MEMORY_FIELDS)}) ON CONFLICT(id) DO NOTHING")


def add_memory(mem: dict) -> bool:
    """Save a NEW memory. Never replaces one: returns False if a memory with this ID
    already exists, in any account, and leaves that memory exactly as it was."""
    return execute(_INSERT_ONLY, _to_row(mem)) == 1


def put_memory(mem: dict) -> None:
    """Save changes to a memory that was looked up (and its owner checked) first."""
    execute(_UPSERT, _to_row(mem))


def get_memory(memory_id: str) -> dict | None:
    rows = query("SELECT * FROM memories WHERE id = ?", (memory_id,))
    return _to_memory(rows[0]) if rows else None


def remove_memory(memory_id: str) -> None:
    """Delete a memory and its outcomes (callers wrap this in a transaction)."""
    execute("DELETE FROM outcomes WHERE memory_id = ?", (memory_id,))
    execute("DELETE FROM memories WHERE id = ?", (memory_id,))


# ---------- outcomes: did a memory help? ----------

_OUTCOME_FIELDS = ("memory_id", "account", "agent_id", "result", "note", "recorded_at",
                   "signature")
# The latest outcome of a memory: newest first, then by agent ID so ties are stable.
LATEST_OUTCOME = ("(SELECT o.result FROM outcomes o WHERE o.memory_id = memories.id "
                  "ORDER BY o.recorded_at DESC, o.agent_id LIMIT 1)")


def _to_outcome(row) -> dict:
    out = {f: row[f] for f in _OUTCOME_FIELDS}
    out["signature"] = json.loads(out["signature"]) if out["signature"] else None
    return out


def put_outcome(outcome: dict) -> None:
    """Save an agent's outcome for a memory, replacing its earlier one."""
    values = [outcome.get(f) for f in _OUTCOME_FIELDS]
    values[-1] = json.dumps(outcome.get("signature"), ensure_ascii=False)
    execute(f"INSERT INTO outcomes ({', '.join(_OUTCOME_FIELDS)}) VALUES "
            f"({', '.join('?' for _ in _OUTCOME_FIELDS)}) ON CONFLICT(memory_id, agent_id) DO "
            "UPDATE SET " + ", ".join(f"{f} = excluded.{f}" for f in _OUTCOME_FIELDS[1:]), values)


def get_outcome(memory_id: str, agent_id: str) -> dict | None:
    rows = query("SELECT * FROM outcomes WHERE memory_id = ? AND agent_id = ?",
                 (memory_id, agent_id))
    return _to_outcome(rows[0]) if rows else None


def outcomes_for(memory_ids: list[str]) -> dict[str, list[dict]]:
    """Every outcome of these memories, newest first, by memory ID."""
    found: dict[str, list[dict]] = {}
    ids = list(dict.fromkeys(memory_ids))
    for start in range(0, len(ids), 500):  # SQLite allows only so many ? at once
        part = ids[start:start + 500]
        for row in query(f"SELECT * FROM outcomes WHERE memory_id IN "
                         f"({', '.join('?' for _ in part)}) ORDER BY recorded_at DESC, agent_id",
                         part):
            found.setdefault(row["memory_id"], []).append(_to_outcome(row))
    return found


def already_restored(account: str, memory_id: str) -> bool:
    """True if this account has the memory, or a restored copy of it, already."""
    return bool(query("SELECT 1 FROM memories WHERE account = ? AND (id = ? OR restored_from = ?) "
                      "LIMIT 1", (account, memory_id, memory_id)))


def count_memories(account: str) -> int:
    return query("SELECT count(*) FROM memories WHERE account = ?", (account,))[0][0]


def has_searchable(account: str) -> bool:
    """True if Cognee holds at least one of this account's memories."""
    return bool(query("SELECT 1 FROM memories WHERE account = ? AND data_id IS NOT NULL LIMIT 1",
                      (account,)))


def memories_by_data_id(account: str, data_ids: list[str]) -> dict[str, dict]:
    """This account's memories that Cognee keeps under these IDs."""
    found = {}
    data_ids = list(dict.fromkeys(data_ids))
    for start in range(0, len(data_ids), 500):  # SQLite allows only so many ? at once
        part = data_ids[start:start + 500]
        rows = query(f"SELECT * FROM memories WHERE account = ? AND data_id IN "
                     f"({', '.join('?' for _ in part)})", [account, *part])
        found.update({row["data_id"]: _to_memory(row) for row in rows})
    return found


def _utc_now() -> str:
    """Now, written the way Memoria stores times, so the two compare as text."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def visible_memories(account: str, agent_id: str, *, waiting: bool = False,
                     topic: str | None = None, saved_by: str | None = None,
                     limit: int | None = None, topic_under: str | None = None,
                     latest_outcome: str | None = None) -> list[dict]:
    """Memories this agent may read (its own, plus ones shared with it whose sharing
    hasn't expired), newest first.

    topic: topics containing this text. topic_under: this topic and everything under it
    ("customers" matches "customers" and "customers/acme", not "customers-old").
    """
    sql = ["SELECT * FROM memories WHERE account = ? AND (agent_id = ? OR (visibility = 'shared'"
           " AND (share_expires_at IS NULL OR share_expires_at > ?)"
           " AND EXISTS (SELECT 1 FROM json_each(memories.shared_with) WHERE value = ?)))"]
    params: list = [account, agent_id, _utc_now(), agent_id]
    if waiting:
        sql.append("AND needs_sync = 1")
    if topic:
        sql.append("AND instr(lower(topic), lower(?)) > 0")
        params.append(topic.strip())
    if topic_under:
        sql.append("AND (lower(topic) = lower(?) OR "
                   "substr(lower(topic), 1, length(?) + 1) = lower(?) || '/')")
        params += [topic_under, topic_under, topic_under]
    if saved_by:
        sql.append("AND agent_id = ?")
        params.append(saved_by.strip().lower())
    if latest_outcome == "none":
        sql.append("AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.memory_id = memories.id)")
    elif latest_outcome:
        sql.append(f"AND {LATEST_OUTCOME} = ?")
        params.append(latest_outcome)
    sql.append("ORDER BY coalesce(updated_at, created_at) DESC, id")
    if limit:
        sql.append("LIMIT ?")
        params.append(int(limit))
    return [_to_memory(row) for row in query(" ".join(sql), params)]


def all_memories() -> list[dict]:
    """Every memory of every account, newest first (for the host's dashboard)."""
    return [_to_memory(row) for row in
            query("SELECT * FROM memories ORDER BY coalesce(updated_at, created_at) DESC, id")]


def waiting_memories() -> list[dict]:
    """Memories whose latest version Cognee doesn't have yet, fewest failed tries first
    (so memories that keep failing never hold up new ones), leaving out those Memoria
    gave up on (SYNC_GIVE_UP)."""
    return [_to_memory(row) for row in
            query("SELECT * FROM memories WHERE needs_sync = 1 AND attempts < ? "
                  "ORDER BY attempts, created_at, id", (SYNC_GIVE_UP,))]


def sync_failed(memory_id: str, error: str) -> bool:
    """Note one failed try at sending a memory to Cognee. True if Memoria now gives up."""
    with transaction() as conn:
        conn.execute("UPDATE memories SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                     (error[:1000], memory_id))
        row = conn.execute("SELECT attempts FROM memories WHERE id = ?",
                           (memory_id,)).fetchone()
        return bool(row) and row["attempts"] >= SYNC_GIVE_UP


def record_added(memory_id: str, data_id: str, dataset_id: str, version: int) -> bool:
    """Remember where Cognee stored a memory. False if the memory was deleted meanwhile."""
    return execute("UPDATE memories SET data_id = ?, dataset_id = ?, processed = 0, "
                   "added_version = ? WHERE id = ?",
                   (data_id, dataset_id, version, memory_id)) == 1


def mark_synced(memory_id: str, version: int, processed: bool) -> None:
    """Cognee now has this version: done, unless the memory was changed again meanwhile."""
    execute("UPDATE memories SET processed = CASE WHEN ? THEN 1 ELSE processed END, "
            "needs_sync = CASE WHEN sync_version = ? THEN 0 ELSE needs_sync END WHERE id = ?",
            (int(processed), version, memory_id))


# ---------- agents, usage and the forget queue ----------

def touch_agent(principal: str, now: str) -> None:
    execute("INSERT INTO agents (principal, first_seen, last_seen) VALUES (?, ?, ?) "
            "ON CONFLICT(principal) DO UPDATE SET last_seen = excluded.last_seen",
            (principal, now, now))


def use_write(account: str, today: str, most: int) -> bool:
    """Count one save or update for today. False (and nothing counted) if over `most`."""
    with transaction() as conn:
        row = conn.execute("SELECT day, writes FROM usage WHERE account = ?",
                           (account,)).fetchone()
        writes = row["writes"] if row and row["day"] == today else 0
        if writes >= most:
            return False
        conn.execute("INSERT INTO usage (account, day, writes) VALUES (?, ?, ?) "
                     "ON CONFLICT(account) DO UPDATE SET day = excluded.day, "
                     "writes = excluded.writes", (account, today, writes + 1))
        return True


def writes_today(account: str, today: str) -> int:
    rows = query("SELECT writes FROM usage WHERE account = ? AND day = ?", (account, today))
    return rows[0][0] if rows else 0


def queue_forget(data_id: str, dataset_id: str, memory_id: str) -> None:
    execute("INSERT OR IGNORE INTO forget_queue (data_id, dataset_id, memory_id) "
            "VALUES (?, ?, ?)", (data_id, dataset_id, memory_id))


def forget_queue() -> list[dict]:
    return [dict(row) for row in query("SELECT * FROM forget_queue ORDER BY rowid")]


def forget_tried(data_id: str, done: bool, give_up_after: int) -> bool:
    """Note one try at removing an item. Returns True if Memoria gave up on it."""
    with transaction() as conn:
        conn.execute("UPDATE forget_queue SET attempts = attempts + 1 WHERE data_id = ?",
                     (data_id,))
        row = conn.execute("SELECT attempts FROM forget_queue WHERE data_id = ?",
                           (data_id,)).fetchone()
        gave_up = bool(row) and not done and row["attempts"] >= give_up_after
        if done or gave_up:
            conn.execute("DELETE FROM forget_queue WHERE data_id = ?", (data_id,))
        return gave_up


def has_work() -> bool:
    return bool(query("SELECT 1 FROM memories WHERE needs_sync = 1 AND attempts < ? "
                      "UNION ALL SELECT 1 FROM forget_queue LIMIT 1", (SYNC_GIVE_UP,)))


# ---------- importing Memoria's older JSON files ----------

def _import_json_files(conn: sqlite3.Connection, folder: Path) -> None:
    names = ("memories.json", "agents.json", "usage.json", "pending_forget.json",
             "accounts.json")
    found = {name: folder / name for name in names if (folder / name).exists()}
    if not found:
        return
    data = {name: json.loads(path.read_text(encoding="utf-8")) for name, path in found.items()}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for mem in data.get("memories.json", {}).values():
            conn.execute(_INSERT_NEW, _to_row(mem))
        for principal, seen in data.get("agents.json", {}).items():
            conn.execute("INSERT OR IGNORE INTO agents VALUES (?, ?, ?)",
                         (principal, seen.get("first_seen", ""),
                          seen.get("last_seen", seen.get("first_seen", ""))))
        for account, used in data.get("usage.json", {}).items():
            conn.execute("INSERT OR IGNORE INTO usage VALUES (?, ?, ?)",
                         (account, used["day"], used["writes"]))
        for item in data.get("pending_forget.json", []):
            conn.execute("INSERT OR IGNORE INTO forget_queue VALUES (?, ?, ?, ?)",
                         (item["data_id"], item["dataset_id"], item["memory_id"],
                          item.get("attempts", 0)))
        saved = data.get("accounts.json", {})
        for account, info in saved.get("accounts", {}).items():
            conn.execute("INSERT OR IGNORE INTO accounts VALUES (?, ?, ?, ?, ?, ?)",
                         (account, info.get("name", ""), info.get("email", ""),
                          info["created_at"], info.get("status", "active"),
                          json.dumps(info.get("limits", {}))))
        for key_id, key in saved.get("keys", {}).items():
            conn.execute("INSERT OR IGNORE INTO api_keys VALUES (?, ?, ?, ?, ?, ?)",
                         (key_id, key["account"], key["agent"], key["hash"],
                          key["created_at"], key.get("revoked_at")))
        for code_hash, invite in saved.get("invites", {}).items():
            conn.execute("INSERT OR IGNORE INTO invites VALUES (?, ?, ?, ?)",
                         (code_hash, invite["created_at"], invite.get("used_by"),
                          invite.get("used_at")))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    for path in found.values():  # keep the originals, under a name that isn't read again
        path.replace(path.with_name(path.name + ".imported"))
    print(f"Memoria moved {', '.join(found)} into {DB_FILE.name}.", file=sys.stderr)
