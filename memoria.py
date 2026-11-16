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
  - Cognee's databases: the memories themselves plus the knowledge graph
    Cognee builds from them (used for searching).
  - memories.json: a simple list of every memory and its labels and history,
    so labels are always exact and memories can be updated by ID.
  - agents.json: every agent ID that has used Memoria.
  - activity.log: who saved, searched or updated what, and when.
  - pending_forget.json: deleted memories still waiting to be removed from Cognee.

Saving is fast: save, update and delete only change memories.json and answer
straight away. A background worker then does the slow part (Cognee reading the
memory with Gemini and adding it to the knowledge graph), one job at a time,
grouping memories that arrive together into one run. Until it is done the
memory is marked "processing" and find_memory finds it by its exact words.
Nothing is lost if Memoria stops: the worker picks up unfinished work next start.
"""
import asyncio
import json
import re
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

import accounts
import identity
from setup_cognee import STORE, cognee
from cognee import SearchType

DATASET = "memoria"
INDEX_FILE = STORE / "memories.json"
AGENTS_FILE = STORE / "agents.json"
ACTIVITY_FILE = STORE / "activity.log"
FORGET_FILE = STORE / "pending_forget.json"
USAGE_FILE = STORE / "usage.json"  # each developer account's writes today (for its limits)
LOCAL = identity.LOCAL

CONFIDENCE_LEVELS = ("low", "medium", "high")
VISIBILITY_LEVELS = ("private", "shared")
# Agent IDs are short and simple: lowercase letters, numbers and dashes, e.g. "claude-code".
AGENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
# Spam protection: the longest text accepted, for everyone.
MAX_LENGTHS = {"note": 4000, "new_note": 4000, "reason": 2000, "topic": 100, "query": 1000,
               "memory_id": 40}
MAX_SHARED_WITH = 20

_write_lock = asyncio.Lock()  # one change to memories.json at a time
_cognee_lock = asyncio.Lock()  # held by the background worker while it changes Cognee
FORGET_GIVE_UP = 5  # stop retrying a Cognee removal after this many failures


class MemoriaError(ValueError):
    """A problem with what the agent asked for, explained in plain words."""


# ---------- small helpers ----------

def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def _write_json(path, value) -> None:
    STORE.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)  # swap in one step so the file is never half-written


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
    agents = _read_json(AGENTS_FILE, {})
    info = agents.setdefault(identity.principal(account, agent_id), {"first_seen": _now()})
    info["last_seen"] = _now()
    _write_json(AGENTS_FILE, agents)
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


def _use_quota(index: dict, account: str, new_memory: bool) -> None:
    """Spam protection for developer accounts: refuse writes over the account's limits.

    Call while holding _write_lock. This laptop's own agents have no limits.
    """
    limit = accounts.limits(account)
    if limit is None:
        return
    if new_memory:
        count = sum(1 for m in index.values() if account_of(m) == account)
        if count >= limit["max_memories"]:
            raise MemoriaError(f"This account has reached its limit of {limit['max_memories']:,} "
                               "memories. Delete some, or ask the host for a higher limit.")
    usage = _read_json(USAGE_FILE, {})
    today = _now()[:10]
    used = usage.get(account)
    if not used or used["day"] != today:
        used = {"day": today, "writes": 0}
    if used["writes"] >= limit["writes_per_day"]:
        raise MemoriaError(f"This account has used its {limit['writes_per_day']:,} saves and "
                           "updates for today. The limit resets at midnight (UTC).")
    used["writes"] += 1
    usage[account] = used
    _write_json(USAGE_FILE, usage)


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


SIGNED_FIELDS = ("id", "agent_id", "created_at", "topic", "confidence", "note", "reason",
                 "updated_at", "updated_by")


def _signed_content(mem: dict) -> bytes:
    """The exact bytes that get signed: the memory's content and authorship labels."""
    content = {field: mem.get(field) for field in SIGNED_FIELDS}
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
    return {"verification": "verified" if ok else "NOT verified", "verification_detail": detail}


def _public(mem: dict) -> dict:
    """What agents get back: the memory and its labels (not Cognee's internal IDs)."""
    return {
        **_verification(mem),
        "memory_id": mem["id"],
        "account": account_of(mem),
        "topic": mem["topic"],
        "note": mem["note"],
        "reason": mem["reason"],
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
                      shared_with: list[str] | None = None, account: str = LOCAL) -> dict:
    agent_id = check_agent(agent_id, account)
    who = identity.principal(account, agent_id)
    visibility, shared_with = _check_sharing(visibility, shared_with, agent_id)
    mem = {
        "id": "mem-" + uuid.uuid4().hex[:8],
        "account": account,
        "agent_id": agent_id,
        "created_at": _now(),
        "topic": _require(topic, "topic"),
        "confidence": _check_confidence(confidence),
        "note": _require(note, "note"),
        "reason": _require(reason, "reason"),
        "history": [],
        "visibility": visibility,
        "shared_with": shared_with,
    }
    _sign(mem, who)  # prove this agent wrote it
    _mark_changed(mem)  # the background worker adds it to Cognee
    async with _write_lock:
        index = _read_json(INDEX_FILE, {})
        _use_quota(index, account, new_memory=True)
        index[mem["id"]] = mem
        _write_json(INDEX_FILE, index)
    _log(who, "save", memory_id=mem["id"], topic=mem["topic"])
    _kick()
    return _public(mem)


