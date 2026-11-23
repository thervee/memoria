"""Paying for Memoria with the $MEMORIA token. OFF unless MEMORIA_BILLING=on.

How it works:
  - Each developer account has a balance of ACTIONS. 1 credit = MEMORIA_ACTIONS_PER_CREDIT
    actions (default 2), and costs MEMORIA_TOKENS_PER_CREDIT $MEMORIA (default 10,000).
    Balances are kept in actions, so half-credits are never needed.
  - With billing on, save_memory, update_memory, find_memory, list_memories and
    get_outcomes each use 1 action; record_outcome, share_memory, delete_memory,
    get_balance, wallet_challenge, link_wallet and claim_payment are free. The action is
    taken before the work and given back if the work fails. The "local" account (this
    laptop) and the public demo account are never charged.
  - When an account has LOW_BALANCE actions or fewer left, every tool's result carries a
    short "balance_note" saying so, with the pay page link.
  - New accounts get MEMORIA_STARTER_CREDITS credits (default 10) once. Accounts that
    existed before billing was turned on get them once, the first time billing is on.
  - To pay, an account links a wallet (wallet_challenge, then link_wallet with the
    wallet's EIP-191 signature of that one-time message), sends $MEMORIA from it to the
    treasury wallet, and calls claim_payment with the transaction hash. Memoria reads the
    transaction from Robinhood Chain and adds the credits. Each transfer can be claimed
    only once (the payments table refuses a second claim, even two at the same moment).

The server only READS the chain (plain JSON-RPC). It never creates, asks for or stores a
private key or seed phrase.

Settings (environment variables, e.g. Railway Variables):
  MEMORIA_BILLING            "off" (default) or "on"
  MEMORIA_TREASURY_ADDRESS   wallet that receives payments (default below). If billing is on
                             but this isn't a valid address, billing stays off (with a warning)
  MEMORIA_TOKEN_ADDRESS      the $MEMORIA contract (default below)
  MEMORIA_CHAIN_ID           4663 (Robinhood Chain)
  MEMORIA_RPC_URL            the chain's JSON-RPC address (default: the public one, which is
                             rate-limited; an Alchemy URL can be used instead)
  MEMORIA_TOKENS_PER_CREDIT  10000
  MEMORIA_ACTIONS_PER_CREDIT 2
  MEMORIA_STARTER_CREDITS    10
  MEMORIA_MIN_CONFIRMATIONS  20

This file also serves the pay page's HTTP endpoints (see app() at the end) and the public
purchase history at /purchases/<account>.
"""
import asyncio
import json
import os
import re
import secrets
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import accounts
import db
import demo
import identity
import limits
import memoria
import signup

TOKEN_ADDRESS = "0xc84b411cae3b86eec20e9699256be7c43b17bd35"
TREASURY_ADDRESS = "0x7d61Bd6D005B464C9022e1b2FAdD0F16C93E935C"
CHAIN_ID = 4663
PUBLIC_RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
EXPLORER_URL = "https://robinhoodchain.blockscout.com"
DECIMALS = 18
# keccak256("Transfer(address,address,uint256)"): the ERC-20 Transfer event
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
SIGNATURE = re.compile(r"^(0x)?[0-9a-fA-F]{130}$")
CHALLENGE_MINUTES = 10
NEVER_CHARGED = {identity.LOCAL, demo.ACCOUNT}
CHARGED_ACTIONS = ("save_memory", "update_memory", "find_memory", "list_memories",
                   "get_outcomes")
FREE_ACTIONS = ("record_outcome", "share_memory", "delete_memory", "get_balance",
                "wallet_challenge", "link_wallet", "claim_payment")
LOW_BALANCE = 4  # at or below this many actions, results carry a "balance low" note


class BillingError(ValueError):
    """Something about a payment or balance, explained in plain words."""


# ---------- settings ----------

def _number(name: str, default: int, least: int = 1) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        _warn(f"{name} isn't a whole number; using {default}.")
        return default
    return value if value >= least else default


