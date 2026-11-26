"""Shares that expire: share_memory's optional expires_in_days.

Once a share has expired, the agents it was shared with can no longer find, read or list
the memory. The owner keeps full access, and the memory and its signature are untouched.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import db
import memoria as m
from conftest import settle

NOTE = "Acme renews on 1 March."


def save_and_share(**share):
    """alice saves a memory, lets the worker process it, then shares it with bob."""
    async def run():
        mid = (await m.save_memory("alice", NOTE, "From the contract.", "customers/acme"))["memory_id"]
        await settle()
        return mid, await m.share_memory("alice", mid, "shared", ["bob"], **share)
    return asyncio.run(run())


def bob_sees(mid):
    """What bob gets from finding, listing and reading the memory's outcomes."""
    async def run():
        found = [x["memory_id"] for x in await m.find_memory("bob", "When does Acme renew?")]
        listed = [x["memory_id"] for x in await m.list_memories("bob")]
        try:
            await m.get_outcomes("bob", mid)
            read = True
        except m.MemoriaError:
            read = False
        return mid in found, mid in listed, read
    return asyncio.run(run())


def expire(mid):
    """Pretend the share's time has run out."""
    past = (datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=1)).isoformat()
    db.execute("UPDATE memories SET share_expires_at = ? WHERE id = ?", (past, mid))


def test_a_share_that_has_not_expired_works(store):
    mid, shared = save_and_share(expires_in_days=7)
    ends = datetime.fromisoformat(shared["share_expires_at"])
    assert ends.utcoffset() == timedelta(0)  # shown in UTC
    assert timedelta(days=7) - timedelta(minutes=1) < ends - datetime.now(timezone.utc) \
        <= timedelta(days=7)
    assert bob_sees(mid) == (True, True, True)
    found = asyncio.run(m.find_memory("bob", "When does Acme renew?"))[0]
    assert found["share_expires_at"] == shared["share_expires_at"]  # shown to readers too


def test_an_expired_share_is_blocked_for_others_but_not_the_owner(store):
    mid, _ = save_and_share(expires_in_days=1)
    before = db.get_memory(mid)
    expire(mid)
    assert bob_sees(mid) == (False, False, False)
    with pytest.raises(m.MemoriaError, match="not allowed|No memory"):
        asyncio.run(m.record_outcome("bob", mid, "success"))

    async def owner():
        found = await m.find_memory("alice", "When does Acme renew?")
        listed = await m.list_memories("alice")
        await m.get_outcomes("alice", mid)
        updated = await m.update_memory("alice", mid, "Acme renews on 2 March.", "moved")
        return found, listed, updated
    found, listed, updated = asyncio.run(owner())
    assert [x["memory_id"] for x in found] == [mid] and [x["memory_id"] for x in listed] == [mid]
    assert found[0]["verification"] == "verified" and found[0]["share_expires_at"]
    assert updated["verification"] == "verified"
    after = db.get_memory(mid)  # still shared with bob, just no longer readable by him
    assert before["note"] == NOTE
    assert after["visibility"] == "shared" and after["shared_with"] == ["bob"]


def test_expiry_leaves_the_memory_and_its_signature_untouched(store):
    mid, _ = save_and_share(expires_in_days=30)
    before = db.get_memory(mid)
    expire(mid)
    after = db.get_memory(mid)
    for field in ("note", "reason", "topic", "signature", "history", "visibility", "shared_with"):
        assert after[field] == before[field]
    assert m._public(after)["verification"] == "verified"


def test_a_share_with_no_expiry_works_as_before(store):
    mid, shared = save_and_share()
    assert shared["share_expires_at"] is None
    assert db.get_memory(mid).get("share_expires_at") is None
    assert bob_sees(mid) == (True, True, True)


def test_resharing_without_expires_in_days_keeps_the_old_expiry(store):
    mid, first = save_and_share(expires_in_days=5)
    again = asyncio.run(m.share_memory("alice", mid, "shared", ["bob", "carol"]))
    assert again["share_expires_at"] == first["share_expires_at"]  # not wiped
    assert again["shared_with"] == ["bob", "carol"]
    assert db.get_memory(mid)["share_expires_at"] == first["share_expires_at"]


