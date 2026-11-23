"""Paying for Memoria with $MEMORIA (billing.py). The chain is always FAKE here."""
import asyncio
import threading
import time

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from starlette.testclient import TestClient

import accounts
import billing
import db
import demo
import limits
import manage_accounts
import memoria as m
import server
from conftest import free_port
from memoria_client import Memoria

TOKEN = billing.TOKEN_ADDRESS
TREASURY = billing.TREASURY_ADDRESS.lower()
OTHER_TOKEN = "0x" + "ab" * 20
STRANGER = "0x" + "cd" * 20
UNIT = 10_000 * 10 ** 18  # tokens in 1 credit, with 18 decimals


def topic(address: str) -> str:
    return "0x" + "0" * 24 + address.lower()[2:]


def transfer(frm, to=TREASURY, amount=UNIT, token=TOKEN, index=0) -> dict:
    return {"address": token, "topics": [billing.TRANSFER_TOPIC, topic(frm), topic(to)],
            "data": hex(amount), "logIndex": hex(index)}


class FakeChain:
    """Answers billing.rpc() like Robinhood Chain would."""

    def __init__(self):
        # Realistic block numbers: after billing started (billing.CLAIM_START_BLOCK).
        self.chain_id, self.latest, self.receipts, self.down = 4663, 121_300_000, {}, False

    def add(self, tx: str, logs: list, block: int | None = None, status: str = "0x1") -> str:
        block = self.latest - 100 if block is None else block
        self.receipts[tx.lower()] = {"status": status, "blockNumber": hex(block), "logs": logs}
        return tx

    async def rpc(self, method, params=None):
        if self.down:
            raise OSError("connection refused")
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_blockNumber":
            return hex(self.latest)
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0].lower())
        raise AssertionError(f"unexpected {method}")


def tx_hash(n: int) -> str:
    return "0x" + f"{n:064x}"


@pytest.fixture
def chain(monkeypatch):
    fake = FakeChain()
    monkeypatch.setattr(billing, "rpc", fake.rpc)
    return fake


@pytest.fixture
def on(store, devs, chain, monkeypatch):
    """Billing on, with acme and globex (agents trader and analyst) and a fake chain."""
    monkeypatch.setenv("MEMORIA_BILLING", "on")
    return devs


def link(account="acme", agent="trader", wallet=None):
    """Link a new (or given) wallet the proper way. Returns the wallet."""
    wallet = wallet or Account.create()
    message = billing.challenge(account, agent)["message"]
    signature = Account.sign_message(encode_defunct(text=message), wallet.key).signature.hex()
    billing.link_wallet(account, agent, wallet.address, signature)
    return wallet


def claim(tx, account="acme", agent="trader"):
    return asyncio.run(billing.claim(account, agent, tx))


def actions(account="acme") -> int:
    rows = db.query("SELECT actions FROM billing WHERE account = ?", (account,))
    return rows[0][0] if rows else 0


# ---------- linking a wallet ----------

def test_a_signed_challenge_links_the_wallet(on):
    wallet = link()
    assert billing.wallets_of("acme") == [wallet.address.lower()]
    challenge = billing.challenge("acme", "trader")
    for part in ("Account: acme", "Agent: trader", "Nonce: ", "Expires: "):
        assert part in challenge["message"]


def test_a_signature_from_another_wallet_is_refused(on):
    wallet, impostor = Account.create(), Account.create()
    message = billing.challenge("acme", "trader")["message"]
    signature = Account.sign_message(encode_defunct(text=message), impostor.key).signature.hex()
    with pytest.raises(billing.BillingError, match="wasn't made by"):
        billing.link_wallet("acme", "trader", wallet.address, signature)
    assert billing.wallets_of("acme") == []


def test_a_challenge_works_only_once(on):
    wallet = Account.create()
    message = billing.challenge("acme", "trader")["message"]
    signature = Account.sign_message(encode_defunct(text=message), wallet.key).signature.hex()
    billing.link_wallet("acme", "trader", wallet.address, signature)
    with pytest.raises(billing.BillingError, match="no wallet challenge waiting"):
        billing.link_wallet("acme", "trader", wallet.address, signature)


def test_a_wrong_signature_still_uses_up_the_challenge(on):
    wallet = Account.create()
    message = billing.challenge("acme", "trader")["message"]
    with pytest.raises(billing.BillingError):
        billing.link_wallet("acme", "trader", wallet.address, "0x" + "11" * 65)
    signature = Account.sign_message(encode_defunct(text=message), wallet.key).signature.hex()
    with pytest.raises(billing.BillingError, match="no wallet challenge waiting"):
        billing.link_wallet("acme", "trader", wallet.address, signature)


def test_an_expired_challenge_is_refused(on, monkeypatch):
    wallet = Account.create()
    message = billing.challenge("acme", "trader")["message"]
    signature = Account.sign_message(encode_defunct(text=message), wallet.key).signature.hex()
    monkeypatch.setattr(billing, "_now", lambda: "2999-01-01T00:00:00+00:00")
    with pytest.raises(billing.BillingError, match="expired"):
        billing.link_wallet("acme", "trader", wallet.address, signature)


def test_a_challenge_belongs_to_its_agent(on):
    wallet = Account.create()
    message = billing.challenge("acme", "trader")["message"]
    signature = Account.sign_message(encode_defunct(text=message), wallet.key).signature.hex()
    with pytest.raises(billing.BillingError, match="no wallet challenge waiting"):
        billing.link_wallet("acme", "analyst", wallet.address, signature)


def test_a_wallet_can_belong_to_only_one_account(on):
    wallet = link("acme")
    with pytest.raises(billing.BillingError, match="another Memoria account"):
        link("globex", wallet=wallet)
    link("acme", "analyst", wallet=wallet)  # the same account again is fine
    assert billing.wallets_of("globex") == []