def settings() -> dict:
    return {
        "token": (os.environ.get("MEMORIA_TOKEN_ADDRESS", "").strip() or TOKEN_ADDRESS).lower(),
        "treasury": (os.environ.get("MEMORIA_TREASURY_ADDRESS", "").strip()
                     or TREASURY_ADDRESS).lower(),
        "chain_id": _number("MEMORIA_CHAIN_ID", CHAIN_ID),
        "rpc_url": os.environ.get("MEMORIA_RPC_URL", "").strip() or PUBLIC_RPC_URL,
        "tokens_per_credit": _number("MEMORIA_TOKENS_PER_CREDIT", 10_000),
        "actions_per_credit": _number("MEMORIA_ACTIONS_PER_CREDIT", 2),
        "starter_credits": _number("MEMORIA_STARTER_CREDITS", 10, least=0),
        "min_confirmations": _number("MEMORIA_MIN_CONFIRMATIONS", 20),
    }


_warned: set[str] = set()


def _warn(message: str) -> None:
    if message not in _warned:
        _warned.add(message)
        print(f"Memoria billing: {message}", file=sys.stderr, flush=True)


def enabled() -> bool:
    """True when billing is on and set up correctly."""
    if os.environ.get("MEMORIA_BILLING", "off").strip().lower() != "on":
        return False
    s = settings()
    if not ADDRESS.match(s["treasury"]):
        _warn("MEMORIA_BILLING is on, but MEMORIA_TREASURY_ADDRESS isn't a valid wallet "
              "address (0x and 40 hex characters), so billing stays OFF.")
        return False
    if not ADDRESS.match(s["token"]):
        _warn("MEMORIA_BILLING is on, but MEMORIA_TOKEN_ADDRESS isn't a valid contract "
              "address, so billing stays OFF.")
        return False
    return True


def is_billed(account: str) -> bool:
    """Is this account charged for actions right now?"""
    return account not in NEVER_CHARGED and enabled()


