"""Memoria MCP server: gives AI agents shared long-term memory.

ONE Memoria server runs on this laptop and every agent connects to it at
    http://127.0.0.1:8765/mcp
(only this computer can reach that address). Running a single server means
agents never fight over the database files.

Agents must PROVE who they are. Every request needs the header
    Authorization: Bearer <login pass or API key>
  - this laptop's own agents use a login pass made with their secret key
    (identity.py, make_token.py); they belong to the built-in account "local".
  - developers' agents (when hosted online) use an API key (accounts.py), which
    says which account and agent they are.
Requests without a valid pass or key are refused with "401". Any agent or account
name the request claims for itself is ignored; only the proven identity is used.

Spam protection (limits.py): too many requests from one address or one agent,
too many wrong keys, or requests that are too big are refused before they reach
Memoria, with "429 Too Many Requests" or "413 Too Large".

Developers can also sign themselves up at /signup if the host turns it on
(MEMORIA_SIGNUP, see signup.py). It is off unless turned on.

The same server also serves the dashboard webpage at
    http://127.0.0.1:8765/dashboard
which opens without a password but only from this laptop (see dashboard.py).

Start it with start_memoria.bat (or: .venv\\Scripts\\python.exe server.py).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import Context, MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

import accounts  # noqa: E402
import backup  # noqa: E402
import dashboard  # noqa: E402
import demo  # noqa: E402
import identity  # noqa: E402
import limits  # noqa: E402
import memoria  # noqa: E402
import paths  # noqa: E402
import signup  # noqa: E402

# 127.0.0.1:8765 on this laptop; 0.0.0.0 and the host's PORT when online (see paths.py).
HOST, PORT = paths.HOST, paths.PORT
# Set ONLY by the login check below, never trusted from what a request sends.
AGENT_HEADER = "x-memoria-agent"
ACCOUNT_HEADER = "x-memoria-account"

mcp = MCPServer(
    "memoria",
    instructions=(
        "Memoria is your long-term memory. Before starting work, use find_memory to "
        "check what you (or other agents) already learned about the topic. Save useful "
        "findings and decisions with save_memory, always with the reason. When something "
        "you remembered turns out to be outdated or wrong, use update_memory on it "
        "instead of saving a duplicate. Use delete_memory only for memories you wrote "
        "that are wrong and shouldn't be kept at all. Memories are private to you unless you share "
        "them with specific agent IDs (visibility='shared', shared_with=[...]). Every "
        "memory is signed; find_memory shows 'verified' or 'NOT verified' for each one: "
        "treat NOT verified memories with suspicion."
    ),
)


async def _refuse(send, status: int, error: str, message: str, headers=()) -> None:
    body = json.dumps({"error": error, "message": message}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), *headers]})
    await send({"type": "http.response.body", "body": body})


async def _too_many(send, wait: float, message: str) -> None:
    seconds = max(1, round(wait))
    await _refuse(send, 429, "too_many_requests", f"{message} Try again in {seconds} seconds.",
                  [(b"retry-after", str(seconds).encode())])


def _capped(receive, most: int):
    """Stop reading a request body once it passes `most` bytes (for bodies sent in pieces)."""
    seen = 0

    async def wrapped():
        nonlocal seen
        message = await receive()
        if message["type"] == "http.request":
            seen += len(message.get("body", b""))
            if seen > most:
                return {"type": "http.disconnect"}
        return message
    return wrapped


class LoginCheck:
    """Checks every request before it reaches Memoria: limits first, then the login."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":  # start-up / shut-down messages pass straight through
            return await self.app(scope, receive, send)
        ip = limits.client_ip(scope)

        # 1. Spam protection: too many requests from this address?
        if not limits.is_this_computer(ip):
            wait = limits.per_ip.allow(ip)
            if wait:
                return await _too_many(send, wait, "Too many requests from your address.")
            wait = limits.failed_logins.blocked_for(ip)
            if wait:
                return await _too_many(send, wait, "Too many wrong API keys from your address.")

        # 2. Throw away any identity the request claims for itself.
        drop = {AGENT_HEADER.encode(), ACCOUNT_HEADER.encode()}
        headers = [(k, v) for k, v in scope["headers"] if k.lower() not in drop]
        found = dict(headers)
        length = found.get(b"content-length", b"0")
        if not length.isdigit() or int(length) > limits.MAX_REQUEST_BYTES:
            return await _refuse(send, 413, "too_large",
                                 f"Requests can be {limits.MAX_REQUEST_BYTES:,} bytes at most.")

        # 3. Who is this? A login pass (this laptop's agents) or an API key (developers).
        auth = found.get(b"authorization", b"").decode(errors="replace")
        try:
            if not auth.lower().startswith("bearer "):
                raise identity.IdentityError(
                    "No login pass or API key. Send 'Authorization: Bearer <key>'.")
            credential = auth[7:].strip()
            if credential.startswith(accounts.KEY_PREFIX):
                account, agent_id = accounts.check_key(credential)
            else:
                account, agent_id = identity.LOCAL, identity.check_token(credential)
        except (identity.IdentityError, accounts.AccountError) as e:
            if not limits.is_this_computer(ip):
                limits.failed_logins.hit(ip)
            return await _refuse(send, 401, "unauthorized", str(e),
                                 [(b"www-authenticate", b"Bearer")])

        # 4. Spam protection: too many requests from this agent, or from its whole account?
        #    Both are checked before either is counted, so a refused request uses up nothing.
        account_limits = accounts.limits(account)
        if account_limits:
            who = identity.principal(account, agent_id)
            wait = limits.per_agent.blocked_for(who, account_limits["requests_per_minute"])
            if wait:
                return await _too_many(send, wait, "This agent is sending too many requests.")
            wait = limits.per_account.blocked_for(
                account, account_limits["account_requests_per_minute"])
            if wait:
                return await _too_many(send, wait, "This account's agents are sending too many "
                                                   "requests between them.")
            limits.per_agent.hit(who)
            limits.per_account.hit(account)

        # 5. Pass the PROVEN identity on to the tools.
        scope = dict(scope, headers=headers + [(AGENT_HEADER.encode(), agent_id.encode()),
                                               (ACCOUNT_HEADER.encode(), account.encode())])
        await self.app(scope, _capped(receive, limits.MAX_REQUEST_BYTES), send)


