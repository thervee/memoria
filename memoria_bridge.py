"""Memoria bridge: lets any MCP app use Memoria by running one command.

Many AI apps (Claude Desktop, Cursor, VS Code, Codex, Gemini CLI, ...) can start an
MCP server as a command. This is that command. It passes every request on to the
Memoria running on this laptop, logged in as the agent you name, and makes fresh
login passes by itself, so nothing ever expires.

    .venv\\Scripts\\python.exe memoria_bridge.py <agent-id>

connect_agent.bat prints the exact setup to paste into each kind of app.
Messages go to stderr; stdout is reserved for the app.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import anyio  # noqa: E402
from mcp import types  # noqa: E402
from mcp.server.lowlevel import Server  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402

import identity  # noqa: E402
import memoria_client  # noqa: E402

FALLBACK_INSTRUCTIONS = ("Memoria is your long-term memory. Use find_memory before starting "
                         "work and save_memory for findings and decisions, with the reason.")


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)],
                                is_error=True)


def make_handlers(agent_id: str, url: str = memoria_client.URL):
    """The bridge's two jobs: list Memoria's tools, and pass a tool call on to Memoria."""
    auth = memoria_client.FreshPass(agent_id)  # one pass, renewed in time, for all requests

    # Each request opens its own short connection to Memoria, so the bridge keeps
    # working when Memoria is restarted.
    async def on_list_tools(ctx, params):
        try:
            async with memoria_client.session(agent_id, url, auth) as s:
                return await s.list_tools()
        except memoria_client.MemoriaNotRunning:
            return types.ListToolsResult(tools=[])

    async def on_call_tool(ctx, params):
        try:
            async with memoria_client.session(agent_id, url, auth) as s:
                return await s.call_tool(params.name, params.arguments or {})
        except memoria_client.MemoriaNotRunning as e:
            return _error(str(e))

    return on_list_tools, on_call_tool


def make_bridge(agent_id: str, url: str = memoria_client.URL,
                instructions: str = FALLBACK_INSTRUCTIONS) -> Server:
    on_list_tools, on_call_tool = make_handlers(agent_id, url)
    return Server("memoria", instructions=instructions,
                  on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def _instructions(agent_id: str) -> str:
    """Memoria's own instructions for agents, if it's running; otherwise a short version."""
    try:
        async with memoria_client.session(agent_id) as s:
            return s.initialize_result.instructions or FALLBACK_INSTRUCTIONS
    except Exception:
        return FALLBACK_INSTRUCTIONS


async def main(agent_id: str) -> None:
    identity.make_token(agent_id)  # stop now, with a clear message, if not registered
    bridge = make_bridge(agent_id, instructions=await _instructions(agent_id))
    print(f"Memoria bridge running as '{agent_id}'.", file=sys.stderr)
    async with stdio_server() as (read, write):
        await bridge.run(read, write, bridge.create_initialization_options())


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: memoria_bridge.py <agent-id>")
    try:
        anyio.run(main, sys.argv[1].strip().lower())
    except identity.IdentityError as e:
        sys.exit(f"Memoria bridge can't start: {e} Register it with connect_agent.bat.")