def pay_page() -> str:
    return f"{signup._site_url()}/pay"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _tokens(units: int) -> str:
    """Token units (18 decimals) as a plain number, e.g. 10000 or 2.5."""
    text = format(Decimal(units) / (Decimal(10) ** DECIMALS), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _log(account: str, agent: str, action: str, **details) -> None:
    memoria._log(identity.principal(account, agent), action, **details)


# ---------- balances ----------

def _ensure(conn, account: str) -> tuple[sqlite3.Row, int]:
    """The account's billing row (made if missing). With billing on, adds the starter
    credits once. Returns (row, starter actions just added)."""
    conn.execute("INSERT OR IGNORE INTO billing (account, updated_at) VALUES (?, ?)",
                 (account, _now()))
    added = 0
    if enabled() and account not in NEVER_CHARGED:
        s = settings()
        starter = s["starter_credits"] * s["actions_per_credit"]
        if conn.execute("UPDATE billing SET actions = actions + ?, starter_given = 1, "
                        "updated_at = ? WHERE account = ? AND starter_given = 0",
                        (starter, _now(), account)).rowcount:
            added = starter
    return conn.execute("SELECT * FROM billing WHERE account = ?", (account,)).fetchone(), added


def _log_starter(account: str, agent: str, added: int) -> None:
    if added:
        _log(account, agent, "starter_credits", actions_added=added)


def out_of_actions(actions: int = 0) -> str:
    s = settings()
    return (f"This account is out of Memoria actions (balance: {actions}). Buy credits with "
            f"$MEMORIA at {pay_page()} (1 credit = {s['tokens_per_credit']:,} $MEMORIA = "
            f"{s['actions_per_credit']} actions). Agents can also pay themselves: link a "
            f"wallet (wallet_challenge, link_wallet), send $MEMORIA to {s['treasury']} on "
            f"Robinhood Chain, then call claim_payment with the transaction hash.")


def charge(account: str, agent: str, action: str) -> bool:
    """Take 1 action for this request. True if one was taken (give it back with refund()
    if the request fails). Raises BillingError when the balance is empty."""
    if not is_billed(account):
        return False
    with db.transaction() as conn:
        row, added = _ensure(conn, account)
        taken = conn.execute("UPDATE billing SET actions = actions - 1, updated_at = ? "
                             "WHERE account = ? AND actions >= 1",
                             (_now(), account)).rowcount == 1
        left = row["actions"] - 1 if taken else row["actions"]
    _log_starter(account, agent, added)
    if not taken:
        raise BillingError(out_of_actions(left))
    _log(account, agent, "charge", tool=action, actions_left=left)
    return True


def refund(account: str, agent: str, action: str) -> None:
    """Give back the action taken for a request that failed."""
    db.execute("UPDATE billing SET actions = actions + 1, updated_at = ? WHERE account = ?",
               (_now(), account))
    _log(account, agent, "refund", tool=action)


def low_balance_note(account: str) -> str | None:
    """A short note when a billed account is nearly out of actions, else None."""
    if not is_billed(account):
        return None
    row = db.query("SELECT actions, starter_given FROM billing WHERE account = ?", (account,))
    if not row or not row[0]["starter_given"]:
        return None  # the starter credits haven't been added yet, so it isn't low
    actions = row[0]["actions"]
    if actions > LOW_BALANCE:
        return None
    return (f"Balance low: {actions} action{'' if actions == 1 else 's'} left. Buy credits "
            f"at {pay_page()}.")


def wallets_of(account: str) -> list[str]:
    return [r["address"] for r in db.query(
        "SELECT address FROM wallets WHERE account = ? ORDER BY linked_at, address", (account,))]


def balance(account: str, agent: str = "") -> dict:
    """Actions left (and as credits), the linked wallets, and how to pay."""
    with db.transaction() as conn:
        row, added = _ensure(conn, account)
    _log_starter(account, agent or "host", added)
    s = settings()
    actions = row["actions"]
    credits = actions / s["actions_per_credit"]
    return {
        "billing": "on" if enabled() else "off",
        "actions": actions,
        "credits": int(credits) if credits == int(credits) else credits,
        "wallets": wallets_of(account),
        "price": {"tokens_per_credit": s["tokens_per_credit"],
                  "actions_per_credit": s["actions_per_credit"]},
        "pay_page": pay_page(),
        "treasury": s["treasury"],
        "costs": {"1 action each": list(CHARGED_ACTIONS), "free": list(FREE_ACTIONS)},
    }


def adjust(account: str, credits: int, reason: str = "") -> dict:
    """Add (or, with a negative number, remove) credits by hand. Logged."""
    if accounts.get_account(account) is None:
        raise BillingError(f"No account called '{account}'.")
    delta = int(credits) * settings()["actions_per_credit"]
    with db.transaction() as conn:
        row, added = _ensure(conn, account)
        if row["actions"] + delta < 0:
            raise BillingError(f"'{account}' only has {row['actions']} actions, so "
                               f"{-delta} can't be removed.")
        conn.execute("UPDATE billing SET actions = actions + ?, updated_at = ? "
                     "WHERE account = ?", (delta, _now(), account))
    _log_starter(account, "host", added)
    _log(account, "host", "credits_adjusted", credits=int(credits), actions=delta,
         reason=reason)
    return balance(account, "host")


def set_public_purchases(account: str, on: bool) -> None:
    if accounts.get_account(account) is None:
        raise BillingError(f"No account called '{account}'.")
    with db.transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO billing (account, updated_at) VALUES (?, ?)",
                     (account, _now()))
        conn.execute("UPDATE billing SET public_purchases = ?, updated_at = ? "
                     "WHERE account = ?", (int(on), _now(), account))