def _agent(ctx: Context) -> tuple[str, str]:
    """The calling agent's ID and account, as proven by its login pass or API key."""
    headers = {k.lower(): v for k, v in (ctx.headers or {}).items()}
    agent_id = headers.get(AGENT_HEADER, "").strip()
    account = headers.get(ACCOUNT_HEADER, "").strip()
    if not agent_id or not account:  # can't happen behind LoginCheck, but never guess
        raise ToolError("Not logged in.")
    return agent_id, account


def _writer(ctx: Context) -> tuple[str, str]:
    """Like _agent, but refuses the public demo key, which may only use find_memory."""
    agent_id, account = _agent(ctx)
    if demo.is_read_only(account):
        raise ToolError("The demo key is read-only: it can only use find_memory.")
    return agent_id, account


async def _run(action):
    """Run a memory action; show the agent a clear message if its request was wrong."""
    try:
        return await action
    except memoria.MemoriaError as e:
        raise ToolError(str(e)) from e


@mcp.tool()
async def save_memory(note: str, reason: str, topic: str, ctx: Context,
                      confidence: str = "medium", visibility: str = "private",
                      shared_with: list[str] | None = None) -> dict:
    """Save a note or decision to long-term memory, with the reason behind it.

    Args:
        note: What to remember: a finding, fact or decision. Make it understandable on its own.
        reason: Why this matters or why the decision was made (sources welcome).
        topic: Short topic label, e.g. "crypto/chainlink" or "project/website".
        confidence: How sure you are: "low", "medium" or "high".
        visibility: "private" (only you can read it) or "shared" (you plus shared_with).
        shared_with: For shared memories: the agent IDs allowed to read it, e.g. ["trader"].
    """
    agent_id, account = _writer(ctx)
    return await _run(memoria.save_memory(agent_id, note, reason, topic, confidence,
                                          visibility, shared_with, account=account))


@mcp.tool()
async def find_memory(query: str, ctx: Context, topic: str | None = None,
                      saved_by: str | None = None, limit: int = 5) -> list[dict]:
    """Search past memories by topic or question. Best matches come first.

    Only returns memories you are allowed to read: your own, plus ones shared with you.

    Args:
        query: A question or keywords, e.g. "What do we know about Chainlink's token?"
        topic: Optional: only return memories whose topic contains this text.
        saved_by: Optional: only return memories saved by this agent ID.
        limit: How many memories to return (1-20).
    """
    agent_id, account = _agent(ctx)
    return await _run(memoria.find_memory(agent_id, query, topic, saved_by, limit,
                                          account=account))


