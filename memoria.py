"""Memoria's memory engine: save, find and update labelled memories with Cognee.

Every memory carries four labels: which agent wrote it, when, its topic,
and a confidence level (low / medium / high).

Every memory belongs to one ACCOUNT: "local" for this laptop's own agents, or a
developer's account when Memoria is hosted online (see accounts.py). Agents only
ever see memories of their own account, and each account has its own Cognee
dataset, so one developer's searches never touch another's memories.

Every memory also has a visibility setting:
  - private: only the agent that wrote it can find or update it.
  - shared:  the writer plus the agent IDs listed in shared_with.

Every memory is signed (Ed25519) by the agent that saved or last updated it.
find_memory re-checks the signature and reports "verified" or "NOT verified".
The signature covers the memory's content and authorship labels (ID, topic,
confidence, note, reason, who wrote it and when) but not its sharing settings.

Where things are kept (all inside the memory_store folder):
  - memoria.db: Memoria's database (see db.py): every memory with its labels,
    signature and history, the agents, each account's usage and the list of
    deleted memories still to be removed from Cognee.
  - Cognee's databases: the memories again, plus the knowledge graph Cognee
    builds from them (used for searching).
  - activity.log: who saved, searched or updated what, and when.

Saving is fast: save, update and delete only change Memoria's database and answer
straight away. A background worker then does the slow part (Cognee reading the
memory with Gemini and adding it to the knowledge graph), one job at a time,
grouping memories that arrive together into one run (at most RUN_SIZE of them, so
a burst of saves can't make one run use too much memory).

Cognee and its search model are only loaded the first time they're needed (a search
of stored memories, or background work), not at start-up, to keep memory use low.
After every background run Memoria frees what it can and logs how much memory it
uses ("Memoria memory: ... MB"), so it can be watched in the host's logs. Until it is done the
memory is marked "processing" and find_memory finds it by its exact words.
Nothing is lost if Memoria stops: the worker picks up unfinished work next start.
"""
import asyncio
import ctypes
import gc
import json
import re
import secrets
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

import accounts
import db
import identity
from paths import STORE

cognee = None  # Cognee, loaded the first time it's needed: see _engine()

DATASET = "memoria"
ACTIVITY_FILE = STORE / "activity.log"
LOCAL = identity.LOCAL

CONFIDENCE_LEVELS = ("low", "medium", "high")
VISIBILITY_LEVELS = ("private", "shared")
# Agent IDs are short and simple: lowercase letters, numbers and dashes, e.g. "claude-code".
AGENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
# Spam protection: the longest text accepted, for everyone.
MAX_LENGTHS = {"note": 4000, "new_note": 4000, "reason": 2000, "topic": 100, "query": 1000,
               "memory_id": 40}
MAX_SHARED_WITH = 20

_write_lock = asyncio.Lock()  # one change to memories at a time
_cognee_lock = asyncio.Lock()  # held by the background worker while it changes Cognee
FORGET_GIVE_UP = 5  # stop retrying a Cognee removal after this many failures


RUN_SIZE = 3  # at most this many memories in one background run


class MemoriaError(ValueError):
    """A problem with what the agent asked for, explained in plain words."""


# ---------- Cognee, loaded on first use, and memory use ----------

def _engine():
    """Cognee, loading it (and Memoria's memory limits for it) the first time it's used."""
    global cognee
    if cognee is None:
        from setup_cognee import cognee as loaded
        cognee = loaded
        log_memory("after loading Cognee")
    return cognee


def memory_mb() -> int | None:
    """How much memory this process uses right now, in MB (None if unknown)."""
    try:
        with open("/proc/self/status", encoding="ascii") as f:  # Linux (Railway)
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    if sys.platform == "win32":  # this laptop
        try:
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD),
                            ("peak", ctypes.c_size_t), ("now", ctypes.c_size_t)] +                            [(f"x{i}", ctypes.c_size_t) for i in range(7)]
            kernel = ctypes.windll.kernel32
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            kernel.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE,
                                                       ctypes.POINTER(Counters), wintypes.DWORD]
            c = Counters()
            c.cb = ctypes.sizeof(c)
            if kernel.K32GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(c), c.cb):
                return c.now // (1024 * 1024)
        except (AttributeError, OSError):
            pass
    return None


def log_memory(when: str) -> None:
    """Write one line like "Memoria memory: 312 MB (after startup)" to the server log."""
    mb = memory_mb()
    if mb is not None:
        print(f"Memoria memory: {mb} MB ({when})", file=sys.stderr, flush=True)


