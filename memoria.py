"""Memoria's memory engine: save, find and update labelled memories with Cognee.

Every memory carries four labels: which agent wrote it, when, its topic,
and a confidence level (low / medium / high).

Every memory also has a visibility setting:
  - private: only the agent that wrote it can find or update it.
  - shared:  the writer plus the agent IDs listed in shared_with.

Where things are kept (all inside the memory_store folder):
  - Cognee's databases: the memories themselves plus the knowledge graph
    Cognee builds from them (used for searching).
  - memories.json: a simple list of every memory and its labels and history,
    so labels are always exact and memories can be updated by ID.
  - agents.json: every agent ID that has used Memoria.
  - activity.log: who saved, searched or updated what, and when.
"""
import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from uuid import UUID

from setup_cognee import STORE, cognee
from cognee import SearchType

DATASET = "memoria"
INDEX_FILE = STORE / "memories.json"
AGENTS_FILE = STORE / "agents.json"
ACTIVITY_FILE = STORE / "activity.log"

CONFIDENCE_LEVELS = ("low", "medium", "high")
VISIBILITY_LEVELS = ("private", "shared")
# Agent IDs are short and simple: lowercase letters, numbers and dashes, e.g. "claude-code".
AGENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")

_write_lock = asyncio.Lock()  # one save/update at a time


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


def check_agent(agent_id: str) -> str:
    """Make sure the agent ID is valid and remember that this agent exists."""
    agent_id = (agent_id or "").strip().lower()
    if not AGENT_ID_PATTERN.match(agent_id):
        raise MemoriaError(
            "agent_id must be 2-40 characters: lowercase letters, numbers and dashes "
            "(for example 'claude-code' or 'research-bot')."
        )
    agents = _read_json(AGENTS_FILE, {})
    info = agents.setdefault(agent_id, {"first_seen": _now()})
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


def can_see(mem: dict, agent_id: str) -> bool:
    """True if this agent is allowed to read the memory."""
    if mem["agent_id"] == agent_id:
        return True
    return mem.get("visibility") == "shared" and agent_id in mem.get("shared_with", [])


def _require(value: str, name: str) -> str:
    value = (value or "").strip()
    if not value:
        raise MemoriaError(f"{name} can't be empty.")
    return value


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


def _public(mem: dict) -> dict:
    """What agents get back: the memory and its labels (not Cognee's internal IDs)."""
    return {
        "memory_id": mem["id"],
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
    }


# ---------- the three actions ----------

async def save_memory(agent_id: str, note: str, reason: str, topic: str,
                      confidence: str = "medium", visibility: str = "private",
                      shared_with: list[str] | None = None) -> dict:
    agent_id = check_agent(agent_id)
    visibility, shared_with = _check_sharing(visibility, shared_with, agent_id)
    mem = {
        "id": "mem-" + uuid.uuid4().hex[:8],
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
    async with _write_lock:
        result = await cognee.add(
            _as_text(mem),
            dataset_name=DATASET,
            node_set=[f"agent:{agent_id}", f"topic:{mem['topic']}"],
        )
        mem["data_id"] = str(result.data_ids[0])
        mem["dataset_id"] = str(result.dataset_id)
        # Cognee reads the new memory and adds it to its knowledge graph.
        await cognee.cognify(datasets=[DATASET])

        index = _read_json(INDEX_FILE, {})
        index[mem["id"]] = mem
        _write_json(INDEX_FILE, index)
    _log(agent_id, "save", memory_id=mem["id"], topic=mem["topic"])
    return _public(mem)


async def find_memory(agent_id: str, query: str, topic: str | None = None,
                      saved_by: str | None = None, limit: int = 5) -> list[dict]:
    agent_id = check_agent(agent_id)
    query = _require(query, "query")
    limit = max(1, min(int(limit or 5), 20))
    index = _read_json(INDEX_FILE, {})
    _log(agent_id, "find", query=query, topic=topic, saved_by=saved_by)
    if not index:
        return []

    def wanted(mem: dict) -> bool:
        if not can_see(mem, agent_id):  # never show memories this agent isn't allowed to read
            return False
        if topic and topic.strip().lower() not in mem["topic"].lower():
            return False
        if saved_by and saved_by.strip().lower() != mem["agent_id"]:
            return False
        return True

    by_data_id = {m["data_id"]: m for m in index.values()}
    found: list[dict] = []

    # 1. Ask Cognee for the stored passages closest in meaning to the question.
    results = await cognee.search(
        query, query_type=SearchType.CHUNKS, datasets=[DATASET], top_k=max(50, limit * 10)
    )
    for chunk in _chunks(results):
        mem = by_data_id.get(str(chunk.get("document_id")))
        if mem is None:  # fall back to the memory ID written in the text
            match = re.search(r"Memoria memory (mem-[0-9a-f]{8})", chunk.get("text", ""))
            mem = index.get(match.group(1)) if match else None
        if mem and mem not in found and wanted(mem):
            found.append(mem)

    # 2. If a topic was given, top up with the newest memories on that topic.
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


async def update_memory(agent_id: str, memory_id: str, new_note: str, reason: str,
                        confidence: str | None = None, topic: str | None = None) -> dict:
    agent_id = check_agent(agent_id)
    memory_id = _require(memory_id, "memory_id")
    new_note = _require(new_note, "new_note")
    reason = _require(reason, "reason")

    async with _write_lock:
        index = _read_json(INDEX_FILE, {})
        mem = index.get(memory_id)
        if mem is None or not can_see(mem, agent_id):
            raise MemoriaError(f"No memory with ID '{memory_id}'. Use find_memory to look it up.")

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

        # Cognee replaces the stored memory and rebuilds its part of the graph.
        result = await cognee.update(
            data_id=UUID(mem["data_id"]),
            data=_as_text(mem),
            dataset_id=UUID(mem["dataset_id"]),
            node_set=[f"agent:{mem['agent_id']}", f"topic:{mem['topic']}"],
        )
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(f"Cognee could not update the memory: {result['error']}")

        index[memory_id] = mem
        _write_json(INDEX_FILE, index)
    _log(agent_id, "update", memory_id=memory_id)
    out = _public(mem)
    out["previous_note"] = mem["history"][-1]["note"]
    return out


async def share_memory(agent_id: str, memory_id: str, visibility: str,
                       shared_with: list[str] | None = None) -> dict:
    """Change who can read a memory. Only the agent that wrote it may do this."""
    agent_id = check_agent(agent_id)
    memory_id = _require(memory_id, "memory_id")
    async with _write_lock:
        index = _read_json(INDEX_FILE, {})
        mem = index.get(memory_id)
        if mem is None or not can_see(mem, agent_id):
            raise MemoriaError(f"No memory with ID '{memory_id}'. Use find_memory to look it up.")
        if mem["agent_id"] != agent_id:
            raise MemoriaError(
                f"Only the agent that wrote this memory ('{mem['agent_id']}') can change its sharing."
            )
        before = (mem.get("visibility", "private"), mem.get("shared_with", []))
        mem["visibility"], mem["shared_with"] = _check_sharing(visibility, shared_with, agent_id)
        index[memory_id] = mem
        _write_json(INDEX_FILE, index)
    _log(agent_id, "share", memory_id=memory_id,
         before={"visibility": before[0], "shared_with": before[1]},
         after={"visibility": mem["visibility"], "shared_with": mem["shared_with"]})
    return _public(mem)
