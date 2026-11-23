"""Shared test setup for Memoria.

Every test runs in its own temporary folder with its own agents, keys and accounts,
and with a FAKE Cognee that lives in memory. So the tests are fast, never call
Gemini, and never touch the real memory_store, keys or backups folders.
"""
import asyncio
import re
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import accounts  # noqa: E402
import db  # noqa: E402
import identity  # noqa: E402
import limits  # noqa: E402
import memoria  # noqa: E402

AGENTS = ("alice", "bob", "carol")


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


class FakeCognee:
    """Stands in for Cognee. Search matches shared words, but only on processed items,
    and only in the datasets asked for (like the real one)."""

    def __init__(self):
        self.docs = {}        # data_id -> {"text", "processed", "dataset"}
        self.calls = []       # names of the calls made, in order
        self.searched = []    # the datasets each search asked for
        self.fail = {}        # call name -> how many more times it should fail
        self.during_add = None  # something to do while an add is "in progress"

    def _call(self, name):
        self.calls.append(name)
        if self.fail.get(name, 0) > 0:
            self.fail[name] -= 1
            raise RuntimeError(f"pretend {name} failed")

    async def add(self, text, dataset_name, node_set):
        self._call("add")
        if self.during_add:
            hook, self.during_add = self.during_add, None
            await hook()
        data_id = uuid.uuid4()
        self.docs[str(data_id)] = {"text": text, "processed": False, "dataset": dataset_name}
        return SimpleNamespace(data_ids=[data_id],
                               dataset_id=uuid.uuid5(uuid.NAMESPACE_DNS, dataset_name))

    async def cognify(self, datasets):
        self._call("cognify")
        for doc in self.docs.values():
            if doc["dataset"] in datasets:
                doc["processed"] = True

    async def update(self, data_id, data, dataset_id, node_set):
        self._call("update")
        self.docs[str(data_id)].update(text=data, processed=True)
        return {"status": "ok"}

    async def forget(self, data_id, dataset_id):
        self._call("forget")
        del self.docs[str(data_id)]

    async def search(self, query, query_type, datasets, top_k):
        self._call("search")
        self.searched.append(list(datasets))
        hits = [{"document_id": data_id, "text": doc["text"]}
                for data_id, doc in self.docs.items()
                if doc["processed"] and doc["dataset"] in datasets
                and words(query) & words(doc["text"])]
        return [{"search_result": hits}]

    def count(self, name):
        return self.calls.count(name)


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    """Every test starts with default settings and empty spam-protection counters."""
    for name in ("MEMORIA_SIGNUP", "MEMORIA_TRUST_PROXY", "MEMORIA_DASHBOARD",
                 "MEMORIA_API_KEY", "MEMORIA_URL", "MEMORIA_BILLING",
                 "MEMORIA_TREASURY_ADDRESS", "MEMORIA_TOKEN_ADDRESS", "MEMORIA_CHAIN_ID",
                 "MEMORIA_RPC_URL", "MEMORIA_TOKENS_PER_CREDIT", "MEMORIA_ACTIONS_PER_CREDIT",
                 "MEMORIA_STARTER_CREDITS", "MEMORIA_MIN_CONFIRMATIONS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMORIA_SECRET_KEY", identity.make_server_secret())
    limits.reset_all()
    yield
    limits.reset_all()
    db.close()


@pytest.fixture
def keys(tmp_path, monkeypatch):
    """Temporary keys and accounts, with three of this laptop's agents: alice, bob, carol."""
    keys_dir = tmp_path / "keys"
    monkeypatch.setattr(identity, "KEYS_DIR", keys_dir)
    monkeypatch.setattr(identity, "REGISTRY", keys_dir / "public_keys.json")
    db.close()
    monkeypatch.setattr(db, "DB_FILE", tmp_path / "memory_store" / "memoria.db")
    for agent in AGENTS:
        identity.register_agent(agent)
    return keys_dir


@pytest.fixture
def store(tmp_path, monkeypatch, keys):
    """A temporary memory store and a fake Cognee. Returns the fake Cognee."""
    folder = tmp_path / "memory_store"
    monkeypatch.setattr(memoria, "STORE", folder)
    monkeypatch.setattr(memoria, "ACTIVITY_FILE", folder / "activity.log")
    fake = FakeCognee()
    monkeypatch.setattr(memoria, "cognee", fake)
    # Fresh locks and worker for each test (each test runs its own event loop).
    monkeypatch.setattr(memoria, "_write_lock", asyncio.Lock())
    monkeypatch.setattr(memoria, "_cognee_lock", asyncio.Lock())
    monkeypatch.setattr(memoria, "_worker", None)
    # The worker waits between retries; don't make the tests wait.
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda seconds: real_sleep(0))
    return fake


@pytest.fixture
def devs(keys):
    """Two developer accounts, acme and globex, each with agents 'trader' and 'analyst'.
    Returns their API keys, e.g. devs["acme/trader"]."""
    made = {}
    for account in ("acme", "globex"):
        accounts.create_account(account, email=f"dev@{account}.example")
        for agent in ("trader", "analyst"):
            made[f"{account}/{agent}"] = accounts.create_key(account, agent)
    return made


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def memoria_url(store):
    """A real Memoria agents' server on a spare port. Returns its address."""
    import uvicorn

    import server
    port = free_port()
    app = server.LoginCheck(server.mcp.streamable_http_app(host="127.0.0.1"))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
    web = uvicorn.Server(config)
    thread = threading.Thread(target=web.run, daemon=True)
    thread.start()
    for _ in range(100):
        if web.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    web.should_exit = True
    thread.join(timeout=10)


# ---------- looking inside Memoria's database ----------

def index() -> dict:
    """Every stored memory, by ID, as Memoria keeps it."""
    return {mem["id"]: mem for mem in db.all_memories()}


def save_index(data: dict) -> None:
    """Store these memories exactly as given (for tests that tamper with the data)."""
    for mem in data.values():
        db.put_memory(mem)


def pending_forget() -> list:
    return db.forget_queue()


def everything_stored() -> str:
    """All of Memoria's database as text, to check what is (and isn't) kept."""
    tables = [row[0] for row in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")]
    return "\n".join(str(tuple(row)) for t in tables for row in db.query(f"SELECT * FROM {t}"))


async def settle():
    """Wait until the background worker has finished everything.

    (Each asyncio.run() ends by cancelling a worker it started, so start one again here.)
    """
    memoria.start_background_work()
    while memoria._worker is not None and not memoria._worker.done():
        await memoria._worker
