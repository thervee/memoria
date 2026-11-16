"""Connect a new agent to Memoria: registers it if needed, then prints the setup to paste.

Usage:  .venv\\Scripts\\python.exe connect_agent.py <agent-id>
Or double-click connect_agent.bat and type the ID.
"""
import json
import sys
from pathlib import Path

import identity

HERE = Path(__file__).resolve().parent
PYTHON = HERE / ".venv" / "Scripts" / "python.exe"
BRIDGE = HERE / "memoria_bridge.py"


def setup_text(agent_id: str) -> str:
    app_config = {"mcpServers": {"memoria": {"command": str(PYTHON),
                                             "args": [str(BRIDGE), agent_id]}}}
    return f"""
=== Option 1: Claude Code ===
Paste this into PowerShell:

  claude mcp add memoria-{agent_id} -s user -- "{PYTHON}" "{BRIDGE}" {agent_id}

=== Option 2: any other MCP app (Claude Desktop, Cursor, VS Code, ...) ===
Add this to the app's MCP settings file (merge it with any servers already there).
VS Code calls the section "servers" instead of "mcpServers".

{json.dumps(app_config, indent=2)}

=== Option 3: your own Python agent ===
Run it with {PYTHON}

  import asyncio, sys
  sys.path.insert(0, r"{HERE}")
  from memoria_client import Memoria

  async def main():
      async with Memoria("{agent_id}") as mem:
          print(await mem.find("What do we know about this topic?"))

  asyncio.run(main())

In every option, login passes are made and renewed automatically.
Memoria must be running (start_memoria.bat) for the agent to use it.
"""


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: connect_agent.py <agent-id>")
    agent_id = sys.argv[1].strip().lower()
    try:
        if identity.public_key_of(agent_id):
            print(f"'{agent_id}' is already registered.")
        else:
            identity.register_agent(agent_id)
            print(f"Registered '{agent_id}'. Its secret key is saved, encrypted, in the keys folder.")
        identity.make_token(agent_id)  # check its secret key works on this laptop
    except identity.IdentityError as e:
        sys.exit(f"Can't connect '{agent_id}': {e}")
    print(setup_text(agent_id))


if __name__ == "__main__":
    main()
