"""Shared memories are read-only for the agents they're shared with (public audit, bug 2).

Before the fix, an agent a memory was shared with could update it: "pay to 0xAAA" could
become "pay to 0xBBB", and it still showed the original author with "verified".
"""
import asyncio

import pytest

import db
import identity
import memoria as m
from conftest import settle
from memoria_client import Memoria

PAY_A, PAY_B = "Pay the supplier at 0xAAA.", "Pay the supplier at 0xBBB."


def share_payment():
    """alice saves a payment instruction and shares it with bob."""
    return asyncio.run(m.save_memory("alice", PAY_A, "From the signed invoice.", "payments",
                                     visibility="shared", shared_with=["bob"]))["memory_id"]


def test_shared_reader_cannot_change_the_payment_address(store):
    mid = share_payment()
    with pytest.raises(m.MemoriaError, match=r"Only the agent that wrote this memory \('alice'\) can update it"):
        asyncio.run(m.update_memory("bob", mid, PAY_B, "address changed"))
    mem = db.get_memory(mid)
    assert mem["note"] == PAY_A and mem.get("updated_by") is None and mem["history"] == []
    shown = m._public(mem)
    assert shown["verification"] == "verified" and shown["signed_by_author"] is True


def test_shared_reader_cannot_delete_or_reshare(store):
    mid = share_payment()
    with pytest.raises(m.MemoriaError, match="Only the agent that wrote"):
        asyncio.run(m.delete_memory("bob", mid))
    with pytest.raises(m.MemoriaError, match="Only the agent that wrote"):
        asyncio.run(m.share_memory("bob", mid, "shared", ["carol"]))
    assert db.get_memory(mid)["note"] == PAY_A


def test_shared_reader_can_still_read_it_and_the_owner_can_still_edit(store):
    mid = share_payment()

    async def scenario():
        await settle()
        found = await m.find_memory("bob", "supplier", topic="payments")
        assert [x["note"] for x in found] == [PAY_A]
        assert found[0]["verification"] == "verified" and found[0]["signed_by_author"]
        updated = await m.update_memory("alice", mid, PAY_B, "supplier moved")
        assert updated["verification"] == "verified" and updated["signed_by_author"]
        assert [x["note"] for x in await m.find_memory("bob", "supplier", topic="payments")] \
            == [PAY_B]  # still shared, so bob sees the owner's change
    asyncio.run(scenario())


def test_shared_reader_cannot_edit_over_mcp(memoria_url, devs):
    async def run():
        async with Memoria(api_key=devs["acme/trader"], url=memoria_url) as trader:
            saved = await trader.save(PAY_A, reason="invoice", topic="payments",
                                      visibility="shared", shared_with=["analyst"])
        async with Memoria(api_key=devs["acme/analyst"], url=memoria_url) as analyst:
            found = await analyst.find("supplier", topic="payments")
            assert [x["note"] for x in found] == [PAY_A]
            for attempt in (analyst.update(saved["memory_id"], PAY_B, reason="changed"),
                            analyst.delete(saved["memory_id"])):
                with pytest.raises(Exception, match="Only the agent that wrote"):
                    await attempt
        return saved["memory_id"]
    mid = asyncio.run(run())
    assert db.get_memory(mid)["note"] == PAY_A


def edited_by_bob_before_the_fix(mid):
    """What the bug left behind: bob's version of alice's memory, signed by bob."""
    mem = db.get_memory(mid)
    mem["history"].append({"note": mem["note"], "reason": mem["reason"],
                           "confidence": mem["confidence"], "topic": mem["topic"],
                           "replaced_at": "2026-11-20T00:00:00+00:00", "replaced_by": "bob"})
    mem.update(note=PAY_B, updated_at="2026-11-20T00:00:00+00:00", updated_by="bob")
    m._sign(mem, "bob")
    db.put_memory(mem)
    return mem


def test_memories_edited_before_the_fix_never_claim_the_author_signed_them(store):
    mem = edited_by_bob_before_the_fix(share_payment())
    shown = m._public(mem)
    assert shown["saved_by"] == "alice" and shown["updated_by"] == "bob"
    assert shown["verification"] == "verified"  # it's a valid signature: bob's
    assert shown["signed_by_author"] is False
    assert "signed by bob, who changed it; 'alice' saved the original but did NOT sign this " \
           "version" in shown["verification_detail"]


def test_relabelling_bobs_version_as_alices_is_not_verified(store):
    mem = edited_by_bob_before_the_fix(share_payment())
    mem["updated_by"] = "alice"  # pretend alice made bob's change
    db.put_memory(mem)
    shown = m._public(db.get_memory(mem["id"]))
    assert shown["verification"] == "NOT verified" and shown["signed_by_author"] is False


def test_owner_can_fix_a_memory_edited_before_the_fix(store):
    mid = edited_by_bob_before_the_fix(share_payment())["id"]
    fixed = asyncio.run(m.update_memory("alice", mid, PAY_A, "undo bob's change"))
    assert fixed["verification"] == "verified" and fixed["signed_by_author"] is True
    assert fixed["note"] == PAY_A and fixed["updated_by"] == "alice"


def test_existing_shares_keep_working(store):
    mid = share_payment()
    assert identity.public_key_of("bob") is not None
    found = asyncio.run(m.find_memory("bob", "supplier", topic="payments"))
    assert found[0]["memory_id"] == mid and found[0]["shared_with"] == ["bob"]
