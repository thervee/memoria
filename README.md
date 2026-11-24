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
3. **Manage memories in your browser** at **http://127.0.0.1:8765/dashboard**. See below.

## Dashboard
Open **http://127.0.0.1:8765/dashboard** in your browser (type `http://`, not `https://`).
It opens straight away, with no password, and runs whenever Memoria runs.
- **Only this laptop can open it**, and other websites can't make changes behind your back:
  changes are only accepted from the dashboard page itself.
- **No password means** anyone using this laptop (and any program running on it) can open the
  dashboard and see every memory, including agents' private ones.
- **See everything:** every memory with its topic, note, writer, date, confidence, verified status and sharing.
- **Search and filter** by any words, agent, topic or verified status.
- **Click a memory** to see its details, signature and every version in its history.
- **Change sharing** (private, or shared with chosen agents) and **delete** memories (after an
  "are you sure?" step). Both are done *as the agent that wrote the memory*, using its own key, the
  same rule agents follow, and the activity log marks them `"via": "dashboard"`.

## The tools
| Tool | What it does |
|---|---|
| `save_memory` | Saves a note or decision, with its reason, topic and confidence (low/medium/high) |
| `find_memory` | Searches memories by question or topic (can also filter by which agent saved them) |
| `list_memories` | Lists the memories you can read (your own and ones shared with you), newest first; can filter by topic (including everything under it), who saved it and how many (default 20, max 100) |
| `update_memory` | Replaces a memory's note with new information; the old version is kept in history |
| `share_memory` | Changes who can read a memory (only the agent that wrote it can do this) |
| `delete_memory` | Deletes a memory for good, with its history (only the agent that wrote it can do this; agents it's shared with can't) |
| `record_outcome` | Records whether a memory helped: `success`, `failure` or `mixed`, with an optional note (up to 500 characters). Any agent that can read the memory can record one; recording again replaces your earlier one. Signed, and free |
| `get_outcomes` | Every outcome recorded for a memory, newest first, each with its signature check |

Every memory is labelled with: the agent ID that wrote it, when it was saved or updated, its topic and its confidence.

**Outcomes** show whether a memory actually helped. `find_memory` and `list_memories` show an outcome
summary for every memory (how many successes, failures and mixed results, the latest one, and
whether their signatures checked out), rank memories that helped a little higher (at most 2 places),
and take an `outcome` filter (`success`, `failure`, `mixed` or `none`) for the latest outcome.
For example, an agent saves "Token X looked safe"; later another agent it's shared with calls
`record_outcome(memory_id, "failure", note="rugged")`, so everyone who finds that memory sees it failed.
Deleting a memory deletes its outcomes. The demo keys can't use these tools.

## Paying with $MEMORIA
Memoria can be paid for with the $MEMORIA token on Robinhood Chain (see `billing.py`). It is **off**
until `MEMORIA_BILLING=on`; with it off, nothing changes and the payment tools aren't offered.

- **Price:** 1 credit = 10,000 $MEMORIA = 2 actions. Balances are kept in actions.
- **What costs 1 action:** `save_memory`, `update_memory`, `find_memory`, `list_memories` and `get_outcomes`.
  **Free:** `record_outcome`, `share_memory`, `delete_memory`, `get_balance`, `wallet_challenge`,
  `link_wallet` and `claim_payment`. A failed action is given back. With 4 actions or fewer left,
  every tool's result carries a short "balance low" note with the pay page link.
- **Starter credits:** every account gets 10 credits (20 actions) once, including accounts made
  before billing was turned on. This laptop's "local" account and the public demo are never charged.
- **Treasury** (receives payments): `0x7d61Bd6D005B464C9022e1b2FAdD0F16C93E935C`.
  Memoria only reads the chain; it never holds or asks for a private key.

| Tool (only when billing is on) | What it does |
|---|---|
| `wallet_challenge` | A one-time message (10 minutes) for a wallet to sign |
| `link_wallet` | Links the wallet that signed it (EIP-191 `personal_sign`); one account per wallet |
| `claim_payment` | Adds credits for a $MEMORIA transfer from a linked wallet to the treasury (after 20 confirmations; once per payment) |
| `get_balance` | Actions left (also as credits), linked wallets and how to pay |