@pytest.mark.parametrize("address, signature, error", [
    ("not-an-address", "0x" + "11" * 65, "wallet address"),
    ("0x" + "12" * 20, "0x1234", "wallet signature"),
])
def test_link_checks_what_it_is_given(on, address, signature, error):
    with pytest.raises(billing.BillingError, match=error):
        billing.link_wallet("acme", "trader", address, signature)


# ---------- claiming a payment ----------

def test_claim_adds_credits_as_actions(on, chain):
    wallet = link()
    tx = chain.add(tx_hash(1), [transfer(wallet.address, amount=25_000 * 10 ** 18)])
    result = claim(tx)
    assert result["credits"] == 2 and result["actions_added"] == 4  # 2.5 credits -> 2
    assert result["tokens"] == "25000"
    assert result["explorer"] == f"https://robinhoodchain.blockscout.com/tx/{tx}"
    assert actions() == 20 + 4  # starter credits (10 x 2) + the payment
    assert result["balance"]["actions"] == 24 and result["balance"]["credits"] == 12


def test_actions_per_credit_and_price_come_from_the_settings(on, chain, monkeypatch):
    monkeypatch.setenv("MEMORIA_TOKENS_PER_CREDIT", "5000")
    monkeypatch.setenv("MEMORIA_ACTIONS_PER_CREDIT", "3")
    monkeypatch.setenv("MEMORIA_STARTER_CREDITS", "0")
    wallet = link()
    tx = chain.add(tx_hash(2), [transfer(wallet.address, amount=12_000 * 10 ** 18)])
    assert claim(tx)["actions_added"] == 2 * 3
    assert actions() == 6


def test_transfers_in_one_transaction_are_added_together(on, chain):
    wallet = link()
    half = UNIT // 2
    tx = chain.add(tx_hash(3), [transfer(wallet.address, amount=half, index=4),
                                transfer(wallet.address, amount=half, index=7),
                                transfer(wallet.address, to=STRANGER, index=8)])
    assert claim(tx)["credits"] == 1
    assert sorted(r[0] for r in db.query("SELECT log_index FROM payments")) == [4, 7]


def test_a_payment_can_be_claimed_only_once(on, chain):
    wallet = link()
    tx = chain.add(tx_hash(4), [transfer(wallet.address)])
    claim(tx)
    with pytest.raises(billing.BillingError, match="already claimed"):
        claim(tx)
    with pytest.raises(billing.BillingError, match="already claimed"):
        claim(tx.upper().replace("0X", "0x"))  # the same hash in capitals
    assert actions() == 20 + 2


def test_two_claims_at_the_same_moment_pay_once(on, chain):
    wallet = link()
    tx = chain.add(tx_hash(5), [transfer(wallet.address)])

    async def both():
        return await asyncio.gather(billing.claim("acme", "trader", tx),
                                    billing.claim("acme", "trader", tx),
                                    return_exceptions=True)
    results = asyncio.run(both())
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, billing.BillingError) for r in results) == 1
    assert actions() == 20 + 2