def _free_memory() -> None:
    """Give back memory that a finished background run no longer needs."""
    gc.collect()
    if sys.platform.startswith("linux"):
        try:  # ask the C memory allocator to return freed memory to the system
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except (OSError, AttributeError):
            pass


# ---------- small helpers ----------

def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _log(agent_id: str, action: str, **details) -> None:
    STORE.mkdir(parents=True, exist_ok=True)
    entry = {"time": _now(), "agent": agent_id, "action": action, **details}
    with ACTIVITY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def account_of(mem: dict) -> str:
    return mem.get("account", LOCAL)  # memories from before accounts existed are "local"


def dataset_for(account: str) -> str:
    """Each account's own Cognee dataset ("memoria" for this laptop's agents)."""
    return DATASET if account == LOCAL else f"{DATASET}-{account}"


def check_agent(agent_id: str, account: str = LOCAL) -> str:
    """Make sure the agent ID is valid and remember that this agent exists."""
    agent_id = (agent_id or "").strip().lower()
    if not AGENT_ID_PATTERN.match(agent_id) or not AGENT_ID_PATTERN.match(account or ""):
        raise MemoriaError(
            "agent_id must be 2-40 characters: lowercase letters, numbers and dashes "
            "(for example 'claude-code' or 'research-bot')."
        )
    db.touch_agent(identity.principal(account, agent_id), _now())
    return agent_id


def _check_confidence(confidence: str) -> str:
    confidence = (confidence or "").strip().lower()
    if confidence not in CONFIDENCE_LEVELS:
        raise MemoriaError("confidence must be 'low', 'medium' or 'high'.")
    return confidence


def _check_sharing(visibility: str, shared_with, owner: str) -> tuple[str, list[str]]:
    """Check a visibility setting and clean up the list of agents it is shared with."""
    visibility = (visibility or "").strip().lower()
    if visibility not in VISIBILITY_LEVELS:
        raise MemoriaError("visibility must be 'private' or 'shared'.")
    if visibility == "private":
        return "private", []
    if isinstance(shared_with, str):  # allow "trader, analyst" as well as a list
        shared_with = shared_with.split(",")
    if len(shared_with or []) > MAX_SHARED_WITH:
        raise MemoriaError(f"A memory can be shared with {MAX_SHARED_WITH} agents at most.")
    readers = []
    for agent in shared_with or []:
        agent = agent.strip().lower()
        if not AGENT_ID_PATTERN.match(agent):
            raise MemoriaError(f"'{agent}' is not a valid agent ID in shared_with.")
        if agent != owner and agent not in readers:
            readers.append(agent)
    if not readers:
        raise MemoriaError(
            "A shared memory needs at least one other agent ID in shared_with, "
            "e.g. [\"trader\"]. Use visibility 'private' to keep it to yourself."
        )
    return "shared", readers


def can_see(mem: dict, agent_id: str, account: str = LOCAL) -> bool:
    """True if this agent (of this account) is allowed to read the memory."""
    if account_of(mem) != account:  # never across accounts
        return False
    if mem["agent_id"] == agent_id:
        return True
    return mem.get("visibility") == "shared" and agent_id in mem.get("shared_with", [])


def _require(value: str, name: str) -> str:
    value = (value or "").strip()
    if not value:
        raise MemoriaError(f"{name} can't be empty.")
    longest = MAX_LENGTHS.get(name, 10_000)
    if len(value) > longest:
        raise MemoriaError(f"{name} is too long ({longest:,} characters at most).")
    return value


def _use_quota(account: str, new_memory: bool) -> None:
    """Spam protection for developer accounts: refuse writes over the account's limits.

    Call while holding _write_lock. This laptop's own agents have no limits.
    """
    limit = accounts.limits(account)
    if limit is None:
        return
    if new_memory and db.count_memories(account) >= limit["max_memories"]:
        raise MemoriaError(f"This account has reached its limit of {limit['max_memories']:,} "
                           "memories. Delete some, or ask the host for a higher limit.")
    if not db.use_write(account, _now()[:10], limit["writes_per_day"]):
        raise MemoriaError(f"This account has used its {limit['writes_per_day']:,} saves and "
                           "updates for today. The limit resets at midnight (UTC).")