**How an agent pays:** call `wallet_challenge`, sign the message with its wallet, call `link_wallet`,
send $MEMORIA from that wallet to the treasury, then call `claim_payment` with the transaction hash.
**People** pay on the website's pay page, `https://www.memoriaagent.site/pay`, which does the same steps
with a browser wallet. The pay page uses `/billing/config`, `/billing/challenge`, `/billing/link`,
`/billing/claim` and `/billing/balance` (API key required, website only). An account's purchases can be
made public at `/purchases/<account>` with `manage_accounts.py public-purchases <account> on`.

Settings (Railway Variables): `MEMORIA_BILLING`, `MEMORIA_TREASURY_ADDRESS`, `MEMORIA_TOKEN_ADDRESS`,
`MEMORIA_CHAIN_ID`, `MEMORIA_RPC_URL`, `MEMORIA_TOKENS_PER_CREDIT`, `MEMORIA_ACTIONS_PER_CREDIT`,
`MEMORIA_STARTER_CREDITS`, `MEMORIA_MIN_CONFIRMATIONS` (defaults and meanings in `.env.example`).
Host commands: `manage_accounts.py balance <account>`, `add-credits` / `remove-credits <account> <credits>`,
and `link-wallet` / `unlink-wallet <account> <address>`: wallets that can't sign messages can ask
Memoria to link them by hand. `link-wallet` first gives a small, exact test amount (1 to 2 $MEMORIA)
for the wallet to send to the treasury; `link-wallet <account> <address> --tx <hash>` then links it
only if the chain shows that payment (recorded with agent "admin" and logged).

Payments count in whole credits; leftover $MEMORIA is kept per account and added to its next payment.
Transfers to the treasury from before billing started (`MEMORIA_CLAIM_START_BLOCK`, by default
block 121276863, that of the first payment) can never be claimed. People who paid but didn't get credits can use
"Claim a past payment" on the pay page.

## Saving is instant; processing happens in the background
Saving, updating and deleting answer straight away. The slow part (Gemini reading the memory and
Cognee adding it to its knowledge graph, which can take minutes on the free tier) is done afterwards by
a background worker, one job at a time. Memories that arrive together are processed in one run.
- While a memory is waiting it shows **processing: true** (⏳ Processing in the dashboard).
  `find_memory` still finds it, by the words in the question rather than by meaning.
- If processing fails (for example Gemini is busy), the worker waits and tries again, waiting longer
  each time (up to 10 minutes). Failures are written to `memory_store\activity.log` as `background_error`.
- If Memoria stops halfway, nothing is lost: the worker finishes the work the next time Memoria starts.

## Agent IDs and keys
Every agent has an ID (lowercase letters, numbers and dashes, e.g. `researcher`) and its own pair of
Ed25519 keys:
- a **secret key** in `keys/<agent>.key`. It's encrypted by Windows for your account only, the `keys`
  folder can only be opened by your Windows account, and it's never uploaded to GitHub.
- a **public key** in `keys/public_keys.json`. It's safe to share and lets anyone check that agent's signatures.

