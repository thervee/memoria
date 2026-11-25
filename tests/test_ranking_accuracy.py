"""find_memory's ranking with outcomes, checked the way Duke's review did.

Five scenarios, each with one correct memory and some lookalikes. The fake search scores
every memory against the question (a distance: lower is closer), like the real one.
Outcomes are recorded on some memories, then each question is asked and we check
whether the correct memory comes first (top-1) and is in the first three (top-3).

The old ranking (one outcome = one whole place, ties broken by outcomes) is kept here
so the test can show both, before and after the change.
"""
import asyncio

import pytest

import db
import identity
import memoria as m
from conftest import settle

AGENTS = ["bob", "carol", "dave", "erin"]  # they record outcomes; alice saves and asks

# Each scenario: the question, the correct memory, and every memory with its distance
# from the question and the outcomes recorded on it.
SCENARIOS = [
    {   # The correct memory is the best match; a lookalike that helped once is close by.
        "query": "Is the PEPE pool safe to buy?",
        "correct": "PEPE pool: liquidity locked for a year, contract renounced.",
        "memories": [
            ("PEPE pool: liquidity locked for a year, contract renounced.", 0.15, []),
            ("PEPE2 pool: liquidity locked for a month only.", 0.17, ["success"]),
            ("PEPE airdrop was a phishing site.", 0.24, []),
            ("Frog tokens pumped in March.", 0.29, []),
        ]},
    {   # The correct memory is the best match; a weaker lookalike helped twice.
        "query": "What fee does the DOGE curve take?",
        "correct": "DOGE curve takes a 1% fee on every buy and sell.",
        "memories": [
            ("DOGE curve takes a 1% fee on every buy and sell.", 0.14, []),
            ("DOGE on Uniswap has no curve fee.", 0.19, []),
            ("SHIB curve takes a 2% fee.", 0.21, ["success", "success"]),
            ("Curve fees are paid in ETH.", 0.27, []),
        ]},
    {   # The correct memory is the best match but failed once (in another context).
        "query": "When does the WIF unlock happen?",
        "correct": "WIF team tokens unlock on 1 December.",
        "memories": [
            ("WIF team tokens unlock on 1 December.", 0.16, ["failure"]),
            ("WIF listing on a new exchange in December.", 0.20, []),
            ("BONK unlock was delayed.", 0.23, []),
        ]},
    {   # A lookalike matches a hair better but keeps failing; the correct one keeps helping.
        "query": "Which wallet funds the MOG deployer?",
        "correct": "MOG deployer is funded from wallet 0xabc, a known team wallet.",
        "memories": [
            ("MOG copycat deployer is funded from a fresh wallet.", 0.17, ["failure", "failure"]),
            ("MOG deployer is funded from wallet 0xabc, a known team wallet.", 0.18,
             ["success"] * 4),
            ("Deployer wallets are often reused.", 0.26, []),
        ]},
    {   # The correct memory is clearly a weaker match and has helped only once.
        "query": "Did BRETT holders get rugged?",
        "correct": "BRETT v1 holders were rugged when liquidity was pulled.",
        "memories": [
            ("BRETT holders were airdropped a new token.", 0.15, []),
            ("BRETT v1 holders were rugged when liquidity was pulled.", 0.21, ["success"]),
            ("Rugs usually start with liquidity being pulled.", 0.24, []),
        ]},
]


def old_ranked(memories, summaries, distances=None):
    """The ranking before this change, kept to compare against."""
    def key(item):
        place, mem = item
        good = summaries[mem["id"]]["_verified"]
        net = good["success"] - good["failure"]
        shift = max(-2, min(2, net))
        return (place - shift, -net, place)
    return [mem for _, mem in sorted(enumerate(memories), key=key)]


@pytest.fixture
def scenarios(store, monkeypatch):
    """Every scenario's memories saved and processed, with the fake search scoring them."""
    for agent in AGENTS:
        if identity.public_key_of(agent) is None:
            identity.register_agent(agent)
    distance = {(s["query"], note): d for s in SCENARIOS for note, d, _ in s["memories"]}

    async def search(query, query_type, datasets, top_k):
        store._call("search")
        hits = [{"document_id": data_id, "text": doc["text"],
                 "score": distance.get((query, doc["note"]), 0.5)}  # 0.5: unrelated
                for data_id, doc in store.docs.items()
                if doc["processed"] and doc["dataset"] in datasets]
        hits.sort(key=lambda hit: hit["score"])  # closest first, like the real search
        return [{"search_result": hits}]
    monkeypatch.setattr(store, "search", search)
    real_add = store.add

    async def add(text, dataset_name, node_set):  # remember each passage's note
        result = await real_add(text, dataset_name, node_set)
        store.docs[str(result.data_ids[0])]["note"] = text.split("Note: ")[1].split("\n")[0]
        return result
    monkeypatch.setattr(store, "add", add)

    async def save_all():
        ids = {}
        for s in SCENARIOS:
            for note, _, _ in s["memories"]:
                saved = await m.save_memory("alice", note, "research", "crypto",
                                            visibility="shared", shared_with=AGENTS)
                ids[note] = saved["memory_id"]
        await settle()
        return ids
    return asyncio.run(save_all())


def record_all(ids):
    for s in SCENARIOS:
        for note, _, results in s["memories"]:
            for agent, result in zip(AGENTS, results):
                asyncio.run(m.record_outcome(agent, ids[note], result))


def accuracy(ids):
    """Top-1 and top-3 accuracy over all scenarios, as numbers out of len(SCENARIOS)."""
    top1 = top3 = 0
    for s in SCENARIOS:
        found = asyncio.run(m.find_memory("alice", s["query"], limit=5))
        notes = [x["note"] for x in found]
        top1 += notes[:1] == [s["correct"]]
        top3 += s["correct"] in notes[:3]
    return top1, top3


def test_outcomes_nudge_the_ranking_without_burying_the_best_match(scenarios, monkeypatch):
    n = len(SCENARIOS)
    no_outcomes = accuracy(scenarios)
    record_all(scenarios)
    with monkeypatch.context() as old:
        old.setattr(m, "_ranked", old_ranked)
        before = accuracy(scenarios)
    after = accuracy(scenarios)

    print(f"\n{'':24}{'top-1':>8}{'top-3':>8}")
    for label, (t1, t3) in (("no outcomes yet", no_outcomes), ("old ranking", before),
                            ("new ranking", after)):
        print(f"{label:24}{t1 / n:>8.0%}{t3 / n:>8.0%}")

    assert no_outcomes == (3, 5)  # the search alone: right first 3 times in 5
    assert before == (2, 5)       # the old ranking made it worse, like Duke found
    assert after[0] > before[0] and after[1] >= before[1]
    # The one miss left (the last scenario): the correct memory is clearly a weaker match
    # and helped only once, which isn't enough to lift it over a much better match. It's
    # still in the top 3.
    assert after == (4, 5)
    assert db.query("SELECT count(*) FROM outcomes")[0][0] == 11  # all were recorded
