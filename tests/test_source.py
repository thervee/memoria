"""The optional "source" of a memory: signed, returned, and "no source" when missing."""
import asyncio
import json
import sqlite3

import pytest

import db
import identity
import memoria
from conftest import index, save_index, settle
from memoria_client import Memoria

URL = "https://example.com/report"


def save(agent="alice", note="LINK fell 21% in 11 days.", **extra):
    return asyncio.run(memoria.save_memory(agent, note, "price data", "crypto/link", **extra))


def test_source_is_saved_signed_and_returned(store):
    with_source = save(source=URL)
    without = save(note="LINK might bounce next week.")
    assert with_source["source"] == URL and with_source["has_source"] is True
    assert without["source"] == "no source" and without["has_source"] is False
    assert with_source["verification"] == "verified"
    assert "including its source" in with_source["verification_detail"]
    assert without["verification"] == "verified"
    assert "no source" in without["verification_detail"]


def test_find_returns_the_source_or_no_source(store):
    save(source=URL)
    save(note="LINK might bounce next week.")

    async def find():
        await settle()
        return await memoria.find_memory("alice", "LINK", topic="crypto", limit=10)
    found = {m["note"]: m for m in asyncio.run(find())}
    assert found["LINK fell 21% in 11 days."]["source"] == URL
    assert found["LINK might bounce next week."]["source"] == "no source"
    assert all(m["verification"] == "verified" for m in found.values())


def test_changing_the_source_breaks_the_signature(store):
    saved = save(source=URL)
    data = index()
    data[saved["memory_id"]]["source"] = "https://example.com/something-else"
    save_index(data)
    assert memoria._verification(db.get_memory(saved["memory_id"]))["verification"] == \
        "NOT verified"


def test_adding_a_source_afterwards_breaks_the_signature(store):
    saved = save()
    data = index()
    data[saved["memory_id"]]["source"] = URL
    save_index(data)
    assert memoria._verification(db.get_memory(saved["memory_id"]))["verification"] == \
        "NOT verified"


def test_update_keeps_the_source_and_still_verifies(store):
    saved = save(source=URL)
    updated = asyncio.run(memoria.update_memory("alice", saved["memory_id"],
                                                "LINK fell 23% in 12 days.", "new data"))
    assert updated["source"] == URL and updated["verification"] == "verified"


@pytest.mark.parametrize("given, kept", [
    ("   ", None), ("", None), (None, None),
    ("  0xabc123\n  ", "0xabc123"), ("q3 report.pdf", "q3 report.pdf"),
])
def test_source_is_tidied_and_empty_means_none(store, given, kept):
    saved = save(source=given)
    assert db.get_memory(saved["memory_id"]).get("source") == kept


def test_too_long_or_non_text_sources_are_refused(store):
    with pytest.raises(memoria.MemoriaError, match="500 characters"):
        save(source="x" * 501)
    with pytest.raises(memoria.MemoriaError, match="must be text"):
        save(source=["https://example.com"])


def test_old_databases_get_the_column_and_old_memories_still_verify(keys, tmp_path, monkeypatch):
    """A database from before sources existed, with a memory signed the old way."""
    old_file = tmp_path / "old" / "memoria.db"
    old_file.parent.mkdir()
    mem = {"id": "mem-0ld00000", "agent_id": "alice", "created_at": "2026-11-01T00:00:00+00:00",
           "topic": "t", "confidence": "high", "note": "Old note.", "reason": "old",
           "updated_at": None, "updated_by": None}
    old_bytes = ("memoria-memory-v1|" + json.dumps(mem, sort_keys=True, ensure_ascii=False,
                                                    separators=(",", ":"))).encode("utf-8")
    sig = identity.sign("alice", old_bytes)
    conn = sqlite3.connect(old_file)
    schema = db.SCHEMA.replace(
        "    source        TEXT,                         -- where the claim came from (optional)\n",
        "")
    conn.executescript(schema)
    assert "source" not in {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
    conn.execute("INSERT INTO memories (id, account, agent_id, created_at, topic, confidence, "
                 "note, reason, signature) VALUES (?, 'local', ?, ?, ?, ?, ?, ?, ?)",
                 (mem["id"], mem["agent_id"], mem["created_at"], mem["topic"],
                  mem["confidence"], mem["note"], mem["reason"], json.dumps(sig)))
    conn.commit()
    conn.close()

    db.close()
    monkeypatch.setattr(db, "DB_FILE", old_file)
    found = db.get_memory("mem-0ld00000")  # opening it adds the column
    assert found.get("source") is None
    shown = memoria._public(found)
    assert shown["verification"] == "verified" and shown["source"] == "no source"


def test_source_over_mcp(memoria_url, devs):
    async def run():
        async with Memoria(api_key=devs["acme/trader"], url=memoria_url) as mem:
            await mem.save("Acme renews on March 1.", reason="renewal email",
                           topic="customers/acme", source="acme-renewal.eml")
            await mem.save("Acme may expand next year.", reason="hunch", topic="customers/acme")
            return await mem.find("Acme", topic="customers/acme", limit=10)
    found = {m["note"]: m for m in asyncio.run(run())}
    assert found["Acme renews on March 1."]["source"] == "acme-renewal.eml"
    assert found["Acme may expand next year."]["source"] == "no source"
    assert all(m["verification"] == "verified" for m in found.values())