@mcp.tool()
async def update_memory(memory_id: str, new_note: str, reason: str, ctx: Context,
                        confidence: str | None = None, topic: str | None = None) -> dict:
    """Update an existing memory with new information. The old version is kept in history.

    You can update memories you wrote or that were shared with you.

    Args:
        memory_id: The memory's ID (like "mem-1a2b3c4d"), from find_memory or save_memory.
        new_note: The full, up-to-date note. It replaces the old note.
        reason: Why it changed, e.g. "Price changed" or "Earlier figure was wrong".
        confidence: Optional new confidence: "low", "medium" or "high".
        topic: Optional new topic label.
    """
    agent_id, account = _writer(ctx)
    return await _run(memoria.update_memory(agent_id, memory_id, new_note, reason,
                                            confidence, topic, account=account))


@mcp.tool()
async def share_memory(memory_id: str, visibility: str, ctx: Context,
                       shared_with: list[str] | None = None) -> dict:
    """Change who can read a memory. Only the agent that wrote the memory can do this.

    Args:
        memory_id: The memory's ID (like "mem-1a2b3c4d").
        visibility: "private" (only the writer) or "shared" (the writer plus shared_with).
        shared_with: For shared: the full list of agent IDs allowed to read it. It replaces the old list.
    """
    agent_id, account = _writer(ctx)
    return await _run(memoria.share_memory(agent_id, memory_id, visibility, shared_with,
                                           account=account))


@mcp.tool()
async def delete_memory(memory_id: str, ctx: Context) -> dict:
    """Delete a memory for good, including its history. Only the agent that wrote it can do this.

    Use this for memories that are wrong or no longer useful and shouldn't be kept at all.
    If a memory is only out of date, use update_memory instead: it keeps the history.

    Args:
        memory_id: The memory's ID (like "mem-1a2b3c4d"), from find_memory or save_memory.
    """
    agent_id, account = _writer(ctx)
    return await _run(memoria.delete_memory(agent_id, memory_id, account=account))


def _already_running() -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1" if HOST == "0.0.0.0" else HOST, PORT)) == 0


async def health(scope, receive, send) -> None:
    """GET /health: is Memoria up and can it reach its database? (Railway checks this.)"""
    try:
        import db
        db.query("SELECT 1")
        status, body = 200, {"status": "ok"}
    except Exception:
        status, body = 503, {"status": "database unavailable"}
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": json.dumps(body).encode()})


def build_app():
    """The whole of Memoria: the agents' server, the dashboard and sign-up, at one address."""
    agents_app = LoginCheck(mcp.streamable_http_app(host=HOST))

    async def memoria_app(scope, receive, send):
        """Send each address to the right part of Memoria."""
        if scope["type"] == "lifespan":  # on start-up, finish memories left half-processed
            async def receive_and_resume():
                message = await receive()
                if message["type"] == "lifespan.startup":
                    if paths.ONLINE:  # the public read-only demo key (see demo.py)
                        try:
                            await demo.ensure()
                        except Exception as e:
                            print(f"Memoria demo key not set up: {e}", file=sys.stderr)
                    memoria.start_background_work()
                    backup.start()  # daily backups of memory_store
                return message
            return await agents_app(scope, receive_and_resume, send)
        if scope["type"] == "http" and scope["path"] == "/health":
            return await health(scope, receive, send)
        if scope["type"] == "http" and dashboard.handles(scope["path"]):
            return await dashboard.app(scope, receive, send)
        if scope["type"] == "http" and signup.handles(scope["path"]):
            return await signup.app(scope, receive, send)
        return await agents_app(scope, receive, send)

    return memoria_app


if __name__ == "__main__":
    problems = paths.online_problems()
    if problems:  # online, refuse to start rather than run half set up
        print("Memoria can't start online yet:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        sys.exit(1)
    if _already_running():
        print("Memoria is already running. Nothing to do.", file=sys.stderr)
        sys.exit(0)
    if paths.ONLINE:
        print(f"Memoria is running online on port {PORT} (data in {paths.DATA_DIR}).",
              file=sys.stderr)
    else:
        print(f"Memoria is running at http://{HOST}:{PORT}/mcp  (close this window to stop it)",
              file=sys.stderr)
    import uvicorn

    uvicorn.run(build_app(), host=HOST, port=PORT, log_level="warning")