**Connect a new agent:** double-click `connect_agent.bat` and type its ID. It registers the agent
(if it isn't already) and prints the setup to copy, for three kinds of agent:
1. **Claude Code:** one `claude mcp add ...` command to paste into PowerShell.
2. **Any other MCP app** (Claude Desktop, Cursor, VS Code, ...): a few lines for the app's MCP settings.
   The app runs `memoria_bridge.py`, which passes everything on to Memoria as that agent.
3. **Your own Python agent:** a short example using `memoria_client.py`
   (`async with Memoria("trader") as mem: await mem.find(...)`).

In all three, login passes are made and renewed automatically: nothing to copy every hour.
(`register_agent.bat` still works if you only want to create an agent's keys.)
Registered so far: `claude-code`, `researcher`, `trader`, `outsider`.

**Agents must prove who they are.** Every request needs `Authorization: Bearer <login pass>`, where
the pass is made with the agent's secret key and is valid for 1 hour. `make_token.py <agent-id>` prints
one. Claude Code runs it automatically when it connects, and the bridge and Python helper make their
own. Requests without a valid pass get "401" and
are refused, and an agent can't claim another agent's ID.

## Sharing
Every memory is either:
- **private**: only the agent that wrote it can find or update it (this is the default), or
- **shared**: the writer plus the agent IDs listed in `shared_with`, e.g. `["trader"]`.

Agents only ever see memories they're allowed to read. Anything else acts as if it doesn't exist.

## Signatures
Every saved or updated memory is signed with the secret key of the agent that wrote that version.
The signature and the agent's public key are stored with the memory. `find_memory` re-checks every
signature and shows **verified** or **NOT verified** with a reason, for example:
- `verified: signed by researcher`
- `NOT verified: the memory was changed after it was signed`
- `NOT verified: no signature (saved before signing was added)`

The signature covers the memory's content and authorship labels (ID, topic, confidence, note, reason,
who wrote it and when). It does not cover sharing settings, so changing who can read a memory doesn't
affect it.

**Limit to know:** Memoria signs on the agent's behalf, after the agent has proven who it is with its key.
So a signature proves "an agent holding this key saved it, and it hasn't changed since". It does not
protect against someone who already controls Memoria and your Windows account on this laptop.

## Files
| File / folder | What it is |
|---|---|
| `.env` | Settings and your Gemini API key. **Keep private.** |
| `setup_cognee.py` | Points Cognee at local storage |
| `memoria.py` | The memory engine (save / find / update) and the background worker |
| `db.py` | Memoria's database: where memories, agents and accounts are kept |
| `billing.py` | Paying with $MEMORIA: balances, wallet linking, claiming payments, the pay page's endpoints |
| `paths.py` | Where data is kept (`MEMORIA_DATA_DIR`) and whether Memoria runs online (`MEMORIA_ONLINE`) |
| `Dockerfile` / `.dockerignore` / `.railway/railway.ts` | Everything Railway needs to build and run Memoria |
| `requirements.txt` | Every package Memoria needs, with exact versions |
| `requirements-dev.txt` | The extra package needed to run the tests (pytest) |
| `tests/` | The automatic tests (`pytest.ini` holds their settings) |
| `server.py` | The shared MCP server that gives agents the memory tools (checks every login) |
| `identity.py` | Agent keys, login passes and signatures |
| `register_agent.bat` / `register_agent.py` | Register a new agent (creates its keys) |
| `make_token.py` | Makes a login pass for an agent (Claude Code runs it automatically) |
| `connect_agent.bat` / `connect_agent.py` | Connect a new agent: registers it and prints the setup to paste |
| `memoria_bridge.py` | Lets any MCP app use Memoria as a command, with login passes handled for it |
| `memoria_client.py` | Use Memoria from your own Python agents, with login passes handled for you |
| `accounts.py` | Developer accounts, API keys, invite codes and per-account limits (for hosting) |
| `manage_accounts.py` | The host's tool for creating accounts and API keys |
| `limits.py` | Spam protection: requests per minute, wrong-key lock-outs, request size |
| `signup.py` | Optional developer sign-up at `/signup` (off unless turned on) |
| `dashboard.py` | The dashboard's server side (list, history, sharing, delete, and its safety checks) |
| `dashboard/` | The dashboard webpage (`index.html`, `app.css`, `app.js`) |
| `keys/` | **Agents' secret keys** and the public key list. Never shared or uploaded. |
| `start_memoria.bat` | Double-click to start Memoria |
| `start_memoria_hidden.vbs` | Starts Memoria with no window (used at Windows login) |
| `keep_memoria_running.py` | The watchdog that restarts Memoria if it crashes |
| `stop_memoria.bat` | Double-click to stop Memoria (and its watchdog) |
| `memory_store/` | **All your memories.** Backed up automatically every day (see Backups). |
| `backups/` | The automatic backups (private: never uploaded to GitHub) |
| `backup.py` | Makes the daily backups and deletes the oldest ones |
| `restore_backup.bat` / `restore_backup.py` | Double-click to put a backup back (Memoria must be stopped) |
| `memory_store/memoria.db` | **Memoria's database** (see Database): memories, agents, accounts, API keys |
| `memory_store/*.json.imported` | The older JSON files, kept after they were moved into the database |
| `memory_store/activity.log` | Who saved, searched or updated what, and when (plus any background processing errors) |

## Backups
Memoria backs up `memory_store` by itself: when it starts (if the newest backup is more than a day
old) and then once a day while it runs. Each backup is one zip file in the `backups` folder, e.g.
`memoria-backup-2026-11-16_104640.zip`, and the newest 14 are kept.
- **What's in it:** your memories with their history and signatures, the agent list, the activity log
  and Cognee's databases. Left out, because they can be re-created: the downloaded search model
  (it downloads again if needed), logs and lock files.
- **Safe copies:** for the few seconds a backup takes, Memoria holds new saves and pauses the
  background worker, so the copy is never caught halfway through a change. Searches carry on.
- **Each backup is checked** after it's written. Backups and any failures are written to
  `memory_store\activity.log` (`backup` / `backup_error`); a failed backup is tried again an hour later.
- **Keep them somewhere else too:** the `backups` folder is on the same disk as Memoria, so it protects
  against mistakes and damaged files, not a broken disk. To keep backups on a USB drive or another
  disk, add `MEMORIA_BACKUP_DIR=E:\MemoriaBackups` (your own folder) to `.env` and restart Memoria.
  `MEMORIA_BACKUP_KEEP=30` keeps more of them. Backups hold your private memories, so only choose a
  folder you trust (a cloud-synced folder uploads them to that cloud).

**To restore a backup:**
1. Double-click `stop_memoria.bat`.
2. Double-click `restore_backup.bat`, and type the number of the backup you want.
3. Double-click `start_memoria.bat`.

Nothing is thrown away: your current `memory_store` is renamed to `memory_store.old-<date>` and
kept. Delete that folder once you're happy with the restore.

**Note:** backups cover `memory_store` only. Agents' keys live in the `keys` folder, which isn't in
the backups. Without it, agents can't log in, and memories show NOT verified.

## Hosting online (prepared, not deployed yet)
Memoria is ready to serve other developers, each with their own account. **Nothing is online yet:**
everything below is off or unused until you set it up on a server. On this laptop, nothing changes:
your agents are in the built-in account `local` and log in with login passes as before.

**Developer accounts and API keys** (`accounts.py`, `manage_accounts.py`)
- Each developer has an **account** (e.g. `acme`) with up to 10 **agents** (e.g. `trader`).
- Each **API key** belongs to one agent of one account. Agents send it as
  `Authorization: Bearer mem_...`. A key can't act as another agent or see another account.
- Only a hash of each key is stored (in Memoria's database), so a copy of it can't be used to log
  in. A key is shown once, when it's made.
- Developers' agents still sign every memory. Their signing keys live in `keys\<account>\` and are
  locked with a **server secret** (`MEMORIA_SECRET_KEY` in `.env`), because servers usually don't run
  Windows. Back the secret up privately: without it those agents can't sign any more.

**Each developer's memories are kept apart**
- Agents only ever see memories of their own account. Sharing (`shared_with`) only reaches agents of the
  same account: acme's `analyst` and globex's `analyst` are different agents.
- Each account has its **own Cognee dataset** (`memoria-acme`), so a developer's searches never even
  look at anyone else's memories.
- The account is part of each memory's signature, so a memory moved to another account in the file
  shows NOT verified.

**Spam protection** (`limits.py`, and limits per account)
| What | Default | Change it |
|---|---|---|
| Requests per minute, per agent | 60 | `set-limit <account> requests_per_minute <n>` |
| Requests per minute, per account (all its agents together) | 120 | `set-limit <account> account_requests_per_minute <n>` |
| Saves + updates per day, per account (each costs AI processing) | 200 | `set-limit <account> writes_per_day <n>` |
| Memories kept, per account | 2,000 | `set-limit <account> max_memories <n>` |
| Agents per account | 10 | `set-limit <account> max_agents <n>` |
| Requests per minute, per internet address | 120 | `MEMORIA_IP_LIMIT` in `.env` |
| Wrong API keys per address before a 10-minute lock-out | 10 | `MEMORIA_FAILED_LOGIN_LIMIT` |
| Size of one request | 64 KB | `MEMORIA_MAX_REQUEST_BYTES` |
| Length of a note / reason / topic / question | 4,000 / 2,000 / 100 / 1,000 characters | (fixed) |

Going over a limit gets a clear message (and `429 Too Many Requests` with a `Retry-After` time for the
per-minute limits). This laptop's own agents have no daily or per-minute limits.

**Sign-up** (`signup.py`): off unless you turn it on with `MEMORIA_SIGNUP` in `.env`:
- `off` (default): you create accounts yourself with `manage_accounts.py`.
- `invite`: developers sign up with a one-time code you make with `manage_accounts.py invite`.
- `open`: anyone can sign up.

Developers send `POST /signup` with `{"account": "acme", "email": "dev@acme.example"}` (plus `"invite"`
if needed) and get an API key back. Each address can try 3 times an hour, and at most 50 accounts are
made per day in total.

**Managing accounts** (run on the server): `python manage_accounts.py <command>`
- `make-secret`: make the server secret for `.env`, once, before anything else.
- `create acme --email dev@acme.example`, then `new-key acme trader`: make an account and an API key.
- `list`, `revoke-key <key-id>`, `disable acme` / `enable acme`, `set-limit ...`, `invite`.

**Connecting a developer's agent:** in an MCP app, run `memoria_bridge.py` with the environment variables
`MEMORIA_API_KEY=mem_...` and `MEMORIA_URL=https://<your address>/mcp`. In Python, use
`Memoria(api_key="mem_...", url="https://<your address>/mcp")`.

**The dashboard stays private.** It shows every account's memories, so it only opens on the computer
Memoria runs on. It refuses anything that came through a proxy, and you can turn it off with
`MEMORIA_DASHBOARD=off`.

**Still to do before going online:**
1. Deploy on Railway (see "Deploying on Railway" below). Railway adds HTTPS, and Memoria is ready for it.
2. Check the Gemini plan: every save costs AI processing, and the free tier won't cover many developers.
3. Optional: email checks at sign-up, a page where developers manage their own keys, and terms of use
   and a privacy notice (you'd be storing other people's data).

## Deploying on Railway (ready, not deployed)
Everything Railway needs is in the repository: `Dockerfile`, `.dockerignore` and `.railway/railway.ts`
(Railway's infrastructure-as-code file: build from the Dockerfile, the `/health` check with a 300-second
timeout, restart on failure up to 10 times, and the volume at `/data`; it never holds variables or
secrets). To change these settings, edit that file, then run `npm install` once, `railway config plan`
to preview and `railway config apply` to put them in place.
On this laptop nothing changes: none of it is used here.

**What the Docker image does**
- It holds **only code**. The Dockerfile copies the server's Python files one by one, and `.dockerignore`
  shuts everything else out, so `.env`, `keys`, `memory_store` and `backups` can never get into it.
- It runs Memoria **online** (`MEMORIA_ONLINE=1`): listening on `0.0.0.0` and Railway's `PORT`, the
  dashboard off, and visitors' real addresses read from Railway's proxy for the spam limits.
- It keeps **all data in `/data`** (`MEMORIA_DATA_DIR=/data`), where you mount a Railway volume:
  `/data/memory_store` (database, Cognee's data, logs), `/data/keys` (agents' locked signing keys) and
  `/data/backups` (the daily backups).
- It holds the settings that aren't secret (Gemini model, search model, and so on). **Secrets are not in
  it**: you add them in Railway.
- **`/health`** answers `{"status": "ok"}` when Memoria and its database are up. Railway checks it on
  every deploy.
- **It refuses to start** if a secret is missing, if no volume is attached, or if `/data` isn't on the
  volume, and says why in the deploy logs, so it never runs half set up and loses data.

**What to set in Railway** (once)
1. New project, then deploy this GitHub repository. Railway builds the Dockerfile; run
   `railway config apply` once to give it the settings in `.railway/railway.ts`.
2. Add a **volume** to the service, mounted at **`/data`**.
3. Add two **variables**:
   - `LLM_API_KEY`: your Gemini API key.
   - `MEMORIA_SECRET_KEY`: run `.venv\Scripts\python.exe manage_accounts.py make-secret` on this laptop
     and copy the value. Keep a private copy: without it, developers' agents can't sign any more.
4. In the service's settings, under Networking, **generate a domain**. Agents connect to
   `https://<that domain>/mcp`.
5. Optional variables: `MEMORIA_SIGNUP` (`invite` or `open`), and any spam-limit settings from
   `.env.example`.

**After deploying**
- Create developer accounts **on the server**: open a shell into the service (`railway ssh` with the
  Railway command-line tool), then run `python manage_accounts.py create acme` and
  `python manage_accounts.py new-key acme trader`.
- Backups are made daily in `/data/backups`, on the same volume. Download one now and then to keep a
  copy somewhere else.
- Keep the service at **one replica**: the database is a file on the volume.

## Database
Memoria keeps its own records in one database file, `memory_store\memoria.db` (SQLite: a full
database in a single file, built into Python, with nothing to install or run).
- **What's in it:** every memory with its labels, signature, sharing and history; the agents; developer
  accounts, hashed API keys and invite codes; each account's usage today; and deleted memories still
  waiting to be removed from Cognee. The activity log stays a plain text file (`activity.log`).
- **Why:** finding one memory or one API key no longer means reading every memory, so it stays quick
  with many developers and thousands of memories. It's also safe when two programs use it at once
  (Memoria and `manage_accounts.py`), and every change either happens completely or not at all.
- **Your older files were moved in automatically** the first time Memoria started with the database.
  The originals are kept as `memories.json.imported` (and so on) and are never read again. Delete them
  whenever you like.
- **Backups copy it safely:** SQLite's own backup feature makes the copy, so it's complete and
  consistent even while Memoria is using the database. Restoring works the same as before.
- **Later, if needed:** a very busy service could move to a database server (e.g. Postgres). All of
  Memoria's database code is in `db.py` and `accounts.py`, so only those two files would change.
- **Don't edit it by hand while Memoria runs.** Use the dashboard, the agents' tools or
  `manage_accounts.py`. To look inside, any SQLite viewer works (e.g. "DB Browser for SQLite").

## Tests
220 automatic tests check the rules that matter most: login passes, signatures, privacy and sharing,
who may delete, connecting agents (the bridge and Python helper), the background worker (including
retries after failures), backups and restoring them, the dashboard's safety checks, and hosting:
developer accounts and API keys, keeping each developer's memories apart, spam protection and sign-up,
and the database: importing the older files, surviving restarts, two programs at once, using indexes
instead of reading everything, and backups never catching a change halfway; and running online:
this laptop's settings unchanged, the data folder, the start-up checks, `/health`, what goes into the
Docker image and what Git ignores, plus a real run in online mode.
They take about 25 seconds, use a pretend Cognee (so no Gemini calls), and each one runs in its own
temporary folder, so they never touch your real memories or keys.

Run them (in PowerShell, in the Memoria folder):
1. Once: `.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`
2. Each time: `.venv\Scripts\python.exe -m pytest`

## Setup used
- AI model: Google Gemini `gemini-3.5-flash-lite` (free tier). It processes memories into Cognee's knowledge graph.
- Search model: `BAAI/bge-small-en-v1.5`, running locally for free.

## Useful commands (type in PowerShell)
- Check the connection: `claude mcp get memoria`
- Disconnect from Claude Code: `claude mcp remove memoria -s user`
- Connect another agent: `.venv\Scripts\python.exe connect_agent.py <its-id>` (or double-click
  `connect_agent.bat`) and paste the setup it prints.
- For an app that can only send a fixed header: connect it to `http://127.0.0.1:8765/mcp` with the header
  printed by `.venv\Scripts\python.exe make_token.py <its-id>` (make a new one at least every hour)