def _as_text(mem: dict) -> str:
    """The text Cognee stores and searches. The labels are written into it too."""
    lines = [
        f"Memoria memory {mem['id']}",
        f"Topic: {mem['topic']}",
        f"Saved by agent: {mem['agent_id']} on {mem['created_at']}",
    ]
    if mem.get("updated_at"):
        lines.append(f"Last updated by agent: {mem['updated_by']} on {mem['updated_at']}")
    lines += [
        f"Confidence: {mem['confidence']}",
        "",
        f"Note: {mem['note']}",
        f"Reason: {mem['reason']}",
    ]
    return "\n".join(lines)


SAVE_ATTEMPTS = 5


def _new_memory_id() -> str:
    """A new memory ID: "mem-" and 32 hex characters (128 random bits).
    Memories saved before this have shorter IDs ("mem-" and 8), which still work."""
    return "mem-" + secrets.token_hex(16)


NO_SOURCE = "no source"
MAX_SOURCE = 500


def _check_source(source) -> str | None:
    """An optional source: a URL, transaction hash, file name or short note. None if empty."""
    if source is None:
        return None
    if not isinstance(source, str):
        raise MemoriaError("source must be text, e.g. a URL, a transaction hash or a file name.")
    source = " ".join(source.split())  # one line, no stray spaces
    if len(source) > MAX_SOURCE:
        raise MemoriaError(f"source can be {MAX_SOURCE} characters at most.")
    return source or None


SIGNED_FIELDS = ("id", "agent_id", "created_at", "topic", "confidence", "note", "reason",
                 "updated_at", "updated_by")


def _signed_content(mem: dict) -> bytes:
    """The exact bytes that get signed: the memory's content and authorship labels."""
    content = {field: mem.get(field) for field in SIGNED_FIELDS}
    if mem.get("source"):  # (left out when there is none, so older signatures still match)
        content["source"] = mem["source"]
    if account_of(mem) != LOCAL:  # (left out for "local", so older signatures still match)
        content["account"] = account_of(mem)
    return ("memoria-memory-v1|" + json.dumps(content, sort_keys=True, ensure_ascii=False,
                                              separators=(",", ":"))).encode("utf-8")


def _sign(mem: dict, agent_id: str) -> None:
    try:
        sig = identity.sign(agent_id, _signed_content(mem))
    except identity.IdentityError as e:
        raise MemoriaError(str(e)) from e
    sig["signed_at"] = _now()
    mem["signature"] = sig


def _verification(mem: dict) -> dict:
    """Check the memory's signature right now."""
    expected = identity.principal(account_of(mem), mem.get("updated_by") or mem["agent_id"])
    ok, detail = identity.verify(_signed_content(mem), mem.get("signature"), expected)
    by_author = ok and (mem.get("updated_by") or mem["agent_id"]) == mem["agent_id"]
    if ok and not by_author:
        # Only possible for memories changed by a shared reader before that was blocked:
        # the signature is the editor's, so never present it as the author's.
        detail += (f", who changed it; '{mem['agent_id']}' saved the original but did NOT "
                   "sign this version")
    if ok:
        detail += ", including its source" if mem.get("source") else " (it has no source)"
    return {"verification": "verified" if ok else "NOT verified", "verification_detail": detail,
            "signed_by_author": by_author}


def _public(mem: dict) -> dict:
    """What agents get back: the memory and its labels (not Cognee's internal IDs)."""
    return {
        **_verification(mem),
        "memory_id": mem["id"],
        "account": account_of(mem),
        "topic": mem["topic"],
        "note": mem["note"],
        "reason": mem["reason"],
        # Where the claim came from, so readers can check it. "no source" means it can't be.
        "source": mem.get("source") or NO_SOURCE,
        "has_source": bool(mem.get("source")),
        "confidence": mem["confidence"],
        "saved_by": mem["agent_id"],
        "saved_at": mem["created_at"],
        "updated_by": mem.get("updated_by"),
        "updated_at": mem.get("updated_at"),
        "times_updated": len(mem.get("history", [])),
        "visibility": mem.get("visibility", "private"),
        "shared_with": mem.get("shared_with", []),
        # True while the background worker is still adding it to Cognee's search.
        "processing": bool(mem.get("needs_sync")),
        "signed_by": (mem.get("signature") or {}).get("signed_by"),
        "public_key": (mem.get("signature") or {}).get("public_key"),
    }


# ---------- the actions ----------

