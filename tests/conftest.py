"""Shared test setup for Memoria.

Every test runs in its own temporary folder with its own agents and keys, and
with a FAKE Cognee that lives in memory. So the tests are fast, never call
Gemini, and never touch the real memory_store or keys folders.
"""
import asyncio
import re
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import identity  # noqa: E402
import memoria  # noqa: E402

AGENTS = ("alice", "bob", "carol")


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


class FakeCognee:
    """Stands in for Cognee. Search matches shared words, but only on processed items."""

    def __init__(self):
        self.docs = {}        # data_id -> {"text": ..., "processed": bool}
        self.dataset_id = uuid.uuid4()
        self.calls = []       # names of the calls made, in order
        self.fail = {}        # call name -> how many more times it should fail

    def _call(self, name):
        self.calls.append(name)
        if self.fail.get(name, 0) > 0:
            self.fail[name] -= 1
            raise RuntimeError(f"pretend {name} failed")

    async def add(self, text, dataset_name, node_set):
        self._call("add")
        data_id = uuid.uuid4()
        self.docs[str(data_id)] = {"text": text, "processed": False}
        return SimpleNamespace(data_ids=[data_id], dataset_id=self.dataset_id)

    async def cognify(self, datasets):
        self._call("cognify")
        for doc in self.docs.values():
            doc["processed"] = True

    async def update(self, data_id, data, dataset_id, node_set):
        self._call("update")
        self.docs[str(data_id)] = {"text": data, "processed": True}
        return {"status": "ok"}

    async def forget(self, data_id, dataset_id):
        self._call("forget")
        del self.docs[str(data_id)]

    async def search(self, query, query_type, datasets, top_k):
        self._call("search")
        hits = [{"document_id": data_id, "text": doc["text"]}
                for data_id, doc in self.docs.items()
                if doc["processed"] and words(query) & words(doc["text"])]
        return [{"search_result": hits}]

    def count(self, name):
        return self.calls.count(name)


@pytest.fixture
def keys(tmp_path, monkeypatch):
    """A temporary keys folder with three registered agents: alice, bob and carol."""
    keys_dir = tmp_path / "keys"
    monkeypatch.setattr(identity, "KEYS_DIR", keys_dir)
    monkeypatch.setattr(identity, "REGISTRY", keys_dir / "public_keys.json")
    for agent in AGENTS:
        identity.register_agent(agent)
    return keys_dir


@pytest.fixture
def store(tmp_path, monkeypatch, keys):
    """A temporary memory store and a fake Cognee. Returns the fake Cognee."""
    folder = tmp_path / "memory_store"
    monkeypatch.setattr(memoria, "STORE", folder)
    monkeypatch.setattr(memoria, "INDEX_FILE", folder / "memories.json")
    monkeypatch.setattr(memoria, "AGENTS_FILE", folder / "agents.json")
    monkeypatch.setattr(memoria, "ACTIVITY_FILE", folder / "activity.log")
    monkeypatch.setattr(memoria, "FORGET_FILE", folder / "pending_forget.json")
    fake = FakeCognee()
    monkeypatch.setattr(memoria, "cognee", fake)
    # Fresh lock and worker for each test (each test runs its own event loop).
    monkeypatch.setattr(memoria, "_write_lock", asyncio.Lock())
    monkeypatch.setattr(memoria, "_cognee_lock", asyncio.Lock())
    monkeypatch.setattr(memoria, "_worker", None)
    # The worker waits between retries; don't make the tests wait.
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda seconds: real_sleep(0))
    return fake


async def settle():
    """Wait until the background worker has finished everything.

    (Each asyncio.run() ends by cancelling a worker it started, so start one again here.)
    """
    memoria.start_background_work()
    while memoria._worker is not None and not memoria._worker.done():
        await memoria._worker