async def find_memory(agent_id: str, query: str, topic: str | None = None,
                      saved_by: str | None = None, limit: int = 5,
                      account: str = LOCAL) -> list[dict]:
    agent_id = check_agent(agent_id, account)
    query = _require(query, "query")
    limit = max(1, min(int(limit or 5), 20))
    index = _read_json(INDEX_FILE, {})
    _log(identity.principal(account, agent_id), "find", query=query, topic=topic, saved_by=saved_by)
    _kick()  # finish any work left over from before Memoria was restarted
    if not index:
        return []

    def wanted(mem: dict) -> bool:
        if not can_see(mem, agent_id, account):  # never show memories this agent isn't allowed to read
            return False
        if topic and topic.strip().lower() not in mem["topic"].lower():
            return False
        if saved_by and saved_by.strip().lower() != mem["agent_id"]:
            return False
        return True

    by_data_id = {m["data_id"]: m for m in index.values()
                  if m.get("data_id") and account_of(m) == account}
    found: list[dict] = []

    # 1. Ask Cognee for the stored passages closest in meaning to the question.
    #    (If nothing has been processed yet, Cognee has no search data, so skip it.)
    if by_data_id:
        results = await cognee.search(
            query, query_type=SearchType.CHUNKS, datasets=[dataset_for(account)],
            top_k=max(50, limit * 10)
        )
        for chunk in _chunks(results):
            mem = by_data_id.get(str(chunk.get("document_id")))
            if mem is None:  # fall back to the memory ID written in the text
                match = re.search(r"Memoria memory (mem-[0-9a-f]{8})", chunk.get("text", ""))
                mem = index.get(match.group(1)) if match else None
            if mem and mem not in found and wanted(mem):
                found.append(mem)

    # 2. Memories still being processed aren't in Cognee's search yet (or only in their
    #    old version): find them by the words of the question instead.
    words = _words(query)
    for mem in index.values():
        if mem.get("needs_sync") and mem not in found and wanted(mem) \
                and words & _words(f"{mem['topic']} {mem['note']} {mem['reason']}"):
            found.append(mem)

    # 3. If a topic was given, top up with the newest memories on that topic.
    if topic and len(found) < limit:
        same_topic = sorted(
            (m for m in index.values() if wanted(m) and m not in found),
            key=lambda m: m.get("updated_at") or m["created_at"],
            reverse=True,
        )
        found += same_topic

    return [_public(m) for m in found[:limit]]


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
        index = _read_json(INDEX_FILE, {})
        mem = index.get(memory_id)
        if mem is None or not can_see(mem, agent_id, account):
            raise MemoriaError(f"No memory with ID '{memory_id}'. Use find_memory to look it up.")
        _use_quota(index, account, new_memory=False)

        # Keep the old version in the history so nothing is lost.
        mem["history"].append({
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

        index[memory_id] = mem
        _write_json(INDEX_FILE, index)
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
        index = _read_json(INDEX_FILE, {})
        mem = index.get(memory_id)
        if mem is None or not can_see(mem, agent_id, account):
            raise MemoriaError(f"No memory with ID '{memory_id}'. Use find_memory to look it up.")
        if mem["agent_id"] != agent_id:
            raise MemoriaError(
                f"Only the agent that wrote this memory ('{mem['agent_id']}') can change its sharing."
            )
        before = (mem.get("visibility", "private"), mem.get("shared_with", []))
        mem["visibility"], mem["shared_with"] = _check_sharing(visibility, shared_with, agent_id)
        index[memory_id] = mem
        _write_json(INDEX_FILE, index)
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
        index = _read_json(INDEX_FILE, {})
        mem = index.get(memory_id)
        if mem is None or not can_see(mem, agent_id, account):
            raise MemoriaError(f"No memory with ID '{memory_id}'.")
        if mem["agent_id"] != agent_id:
            raise MemoriaError(
                f"Only the agent that wrote this memory ('{mem['agent_id']}') can delete it."
            )
        del index[memory_id]
        _write_json(INDEX_FILE, index)
        # The background worker removes it from Cognee (text, search data and graph).
        # If it was never added to Cognee, the worker notices that and skips it.
        if mem.get("data_id"):
            _queue_forget(mem["data_id"], mem["dataset_id"], memory_id)
    _log(identity.principal(account, agent_id), "delete", memory_id=memory_id, topic=mem["topic"],
         **({"via": via} if via else {}))
    _kick()
    return {"deleted": memory_id}


# ---------- the background worker: keeps Cognee in step with memories.json ----------
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


def _queue_forget(data_id: str, dataset_id: str, memory_id: str) -> None:
    """Note that Cognee should remove this item. Call while holding _write_lock."""
    pending = _read_json(FORGET_FILE, [])
    pending.append({"data_id": data_id, "dataset_id": dataset_id,
                    "memory_id": memory_id, "attempts": 0})
    _write_json(FORGET_FILE, pending)


def _has_work() -> bool:
    return (any(m.get("needs_sync") for m in _read_json(INDEX_FILE, {}).values())
            or bool(_read_json(FORGET_FILE, [])))


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
    if _has_work():
        _kick()


def _background_error(what: str, error: Exception) -> None:
    message = f"{type(error).__name__}: {error}"
    print(f"Memoria background work failed ({what}): {message}", file=sys.stderr)
    _log("memoria", "background_error", what=what, error=message)


async def _work_loop() -> None:
    """Keep working until everything is in Cognee. On failure, wait and try again."""
    wait = 15
    while _has_work():
        async with _cognee_lock:  # a backup can pause the worker between passes
            succeeded = await _work_once()
        if succeeded:
            wait = 15
        else:  # e.g. Gemini is down or over its limit: back off, up to 10 minutes
            await asyncio.sleep(wait)
            wait = min(wait * 2, 600)


async def _work_once() -> bool:
    """One pass over everything waiting. Returns False if anything failed."""
    ok = True

    # 1. Remove deleted memories from Cognee.
    async with _write_lock:
        to_forget = _read_json(FORGET_FILE, [])
    for item in to_forget:
        try:
            await cognee.forget(data_id=UUID(item["data_id"]),
                                dataset_id=UUID(item["dataset_id"]))
            removed, gave_up = True, False
        except Exception as e:
            _background_error(f"removing {item['memory_id']}", e)
            ok, removed = False, False
            gave_up = item["attempts"] + 1 >= FORGET_GIVE_UP
        async with _write_lock:
            pending = _read_json(FORGET_FILE, [])
            for entry in pending:
                if entry["data_id"] == item["data_id"]:
                    entry["attempts"] += 1
            if removed or gave_up:  # done (or hopeless): stop trying
                pending = [e for e in pending if e["data_id"] != item["data_id"]]
                if gave_up:
                    _log("memoria", "background_gave_up", memory_id=item["memory_id"])
            _write_json(FORGET_FILE, pending)

    # 2. Send new and changed memories to Cognee.
    async with _write_lock:
        todo = [dict(m) for m in _read_json(INDEX_FILE, {}).values() if m.get("needs_sync")]
    added = []
    for mem in todo:
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
                result = await cognee.update(data_id=UUID(mem["data_id"]), data=_as_text(mem),
                                             dataset_id=UUID(mem["dataset_id"]),
                                             node_set=node_set)
                if isinstance(result, dict) and result.get("error"):
                    raise RuntimeError(result["error"])
                await _mark_done(mem, processed=True)
            else:
                result = await cognee.add(_as_text(mem), dataset_name=dataset_for(account_of(mem)),
                                          node_set=node_set)
                if await _record_added(mem, str(result.data_ids[0]), str(result.dataset_id)):
                    added.append(mem)
        except Exception as e:
            ok = False
            _background_error(f"storing {mem['id']}", e)

    # 3. Cognee reads all newly added memories in ONE run and builds the graph.
    if added:
        try:
            await cognee.cognify(datasets=sorted({dataset_for(account_of(m)) for m in added}))
        except Exception as e:
            _background_error("building the knowledge graph", e)
            return False  # the memories stay "processing"; next pass tries again
        for mem in added:
            await _mark_done(mem, processed=True)
    return ok


async def _record_added(mem: dict, data_id: str, dataset_id: str) -> bool:
    """Remember where Cognee stored a new memory. False if it was deleted meanwhile."""
    async with _write_lock:
        index = _read_json(INDEX_FILE, {})
        current = index.get(mem["id"])
        if current is None:  # deleted while being added: remove it from Cognee too
            _queue_forget(data_id, dataset_id, mem["id"])
            return False
        current["data_id"], current["dataset_id"] = data_id, dataset_id
        current["processed"] = False  # stored in Cognee, graph not built yet
        current["added_version"] = mem.get("sync_version")  # the version Cognee now holds
        _write_json(INDEX_FILE, index)
        return True


async def _mark_done(mem: dict, processed: bool = False) -> None:
    """Mark the memory as fully in Cognee, unless it was changed again meanwhile."""
    async with _write_lock:
        index = _read_json(INDEX_FILE, {})
        current = index.get(mem["id"])
        if current is None:
            return
        if processed:  # Cognee has built the graph for the version it holds
            current["processed"] = True
        if current.get("sync_version") == mem.get("sync_version"):
            current["needs_sync"] = False
        _write_json(INDEX_FILE, index)


# ---------- read-only views for the dashboard (the laptop owner sees everything) ----------

def all_memories() -> list[dict]:
    """Every memory with its labels and signature check, newest first."""
    index = _read_json(INDEX_FILE, {})
    out = [_public(m) for m in index.values()]
    out.sort(key=lambda m: m["updated_at"] or m["saved_at"], reverse=True)
    return out


def memory_versions(memory_id: str) -> dict | None:
    """One memory plus every version of it, newest first."""
    mem = _read_json(INDEX_FILE, {}).get(memory_id)
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