async def save_memory(agent_id: str, note: str, reason: str, topic: str,
                      confidence: str = "medium", visibility: str = "private",
                      shared_with: list[str] | None = None, account: str = LOCAL,
                      source: str | None = None) -> dict:
    agent_id = check_agent(agent_id, account)
    who = identity.principal(account, agent_id)
    visibility, shared_with = _check_sharing(visibility, shared_with, agent_id)
    source = _check_source(source)
    mem = {
        "id": _new_memory_id(),
        "account": account,
        "agent_id": agent_id,
        "created_at": _now(),
        "topic": _require(topic, "topic"),
        "confidence": _check_confidence(confidence),
        "note": _require(note, "note"),
        "reason": _require(reason, "reason"),
        "source": source,
        "history": [],
        "visibility": visibility,
        "shared_with": shared_with,
    }
    _sign(mem, who)  # prove this agent wrote it
    _mark_changed(mem)  # the background worker adds it to Cognee
    async with _write_lock:
        _use_quota(account, new_memory=True)
        # Never replace an existing memory (of any account). An ID that is already
        # taken just gets a new one; with 128 random bits that should never happen.
        for _ in range(SAVE_ATTEMPTS):
            if db.add_memory(mem):
                break
            mem["id"] = _new_memory_id()
            _sign(mem, who)  # the ID is part of what's signed
        else:
            raise MemoriaError("Could not save the memory just now. Please try again.")
    _log(who, "save", memory_id=mem["id"], topic=mem["topic"])
    _kick()
    return _public(mem)


async def find_memory(agent_id: str, query: str, topic: str | None = None,
                      saved_by: str | None = None, limit: int = 5,
                      account: str = LOCAL, outcome: str | None = None) -> list[dict]:
    agent_id = check_agent(agent_id, account)
    outcome = _check_outcome_filter(outcome)
    query = _require(query, "query")
    limit = max(1, min(int(limit or 5), 20))
    _log(identity.principal(account, agent_id), "find", query=query, topic=topic, saved_by=saved_by)
    _kick()  # finish any work left over from before Memoria was restarted

    def wanted(mem: dict) -> bool:
        if not can_see(mem, agent_id, account):  # never show memories this agent isn't allowed to read
            return False
        if topic and topic.strip().lower() not in mem["topic"].lower():
            return False
        if saved_by and saved_by.strip().lower() != mem["agent_id"]:
            return False
        return True

    found: list[dict] = []

    # 1. Ask Cognee for the stored passages closest in meaning to the question.
    #    (If none of this account's memories are in Cognee yet, there's nothing to search.)
    if db.has_searchable(account):
        from cognee import SearchType
        results = await _engine().search(
            query, query_type=SearchType.CHUNKS, datasets=[dataset_for(account)],
            top_k=max(50, limit * 10)
        )
        chunks = _chunks(results)
        by_data_id = db.memories_by_data_id(account, [str(c.get("document_id")) for c in chunks])
        for chunk in chunks:
            mem = by_data_id.get(str(chunk.get("document_id")))
            if mem is None:  # fall back to the memory ID written in the text
                match = re.search(r"Memoria memory (mem-[0-9a-f]{8,32})", chunk.get("text", ""))
                mem = db.get_memory(match.group(1)) if match else None
            if mem and mem not in found and wanted(mem):
                found.append(mem)

    # 2. Memories still being processed aren't in Cognee's search yet (or only in their
    #    old version): find them by the words of the question instead.
    words = _words(query)
    for mem in db.visible_memories(account, agent_id, waiting=True, topic=topic,
                                   saved_by=saved_by):
        if mem not in found and wanted(mem) \
                and words & _words(f"{mem['topic']} {mem['note']} {mem['reason']}"):
            found.append(mem)

    # 3. If a topic was given, top up with the newest memories on that topic.
    if topic and len(found) < limit:
        for mem in db.visible_memories(account, agent_id, topic=topic, saved_by=saved_by,
                                       limit=limit + len(found)):
            if mem not in found and wanted(mem):
                found.append(mem)

    summaries = _summaries(found)
    if outcome:
        found = [m for m in found if _latest_result(summaries[m["id"]]) == outcome]
    found = _ranked(found, summaries)
    return [_with_outcomes(m, summaries[m["id"]]) for m in found[:limit]]


LIST_DEFAULT, LIST_MAX = 20, 100


