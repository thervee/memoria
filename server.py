"""Memoria MCP server: gives AI agents shared long-term memory.

ONE Memoria server runs on this laptop and every agent connects to it at
    http://127.0.0.1:8765/mcp
(only this computer can reach that address). Running a single server means
agents never fight over the database files.

Agents must PROVE who they are. Every request needs the header
    Authorization: Bearer <login pass>
where the login pass is made with the agent's secret key (see identity.py and
make_token.py). Requests without a valid pass are refused with "401". Any agent
name the request claims for itself is ignored; only the proven ID is used.

Start it with start_memoria.bat (or: .venv\\Scripts\\python.exe server.py).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import Context, MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

import identity  # noqa: E402
import memoria  # noqa: E402

HOST, PORT = "127.0.0.1", 8765
AGENT_HEADER = "x-memoria-agent"  # set ONLY by the login check below, never trusted from agents

mcp = MCPServer(
    "memoria",
    instructions=(
        "Memoria is your long-term memory. Before starting work, use find_memory to "
        "check what you (or other agents) already learned about the topic. Save useful "
        "findings and decisions with save_memory, always with the reason. When something "
        "you remembered turns out to be outdated or wrong, use update_memory on it "
        "instead of saving a duplicate. Memories are private to you unless you share "
        "them with specific agent IDs (visibility='shared', shared_with=[...]). Every "
        "memory is signed; find_memory shows 'verified' or 'NOT verified' for each one: "
        "treat NOT verified memories with suspicion."
    ),
)


class LoginCheck:
    """Checks the login pass on every request before it reaches Memoria."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":  # start-up / shut-down messages pass straight through
            return await self.app(scope, receive, send)
        headers = [(k, v) for k, v in scope["headers"] if k.lower() != AGENT_HEADER.encode()]
        auth = dict(headers).get(b"authorization", b"").decode(errors="replace")
        try:
            if not auth.lower().startswith("bearer "):
                raise identity.IdentityError("No login pass. Send 'Authorization: Bearer <pass>'.")
            agent_id = identity.check_token(auth[7:])
        except identity.IdentityError as e:
            body = ('{"error": "unauthorized", "message": ' + json.dumps(str(e)) + "}").encode()
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body", "body": body})
            return
        # Replace whatever the agent claimed with the identity its key proved.
        scope = dict(scope, headers=headers + [(AGENT_HEADER.encode(), agent_id.encode())])
        await self.app(scope, receive, send)


def _agent(ctx: Context) -> str:
    """The calling agent's ID, as proven by its login pass."""
    headers = {k.lower(): v for k, v in (ctx.headers or {}).items()}
    agent_id = headers.get(AGENT_HEADER, "").strip()
    if not agent_id:  # can't happen behind LoginCheck, but never guess an identity
        raise ToolError("Not logged in.")
    return agent_id


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
    return await _run(memoria.save_memory(_agent(ctx), note, reason, topic, confidence,
                                          visibility, shared_with))


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
    return await _run(memoria.find_memory(_agent(ctx), query, topic, saved_by, limit))


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
    return await _run(memoria.update_memory(_agent(ctx), memory_id, new_note, reason,
                                            confidence, topic))


@mcp.tool()
async def share_memory(memory_id: str, visibility: str, ctx: Context,
                       shared_with: list[str] | None = None) -> dict:
    """Change who can read a memory. Only the agent that wrote the memory can do this.

    Args:
        memory_id: The memory's ID (like "mem-1a2b3c4d").
        visibility: "private" (only the writer) or "shared" (the writer plus shared_with).
        shared_with: For shared: the full list of agent IDs allowed to read it. It replaces the old list.
    """
    return await _run(memoria.share_memory(_agent(ctx), memory_id, visibility, shared_with))


def _already_running() -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex((HOST, PORT)) == 0


if __name__ == "__main__":
    if _already_running():
        print("Memoria is already running. Nothing to do.", file=sys.stderr)
        sys.exit(0)
    print(f"Memoria is running at http://{HOST}:{PORT}/mcp  (close this window to stop it)",
          file=sys.stderr)
    import uvicorn
    uvicorn.run(LoginCheck(mcp.streamable_http_app(host=HOST)), host=HOST, port=PORT,
                log_level="warning")
