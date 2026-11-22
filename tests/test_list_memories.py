"""list_memories: the memories an agent may read, newest first, with filters."""
import asyncio

import pytest

import db
import memoria as m
from conftest import index, save_index
from memoria_client import Memoria


def save(agent, note, topic, account="local", **extra):
    return asyncio.run(m.save_memory(agent, note, "r", topic, account=account, **extra))


def listed(agent, account="local", **filters):
    return asyncio.run(m.list_memories(agent, account=account, **filters))


def notes(items):
    return [x["note"] for x in items]


def test_lists_own_and_shared_newest_first_never_others_private(store):
    save("alice", "Alice one.", "customers/acme")
    save("bob", "Bob private.", "customers/acme")
    save("bob", "Bob shared with alice.", "customers/globex", visibility="shared",
         shared_with=["alice"])
    save("bob", "Bob shared with carol.", "customers/initech", visibility="shared",
         shared_with=["carol"])
    save("alice", "Alice two.", "notes")
    assert sorted(notes(listed("alice"))) == ["Alice one.", "Alice two.", "Bob shared with alice."]
    assert notes(listed("carol")) == ["Bob shared with carol."]


def test_never_lists_other_accounts(store, devs):
    save("trader", "Acme's plan.", "plans", account="acme")
    save("analyst", "Globex's plan.", "plans", account="globex", visibility="shared",
         shared_with=["trader"])  # shared with globex's trader, not acme's
    assert notes(listed("trader", account="acme")) == ["Acme's plan."]
    assert notes(listed("trader", account="globex")) == ["Globex's plan."]
    assert listed("analyst", account="acme") == []


@pytest.mark.parametrize("topic", ["customers", "customers/", " customers/ ", "CUSTOMERS"])
def test_topic_includes_everything_under_it(store, topic):
    save("alice", "Top level.", "customers")
    save("alice", "Acme.", "customers/acme")
    save("alice", "Acme EU.", "customers/acme/eu")
    save("alice", "Not under it.", "customers-old")
    save("alice", "Elsewhere.", "my/customers")
    assert sorted(notes(listed("alice", topic=topic))) == ["Acme EU.", "Acme.", "Top level."]


def test_a_deeper_topic_only_lists_its_own_branch(store):
    save("alice", "Acme.", "customers/acme")
    save("alice", "Acme EU.", "customers/acme/eu")
    save("alice", "Globex.", "customers/globex")
    assert sorted(notes(listed("alice", topic="customers/acme"))) == ["Acme EU.", "Acme."]


def test_saved_by_filter(store):
    save("alice", "Alice's.", "t")
    save("bob", "Bob's, shared.", "t", visibility="shared", shared_with=["alice"])
    assert notes(listed("alice", saved_by="bob")) == ["Bob's, shared."]
    assert notes(listed("alice", saved_by="alice")) == ["Alice's."]


def test_limit_defaults_to_20_and_is_capped_at_100(store):
    for i in range(105):
        db.put_memory({"id": f"mem-{i:032x}", "account": "local", "agent_id": "alice",
                       "created_at": f"2026-11-01T00:{i // 60:02d}:{i % 60:02d}+00:00",
                       "topic": "bulk", "confidence": "low", "note": f"n{i}", "reason": "r"})
    assert len(listed("alice")) == 20
    assert len(listed("alice", limit=500)) == 100
    assert len(listed("alice", limit=0)) == 1
    assert notes(listed("alice", limit=2)) == ["n104", "n103"]  # newest first
    with pytest.raises(m.MemoriaError, match="limit must be a number"):
        listed("alice", limit="lots")


def test_same_fields_and_signature_check_as_find(store):
    saved = save("alice", "Penguins huddle.", "animals", source="https://example.com/penguins")
    shown = listed("alice")[0]
    found = asyncio.run(m.find_memory("alice", "penguins", topic="animals"))[0]
    assert shown == found
    assert shown["verification"] == "verified" and shown["signed_by_author"] is True
    assert shown["source"] == "https://example.com/penguins"
    data = index()
    data[saved["memory_id"]]["note"] = "Penguins fly."  # changed after it was signed
    save_index(data)
    assert listed("alice")[0]["verification"] == "NOT verified"


def test_list_over_mcp(memoria_url, devs):
    async def run():
        async with Memoria(api_key=devs["acme/trader"], url=memoria_url) as trader:
            await trader.save("Acme renews March 1.", reason="email", topic="customers/acme")
            await trader.save("Private note.", reason="r", topic="notes")
        async with Memoria(api_key=devs["globex/trader"], url=memoria_url) as other:
            assert await other.list() == []  # another account sees nothing
        async with Memoria(api_key=devs["acme/analyst"], url=memoria_url) as analyst:
            assert await analyst.list() == []  # not shared with this agent
        async with Memoria(api_key=devs["acme/trader"], url=memoria_url) as trader:
            return await trader.list(topic="customers/")
    items = asyncio.run(run())
    assert [x["note"] for x in items] == ["Acme renews March 1."]
    assert items[0]["verification"] == "verified"