async def list_memories(agent_id: str, topic: str | None = None, saved_by: str | None = None,
                        limit: int = LIST_DEFAULT, account: str = LOCAL,
                        outcome: str | None = None) -> list[dict]:
    """The memories this agent may read (its own, plus ones shared with it), newest first.

    topic matches that topic and everything under it ("customers/" or "customers" also
    finds "customers/acme"). Uses the same permission check as find_memory.
    """
    agent_id = check_agent(agent_id, account)
    topic = (topic or "").strip().strip("/") or None
    if topic and len(topic) > MAX_LENGTHS["topic"]:
        raise MemoriaError(f"topic can be {MAX_LENGTHS['topic']} characters at most.")
    try:
        limit = max(1, min(int(LIST_DEFAULT if limit is None else limit), LIST_MAX))
    except (TypeError, ValueError):
        raise MemoriaError(f"limit must be a number from 1 to {LIST_MAX}.") from None
    outcome = _check_outcome_filter(outcome)
    _log(identity.principal(account, agent_id), "list", topic=topic, saved_by=saved_by,
         **({"outcome": outcome} if outcome else {}))
    found = [m for m in db.visible_memories(account, agent_id, topic_under=topic,
                                            saved_by=saved_by, limit=limit,
                                            latest_outcome=outcome)
             if can_see(m, agent_id, account)]
    summaries = _summaries(found)
    return [_with_outcomes(m, summaries[m["id"]]) for m in _ranked(found, summaries)]


# ---------- outcomes: did a memory actually help? ----------

OUTCOME_RESULTS = ("success", "failure", "mixed")
OUTCOME_FILTERS = OUTCOME_RESULTS + ("none",)
MAX_OUTCOME_NOTE = 500
# Ranking: each net success (successes minus failures) moves a memory up one place, each
# net failure down one place, at most 2 places either way; on a tie the memory with the
# better record goes first. Relevance still decides the order otherwise, and memories with
# no outcomes don't move. Only outcomes whose signatures check out count.
OUTCOME_STEP, OUTCOME_MAX_SHIFT = 1, 2


def _outcome_content(outcome: dict) -> bytes:
    """The exact bytes that get signed for an outcome."""
    content = {f: outcome.get(f) for f in ("memory_id", "account", "agent_id", "result",
                                           "note", "recorded_at")}
    return ("memoria-outcome-v1|" + json.dumps(content, sort_keys=True, ensure_ascii=False,
                                               separators=(",", ":"))).encode("utf-8")


def _outcome_verified(outcome: dict) -> tuple[bool, str]:
    signer = identity.principal(outcome["account"], outcome["agent_id"])
    return identity.verify(_outcome_content(outcome), outcome.get("signature"), signer)


def _outcome_public(outcome: dict) -> dict:
    ok, detail = _outcome_verified(outcome)
    return {"result": outcome["result"], "note": outcome["note"], "agent": outcome["agent_id"],
            "recorded_at": outcome["recorded_at"],
            "verification": "verified" if ok else "NOT verified",
            "verification_detail": detail}


def _outcome_summary(outcomes: list[dict]) -> dict:
    """Counts by result, the latest outcome and whether every signature checked out."""
    shown = [_outcome_public(o) for o in outcomes]  # newest first
    counts = {r: sum(1 for o in shown if o["result"] == r) for r in OUTCOME_RESULTS}
    verified = {r: sum(1 for o in shown if o["result"] == r and o["verification"] == "verified")
                for r in OUTCOME_RESULTS}
    not_verified = sum(1 for o in shown if o["verification"] != "verified")
    return {**counts, "total": len(shown), "latest": shown[0] if shown else None,
            "signatures": ("none" if not shown else "all verified" if not not_verified
                           else f"{not_verified} NOT verified"),
            "_verified": verified}


def _summaries(memories: list[dict]) -> dict[str, dict]:
    by_memory = db.outcomes_for([m["id"] for m in memories])
    return {m["id"]: _outcome_summary(by_memory.get(m["id"], [])) for m in memories}


def _latest_result(summary: dict) -> str:
    return summary["latest"]["result"] if summary["latest"] else "none"


def _check_outcome_filter(outcome: str | None) -> str | None:
    outcome = (outcome or "").strip().lower() or None
    if outcome and outcome not in OUTCOME_FILTERS:
        raise MemoriaError("outcome must be 'success', 'failure', 'mixed' or 'none'.")
    return outcome


def _ranked(memories: list[dict], summaries: dict[str, dict]) -> list[dict]:
    """Nudge memories that helped up and ones that failed down, a little."""
    def key(item):
        place, mem = item
        good = summaries[mem["id"]]["_verified"]
        net = good["success"] - good["failure"]
        shift = max(-OUTCOME_MAX_SHIFT, min(OUTCOME_MAX_SHIFT, OUTCOME_STEP * net))
        return (place - shift, -net, place)
    return [mem for _, mem in sorted(enumerate(memories), key=key)]


