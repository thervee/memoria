"""find_memory's minimum relevance, and share_memory's warning about unknown agents."""
import asyncio

import pytest

import memoria as m
from conftest import settle


def save(note, topic="animals", agent="alice", **extra):
    return asyncio.run(m.save_memory(agent, note, "r", topic, **extra))["memory_id"]


@pytest.fixture
def scored(store, monkeypatch):
    """Make the fake search behave like a real vector search: it returns every stored
    passage, each with a distance score set by the test (lower is closer)."""
    scores = {}

    async def search(query, query_type, datasets, top_k):
        store._call("search")
        return [{"search_result": [
            {"document_id": data_id, "text": doc["text"], "score": scores.get(query, 0.2)}
            for data_id, doc in store.docs.items()
            if doc["processed"] and doc["dataset"] in datasets]}]
    monkeypatch.setattr(store, "search", search)
    return scores


def find(query, **kw):
    async def go():
        await settle()
        return await m.find_memory("alice", query, **kw)
    return asyncio.run(go())


def test_a_nonsense_query_returns_nothing(scored):
    save("Penguins huddle together to keep warm.")
    save("Owls hunt at night.")
    scored["xqzv blorp flimflam zorg"] = 0.43  # what the real model gives nonsense
    assert find("xqzv blorp flimflam zorg") == []


def test_naming_a_topic_still_lists_that_topics_newest(scored):
    save("Penguins huddle together to keep warm.")
    save("Spreadsheets need formulas.", topic="office")
    scored["xqzv blorp"] = 0.43
    found = find("xqzv blorp", topic="animals")  # the agent asked for this topic
    assert [x["note"] for x in found] == ["Penguins huddle together to keep warm."]


def test_a_good_match_is_still_found(scored):
    save("Penguins huddle together to keep warm.")
    scored["How do penguins stay warm?"] = 0.21
    assert [x["note"] for x in find("How do penguins stay warm?")] == [
        "Penguins huddle together to keep warm."]


def test_the_threshold_is_0_40_and_can_be_changed(scored, monkeypatch):
    save("Penguins huddle together to keep warm.")
    scored["loose"] = 0.40
    assert len(find("loose")) == 1  # exactly at the limit: kept
    scored["loose"] = 0.41
    assert find("loose") == []
    monkeypatch.setenv("MEMORIA_SEARCH_MAX_DISTANCE", "0.5")
    assert len(find("loose")) == 1


def test_sharing_with_unknown_agents_warns_but_still_shares(store):
    mid = save("Penguins huddle.")
    out = asyncio.run(m.share_memory("alice", mid, "shared", ["bob", "zed", "yan"]))
    assert out["shared_with"] == ["bob", "zed", "yan"]
    assert out["warning"].startswith("zed, yan are not an agent on this account (yet).")
    assert "bob" not in out["warning"]
    one = asyncio.run(m.share_memory("alice", mid, "shared", ["zed"]))
    assert one["warning"].startswith("zed is not an agent on this account (yet).")


def test_sharing_with_known_agents_has_no_warning(store, devs):
    mid = save("Acme's note.", topic="t", agent="trader", account="acme")
    out = asyncio.run(m.share_memory("trader", mid, "shared", ["analyst"], account="acme"))
    assert "warning" not in out
    # an agent of ANOTHER account isn't on this one
    other = asyncio.run(m.share_memory("trader", mid, "shared", ["ghost"], account="acme"))
    assert "ghost" in other["warning"]
