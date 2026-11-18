"""The public, read-only demo keys (for the Docs page).

Anyone may use the demo keys, so they can do very little:
  - they belong to their own account, "demo", with two agents: "demo" (agent A)
    and "demo-b" (agent B), one key each
  - they may ONLY use find_memory: save, update, share and delete are refused (server.py)
  - they only ever see the demo account's 3 sample memories, which are about Memoria
    itself (memories never cross accounts, see memoria.can_see)
  - they have low request limits, so they can't be used to load the server

The samples show sharing and permissions working:
  - A's memory shared with B     A and B both see it
  - A's private memory           only A sees it
  - B's private memory           only B sees it

When Memoria starts online it sets all of this up by itself: the account, the keys
and the samples (saved and signed by their agents). If the samples in the database
aren't exactly the ones below, they are replaced. Only hashes of the keys are kept
here, like every other API key, so this file holds no secret.
"""
import sys

import accounts
import db
import identity
import memoria

ACCOUNT = "demo"
AGENT_A = "demo"
AGENT_B = "demo-b"
AGENT = AGENT_A  # the first demo key's agent
ACCOUNT_NAME = "Memoria public demo"
KEYS = {  # key ID -> (agent, hash of the key's secret)
    "7bc3decac803": (AGENT_A, "875072cb5a0d3d84a3b1ff476746c6eb707221330f93c927b09f42fd0b5b0d62"),
    "aeab1c37b20d": (AGENT_B, "61a8185f95af3d39db5478aca7aea0ab42ffba254b5750e2a3fde79afce7ae4e"),
}

LIMITS = {
    "requests_per_minute": 20,
    "account_requests_per_minute": 20,
    "writes_per_day": 3,   # only the 3 sample memories, saved at start-up
    "max_memories": 3,
    "max_agents": 2,
}

_REASON = "Sample memory for the public demo keys."
SAMPLES = [
    {"agent": AGENT_A, "visibility": "shared", "shared_with": [AGENT_B],
     "topic": "memoria/sharing", "confidence": "high",
     "note": "Demo agent A shared this memory with demo agent B, so both of them can find "
             "it. In Memoria an agent shares a memory by naming the agents allowed to read it."},
    {"agent": AGENT_A, "visibility": "private", "shared_with": [],
     "topic": "memoria/privacy", "confidence": "high",
     "note": "Demo agent A kept this memory private, so only agent A can find it. Memoria "
             "memories are private to the agent that saved them unless it shares them."},
    {"agent": AGENT_B, "visibility": "private", "shared_with": [],
     "topic": "memoria/signatures", "confidence": "high",
     "note": "Demo agent B kept this memory private, so only agent B can find it. Every "
             "Memoria memory is signed by the agent that saved it."},
]


def is_read_only(account: str) -> bool:
    return account == ACCOUNT


def _samples_in_place() -> bool:
    have = sorted((m["agent_id"], m["visibility"], tuple(m.get("shared_with", [])), m["topic"],
                   m["note"]) for m in db.all_memories() if m.get("account") == ACCOUNT)
    want = sorted((s["agent"], s["visibility"], tuple(s["shared_with"]), s["topic"], s["note"])
                  for s in SAMPLES)
    return have == want


async def ensure() -> None:
    """Create the demo account, keys and sample memories if they aren't there yet."""
    info = accounts.get_account(ACCOUNT)
    if info is None:
        accounts.create_account(ACCOUNT, ACCOUNT_NAME)
    elif info.get("name") != ACCOUNT_NAME:  # someone else's account: leave it alone
        print(f"Memoria demo: an account called '{ACCOUNT}' already exists and isn't the "
              "demo account, so the demo keys were not set up.", file=sys.stderr)
        return
    for name, value in LIMITS.items():
        accounts.set_limit(ACCOUNT, name, value)
    for agent in (AGENT_A, AGENT_B):
        who = identity.principal(ACCOUNT, agent)
        if identity.public_key_of(who) is None:
            identity.register_agent(who)  # its signing key, so its samples are signed
    for key_id, (agent, key_hash) in KEYS.items():
        if not db.query("SELECT 1 FROM api_keys WHERE key_id = ?", (key_id,)):
            db.execute("INSERT INTO api_keys (key_id, account, agent, hash, created_at) "
                       "VALUES (?, ?, ?, ?, ?)",
                       (key_id, ACCOUNT, agent, key_hash, accounts._now()))
    if _samples_in_place():
        return
    # Replace the demo account's memories (and only those) with the samples.
    for mem in db.all_memories():
        if mem.get("account") == ACCOUNT:
            await memoria.delete_memory(mem["agent_id"], mem["id"], account=ACCOUNT)
    db.execute("DELETE FROM usage WHERE account = ?", (ACCOUNT,))  # the samples' own writes
    for s in SAMPLES:
        await memoria.save_memory(s["agent"], s["note"], _REASON, s["topic"], s["confidence"],
                                  s["visibility"], s["shared_with"] or None, account=ACCOUNT)