def payments_of(account: str) -> list[dict]:
    """The account's claimed payments, one entry per transaction, newest first."""
    rows = db.query("SELECT tx_hash, min(claimed_at) AS claimed_at, sum(credits) AS credits, "
                    "sum(actions) AS actions, group_concat(amount) AS amounts, "
                    "min(from_address) AS from_address FROM payments WHERE account = ? "
                    "GROUP BY tx_hash ORDER BY claimed_at DESC, tx_hash", (account,))
    return [{
        "date": r["claimed_at"][:10],
        "claimed_at": r["claimed_at"],
        "tx_hash": r["tx_hash"],
        "explorer": f"{EXPLORER_URL}/tx/{r['tx_hash']}",
        "tokens": _tokens(sum(int(a) for a in r["amounts"].split(","))),
        "credits": r["credits"],
        "actions": r["actions"],
    } for r in rows]


def public_purchases(account: str) -> list[dict] | None:
    """The account's purchase history if it chose to make it public, else None."""
    account = (account or "").strip().lower()
    row = db.query("SELECT public_purchases FROM billing WHERE account = ?", (account,))
    if not row or not row[0]["public_purchases"]:
        return None
    return [{k: p[k] for k in ("date", "tx_hash", "explorer", "tokens", "credits", "actions")}
            for p in payments_of(account)]


# ---------- linking a wallet ----------

