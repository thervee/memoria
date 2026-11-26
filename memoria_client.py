"""Connect to Memoria from Python, with login passes handled for you.

For agents you write yourself in Python. A fresh login pass is made automatically
whenever the old one is close to expiring, so the agent never has to think about it.

    import asyncio
    from memoria_client import Memoria

    async def main():
        async with Memoria("trader") as mem:
            saved = await mem.save("LINK fell 21% in 11 days.", reason="CoinGecko",
                                   topic="crypto/chainlink", confidence="high")
            for found in await mem.find("What happened to the LINK price?"):
                print(found["note"], found["verification"])

    asyncio.run(main())

The agent must be registered on this laptop first (connect_agent.bat does that).

For a Memoria hosted online, use the API key the host gave you instead:

    async with Memoria(api_key="mem_...", url="https://memoria.example.com/mcp") as mem:
        ...
"""
import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx2  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

import identity  # noqa: E402

URL = "http://127.0.0.1:8765/mcp"
REFRESH_AFTER = identity.TOKEN_LIFETIME - 600  # make a new pass 10 minutes before it expires


class MemoriaNotRunning(ConnectionError):
    """Memoria isn't answering at 127.0.0.1:8765."""


class ApiKey(httpx2.Auth):
    """Adds 'Authorization: Bearer <API key>' to every request (for a hosted Memoria)."""

    def __init__(self, api_key: str):
        self.api_key = api_key.strip()

    def token(self) -> str:
        return self.api_key

    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {self.api_key}"
        yield request


class FreshPass(httpx2.Auth):
    """Adds 'Authorization: Bearer <login pass>' to every request, renewing it in time."""

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self._token, self._made_at = None, 0.0

    def token(self) -> str:
        if self._token is None or time.time() - self._made_at > REFRESH_AFTER:
            self._token, self._made_at = identity.make_token(self.agent_id), time.time()
        return self._token

    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {self.token()}"
        yield request


@asynccontextmanager
async def session(agent_id: str | None = None, url: str = URL, auth=None):
    """An open MCP session with Memoria, logged in as agent_id (or with `auth`)."""
    auth = auth or FreshPass(agent_id)
    auth.token()  # fail early, with a clear message, if the agent isn't registered
    try:
        async with httpx2.AsyncClient(auth=auth, timeout=httpx2.Timeout(30, read=300)) as http:
            async with streamable_http_client(url, http_client=http) as (read, write):
                async with ClientSession(read, write) as s:
                    await s.initialize()  # afterwards s.initialize_result holds the details
                    yield s
    except Exception as e:
        # The MCP library runs several tasks at once and bundles their errors into a
        # group. Turn "couldn't connect" into one clear message, and unwrap single errors.
        if _contains(e, httpx2.ConnectError):
            if url.startswith(("http://127.0.0.1", "http://localhost")):
                message = "Memoria isn't running. Start it by double-clicking start_memoria.bat."
            else:
                message = f"Can't reach Memoria at {url}. Check the address and your connection."
            raise MemoriaNotRunning(message) from None
        while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
            e = e.exceptions[0]
        raise e


def _contains(error: BaseException, kind: type) -> bool:
    if isinstance(error, kind):
        return True
    if isinstance(error, BaseExceptionGroup):
        return any(_contains(inner, kind) for inner in error.exceptions)
    return False


def _result(result):
    """Turn a tool result into plain Python data, or raise if Memoria refused."""
    text = "\n".join(c.text for c in result.content if getattr(c, "text", None))
    if result.is_error:
        raise RuntimeError(text.removeprefix("Error executing tool ").strip())
    data = result.structured_content
    if isinstance(data, dict) and set(data) == {"result"}:  # lists come wrapped
        return data["result"]
    if data is not None:
        return data
    return json.loads(text) if text else None


class Memoria:
    """Memoria's five tools as simple Python methods, for one agent."""

    def __init__(self, agent_id: str | None = None, url: str = URL, api_key: str | None = None):
        if not agent_id and not api_key:
            raise ValueError("Give an agent_id (this laptop) or an api_key (hosted Memoria).")
        self.agent_id, self.url = agent_id, url
        self.auth = ApiKey(api_key) if api_key else FreshPass(agent_id)
        self._session = None
        self._stack = None

    async def __aenter__(self):
        self._stack = session(self.agent_id, self.url, self.auth)
        self._session = await self._stack.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self._stack.__aexit__(*exc)

    async def _call(self, tool: str, **args):
        args = {k: v for k, v in args.items() if v is not None}
        return _result(await self._session.call_tool(tool, args))

    async def save(self, note, reason, topic, confidence="medium", visibility="private",
                   shared_with=None, source=None) -> dict:
        return await self._call("save_memory", note=note, reason=reason, topic=topic,
                                source=source,
                                confidence=confidence, visibility=visibility,
                                shared_with=shared_with)

    async def find(self, query, topic=None, saved_by=None, limit=5, outcome=None) -> list[dict]:
        return await self._call("find_memory", query=query, topic=topic, saved_by=saved_by,
                                limit=limit, outcome=outcome)

    async def list(self, topic=None, saved_by=None, limit=20, outcome=None) -> list[dict]:
        return await self._call("list_memories", topic=topic, saved_by=saved_by, limit=limit,
                                outcome=outcome)

    async def record_outcome(self, memory_id, result, note="") -> dict:
        return await self._call("record_outcome", memory_id=memory_id, result=result, note=note)

    async def outcomes(self, memory_id) -> dict:
        return await self._call("get_outcomes", memory_id=memory_id)

    # Paying with $MEMORIA (only when the host has turned billing on)
    async def wallet_challenge(self) -> dict:
        return await self._call("wallet_challenge")

    async def link_wallet(self, address, signature) -> dict:
        return await self._call("link_wallet", address=address, signature=signature)

    async def claim_payment(self, tx_hash) -> dict:
        return await self._call("claim_payment", tx_hash=tx_hash)

    async def balance(self) -> dict:
        return await self._call("get_balance")

    async def update(self, memory_id, new_note, reason, confidence=None, topic=None) -> dict:
        return await self._call("update_memory", memory_id=memory_id, new_note=new_note,
                                reason=reason, confidence=confidence, topic=topic)

    async def share(self, memory_id, visibility, shared_with=None, expires_in_days=None,
                    remove_expiry=False) -> dict:
        extra = {} if expires_in_days is None else {"expires_in_days": expires_in_days}
        if remove_expiry:
            extra["remove_expiry"] = True
        return await self._call("share_memory", memory_id=memory_id, visibility=visibility,
                                shared_with=shared_with, **extra)

    async def delete(self, memory_id) -> dict:
        return await self._call("delete_memory", memory_id=memory_id)

    async def export(self, topic=None) -> dict:
        return await self._call("export_memories", topic=topic)

    async def restore(self, export) -> dict:
        return await self._call("restore_memories", export=export)
