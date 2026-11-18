"""The public, read-only demo key (for the Docs page).

Anyone may use the demo key, so it can do very little:
  - it belongs to its own account, "demo", with one agent, "demo"
  - it may ONLY use find_memory: save, update, share and delete are refused (server.py)
  - it only ever sees the demo account's 3 sample memories, which are about Memoria
    itself (memories never cross accounts, see memoria.can_see)
  - it has low request limits, so it can't be used to load the server

When Memoria starts online it sets all of this up by itself, once: the account, the
key and the 3 sample memories (saved and signed by the demo agent). Only a hash of
the key is kept here, like every other API key, so this file holds no secret.
"""
import sys

import accounts
import db
import identity
import memoria

ACCOUNT = "demo"
AGENT = "demo"
ACCOUNT_NAME = "Memoria public demo"
KEY_ID = "7bc3decac803"
KEY_HASH = "875072cb5a0d3d84a3b1ff476746c6eb707221330f93c927b09f42fd0b5b0d62"

LIMITS = {
    "requests_per_minute": 20,
    "account_requests_per_minute": 20,
    "writes_per_day": 3,   # only the 3 sample memories, saved once at start-up
    "max_memories": 3,
    "max_agents": 1,
}

SAMPLES = [
    {"topic": "memoria/about", "confidence": "high",
     "note": "Memoria is shared long-term memory for AI agents. Agents save findings and "
             "decisions with save_memory and look them up later with find_memory.",
     "reason": "Sample memory for the public demo key."},
    {"topic": "memoria/signatures", "confidence": "high",
     "note": "Every Memoria memory is signed by the agent that saved it, and find_memory "
             "shows whether each signature is verified.",
     "reason": "Sample memory for the public demo key."},
    {"topic": "memoria/privacy", "confidence": "high",
     "note": "Memoria memories are private to the agent that saved them unless shared with "
             "named agents, and never cross from one account to another.",
     "reason": "Sample memory for the public demo key."},
]


def is_read_only(account: str) -> bool:
    return account == ACCOUNT


async def ensure() -> None:
    """Create the demo account, key and sample memories if they aren't there yet."""
    info = accounts.get_account(ACCOUNT)
    if info is None:
        accounts.create_account(ACCOUNT, ACCOUNT_NAME)
    elif info.get("name") != ACCOUNT_NAME:  # someone else's account: leave it alone
        print(f"Memoria demo: an account called '{ACCOUNT}' already exists and isn't the "
              "demo account, so the demo key was not set up.", file=sys.stderr)
        return
    for name, value in LIMITS.items():
        accounts.set_limit(ACCOUNT, name, value)
    who = identity.principal(ACCOUNT, AGENT)
    if identity.public_key_of(who) is None:
        identity.register_agent(who)  # its signing key, so the samples are signed
    if not db.query("SELECT 1 FROM api_keys WHERE key_id = ?", (KEY_ID,)):
        db.execute("INSERT INTO api_keys (key_id, account, agent, hash, created_at) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (KEY_ID, ACCOUNT, AGENT, KEY_HASH, accounts._now()))
    if db.count_memories(ACCOUNT) == 0:
        for sample in SAMPLES:
            await memoria.save_memory(AGENT, sample["note"], sample["reason"], sample["topic"],
                                      sample["confidence"], account=ACCOUNT)