def _with_outcomes(mem: dict, summary: dict) -> dict:
    out = _public(mem)
    out["outcomes"] = {k: v for k, v in summary.items() if not k.startswith("_")}
    return out


def _readable(memory_id: str, agent_id: str, account: str, what: str) -> dict:
    """The memory, if this agent may read it. Never says whether a memory exists."""
    mem = db.get_memory(memory_id)
    if mem is None or not can_see(mem, agent_id, account):
        raise MemoriaError(f"You're not allowed to {what} memory '{memory_id}'.")
    return mem


async def record_outcome(agent_id: str, memory_id: str, result: str, note: str = "",
                         account: str = LOCAL) -> dict:
    """Record whether a memory helped. One outcome per agent per memory: recording again
    replaces this agent's earlier one. Any agent that may read the memory can do this."""
    agent_id = check_agent(agent_id, account)
    who = identity.principal(account, agent_id)
    memory_id = _require(memory_id, "memory_id")
    result = (result or "").strip().lower()
    if result not in OUTCOME_RESULTS:
        raise MemoriaError("result must be 'success', 'failure' or 'mixed'.")
    note = (note or "").strip()
    if len(note) > MAX_OUTCOME_NOTE:
        raise MemoriaError(f"note can be {MAX_OUTCOME_NOTE} characters at most.")
    async with _write_lock:
        _readable(memory_id, agent_id, account, "record an outcome for")
        previous = db.get_outcome(memory_id, agent_id)
        outcome = {"memory_id": memory_id, "account": account, "agent_id": agent_id,
                   "result": result, "note": note, "recorded_at": _now()}
        try:
            sig = identity.sign(who, _outcome_content(outcome))
        except identity.IdentityError as e:
            raise MemoriaError(str(e)) from e
        sig["signed_at"] = _now()
        outcome["signature"] = sig
        db.put_outcome(outcome)
    _log(who, "outcome", memory_id=memory_id, result=result,
         **({"replaced": previous["result"]} if previous else {}))
    summary = _outcome_summary(db.outcomes_for([memory_id]).get(memory_id, []))
    return {"memory_id": memory_id, "recorded": _outcome_public(outcome),
            "replaced": previous["result"] if previous else None,
            "outcomes": {k: v for k, v in summary.items() if not k.startswith("_")}}


async def get_outcomes(agent_id: str, memory_id: str, account: str = LOCAL) -> dict:
    """Every outcome recorded for a memory, newest first, each with its signature check."""
    agent_id = check_agent(agent_id, account)
    memory_id = _require(memory_id, "memory_id")
    _readable(memory_id, agent_id, account, "see the outcomes of")
    outcomes = db.outcomes_for([memory_id]).get(memory_id, [])
    summary = _outcome_summary(outcomes)
    return {"memory_id": memory_id,
            "summary": {k: v for k, v in summary.items() if not k.startswith("_")},
            "outcomes": [_outcome_public(o) for o in outcomes]}


def _chunks(results) -> list[dict]:
    """Cognee may wrap search results per dataset; flatten them into passages."""
    out = []
    for item in results or []:
        if isinstance(item, dict) and "search_result" in item:
            out += [c for c in item["search_result"] if isinstance(c, dict)]
        elif isinstance(item, dict):
            out.append(item)
    return out


def _words(text: str) -> set[str]:
    """The meaningful words in a text (3+ letters), for simple matching."""
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


async def update_memory(agent_id: str, memory_id: str, new_note: str, reason: str,
                        confidence: str | None = None, topic: str | None = None,
                        account: str = LOCAL) -> dict:
    agent_id = check_agent(agent_id, account)
    who = identity.principal(account, agent_id)
    memory_id = _require(memory_id, "memory_id")
    new_note = _require(new_note, "new_note")
    reason = _require(reason, "reason")
    if topic:
        topic = _require(topic, "topic")

    async with _write_lock:
        mem = db.get_memory(memory_id)
        if mem is None or not can_see(mem, agent_id, account):
            raise MemoriaError(f"No memory with ID '{memory_id}'. Use find_memory to look it up.")
        if mem["agent_id"] != agent_id:  # shared memories are read-only for their readers
            raise MemoriaError(
                f"Only the agent that wrote this memory ('{mem['agent_id']}') can update it. "
                "Memories shared with you are read-only: save your own memory instead."
            )
        _use_quota(account, new_memory=False)

        # Keep the old version in the history so nothing is lost.
        mem.setdefault("history", []).append({
            "note": mem["note"], "reason": mem["reason"], "confidence": mem["confidence"],
            "topic": mem["topic"], "replaced_at": _now(), "replaced_by": agent_id,
        })
        mem["note"] = new_note
        mem["reason"] = reason
        if confidence:
            mem["confidence"] = _check_confidence(confidence)
        if topic:
            mem["topic"] = topic.strip()
        mem["updated_at"] = _now()
        mem["updated_by"] = agent_id
        _sign(mem, who)  # the new version is signed by the agent that changed it
        _mark_changed(mem)  # the background worker updates it in Cognee
        db.put_memory(mem)
    _log(who, "update", memory_id=memory_id)
    _kick()
    out = _public(mem)
    out["previous_note"] = mem["history"][-1]["note"]
    return out


