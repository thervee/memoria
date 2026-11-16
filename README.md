# Memoria

A memory system for AI agents. Agents save what they learn, find it again later, and update it.
It's built on [Cognee](https://github.com/topoteretes/cognee) and runs on this laptop.

## How to use it
1. **Memoria starts by itself** when you log in to Windows, hidden in the background.
   A watchdog keeps it running: if Memoria crashes, it is restarted within seconds
   (waiting longer, up to 1 minute, if it keeps crashing).
   One Memoria runs on this laptop and every agent connects to it at `http://127.0.0.1:8765/mcp`
   (only this computer can reach that address).
   - To stop it: double-click `stop_memoria.bat`.
   - To start it again by hand: double-click `start_memoria.bat` (it tells you when Memoria is ready).
   - If something seems wrong, look in `memory_store\server.log`.
   - To turn off automatic start: press Windows key + R, type `shell:startup`, press Enter, and delete `Memoria`.
2. In any Claude Code session, just ask, e.g. "Check Memoria for what we know about Chainlink"
   or "Save this decision to Memoria".

## The tools
| Tool | What it does |
|---|---|
| `save_memory` | Saves a note or decision, with its reason, topic and confidence (low/medium/high) |
| `find_memory` | Searches memories by question or topic (can also filter by which agent saved them) |
| `update_memory` | Replaces a memory's note with new information; the old version is kept in history |
| `share_memory` | Changes who can read a memory (only the agent that wrote it can do this) |

Every memory is labelled with: the agent ID that wrote it, when it was saved or updated, its topic and its confidence.

## Agent IDs
Each agent connects with the header `X-Memoria-Agent: <its-id>` (Claude Code's is `claude-code`).
IDs are lowercase letters, numbers and dashes, e.g. `researcher`. Connections without an ID are refused.

## Sharing
Every memory is either:
- **private**: only the agent that wrote it can find or update it (this is the default), or
- **shared**: the writer plus the agent IDs listed in `shared_with`, e.g. `["trader"]`.

Agents only ever see memories they're allowed to read. Anything else acts as if it doesn't exist.

**Important limit:** agent IDs are taken on trust. Memoria believes whatever ID a connection
claims, so this keeps agents' memories tidy and separate, but it isn't a security lock against
a program that deliberately pretends to be another agent.

## Files
| File / folder | What it is |
|---|---|
| `.env` | Settings and your Gemini API key. **Keep private.** |
| `setup_cognee.py` | Points Cognee at local storage |
| `memoria.py` | The memory engine (save / find / update) |
| `server.py` | The shared MCP server that gives agents the memory tools |
| `start_memoria.bat` | Double-click to start Memoria |
| `start_memoria_hidden.vbs` | Starts Memoria with no window (used at Windows login) |
| `keep_memoria_running.py` | The watchdog that restarts Memoria if it crashes |
| `stop_memoria.bat` | Double-click to stop Memoria (and its watchdog) |
| `memory_store/` | **All your memories.** Back up this folder to keep them safe. |
| `memory_store/memories.json` | Readable list of every memory, its labels and its history |
| `memory_store/agents.json` | Every agent ID that has used Memoria |
| `memory_store/activity.log` | Who saved, searched or updated what, and when |

## Setup used
- AI model: Google Gemini `gemini-3.5-flash-lite` (free tier). It processes memories into Cognee's knowledge graph.
- Search model: `BAAI/bge-small-en-v1.5`, running locally for free.

## Useful commands (type in PowerShell)
- Check the connection: `claude mcp get memoria`
- Disconnect from Claude Code: `claude mcp remove memoria -s user`
- Connect another agent: give it the address `http://127.0.0.1:8765/mcp` and the header `X-Memoria-Agent: <its-id>`
