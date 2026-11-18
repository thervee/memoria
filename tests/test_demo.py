"""The public, read-only demo keys (demo.py)."""
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


def _make_key():
    key_id, secret = secrets.token_hex(6), secrets.token_urlsafe(32)
    return key_id, hashlib.sha256(secret.encode()).hexdigest(), f"mem_{key_id}_{secret}"


@pytest.fixture
def demo_keys(store, monkeypatch):
    """Set up the demo account with keys made for this test. Returns {agent: key}."""
    made, keys = {}, {}
    for agent in (demo.AGENT_A, demo.AGENT_B):
        key_id, key_hash, key = _make_key()
        keys[key_id], made[agent] = (agent, key_hash), key
    monkeypatch.setattr(demo, "KEYS", keys)

    async def setup():
        await demo.ensure()
        await settle()
    asyncio.run(setup())
    return made


def demo_memories():
    return [m for m in db.all_memories() if m["account"] == "demo"]


def test_demo_account_has_three_signed_samples_and_low_limits(demo_keys):
    assert accounts.check_key(demo_keys[demo.AGENT_A]) == ("demo", demo.AGENT_A)
    assert accounts.check_key(demo_keys[demo.AGENT_B]) == ("demo", demo.AGENT_B)
    mems = demo_memories()
    assert sorted((m["agent_id"], m["visibility"]) for m in mems) == [
        (demo.AGENT_A, "private"), (demo.AGENT_A, "shared"), (demo.AGENT_B, "private")]
    assert all(memoria._verification(m)["verification"] == "verified" for m in mems)
    assert accounts.limits("demo") == {**accounts.DEFAULT_LIMITS, **demo.LIMITS}


def test_setting_up_again_changes_nothing(demo_keys):
    before = {m["id"] for m in demo_memories()}
    asyncio.run(demo.ensure())
    assert {m["id"] for m in demo_memories()} == before
    assert len(accounts.keys_of("demo")) == 2


def test_old_samples_are_replaced_and_other_accounts_untouched(store, devs):
    async def run():
        accounts.create_account("demo", demo.ACCOUNT_NAME)
        accounts.set_limit("demo", "max_memories", 3)
        accounts.set_limit("demo", "writes_per_day", 3)
        import identity
        identity.register_agent("demo/demo")
        for n in range(3):  # the first version's samples, saved earlier the same day
            await memoria.save_memory("demo", f"Old sample {n}", "old", "memoria/about",
                                      account="demo")
        await memoria.save_memory("trader", "Acme's own note.", "mine", "x", account="acme")
        await settle()
        await demo.ensure()
        await settle()
    asyncio.run(run())
    assert demo._samples_in_place() and len(demo_memories()) == 3
    assert [m["note"] for m in db.all_memories() if m["account"] == "acme"] == ["Acme's own note."]


def test_someone_elses_demo_account_is_left_alone(store):
    accounts.create_account("demo", "Not the demo")
    asyncio.run(demo.ensure())
    assert accounts.keys_of("demo") == [] and db.count_memories("demo") == 0


def test_demo_keys_show_sharing_and_are_read_only(demo_keys, devs, memoria_url):
    a_shared, a_private, b_private = (s["note"] for s in demo.SAMPLES)

    async def run():
        async with Memoria(api_key=devs["acme/trader"], url=memoria_url) as acme:
            await acme.save("Memoria secret plan for acme.", reason="private",
                            topic="memoria/acme")
        for agent, expected in ((demo.AGENT_A, {a_shared, a_private}),
                                (demo.AGENT_B, {a_shared, b_private})):
            async with Memoria(api_key=demo_keys[agent], url=memoria_url) as mem:
                found = await mem.find("Memoria demo agent memory", topic="memoria", limit=20)
                assert {m["note"] for m in found} == expected
                assert all(m["verification"] == "verified" for m in found)
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
    assert len(demo_memories()) == 3