async def share_memory(agent_id: str, memory_id: str, visibility: str,
                       shared_with: list[str] | None = None, via: str | None = None,
                       account: str = LOCAL) -> dict:
    """Change who can read a memory. Only the agent that wrote it may do this."""
    agent_id = check_agent(agent_id, account)
    memory_id = _require(memory_id, "memory_id")
    async with _write_lock:
        mem = db.get_memory(memory_id)
        if mem is None or not can_see(mem, agent_id, account):
            raise MemoriaError(f"No memory with ID '{memory_id}'. Use find_memory to look it up.")
        if mem["agent_id"] != agent_id:
            raise MemoriaError(
                f"Only the agent that wrote this memory ('{mem['agent_id']}') can change its sharing."
            )
        before = (mem.get("visibility", "private"), mem.get("shared_with", []))
        mem["visibility"], mem["shared_with"] = _check_sharing(visibility, shared_with, agent_id)
        db.put_memory(mem)
    _log(identity.principal(account, agent_id), "share", memory_id=memory_id,
         before={"visibility": before[0], "shared_with": before[1]},
         after={"visibility": mem["visibility"], "shared_with": mem["shared_with"]},
         **({"via": via} if via else {}))
    return _public(mem)


async def delete_memory(agent_id: str, memory_id: str, via: str | None = None,
                        account: str = LOCAL) -> dict:
    """Delete a memory for good. Only the agent that wrote it may do this."""
    agent_id = check_agent(agent_id, account)
    memory_id = _require(memory_id, "memory_id")
    async with _write_lock:
        mem = db.get_memory(memory_id)
        if mem is None or not can_see(mem, agent_id, account):
            raise MemoriaError(f"No memory with ID '{memory_id}'.")
        if mem["agent_id"] != agent_id:
            raise MemoriaError(
                f"Only the agent that wrote this memory ('{mem['agent_id']}') can delete it."
            )
        with db.transaction():
            db.remove_memory(memory_id)
            # The background worker removes it from Cognee (text, search data and graph).
            # If it was never added to Cognee, the worker notices that and skips it.
            if mem.get("data_id"):
                db.queue_forget(mem["data_id"], mem["dataset_id"], memory_id)
    _log(identity.principal(account, agent_id), "delete", memory_id=memory_id, topic=mem["topic"],
         **({"via": via} if via else {}))
    _kick()
    return {"deleted": memory_id}


# ---------- the background worker: keeps Cognee in step with Memoria's database ----------
#
# Every memory has two bookkeeping fields:
#   needs_sync:   True while Cognee doesn't have its latest version yet.
#   sync_version: goes up by one on every change, so the worker can tell whether a
#                 memory was changed again while it was busy with the older version.
# Only the worker writes to Cognee, so Cognee never has two jobs running at once.

_worker: asyncio.Task | None = None


def _mark_changed(mem: dict) -> None:
    mem["needs_sync"] = True
    mem["sync_version"] = mem.get("sync_version", 0) + 1


def _kick() -> None:
    """Start the background worker if it isn't already running."""
    global _worker
    if _worker is None or _worker.done():
        _worker = asyncio.get_running_loop().create_task(_work_loop())


@asynccontextmanager
async def paused():
    """Hold every change (saves, updates, deletes and background work) while the block runs.

    Used by backups, so they copy the memories and Cognee's databases at one moment
    instead of halfway through a change. Searches carry on as normal.
    """
    async with _cognee_lock:  # wait for the worker to finish its current pass
        async with _write_lock:
            yield


def start_background_work() -> None:
    """Called when Memoria starts: finish any work left from last time."""
    if db.has_work():
        _kick()