def challenge(account: str, agent: str) -> dict:
    """A one-time message for a wallet to sign (EIP-191 personal_sign)."""
    nonce = secrets.token_hex(16)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    expires = (now + timedelta(minutes=CHALLENGE_MINUTES)).isoformat()
    message = (f"Link this wallet to Memoria.\n\nAccount: {account}\nAgent: {agent}\n"
               f"Nonce: {nonce}\nExpires: {expires}\n\n"
               "Signing this is free. It does not send any tokens or give access to them.")
    with db.transaction() as conn:
        conn.execute("DELETE FROM wallet_challenges WHERE expires_at < ?",
                     ((now - timedelta(days=1)).isoformat(),))
        conn.execute("INSERT INTO wallet_challenges (nonce, account, agent, message, "
                     "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (nonce, account, agent, message, now.isoformat(), expires))
    return {"message": message, "nonce": nonce, "expires_at": expires,
            "next": "Sign this exact message with the wallet (personal_sign), then call "
                    "link_wallet with the wallet address and the signature."}


def _recover(message: str, signature: str) -> str | None:
    from eth_account import Account  # loaded only when needed
    from eth_account.messages import encode_defunct
    try:
        return Account.recover_message(encode_defunct(text=message),
                                       signature=signature).lower()
    except Exception:
        return None


def link_wallet(account: str, agent: str, address: str, signature: str) -> dict:
    """Link a wallet to the account, if it signed this agent's latest challenge."""
    address = (address or "").strip()
    signature = (signature or "").strip()
    if not ADDRESS.match(address):
        raise BillingError("That isn't a wallet address (0x and 40 hex characters).")
    if not SIGNATURE.match(signature):
        raise BillingError("That isn't a wallet signature (65 bytes, 130 hex characters).")
    now = _now()
    with db.transaction() as conn:  # the challenge is used up whatever happens next
        row = conn.execute("SELECT * FROM wallet_challenges WHERE account = ? AND agent = ? "
                           "AND used_at IS NULL ORDER BY created_at DESC, rowid DESC LIMIT 1",
                           (account, agent)).fetchone()
        if row is not None:
            conn.execute("UPDATE wallet_challenges SET used_at = ? WHERE nonce = ?",
                         (now, row["nonce"]))
    if row is None:
        raise BillingError("There's no wallet challenge waiting (each one works once). "
                           "Call wallet_challenge first, then sign its message.")
    if row["expires_at"] <= now:
        raise BillingError(f"That challenge expired (they last {CHALLENGE_MINUTES} minutes). "
                           "Call wallet_challenge again.")
    if _recover(row["message"], signature) != address.lower():
        raise BillingError(f"That signature wasn't made by {address} for the latest "
                           "challenge, so the wallet wasn't linked. Call wallet_challenge "
                           "again and sign its exact message with that wallet.")
    with db.transaction() as conn:
        owner = conn.execute("SELECT account FROM wallets WHERE address = ?",
                             (address.lower(),)).fetchone()
        if owner and owner["account"] != account:
            raise BillingError("That wallet is already linked to another Memoria account. "
                               "A wallet can belong to only one account.")
        if owner is None:
            conn.execute("INSERT INTO wallets (address, account, agent, linked_at) "
                         "VALUES (?, ?, ?, ?)", (address.lower(), account, agent, now))
    _log(account, agent, "link_wallet", wallet=address.lower())
    return {"linked": address.lower(), "wallets": wallets_of(account)}


ADMIN_AGENT = "admin"  # the agent recorded for wallets the host links by hand


def admin_link_wallet(account: str, address: str) -> dict:
    """The host links a wallet to an account by hand, without a signature (for agents
    whose wallets can't sign messages). Same rules as link_wallet: a valid address, and
    a wallet can belong to only one account."""
    address = (address or "").strip()
    if accounts.get_account(account) is None:
        raise BillingError(f"No account called '{account}'.")
    if not ADDRESS.match(address):
        raise BillingError("That isn't a wallet address (0x and 40 hex characters).")
    now = _now()
    with db.transaction() as conn:
        owner = conn.execute("SELECT account FROM wallets WHERE address = ?",
                             (address.lower(),)).fetchone()
        if owner and owner["account"] != account:
            raise BillingError("That wallet is already linked to another Memoria account. "
                               "A wallet can belong to only one account.")
        if owner is None:
            conn.execute("INSERT INTO wallets (address, account, agent, linked_at) "
                         "VALUES (?, ?, ?, ?)", (address.lower(), account, ADMIN_AGENT, now))
    if owner is None:
        _log(account, ADMIN_AGENT, "link_wallet", wallet=address.lower(), by="host",
             linked_at=now)
    return {"linked": address.lower(), "already_linked": owner is not None,
            "wallets": wallets_of(account)}


def admin_unlink_wallet(account: str, address: str) -> dict:
    """The host unlinks a wallet from an account. Payments already claimed stay claimed."""
    address = (address or "").strip()
    if not ADDRESS.match(address):
        raise BillingError("That isn't a wallet address (0x and 40 hex characters).")
    removed = db.execute("DELETE FROM wallets WHERE address = ? AND account = ?",
                         (address.lower(), account))
    if not removed:
        raise BillingError(f"That wallet isn't linked to '{account}'.")
    _log(account, ADMIN_AGENT, "unlink_wallet", wallet=address.lower(), by="host",
         unlinked_at=_now())
    return {"unlinked": address.lower(), "wallets": wallets_of(account)}


# ---------- claiming a payment ----------

def _rpc_call(url: str, method: str, params: list):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": signup.USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as r:
        answer = json.loads(r.read() or b"{}")
    if answer.get("error"):
        raise RuntimeError(f"{method}: {answer['error']}")
    return answer.get("result")


async def rpc(method: str, params: list | None = None):
    """Ask the chain (JSON-RPC). Replaced by a fake chain in the tests."""
    return await asyncio.to_thread(_rpc_call, settings()["rpc_url"], method, params or [])


def _address_from_topic(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def _transfers(receipt: dict) -> list[dict]:
    found = []
    for log in receipt.get("logs") or []:
        topics = log.get("topics") or []
        if len(topics) == 3 and topics[0].lower() == TRANSFER_TOPIC:
            found.append({"log_index": int(log["logIndex"], 16),
                          "token": log["address"].lower(),
                          "from": _address_from_topic(topics[1]),
                          "to": _address_from_topic(topics[2]),
                          "amount": int(log.get("data") or "0x0", 16)})
    return found


async def claim(account: str, agent: str, tx_hash: str) -> dict:
    """Add credits for a $MEMORIA payment from a linked wallet to the treasury."""
    tx = (tx_hash or "").strip().lower()
    if not TX_HASH.match(tx):
        raise BillingError("That isn't a transaction hash (0x and 64 hex characters).")
    s = settings()
    try:
        chain = int(await rpc("eth_chainId"), 16)
        if chain != s["chain_id"]:
            raise BillingError(f"The chain server answers for chain {chain}, not Robinhood "
                               f"Chain ({s['chain_id']}). Check MEMORIA_RPC_URL.")
        receipt = await rpc("eth_getTransactionReceipt", [tx])
        if receipt is None:
            raise BillingError("That transaction wasn't found yet. If you just sent it, wait "
                               "a minute and claim again; otherwise check the hash.")
        if receipt.get("status") != "0x1":
            raise BillingError("That transaction failed on chain, so nothing was paid.")
        latest = int(await rpc("eth_blockNumber"), 16)
    except BillingError:
        raise
    except Exception as e:
        print(f"Memoria billing: chain request failed: {e}", file=sys.stderr, flush=True)
        raise BillingError("Couldn't reach Robinhood Chain just now. Please try again in a "
                           "minute.") from None
    block = int(receipt["blockNumber"], 16)
    confirmations = latest - block + 1
    if confirmations < s["min_confirmations"]:
        wait = s["min_confirmations"] - confirmations
        raise BillingError(f"The payment has {confirmations} of {s['min_confirmations']} "
                           f"confirmations. Wait for {wait} more block(s), then claim again.")

    transfers = _transfers(receipt)
    of_token = [t for t in transfers if t["token"] == s["token"]]
    to_treasury = [t for t in of_token if t["to"] == s["treasury"]]
    linked = set(wallets_of(account))
    mine = [t for t in to_treasury if t["from"] in linked]
    if not mine:
        if not of_token:
            raise BillingError(f"This transaction doesn't send $MEMORIA (contract "
                               f"{s['token']}). Only $MEMORIA payments can be claimed.")
        if not to_treasury:
            raise BillingError(f"The $MEMORIA in this transaction wasn't sent to the Memoria "
                               f"treasury ({s['treasury']}), so it can't be claimed.")
        senders = ", ".join(sorted({t["from"] for t in to_treasury}))
        raise BillingError(f"This payment was sent from {senders}, which isn't linked to your "
                           "account. Link that wallet first (wallet_challenge, then "
                           "link_wallet), then claim again.")

    unit = s["tokens_per_credit"] * 10 ** DECIMALS
    now = _now()
    try:
        with db.transaction() as conn:
            done = {r[0] for r in conn.execute(
                "SELECT log_index FROM payments WHERE tx_hash = ?", (tx,))}
            new = [t for t in mine if t["log_index"] not in done]
            if not new:
                raise BillingError("This payment was already claimed.")
            total = sum(t["amount"] for t in new)
            credits = total // unit
            if credits < 1:
                raise BillingError(f"This payment is {_tokens(total)} $MEMORIA, less than 1 "
                                   f"credit ({s['tokens_per_credit']:,} $MEMORIA), so no "
                                   "credits could be added.")
            actions = credits * s["actions_per_credit"]
            for i, t in enumerate(new):
                conn.execute("INSERT INTO payments (tx_hash, log_index, account, agent, "
                             "from_address, amount, credits, actions, block_number, "
                             "claimed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (tx, t["log_index"], account, agent, t["from"], str(t["amount"]),
                              credits if i == 0 else 0, actions if i == 0 else 0, block, now))
            _, added = _ensure(conn, account)
            conn.execute("UPDATE billing SET actions = actions + ?, updated_at = ? "
                         "WHERE account = ?", (actions, now, account))
    except sqlite3.IntegrityError:  # claimed by another request at the same moment
        raise BillingError("This payment was already claimed.") from None
    _log_starter(account, agent, added)
    _log(account, agent, "payment", tx_hash=tx, tokens=_tokens(total), credits=credits,
         actions=actions)
    return {"claimed": tx, "explorer": f"{EXPLORER_URL}/tx/{tx}", "tokens": _tokens(total),
            "credits": credits, "actions_added": actions, "balance": balance(account, agent)}


# ---------- HTTP endpoints for the pay page, and the public purchase history ----------
#
#   GET  /billing/config     public: is billing on, prices, token, treasury, chain
#   POST /billing/challenge  needs "Authorization: Bearer <API key>"
#   POST /billing/link       {"address": "0x...", "signature": "0x..."}
#   POST /billing/claim      {"tx_hash": "0x..."}
#   GET  /billing/balance
#   GET  /purchases/<account>  public only if that account turned it on, else 404

def handles(path: str) -> bool:
    return path.startswith("/billing/") or path.startswith("/purchases/")


def config() -> dict:
    s = settings()
    return {"billing": enabled(), "chain_id": s["chain_id"], "chain_name": "Robinhood Chain",
            "public_rpc": PUBLIC_RPC_URL, "explorer": EXPLORER_URL, "token": s["token"],
            "decimals": DECIMALS, "treasury": s["treasury"],
            "tokens_per_credit": s["tokens_per_credit"],
            "actions_per_credit": s["actions_per_credit"],
            "starter_credits": s["starter_credits"],
            "min_confirmations": s["min_confirmations"]}


async def _preflight(send, cors) -> None:
    await send({"type": "http.response.start", "status": 204, "headers": [
        *cors, (b"access-control-allow-methods", b"GET, POST"),
        (b"access-control-allow-headers", b"authorization, content-type"),
        (b"access-control-max-age", b"600")]})
    await send({"type": "http.response.body", "body": b""})


async def app(scope, receive, send) -> None:
    cors = signup._cors(scope)
    path, method = scope["path"], scope["method"]
    answer = signup._answer
    error = signup._error
    if method == "OPTIONS":
        return await _preflight(send, cors)

    if path.startswith("/purchases/"):
        if method != "GET":
            return await answer(send, 405, error("Use GET."), cors)
        account = path[len("/purchases/"):].strip("/")
        history = public_purchases(account)
        if history is None:
            return await answer(send, 404, error("Not found."), cors)
        return await answer(send, 200, {"account": account.lower(), "purchases": history},
                            cors)

    if path == "/billing/config":
        return await answer(send, 200, config(), cors)
    if not enabled():
        return await answer(send, 404, error("Billing is not live yet."), cors)

    ip = limits.client_ip(scope)
    wait = limits.billing_per_ip.blocked_for(ip) or limits.failed_logins.blocked_for(ip)
    if wait:
        seconds = max(1, round(wait))
        return await answer(send, 429, error(f"Too many requests. Try again in {seconds} "
                                             "seconds."),
                            [*cors, (b"retry-after", str(seconds).encode())])
    limits.billing_per_ip.hit(ip)

    auth = dict(scope.get("headers", [])).get(b"authorization", b"").decode(errors="replace")
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    try:
        account, agent = accounts.check_key(key)
    except accounts.AccountError:
        limits.failed_logins.hit(ip)
        return await answer(send, 401, error("That API key isn't valid. Check you copied all "
                                             "of it (it starts with mem_)."),
                            [*cors, (b"www-authenticate", b"Bearer")])
    if account in NEVER_CHARGED:
        return await answer(send, 403, error("This key can't use payments."), cors)

    routes = {"/billing/challenge": "POST", "/billing/link": "POST",
              "/billing/claim": "POST", "/billing/balance": "GET"}
    if path not in routes:
        return await answer(send, 404, error("Not found."), cors)
    if method != routes[path]:
        return await answer(send, 405, error(f"Use {routes[path]}."), cors)
    data = {}
    if method == "POST":
        try:
            data = json.loads(await signup._read_body(receive) or b"{}")
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            return await answer(send, 400, error("Send JSON."), cors)
    try:
        if path == "/billing/challenge":
            result = challenge(account, agent)
        elif path == "/billing/link":
            result = link_wallet(account, agent, str(data.get("address", "")),
                                 str(data.get("signature", "")))
        elif path == "/billing/claim":
            result = await claim(account, agent, str(data.get("tx_hash", "")))
        else:
            result = balance(account, agent)
    except BillingError as e:
        return await answer(send, 400, error(str(e)), cors)
    return await answer(send, 200, result, cors)
