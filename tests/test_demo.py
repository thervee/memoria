"""The public, read-only demo key (demo.py)."""
import asyncio
import hashlib
import secrets

import pytest

import accounts
import db
import demo
import memoria
from conftest import settle
from memoria_client import Memoria


@pytest.fixture
def demo_key(store, monkeypatch):
    """Set up the demo account with a key made for this test. Returns the key."""
    key_id, secret = secrets.token_hex(6), secrets.token_urlsafe(32)
    monkeypatch.setattr(demo, "KEY_ID", key_id)
    monkeypatch.setattr(demo, "KEY_HASH", hashlib.sha256(secret.encode()).hexdigest())

    async def setup():
        await demo.ensure()
        await settle()
    asyncio.run(setup())
    return f"mem_{key_id}_{secret}"


def test_demo_account_has_three_signed_samples_and_low_limits(demo_key):
    assert accounts.check_key(demo_key) == ("demo", "demo")
    mems = [m for m in db.all_memories() if m["account"] == "demo"]
    assert len(mems) == 3 and all(m["agent_id"] == "demo" for m in mems)
    assert all(memoria._verification(m)["verification"] == "verified" for m in mems)
    assert accounts.limits("demo") == {**accounts.DEFAULT_LIMITS, **demo.LIMITS}


def test_setting_up_again_changes_nothing(demo_key):
    asyncio.run(demo.ensure())
    assert db.count_memories("demo") == 3
    assert len(accounts.keys_of("demo")) == 1


def test_someone_elses_demo_account_is_left_alone(store):
    accounts.create_account("demo", "Not the demo")
    asyncio.run(demo.ensure())
    assert accounts.keys_of("demo") == [] and db.count_memories("demo") == 0


def test_demo_key_can_only_find_its_own_samples(demo_key, devs, memoria_url):
    async def run():
        async with Memoria(api_key=devs["acme/trader"], url=memoria_url) as acme:
            await acme.save("Memoria secret plan for acme.", reason="private",
                            topic="memoria/acme")
        async with Memoria(api_key=demo_key, url=memoria_url) as mem:
            found = await mem.find("What is Memoria?", limit=20)
            assert found and {m["account"] for m in found} == {"demo"}
            assert len(await mem.find("Memoria memory", topic="memoria", limit=20)) == 3
            some_id = found[0]["memory_id"]
            for attempt in (
                mem.save("Hello", reason="test", topic="x"),
                mem.update(some_id, "changed", reason="test"),
                mem.share(some_id, "shared", ["other"]),
                mem.delete(some_id),
            ):
                with pytest.raises(Exception, match="read-only"):
                    await attempt
    asyncio.run(run())
    assert db.count_memories("demo") == 3