def _background_error(what: str, error: Exception) -> None:
    message = f"{type(error).__name__}: {error}"
    print(f"Memoria background work failed ({what}): {message}", file=sys.stderr)
    _log("memoria", "background_error", what=what, error=message)


async def _work_loop() -> None:
    """Keep working until everything is in Cognee. On failure, wait and try again."""
    wait = 15
    while db.has_work():
        async with _cognee_lock:  # a backup can pause the worker between passes
            succeeded = await _work_once()
        _free_memory()
        log_memory("after background processing")
        if succeeded:
            wait = 15
        else:  # e.g. Gemini is down or over its limit: back off, up to 10 minutes
            await asyncio.sleep(wait)
            wait = min(wait * 2, 600)


async def _work_once() -> bool:
    """One pass over everything waiting. Returns False if anything failed."""
    ok = True

    # 1. Remove deleted memories from Cognee.
    for item in db.forget_queue():
        try:
            await _engine().forget(data_id=UUID(item["data_id"]),
                                dataset_id=UUID(item["dataset_id"]))
            removed = True
        except Exception as e:
            _background_error(f"removing {item['memory_id']}", e)
            ok, removed = False, False
        if db.forget_tried(item["data_id"], done=removed, give_up_after=FORGET_GIVE_UP):
            _log("memoria", "background_gave_up", memory_id=item["memory_id"])

    # 2. Send new and changed memories to Cognee (a few at a time: see RUN_SIZE).
    added = []
    for mem in db.waiting_memories()[:RUN_SIZE]:
        node_set = [f"account:{account_of(mem)}", f"agent:{mem['agent_id']}",
                    f"topic:{mem['topic']}"]
        try:
            if mem.get("data_id") and not mem.get("processed", True) \
                    and mem.get("added_version") == mem.get("sync_version"):
                # Added earlier, but building the graph failed: Cognee already has this
                # exact version, so it only needs the graph-building run below.
                added.append(mem)
            elif mem.get("data_id"):
                # Already in Cognee: replace it. Cognee rebuilds its part of the graph itself.
                result = await _engine().update(data_id=UUID(mem["data_id"]), data=_as_text(mem),
                                             dataset_id=UUID(mem["dataset_id"]),
                                             node_set=node_set)
                if isinstance(result, dict) and result.get("error"):
                    raise RuntimeError(result["error"])
                db.mark_synced(mem["id"], mem["sync_version"], processed=True)
            else:
                result = await _engine().add(_as_text(mem), dataset_name=dataset_for(account_of(mem)),
                                          node_set=node_set)
                data_id, dataset_id = str(result.data_ids[0]), str(result.dataset_id)
                if db.record_added(mem["id"], data_id, dataset_id, mem["sync_version"]):
                    added.append(mem)
                else:  # deleted while being added: remove it from Cognee too
                    db.queue_forget(data_id, dataset_id, mem["id"])
        except Exception as e:
            ok = False
            _background_error(f"storing {mem['id']}", e)

    # 3. Cognee reads all newly added memories in ONE run and builds the graph.
    if added:
        try:
            await _engine().cognify(datasets=sorted({dataset_for(account_of(m)) for m in added}))
        except Exception as e:
            _background_error("building the knowledge graph", e)
            return False  # the memories stay "processing"; next pass tries again
        for mem in added:
            db.mark_synced(mem["id"], mem["sync_version"], processed=True)
    return ok


# ---------- read-only views for the dashboard (the laptop owner sees everything) ----------

def all_memories() -> list[dict]:
    """Every memory with its labels and signature check, newest first."""
    return [_public(m) for m in db.all_memories()]


def memory_versions(memory_id: str) -> dict | None:
    """One memory plus every version of it, newest first."""
    mem = db.get_memory(memory_id)
    if mem is None:
        return None
    versions = []
    author, written_at = mem["agent_id"], mem["created_at"]
    for old in mem.get("history", []):
        versions.append({"note": old["note"], "reason": old["reason"],
                         "confidence": old["confidence"], "topic": old["topic"],
                         "written_by": author, "written_at": written_at,
                         "replaced_by": old["replaced_by"], "replaced_at": old["replaced_at"]})
        author, written_at = old["replaced_by"], old["replaced_at"]
    versions.append({"note": mem["note"], "reason": mem["reason"],
                     "confidence": mem["confidence"], "topic": mem["topic"],
                     "written_by": author, "written_at": written_at, "current": True})
    versions.reverse()
    for number, version in enumerate(versions):
        version["version"] = len(versions) - number
    return {**_public(mem), "versions": versions}