def test_the_database_itself_refuses_a_second_claim(on):
    row = ("0x" + "ef" * 32, 0, "acme", "trader", "0x" + "12" * 20, "1", 1, 2, 1, "now")
    sql = ("INSERT INTO payments (tx_hash, log_index, account, agent, from_address, amount, "
           "credits, actions, block_number, claimed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
    db.execute(sql, row)
    with pytest.raises(Exception, match="UNIQUE"):
        db.execute(sql, row)


def test_another_accounts_claimed_payment_cant_be_claimed_again(on, chain):
    wallet = link("acme")
    tx = chain.add(tx_hash(6), [transfer(wallet.address)])
    claim(tx, "acme")
    with pytest.raises(billing.BillingError):
        claim(tx, "globex")
    assert actions("globex") == 0


def test_wrong_token_is_refused(on, chain):
    wallet = link()
    tx = chain.add(tx_hash(7), [transfer(wallet.address, token=OTHER_TOKEN)])
    with pytest.raises(billing.BillingError, match=r"doesn't send \$MEMORIA"):
        claim(tx)


def test_wrong_recipient_is_refused(on, chain):
    wallet = link()
    tx = chain.add(tx_hash(8), [transfer(wallet.address, to=STRANGER)])
    with pytest.raises(billing.BillingError, match="wasn't sent to the Memoria treasury"):
        claim(tx)


def test_payment_from_an_unlinked_wallet_is_refused(on, chain):
    link()
    tx = chain.add(tx_hash(9), [transfer(STRANGER)])
    with pytest.raises(billing.BillingError, match="isn't linked to your account"):
        claim(tx)
    assert db.query("SELECT count(*) FROM payments")[0][0] == 0


def test_someone_elses_linked_wallet_cant_be_claimed_by_you(on, chain):
    wallet = link("globex")
    tx = chain.add(tx_hash(10), [transfer(wallet.address)])
    with pytest.raises(billing.BillingError, match="isn't linked to your account"):
        claim(tx, "acme")


def test_not_enough_confirmations_says_how_long_to_wait(on, chain):
    wallet = link()
    tx = chain.add(tx_hash(11), [transfer(wallet.address)], block=chain.latest - 4)  # 5 so far
    with pytest.raises(billing.BillingError, match="5 of 20 confirmations. Wait for 15 more"):
        claim(tx)
    chain.latest += 15
    assert claim(tx)["credits"] == 1


@pytest.mark.parametrize("setup, error", [
    (lambda c: None, "wasn't found yet"),
    (lambda c: c.add(tx_hash(12), [], status="0x0"), "failed on chain"),
    (lambda c: setattr(c, "down", True), "Couldn't reach Robinhood Chain"),
    (lambda c: setattr(c, "chain_id", 1), "not Robinhood Chain"),
])
def test_chain_problems_are_explained(on, chain, setup, error):
    setup(chain)
    with pytest.raises(billing.BillingError, match=error):
        claim(tx_hash(12))


def test_less_than_one_credit_is_kept_as_leftover(on, chain):
    wallet = link()
    tx = chain.add(tx_hash(13), [transfer(wallet.address, amount=4_000 * 10 ** 18)])
    done = claim(tx)
    assert done["credits"] == 0 and done["actions_added"] == 0
    assert done["leftover_tokens"] == "4000"
    assert "6000 more makes 1 credit" in done["message"]
    assert db.query("SELECT count(*) FROM payments")[0][0] == 1  # recorded: can't claim twice
    with pytest.raises(billing.BillingError, match="already claimed"):
        claim(tx)
    assert actions() == 20


def test_leftover_is_added_to_the_next_payment(on, chain):
    wallet = link()
    first = claim(chain.add(tx_hash(14), [transfer(wallet.address, amount=15_000 * 10 ** 18)]))
    assert first["credits"] == 1 and first["leftover_tokens"] == "5000"
    second = claim(chain.add(tx_hash(15), [transfer(wallet.address, amount=5_000 * 10 ** 18)]))
    assert second["credits"] == 1 and second["leftover_tokens"] == "0"
    assert "message" not in second
    assert actions() == 20 + 2 + 2
    assert billing.balance("acme")["leftover_tokens"] == "0"


def test_leftover_counts_fractions_of_a_token(on, chain):
    wallet = link()
    claim(chain.add(tx_hash(16), [transfer(wallet.address, amount=UNIT + 5 * 10 ** 17)]))
    assert billing.balance("acme")["leftover_tokens"] == "0.5"


def test_transfers_from_before_billing_started_can_never_be_claimed(on, chain):
    wallet = link()
    start = billing.settings()["claim_start_block"]
    assert start == 121_276_863  # the block of the first payment
    old = chain.add(tx_hash(17), [transfer(wallet.address, amount=50 * UNIT)], block=start - 1)
    with pytest.raises(billing.BillingError, match="before Memoria billing started"):
        claim(old)
    assert db.query("SELECT count(*) FROM payments")[0][0] == 0
    at_start = chain.add(tx_hash(18), [transfer(wallet.address)], block=start)
    assert claim(at_start)["credits"] == 1  # the launch block itself counts


def test_nomad_ais_payment_in_block_121327603_is_still_claimable(on, chain):
    """The real payment 0x90b2...42ea (28 credits) is in block 121327603, after the start."""
    accounts.create_account("nomad-ai", email="dev@nomad.example")
    accounts.create_key("nomad-ai", "main")
    wallet = link("nomad-ai", "main")
    chain.latest = 121_327_603 + 100
    tx = chain.add("0x90b2690813329e5ef22599ba40717600eea6afb0ea921369196627f4831042ea",
                   [transfer(wallet.address, amount=28 * UNIT)], block=121_327_603)
    done = claim(tx, "nomad-ai", "main")
    assert done["credits"] == 28 and done["actions_added"] == 56


def test_the_start_block_can_be_changed(on, chain, monkeypatch):
    wallet = link()
    monkeypatch.setenv("MEMORIA_CLAIM_START_BLOCK", str(chain.latest - 50))
    tx = chain.add(tx_hash(19), [transfer(wallet.address)], block=chain.latest - 100)
    with pytest.raises(billing.BillingError, match="before Memoria billing started"):
        claim(tx)


def test_a_bad_hash_is_refused(on):
    with pytest.raises(billing.BillingError, match="transaction hash"):
        claim("0x1234")


# ---------- charging ----------

def run_as(key, url, work):
    async def go():
        async with Memoria(api_key=key, url=url) as mem:
            return await work(mem)
    return asyncio.run(go())


def test_save_update_find_and_list_each_use_one_action(on, memoria_url):
    key = on["acme/trader"]

    async def work(mem):
        saved = await mem.save("Acme renews March 1.", reason="email", topic="customers")
        await mem.update(saved["memory_id"], "Acme renews March 2.", reason="moved")
        await mem.find("Acme", topic="customers")
        await mem.list()
        after_four = actions()
        await mem.share(saved["memory_id"], "shared", ["analyst"])
        await mem.delete(saved["memory_id"])
        return after_four
    assert run_as(key, memoria_url, work) == 20 - 4
    assert actions() == 16  # share and delete were free


def test_a_failed_action_gives_the_action_back(on, memoria_url):
    async def work(mem):
        with pytest.raises(Exception, match="No memory with ID"):
            await mem.update("mem-00000000", "x", reason="r")
    run_as(on["acme/trader"], memoria_url, work)
    assert actions() == 20


def test_out_of_actions_explains_how_to_pay(on, memoria_url, monkeypatch):
    monkeypatch.setenv("MEMORIA_STARTER_CREDITS", "0")

    async def work(mem):
        with pytest.raises(Exception, match=r"out of Memoria actions.*memoriaagent.site/pay"):
            await mem.save("x", reason="r", topic="t")
    run_as(on["acme/trader"], memoria_url, work)
    assert db.count_memories("acme") == 0 and actions() == 0


def test_charging_takes_only_what_is_there(on):
    billing.charge("acme", "trader", "find_memory")  # 20 -> 19
    db.execute("UPDATE billing SET actions = 1 WHERE account = 'acme'")
    assert billing.charge("acme", "trader", "find_memory") is True
    with pytest.raises(billing.BillingError, match="out of Memoria actions"):
        billing.charge("acme", "trader", "find_memory")
    assert actions() == 0


def test_starter_credits_are_given_once(on):
    billing.balance("acme")
    billing.charge("acme", "trader", "save_memory")
    billing.balance("acme")
    assert actions() == 20 - 1
    assert db.query("SELECT starter_given FROM billing WHERE account = 'acme'")[0][0] == 1


def test_existing_accounts_get_starter_credits_when_billing_turns_on(store, devs, monkeypatch):
    billing.adjust("acme", 3)  # billing off: no starter credits yet
    assert actions() == 6
    monkeypatch.setenv("MEMORIA_BILLING", "on")
    assert billing.balance("acme")["actions"] == 6 + 20
    monkeypatch.setenv("MEMORIA_BILLING", "off")
    monkeypatch.setenv("MEMORIA_BILLING", "on")
    assert billing.balance("acme")["actions"] == 26  # never twice


def test_billing_off_changes_nothing(store, devs, memoria_url):
    assert billing.enabled() is False
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == {"save_memory", "find_memory", "list_memories", "update_memory",
                     "share_memory", "delete_memory", "record_outcome", "get_outcomes"}

    async def work(mem):
        await mem.save("Free.", reason="r", topic="t")
        return await mem.find("Free", topic="t")
    assert len(run_as(devs["acme/trader"], memoria_url, work)) == 1
    assert db.query("SELECT count(*) FROM billing")[0][0] == 0


def test_an_invalid_treasury_keeps_billing_off(store, monkeypatch, capsys):
    monkeypatch.setenv("MEMORIA_BILLING", "on")
    monkeypatch.setenv("MEMORIA_TREASURY_ADDRESS", "not-a-wallet")
    billing._warned.clear()
    assert billing.enabled() is False
    assert "billing stays OFF" in capsys.readouterr().err


def test_local_and_demo_are_never_charged(on, monkeypatch):
    monkeypatch.setenv("MEMORIA_STARTER_CREDITS", "0")
    asyncio.run(m.save_memory("alice", "Laptop note.", "r", "t"))
    assert billing.charge("local", "alice", "save_memory") is False
    assert billing.charge(demo.ACCOUNT, demo.AGENT, "find_memory") is False
    assert db.query("SELECT count(*) FROM billing")[0][0] == 0


def test_demo_keys_work_as_before_and_cant_pay(on, monkeypatch):
    import hashlib
    import secrets
    key_id, secret = secrets.token_hex(6), secrets.token_urlsafe(32)
    monkeypatch.setattr(demo, "KEYS", {key_id: (demo.AGENT, hashlib.sha256(
        secret.encode()).hexdigest())})
    asyncio.run(demo.ensure())
    key = f"mem_{key_id}_{secret}"
    r = TestClient(billing.app).get("/billing/balance",
                                    headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 403 and "can't use payments" in r.json()["message"]
    assert billing.is_billed(demo.ACCOUNT) is False


# ---------- the MCP payment tools ----------

@pytest.fixture
def billing_url(on):
    """A Memoria server offering the payment tools, as it would with billing on."""
    import uvicorn
    from mcp.server.mcpserver import MCPServer
    mcp = MCPServer("memoria-test")
    for tool in (server.save_memory, server.find_memory, server.list_memories,
                 server.update_memory, server.share_memory, server.delete_memory):
        mcp.tool()(tool)
    server.add_billing_tools(mcp)
    port = free_port()
    web = uvicorn.Server(uvicorn.Config(server.LoginCheck(mcp.streamable_http_app(
        host="127.0.0.1")), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=web.run, daemon=True)
    thread.start()
    while not web.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    web.should_exit = True
    thread.join(timeout=10)


def test_an_agent_can_link_pay_and_check_its_balance_over_mcp(on, chain, billing_url):
    wallet = Account.create()

    async def work(mem):
        message = (await mem.wallet_challenge())["message"]
        signature = Account.sign_message(encode_defunct(text=message),
                                         wallet.key).signature.hex()
        linked = await mem.link_wallet(wallet.address, signature)
        chain.add(tx_hash(20), [transfer(wallet.address, amount=3 * UNIT)])
        paid = await mem.claim_payment(tx_hash(20))
        return linked, paid, await mem.balance()
    linked, paid, balance = run_as(on["acme/trader"], billing_url, work)
    assert linked["wallets"] == [wallet.address.lower()]
    assert paid["credits"] == 3 and paid["actions_added"] == 6
    assert balance["actions"] == 26 and balance["credits"] == 13  # get_balance itself is free


def test_payment_tools_explain_errors_over_mcp(on, billing_url):
    async def work(mem):
        with pytest.raises(Exception, match="already claimed|wasn't found yet"):
            await mem.claim_payment(tx_hash(21))
    run_as(on["acme/trader"], billing_url, work)


# ---------- public purchase history ----------

def test_purchase_history_is_private_until_turned_on(on, chain):
    wallet = link()
    claim(chain.add(tx_hash(30), [transfer(wallet.address, amount=2 * UNIT)]))
    client = TestClient(billing.app)
    assert client.get("/purchases/acme").status_code == 404
    billing.set_public_purchases("acme", True)
    r = client.get("/purchases/acme")
    assert r.status_code == 200
    [entry] = r.json()["purchases"]
    assert entry["tokens"] == "20000" and entry["credits"] == 2 and entry["actions"] == 4
    assert entry["explorer"].endswith(tx_hash(30))
    text = r.text.lower()
    assert "@" not in text and "mem_" not in text and wallet.address.lower() not in text
    billing.set_public_purchases("acme", False)
    assert client.get("/purchases/acme").status_code == 404
    assert client.get("/purchases/nobody").status_code == 404


# ---------- the HTTP endpoints for the pay page ----------

def http(key=None, origin="https://www.memoriaagent.site", ip="203.0.113.70"):
    client = TestClient(billing.app, base_url="https://memoria.example", client=(ip, 5000))
    client.headers.update({"Origin": origin})
    if key:
        client.headers.update({"Authorization": f"Bearer {key}"})
    return client


def test_config_is_public_and_says_if_billing_is_live(store, monkeypatch):
    assert http().get("/billing/config").json()["billing"] is False
    assert http().get("/billing/balance").json()["message"] == "Billing is not live yet."
    monkeypatch.setenv("MEMORIA_BILLING", "on")
    config = http().get("/billing/config").json()
    assert config["billing"] is True and config["chain_id"] == 4663
    assert config["treasury"] == TREASURY and config["tokens_per_credit"] == 10000
    assert config["public_rpc"] == "https://rpc.mainnet.chain.robinhood.com"


def test_the_rpc_url_setting_is_never_shown(on, monkeypatch):
    monkeypatch.setenv("MEMORIA_RPC_URL", "https://example.alchemy.example/v2/SECRET-KEY")
    assert "SECRET-KEY" not in http().get("/billing/config").text


def test_pay_page_endpoints_link_claim_and_show_the_balance(on, chain):
    client = http(on["acme/trader"])
    wallet = Account.create()
    message = client.post("/billing/challenge").json()["message"]
    signature = Account.sign_message(encode_defunct(text=message), wallet.key).signature.hex()
    linked = client.post("/billing/link", json={"address": wallet.address,
                                               "signature": signature})
    assert linked.status_code == 200 and linked.json()["wallets"] == [wallet.address.lower()]
    chain.add(tx_hash(40), [transfer(wallet.address)])
    paid = client.post("/billing/claim", json={"tx_hash": tx_hash(40)})
    assert paid.status_code == 200 and paid.json()["actions_added"] == 2
    again = client.post("/billing/claim", json={"tx_hash": tx_hash(40)})
    assert again.status_code == 400 and "already claimed" in again.json()["message"]
    assert client.get("/billing/balance").json()["actions"] == 22


def test_pay_page_endpoints_need_a_valid_key(on):
    r = http("mem_000000000000_wrong").get("/billing/balance")
    assert r.status_code == 401 and "API key isn't valid" in r.json()["message"]
    assert http().get("/billing/balance").status_code == 401


def test_pay_page_endpoints_only_answer_the_website(on):
    site = http(on["acme/trader"]).get("/billing/balance")
    assert site.headers["access-control-allow-origin"] == "https://www.memoriaagent.site"
    other = http(on["acme/trader"], origin="https://evil.example").get("/billing/balance")
    assert "access-control-allow-origin" not in other.headers
    pre = http().options("/billing/claim", headers={"Access-Control-Request-Method": "POST"})
    assert pre.status_code == 204 and "authorization" in pre.headers[
        "access-control-allow-headers"]


def test_pay_page_endpoints_are_rate_limited(on, monkeypatch):
    monkeypatch.setattr(limits.billing_per_ip, "limit", 3)
    client = http(on["acme/trader"], ip="203.0.113.71")
    statuses = [client.get("/billing/balance").status_code for _ in range(4)]
    assert statuses == [200, 200, 200, 429]


def test_pay_page_endpoints_check_methods_and_paths(on):
    client = http(on["acme/trader"])
    assert client.get("/billing/claim").status_code == 405
    assert client.get("/billing/nothing").status_code == 404
    assert client.post("/billing/link", content=b"[1]").status_code == 400


# ---------- manage_accounts.py ----------

def test_manage_accounts_balance_credits_and_public_history(on, chain, capsys):
    manage_accounts.main(["add-credits", "acme", "5", "--reason", "thank you"])
    assert actions() == 20 + 10
    manage_accounts.main(["remove-credits", "acme", "2"])
    assert actions() == 26
    with pytest.raises(SystemExit):
        manage_accounts.main(["remove-credits", "acme", "100"])
    assert actions() == 26
    manage_accounts.main(["public-purchases", "acme", "on"])
    manage_accounts.main(["balance", "acme"])
    out = capsys.readouterr().out
    assert "26 actions (13 credits)" in out and "public purchase history: on" in out
    logged = [e for e in (m.json.loads(line) for line in m.ACTIVITY_FILE.read_text()
                          .splitlines()) if e["action"] == "credits_adjusted"]
    assert [e["credits"] for e in logged] == [5, -2]
    assert logged[0]["reason"] == "thank you"


def test_payments_and_charges_are_in_the_activity_log(on, chain):
    wallet = link()
    claim(chain.add(tx_hash(50), [transfer(wallet.address)]))
    billing.charge("acme", "trader", "find_memory")
    actions_logged = [m.json.loads(line)["action"] for line in
                      m.ACTIVITY_FILE.read_text().splitlines()]
    for expected in ("link_wallet", "starter_credits", "payment", "charge"):
        assert expected in actions_logged


def test_new_tables_are_in_backups(on, chain, tmp_path):
    import sqlite3
    import zipfile

    import backup
    wallet = link()
    claim(chain.add(tx_hash(60), [transfer(wallet.address)]))
    made = backup.make_backup(db.DB_FILE.parent, folder=tmp_path / "backups")
    with zipfile.ZipFile(made) as zf:
        zf.extract("memory_store/memoria.db", tmp_path / "restored")
    copy = sqlite3.connect(tmp_path / "restored" / "memory_store" / "memoria.db")
    assert copy.execute("SELECT count(*) FROM payments").fetchone()[0] == 1
    assert copy.execute("SELECT count(*) FROM wallets").fetchone()[0] == 1
    assert copy.execute("SELECT actions FROM billing WHERE account = 'acme'").fetchone()[0] == 22
    copy.close()


def test_older_databases_get_the_new_tables(keys, tmp_path, monkeypatch):
    import sqlite3
    old = tmp_path / "old" / "memoria.db"
    old.parent.mkdir()
    conn = sqlite3.connect(old)
    conn.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', "
                 "email TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, "
                 "status TEXT NOT NULL DEFAULT 'active', limits TEXT NOT NULL DEFAULT '{}')")
    conn.execute("INSERT INTO accounts (id, created_at) VALUES ('acme', 'then')")
    conn.commit()
    conn.close()
    db.close()
    monkeypatch.setattr(db, "DB_FILE", old)
    tables = {r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"billing", "wallets", "wallet_challenges", "payments"} <= tables
    assert accounts.get_account("acme")["id"] == "acme"  # existing data kept


# ---------- linking a wallet by hand, after a test payment (manage_accounts.py) ----------

def proof_amount(account="acme", address=None) -> int:
    return int(db.query("SELECT amount FROM wallet_proofs WHERE account = ? AND address = ?",
                        (account, address.lower()))[0][0])


def test_hand_linking_asks_for_an_exact_test_payment_first(on, chain, capsys):
    wallet = Account.create().address
    manage_accounts.main(["link-wallet", "acme", wallet])
    out = capsys.readouterr().out
    assert "send EXACTLY" in out and "--tx" in out
    assert billing.wallets_of("acme") == []  # nothing linked yet
    amount = proof_amount(address=wallet)
    assert 10 ** 18 < amount < 2 * 10 ** 18  # between 1 and 2 $MEMORIA
    again = billing.admin_proof_request("acme", wallet)  # asking again gives the same amount
    assert again["amount"] == billing._tokens(amount)
    assert again["amount"] in out


def test_the_exact_test_payment_links_the_wallet(on, chain, capsys):
    wallet = Account.create().address
    billing.admin_proof_request("acme", wallet)
    amount = proof_amount(address=wallet)
    tx = chain.add(tx_hash(90), [transfer(wallet, amount=amount)])
    manage_accounts.main(["link-wallet", "acme", wallet, "--tx", tx])
    assert "Linked" in capsys.readouterr().out
    row = db.query("SELECT account, agent FROM wallets WHERE address = ?", (wallet.lower(),))[0]
    assert (row["account"], row["agent"]) == ("acme", "admin")
    entry = [e for e in (m.json.loads(line) for line in m.ACTIVITY_FILE.read_text()
                         .splitlines()) if e["action"] == "link_wallet"][-1]
    assert entry["by"] == "host" and entry["proven_by"] == tx and entry["linked_at"]
    # the test payment can't be claimed again, and counts toward the next credit
    with pytest.raises(billing.BillingError, match="already claimed"):
        claim(tx)
    assert billing.balance("acme")["leftover_tokens"] == billing._tokens(amount)
    # payments from it can now be claimed like any linked wallet
    assert claim(chain.add(tx_hash(91), [transfer(wallet)]))["credits"] == 1


@pytest.mark.parametrize("change, error", [
    (lambda w, a: transfer(w, amount=a + 1), "doesn't send exactly"),          # wrong amount
    (lambda w, a: transfer(STRANGER, amount=a), "doesn't send exactly"),       # wrong sender
    (lambda w, a: transfer(w, to=STRANGER, amount=a), "doesn't send exactly"),  # not treasury
    (lambda w, a: transfer(w, amount=a, token=OTHER_TOKEN), "doesn't send exactly"),
])
def test_anything_but_the_exact_test_payment_is_refused(on, chain, change, error):
    wallet = Account.create().address
    billing.admin_proof_request("acme", wallet)
    tx = chain.add(tx_hash(92), [change(wallet, proof_amount(address=wallet))])
    with pytest.raises(billing.BillingError, match=error):
        asyncio.run(billing.admin_prove_and_link("acme", wallet, tx))
    assert billing.wallets_of("acme") == []


def test_hand_linking_needs_a_request_first_and_it_expires(on, chain, monkeypatch):
    wallet = Account.create().address
    tx = chain.add(tx_hash(93), [transfer(wallet, amount=10 ** 18 + 10 ** 12)])
    with pytest.raises(billing.BillingError, match="No test payment was asked for"):
        asyncio.run(billing.admin_prove_and_link("acme", wallet, tx))
    billing.admin_proof_request("acme", wallet)
    monkeypatch.setattr(billing, "_now", lambda: "2999-01-01T00:00:00+00:00")
    with pytest.raises(billing.BillingError, match="expired"):
        asyncio.run(billing.admin_prove_and_link("acme", wallet, tx))


def test_hand_linking_follows_the_same_rules(on, chain):
    taken = link("globex")
    with pytest.raises(billing.BillingError, match="another Memoria account"):
        billing.admin_proof_request("acme", taken.address)
    with pytest.raises(SystemExit):
        manage_accounts.main(["link-wallet", "acme", "0x1234"])        # not an address
    with pytest.raises(SystemExit):
        manage_accounts.main(["link-wallet", "nobody", Account.create().address])
    assert billing.admin_proof_request("globex", taken.address)["already_linked"] is True


def test_an_old_test_payment_cant_prove_a_wallet(on, chain):
    wallet = Account.create().address
    billing.admin_proof_request("acme", wallet)
    start = billing.settings()["claim_start_block"]
    tx = chain.add(tx_hash(94), [transfer(wallet, amount=proof_amount(address=wallet))],
                   block=start - 1)
    with pytest.raises(billing.BillingError, match="before Memoria billing started"):
        asyncio.run(billing.admin_prove_and_link("acme", wallet, tx))


def test_admin_unlinks_a_wallet(on, chain, capsys):
    wallet = Account.create().address
    billing.admin_proof_request("acme", wallet)
    asyncio.run(billing.admin_prove_and_link(
        "acme", wallet, chain.add(tx_hash(95), [transfer(wallet, amount=proof_amount(
            address=wallet))])))
    with pytest.raises(SystemExit):
        manage_accounts.main(["unlink-wallet", "globex", wallet])  # not globex's to unlink
    assert billing.wallets_of("acme") == [wallet.lower()]
    manage_accounts.main(["unlink-wallet", "acme", wallet])
    assert "Unlinked" in capsys.readouterr().out and billing.wallets_of("acme") == []
    with pytest.raises(billing.BillingError, match="isn't linked to your account"):
        claim(chain.add(tx_hash(96), [transfer(wallet)]))


# ---------- what costs an action, and the "balance low" note ----------

def test_get_outcomes_costs_one_action_and_record_outcome_is_free(on, memoria_url):
    async def work(mem):
        saved = await mem.save("Token X looked safe.", reason="r", topic="crypto")  # 1
        await mem.record_outcome(saved["memory_id"], "failure", "rugged")           # free
        after_record = actions()
        await mem.outcomes(saved["memory_id"])                                       # 1
        return after_record
    assert run_as(on["acme/trader"], memoria_url, work) == 20 - 1
    assert actions() == 20 - 2


def test_free_payment_tools_use_no_actions(on, billing_url):
    async def work(mem):
        await mem.balance()
        await mem.wallet_challenge()
        return (await mem.balance())["costs"]
    costs = run_as(on["acme/trader"], billing_url, work)
    assert actions() == 20
    assert costs == {"1 action each": ["save_memory", "update_memory", "find_memory",
                                       "list_memories", "get_outcomes"],
                     "free": ["record_outcome", "share_memory", "delete_memory", "get_balance",
                              "wallet_challenge", "link_wallet", "claim_payment"]}


def test_a_low_balance_adds_a_note_to_every_result(on, memoria_url):
    billing.balance("acme")  # starter credits added
    db.execute("UPDATE billing SET actions = 6 WHERE account = 'acme'")

    async def work(mem):
        first = await mem.save("Acme renews March 1.", reason="r", topic="customers")  # 5 left
        second = await mem.save("Acme pays in euros.", reason="r", topic="customers")  # 4 left
        found = await mem.find("Acme", topic="customers")                              # 3 left
        none_found = await mem.find("zebra")                                           # 2 left
        shared = await mem.share(second["memory_id"], "shared", ["analyst"])          # free
        return first, second, found, none_found, shared
    first, second, found, none_found, shared = run_as(on["acme/trader"], memoria_url, work)
    assert "balance_note" not in first  # 5 left: not low yet
    assert second["balance_note"] == ("Balance low: 4 actions left. Buy credits at "
                                       "https://www.memoriaagent.site/pay.")
    assert found[0]["balance_note"].startswith("Balance low: 3 actions left")
    assert all("balance_note" not in x for x in found[1:])  # once, on the first memory
    assert none_found == []  # an empty list has nowhere to put it
    assert shared["balance_note"].startswith("Balance low: 2 actions left")  # free tools too


def test_one_action_left_is_said_in_the_singular(on):
    billing.balance("acme")
    db.execute("UPDATE billing SET actions = 1 WHERE account = 'acme'")
    assert billing.low_balance_note("acme").startswith("Balance low: 1 action left.")


def test_no_balance_note_when_billing_is_off_or_never_charged(store, devs, monkeypatch):
    assert billing.low_balance_note("acme") is None  # billing off
    monkeypatch.setenv("MEMORIA_BILLING", "on")
    assert billing.low_balance_note("acme") is None  # starter credits not added yet
    assert billing.low_balance_note("local") is None
    assert billing.low_balance_note(demo.ACCOUNT) is None


def test_share_reminds_who_pays_when_billing_is_on(on, memoria_url):
    async def work(mem):
        saved = await mem.save("Acme renews March 1.", reason="r", topic="customers")
        shared = await mem.share(saved["memory_id"], "shared", ["analyst"])
        private = await mem.share(saved["memory_id"], "private")
        return shared, private
    shared, private = run_as(on["acme/trader"], memoria_url, work)
    assert "spend their own account's actions" in shared["billing_note"]
    assert "billing_note" not in private  # shared with nobody


def test_share_has_no_billing_reminder_when_billing_is_off(store, devs, memoria_url):
    async def work(mem):
        saved = await mem.save("Acme renews March 1.", reason="r", topic="customers")
        return await mem.share(saved["memory_id"], "shared", ["analyst"])
    shared = run_as(devs["acme/trader"], memoria_url, work)
    assert "billing_note" not in shared and "balance_note" not in shared


# ---------- emailing the owner when the balance runs low ----------

@pytest.fixture
def mailbox(on, monkeypatch):
    """A fake Resend: emails land in a list (or fail, when told to)."""
    import signup
    monkeypatch.setenv("RESEND_API_KEY", "test-resend-key")
    box = {"sent": [], "fail": False}

    async def send_email(to, subject, text, html):
        if box["fail"]:
            raise RuntimeError("pretend Resend is down")
        box["sent"].append({"to": to, "subject": subject, "text": text})
    monkeypatch.setattr(signup, "send_email", send_email)
    return box


def set_actions(n, account="acme"):
    billing.balance(account)  # starter credits added first
    db.execute("UPDATE billing SET actions = ? WHERE account = ?", (n, account))


def spend(times, account="acme"):
    for _ in range(times):
        billing.charge(account, "trader", "find_memory")


def emails_logged(account="acme"):
    return [e for e in (m.json.loads(line) for line in m.ACTIVITY_FILE.read_text().splitlines())
            if e["action"].endswith("_balance_email") and e["agent"].startswith(account + "/")]


def test_low_and_empty_emails_go_out_once_each(mailbox):
    set_actions(6)
    spend(1)  # 5 left: not low yet
    assert mailbox["sent"] == []
    spend(1)  # 4 left: low
    assert [e["text"] for e in mailbox["sent"]] == [
        "Your Memoria account acme has 4 actions left. Top up at "
        "https://www.memoriaagent.site/pay."]
    assert mailbox["sent"][0]["to"] == "dev@acme.example"
    assert mailbox["sent"][0]["subject"] == "Your Memoria account is low on actions"
    spend(3)  # 3, 2, 1 left: no more low emails
    assert len(mailbox["sent"]) == 1
    spend(1)  # 0 left: out of actions
    assert mailbox["sent"][-1]["text"] == (
        "Your Memoria account acme is out of actions. Your agents can't save or find memories "
        "until you top up at https://www.memoriaagent.site/pay.")
    with pytest.raises(billing.BillingError):
        spend(1)  # refused at 0: no more emails
    assert len(mailbox["sent"]) == 2
    assert [e["sent"] for e in emails_logged()] == [True, True]


def test_a_payment_resets_the_alerts(mailbox, chain):
    set_actions(5)
    spend(5)  # low at 4, empty at 0
    assert len(mailbox["sent"]) == 2
    wallet = link()
    claim(chain.add(tx_hash(80), [transfer(wallet.address, amount=3 * UNIT)]))  # +6 actions
    spend(2)  # 4 left: a new drop, so a new low email
    assert len(mailbox["sent"]) == 3 and "4 actions left" in mailbox["sent"][-1]["text"]


def test_adding_credits_by_hand_resets_and_removing_them_can_alert(mailbox):
    set_actions(5)
    spend(1)  # 4 left: low email
    manage_accounts.main(["add-credits", "acme", "1"])  # 6 left, alerts reset
    spend(2)  # 4 left again: low email again
    assert len(mailbox["sent"]) == 2
    manage_accounts.main(["remove-credits", "acme", "2"])  # 0 left: out of actions email
    assert mailbox["sent"][-1]["subject"] == "Your Memoria account is out of actions"
    assert len(mailbox["sent"]) == 3


def test_a_refund_is_not_a_top_up(mailbox):
    set_actions(5)
    spend(1)  # 4 left: low email
    billing.refund("acme", "trader", "find_memory")  # 5 again, but not a top-up
    spend(1)  # 4 left: no second email
    assert len(mailbox["sent"]) == 1


def test_the_low_email_says_one_action_in_the_singular(on):
    assert billing.alert_text("acme", "low", 1)[1].startswith(
        "Your Memoria account acme has 1 action left.")


def test_accounts_without_an_email_get_none(mailbox):
    accounts.create_account("noemail")
    accounts.create_key("noemail", "bot")
    set_actions(1, "noemail")
    spend(1, "noemail")
    assert mailbox["sent"] == []
    assert emails_logged("noemail")[-1]["reason"] == "the account has no email"


def test_demo_and_local_get_no_emails(mailbox):
    billing._balance_alerts(demo.ACCOUNT, 0)
    billing._balance_alerts("local", 0)
    assert mailbox["sent"] == []


def test_without_email_set_up_nothing_is_sent_but_it_is_logged(mailbox, monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY")
    set_actions(5)
    spend(1)
    assert mailbox["sent"] == []
    assert emails_logged()[-1]["reason"] == "email isn't set up"


def test_a_failed_email_never_breaks_the_tool_call(mailbox, on, memoria_url):
    mailbox["fail"] = True
    set_actions(5)

    async def work(mem):
        saved = await mem.save("Acme renews March 1.", reason="r", topic="customers")  # 4
        await asyncio.sleep(0.3)  # let the background email try (and fail)
        return saved
    saved = run_as(on["acme/trader"], memoria_url, work)
    assert saved["memory_id"] and actions() == 4
    logged = emails_logged()
    assert logged and logged[-1]["sent"] is False and "Resend is down" in logged[-1]["reason"]
    mailbox["fail"] = False
    spend(1)  # 3 left: the low alert counted as sent, so no repeat after the failure
    assert mailbox["sent"] == []


def test_the_email_is_sent_from_the_server_without_waiting(mailbox, on, memoria_url):
    set_actions(5)

    async def work(mem):
        await mem.find("anything")  # 4 left
        for _ in range(40):
            if mailbox["sent"]:
                break
            await asyncio.sleep(0.05)
    run_as(on["acme/trader"], memoria_url, work)
    assert len(mailbox["sent"]) == 1 and "4 actions left" in mailbox["sent"][0]["text"]


def test_older_databases_get_the_alert_flags(keys, tmp_path, monkeypatch):
    import sqlite3
    old = tmp_path / "old" / "memoria.db"
    old.parent.mkdir()
    conn = sqlite3.connect(old)
    conn.execute("CREATE TABLE billing (account TEXT PRIMARY KEY, actions INTEGER NOT NULL "
                 "DEFAULT 0, starter_given INTEGER NOT NULL DEFAULT 0, public_purchases INTEGER "
                 "NOT NULL DEFAULT 0, updated_at TEXT)")
    conn.execute("INSERT INTO billing (account, actions) VALUES ('acme', 7)")
    conn.commit()
    conn.close()
    db.close()
    monkeypatch.setattr(db, "DB_FILE", old)
    row = db.query("SELECT actions, low_alert_sent, empty_alert_sent FROM billing")[0]
    assert tuple(row) == (7, 0, 0)