def test_a_new_expires_in_days_replaces_the_old_expiry(store):
    mid, first = save_and_share(expires_in_days=5)
    again = asyncio.run(m.share_memory("alice", mid, "shared", ["bob"], expires_in_days=30))
    ends = datetime.fromisoformat(again["share_expires_at"])
    assert ends > datetime.fromisoformat(first["share_expires_at"])
    assert timedelta(days=30) - timedelta(minutes=1) < ends - datetime.now(timezone.utc)         <= timedelta(days=30)


def test_remove_expiry_clears_it_on_purpose(store):
    mid, _ = save_and_share(expires_in_days=5)
    cleared = asyncio.run(m.share_memory("alice", mid, "shared", ["bob"], remove_expiry=True))
    assert cleared["share_expires_at"] is None
    assert db.get_memory(mid).get("share_expires_at") is None
    # ...and it stays cleared when shared again later without an expiry
    again = asyncio.run(m.share_memory("alice", mid, "shared", ["bob"]))
    assert again["share_expires_at"] is None
    assert bob_sees(mid) == (True, True, True)


def test_remove_expiry_and_a_new_expiry_together_are_refused(store):
    mid, first = save_and_share(expires_in_days=5)
    with pytest.raises(m.MemoriaError, match="not both"):
        asyncio.run(m.share_memory("alice", mid, "shared", ["bob"], expires_in_days=7,
                                   remove_expiry=True))
    assert db.get_memory(mid)["share_expires_at"] == first["share_expires_at"]


def test_making_it_private_clears_the_expiry(store):
    mid, _ = save_and_share(expires_in_days=5)
    private = asyncio.run(m.share_memory("alice", mid, "private"))
    assert private["share_expires_at"] is None


def test_dashboard_sharing_edit_keeps_the_expiry(store):
    from starlette.testclient import TestClient

    import dashboard
    mid, first = save_and_share(expires_in_days=5)
    home = "http://127.0.0.1:8765"
    with TestClient(dashboard.app, base_url=home) as client:
        r = client.post(f"/dashboard/api/memories/{mid}/sharing",
                        headers={"origin": home, "sec-fetch-site": "same-origin"},
                        json={"visibility": "shared", "shared_with": ["bob", "carol"]})
    assert r.status_code == 200
    assert r.json()["share_expires_at"] == first["share_expires_at"]
    assert db.get_memory(mid)["share_expires_at"] == first["share_expires_at"]


@pytest.mark.parametrize("days", [0, 366, -1, 1.5, "7", True])
def test_expires_in_days_must_be_a_whole_number_from_1_to_365(store, days):
    mid = asyncio.run(m.save_memory("alice", NOTE, "r", "t"))["memory_id"]
    with pytest.raises(m.MemoriaError, match="whole number from 1 to 365"):
        asyncio.run(m.share_memory("alice", mid, "shared", ["bob"], expires_in_days=days))
    assert db.get_memory(mid)["visibility"] == "private"  # nothing changed


def test_expiring_share_over_mcp(memoria_url, devs):
    from memoria_client import Memoria

    async def run():
        async with Memoria(api_key=devs["acme/trader"], url=memoria_url) as trader:
            saved = await trader.save(NOTE, reason="contract", topic="customers")
            shared = await trader.share(saved["memory_id"], "shared", ["analyst"],
                                        expires_in_days=365)
            assert shared["share_expires_at"]
            with pytest.raises(Exception, match="whole number"):
                await trader.share(saved["memory_id"], "shared", ["analyst"], expires_in_days=400)
            kept = await trader.share(saved["memory_id"], "shared", ["analyst"])
            assert kept["share_expires_at"] == shared["share_expires_at"]
            cleared = await trader.share(saved["memory_id"], "shared", ["analyst"],
                                         remove_expiry=True)
            assert cleared["share_expires_at"] is None
    asyncio.run(run())
