"""Hosting online: each developer's memories kept apart, and spam protection."""
import asyncio
import json
import os
import re

import pytest
from starlette.testclient import TestClient

import accounts
import dashboard
import identity
import limits
import memoria as m
import memoria_bridge
import server
import signup
import db
from conftest import everything_stored, index, save_index, settle
from memoria_client import Memoria
from mcp import types


def run(coro):
    return asyncio.run(coro)


def save(account, agent="trader", note="Penguins huddle to survive the winter.", **kw):
    kw.setdefault("reason", "test")
    kw.setdefault("topic", "test/animals")
    return run(m.save_memory(agent, note, account=account, **kw))


def found(account, agent="trader", query="penguins winter"):
    async def go():
        await settle()
        return [x["memory_id"] for x in await m.find_memory(agent, query, account=account)]
    return run(go())


# ---------- each developer's memories are kept apart ----------

def test_developers_never_see_each_others_memories(store, devs):
    acme = save("acme")["memory_id"]
    globex = save("globex")["memory_id"]
    local = save("local", agent="alice")["memory_id"]
    assert found("acme") == [acme]
    assert found("globex") == [globex]  # same agent name, different account
    assert found("local", agent="alice") == [local]


def test_sharing_only_reaches_agents_of_the_same_account(store, devs):
    mid = save("acme", visibility="shared", shared_with=["analyst"])["memory_id"]
    assert found("acme", agent="analyst") == [mid]
    assert found("globex", agent="analyst") == []  # globex's "analyst" is someone else


def test_other_accounts_cant_change_or_delete_a_memory(store, devs):
    mid = save("acme")["memory_id"]

    async def scenario():
        for action in (m.update_memory("trader", mid, "hacked", "r", account="globex"),
                       m.share_memory("trader", mid, "private", account="globex"),
                       m.delete_memory("trader", mid, account="globex"),
                       m.delete_memory("trader", mid)):  # nor this laptop's "trader"
            with pytest.raises(m.MemoriaError, match="No memory with ID"):
                await action
    run(scenario())


def test_each_account_has_its_own_cognee_dataset(store, devs):
    save("acme")
    save("globex")
    found("acme")
    assert {doc["dataset"] for doc in store.docs.values()} == {"memoria-acme", "memoria-globex"}
    assert store.searched[-1] == ["memoria-acme"]  # acme's search never looked elsewhere


def test_this_laptops_memories_keep_their_dataset(store):
    save("local", agent="alice")
    found("local", agent="alice")
    assert [doc["dataset"] for doc in store.docs.values()] == ["memoria"]


def test_developer_memories_are_signed_by_account_and_agent(store, devs):
    mem = save("acme")
    assert mem["account"] == "acme" and mem["signed_by"] == "acme/trader"
    assert mem["verification"] == "verified"


def test_moving_a_memory_to_another_account_breaks_its_signature(store, devs):
    mid = save("acme")["memory_id"]
    data = index()
    data[mid]["account"] = "globex"  # someone edits the database to hand it to globex
    save_index(data)
    shown = m.all_memories()[0]
    assert shown["account"] == "globex" and shown["verification"] == "NOT verified"


def test_older_memories_count_as_this_laptops(store):
    mid = save("local", agent="alice")["memory_id"]
    data = index()
    del data[mid]["account"]  # saved before accounts existed
    save_index(data)
    assert m.all_memories()[0]["account"] == "local"
    assert m.all_memories()[0]["verification"] == "verified"
    assert found("local", agent="alice") == [mid]


# ---------- spam protection: daily writes, total memories, sizes ----------

def test_daily_writes_are_limited(store, devs):
    accounts.set_limit("acme", "writes_per_day", 2)
    mid = save("acme")["memory_id"]
    run(m.update_memory("trader", mid, "Penguins huddle in groups.", "r", account="acme"))
    with pytest.raises(m.MemoriaError, match="used its 2 saves and updates for today"):
        save("acme")
    save("globex")  # other accounts aren't affected


def test_writes_reset_the_next_day(store, devs):
    accounts.set_limit("acme", "writes_per_day", 1)
    save("acme")
    db.execute("UPDATE usage SET day = '2000-01-01' WHERE account = 'acme'")
    save("acme")  # a new day: allowed again


def test_total_memories_are_limited(store, devs):
    accounts.set_limit("acme", "max_memories", 1)
    mid = save("acme")["memory_id"]
    with pytest.raises(m.MemoriaError, match="limit of 1 memories"):
        save("acme")
    run(m.delete_memory("trader", mid, account="acme"))
    save("acme")  # room again after deleting


def test_this_laptops_agents_have_no_write_limits(store):
    for _ in range(5):
        save("local", agent="alice")
    assert db.query("SELECT count(*) FROM usage")[0][0] == 0


@pytest.mark.parametrize("kw, error", [
    ({"note": "x" * 4001}, "note is too long"),
    ({"reason": "x" * 2001}, "reason is too long"),
    ({"topic": "x" * 101}, "topic is too long"),
    ({"visibility": "shared", "shared_with": [f"agent-{i}" for i in range(21)]}, "20 agents"),
])
def test_oversized_input_is_refused(store, devs, kw, error):
    with pytest.raises(m.MemoriaError, match=error):
        save("acme", **kw)


def test_oversized_question_is_refused(store, devs):
    with pytest.raises(m.MemoriaError, match="query is too long"):
        run(m.find_memory("trader", "x" * 1001, account="acme"))


# ---------- the login check in front of the server ----------

def call(headers, ip="203.0.113.7", body_chunks=None):
    """Send one request through LoginCheck from an internet address."""
    seen, sent = {}, []

    async def inner_app(scope, receive, send):
        seen["headers"] = dict(scope["headers"])
        seen["raw"] = list(scope["headers"])  # every header, repeats included
        seen["messages"] = [await receive() for _ in range(len(body_chunks or [b""]))]

    chunks = list(body_chunks or [b""])

    async def receive():
        chunk = chunks.pop(0) if chunks else b""
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": "/mcp", "method": "POST", "headers": headers,
             "client": (ip, 5000)}
    asyncio.run(server.LoginCheck(inner_app)(scope, receive, send))
    status = sent[0]["status"] if sent else None
    call.raw = seen.get("raw")
    return status, seen.get("headers"), sent, seen.get("messages")


def bearer(key):
    return [(b"authorization", f"Bearer {key}".encode())]


def test_api_key_logs_in_as_its_account_and_agent(devs):
    status, headers, _, _ = call(bearer(devs["acme/trader"]))
    assert status is None
    assert headers[b"x-memoria-account"] == b"acme" and headers[b"x-memoria-agent"] == b"trader"


def test_claimed_account_or_agent_headers_are_thrown_away(devs):
    _, headers, _, _ = call(bearer(devs["acme/trader"]) + [(b"x-memoria-account", b"globex"),
                                                          (b"X-Memoria-Agent", b"analyst")])
    pairs = [(k.lower(), v) for k, v in call.raw]  # all of them, not just the last of each
    assert [v for k, v in pairs if k == b"x-memoria-account"] == [b"acme"]
    assert [v for k, v in pairs if k == b"x-memoria-agent"] == [b"trader"]


def test_this_laptops_login_passes_still_work(keys):
    status, headers, _, _ = call(bearer(identity.make_token("alice")), ip="127.0.0.1")
    assert status is None and headers[b"x-memoria-account"] == b"local"


def test_each_agent_is_limited_per_minute(devs):
    accounts.set_limit("acme", "requests_per_minute", 3)
    statuses = [call(bearer(devs["acme/trader"]))[0] for _ in range(4)]
    assert statuses == [None, None, None, 429]
    _, _, sent, _ = call(bearer(devs["acme/trader"]))
    assert dict(sent[0]["headers"])[b"retry-after"]
    assert call(bearer(devs["acme/analyst"]))[0] is None  # other agents unaffected


def test_each_account_is_limited_per_minute_across_its_agents(devs):
    accounts.set_limit("acme", "account_requests_per_minute", 3)
    statuses = [call(bearer(devs["acme/trader"]))[0], call(bearer(devs["acme/trader"]))[0],
                call(bearer(devs["acme/analyst"]))[0], call(bearer(devs["acme/analyst"]))[0]]
    assert statuses == [None, None, None, 429]  # 3 between them, though each agent is under 60
    _, _, sent, _ = call(bearer(devs["acme/trader"]))
    assert dict(sent[0]["headers"])[b"retry-after"]
    assert b"account's agents" in sent[1]["body"]
    assert call(bearer(devs["globex/trader"]))[0] is None  # other accounts unaffected


def test_refused_requests_dont_use_up_the_other_limit(devs):
    accounts.set_limit("acme", "requests_per_minute", 2)
    accounts.set_limit("acme", "account_requests_per_minute", 3)
    trader = [call(bearer(devs["acme/trader"]))[0] for _ in range(4)]
    assert trader == [None, None, 429, 429]  # the trader's own limit
    # The trader's refused requests weren't counted for the account: 1 more is still allowed.
    analyst = [call(bearer(devs["acme/analyst"]))[0] for _ in range(2)]
    assert analyst == [None, 429]


def test_account_limit_has_a_default_and_can_be_changed(devs, capsys):
    import manage_accounts
    assert accounts.limits("acme")["account_requests_per_minute"] == 120
    manage_accounts.main(["set-limit", "acme", "account_requests_per_minute", "500"])
    assert accounts.limits("acme")["account_requests_per_minute"] == 500
    manage_accounts.main(["list"])
    assert "500 per account" in capsys.readouterr().out


def test_this_laptops_agents_have_no_account_limit(keys):
    token = identity.make_token("alice")
    assert all(call(bearer(token), ip="127.0.0.1")[0] is None for _ in range(130))


def test_each_address_is_limited_per_minute(devs, monkeypatch):
    monkeypatch.setattr(limits.per_ip, "limit", 5)
    statuses = [call(bearer(devs["acme/trader"]), ip="198.51.100.1")[0] for _ in range(6)]
    assert statuses[-1] == 429 and statuses[:5] == [None] * 5
    assert call(bearer(devs["acme/trader"]), ip="198.51.100.2")[0] is None


def test_requests_from_this_computer_skip_address_limits(keys, monkeypatch):
    monkeypatch.setattr(limits.per_ip, "limit", 2)
    token = identity.make_token("alice")
    assert [call(bearer(token), ip="127.0.0.1")[0] for _ in range(5)] == [None] * 5


def test_guessing_keys_gets_an_address_locked_out(devs):
    for _ in range(limits.failed_logins.limit):
        assert call(bearer("mem_abcdef123456_wrong"))[0] == 401
    # Now even a correct key from that address waits, so guessing is pointless.
    assert call(bearer(devs["acme/trader"]))[0] == 429
    assert call(bearer(devs["acme/trader"]), ip="203.0.113.99")[0] is None


def test_requests_that_are_too_big_are_refused(devs):
    status, _, _, _ = call(bearer(devs["acme/trader"]) + [(b"content-length", b"999999")])
    assert status == 413
    assert call(bearer(devs["acme/trader"]) + [(b"content-length", b"lots")])[0] == 413


def test_bodies_sent_in_pieces_are_cut_off_when_too_big(devs):
    piece = b"x" * 40_000
    _, _, _, messages = call(bearer(devs["acme/trader"]), body_chunks=[piece, piece])
    assert messages[0]["type"] == "http.request" and messages[1]["type"] == "http.disconnect"


def test_behind_a_proxy_the_real_address_is_used(devs, monkeypatch):
    monkeypatch.setenv("MEMORIA_TRUST_PROXY", "1")
    scope = {"client": ("127.0.0.1", 1),
             "headers": [(b"x-forwarded-for", b"6.6.6.6, 198.51.100.4")]}
    assert limits.client_ip(scope) == "198.51.100.4"  # the part the proxy added
    assert not limits.is_this_computer("127.0.0.1")  # the proxy itself gets no exemption


def test_forwarded_header_is_ignored_without_a_proxy(devs):
    scope = {"client": ("203.0.113.7", 1), "headers": [(b"x-forwarded-for", b"127.0.0.1")]}
    assert limits.client_ip(scope) == "203.0.113.7"  # can't pretend to be this computer


# ---------- sign-up ----------

@pytest.fixture
def signups(keys, monkeypatch):
    """Sign-up with a fake Turnstile (token "human" passes) and a fake mailbox."""
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "test-turnstile-secret")
    monkeypatch.setenv("RESEND_API_KEY", "test-resend-key")
    sent = []

    async def turnstile(token, ip):
        return token == "human"

    async def send_email(to, subject, text, html):
        if to.startswith("bounce@"):
            raise RuntimeError("pretend the email service is down")
        sent.append({"to": to, "subject": subject, "text": text, "html": html})
    monkeypatch.setattr(signup, "check_turnstile", turnstile)
    monkeypatch.setattr(signup, "send_email", send_email)

    def client(mode, ip="203.0.113.50"):
        monkeypatch.setenv("MEMORIA_SIGNUP", mode)
        c = TestClient(signup.app, base_url="https://memoria.example", client=(ip, 5000))
        c.sent = sent
        return c
    return client


def ask(client, **data):
    return client.post("/signup", json={"email": "dev@new.example", "turnstile": "human",
                                        **data})


def link_token(client) -> str:
    return re.search(r"verify\.html\?token=([\w-]+)", client.sent[-1]["text"]).group(1)


def verify(client, token):
    return client.post("/signup/verify", json={"token": token})


def test_signup_is_off_unless_turned_on(signups):
    r = ask(signups("off"), account="newco")
    assert r.status_code == 404 and accounts.get_account("newco") is None


def test_signup_emails_a_link_and_the_key_comes_only_after_it_is_clicked(signups):
    client = signups("open")
    r = ask(client, account="newco")
    assert r.status_code == 202 and "api_key" not in r.json()
    assert accounts.get_account("newco") is None  # nothing made before the link is clicked
    email = client.sent[-1]
    assert email["to"] == "dev@new.example" and "newco" in email["text"]
    assert "https://www.memoriaagent.site/verify.html?token=" in email["text"]
    token = link_token(client)
    assert token not in everything_stored()  # only a hash of the link's secret is kept

    done = verify(client, token)
    assert done.status_code == 201
    assert accounts.check_key(done.json()["api_key"]) == ("newco", "main")
    assert accounts.get_account("newco")["email"] == "dev@new.example"
    assert accounts.limits("newco") == accounts.DEFAULT_LIMITS  # the normal limits

    again = verify(client, token)  # the key is shown only once
    assert again.status_code == 400 and "already been used" in again.json()["message"]
    assert "api_key" not in again.json()


def test_new_key_can_save_find_and_share(signups):
    client = signups("open")
    ask(client, account="newco")
    account, agent = accounts.check_key(verify(client, link_token(client)).json()["api_key"])

    async def use_it():
        saved = await m.save_memory(agent, "Renewal is March 1.", "email", "acme",
                                          account=account)
        await m.share_memory(agent, saved["memory_id"], "shared", ["other"],
                                   account=account)
        return await m.find_memory(agent, "renewal", topic="acme", account=account)
    assert [m["note"] for m in asyncio.run(use_it())] == ["Renewal is March 1."]


def test_bad_or_expired_links_are_refused(signups, monkeypatch):
    client = signups("open")
    assert "isn't valid" in verify(client, "made-up").json()["message"]
    ask(client, account="newco")
    token = link_token(client)
    monkeypatch.setattr(accounts, "_now", lambda: "2999-01-01T00:00:00+00:00")
    r = verify(client, token)
    assert r.status_code == 400 and "expired" in r.json()["message"]


def test_signup_needs_the_human_check(signups):
    r = ask(signups("open"), account="newco", turnstile="bot")
    assert r.status_code == 400 and "human" in r.json()["message"]
    r = ask(signups("open", ip="203.0.113.52"), account="newco", turnstile="")
    assert r.status_code == 400
    assert signups("open").sent == [] and accounts.get_account("newco") is None


def test_signup_refuses_to_run_without_its_settings(signups, monkeypatch):
    monkeypatch.delenv("TURNSTILE_SECRET_KEY")
    r = ask(signups("open"), account="newco")
    assert r.status_code == 503 and "isn't set up" in r.json()["message"]


@pytest.mark.parametrize("data, error", [
    ({"account": "newco", "email": "not-an-email"}, "valid email"),
    ({"account": "Not Valid!"}, "lowercase"),
    ({"account": "local"}, "reserved"),
])
def test_signup_checks_what_it_is_given(signups, data, error):
    r = ask(signups("open"), **data)
    assert r.status_code == 400 and error in r.json()["message"]


def test_signup_refuses_taken_names_even_while_waiting(signups):
    client = signups("open")
    ask(client, account="newco")
    r = ask(signups("open", ip="203.0.113.53"), account="newco", email="other@new.example")
    assert r.status_code == 400 and "already taken" in r.json()["message"]


def test_one_account_per_email(signups):
    client = signups("open")
    ask(client, account="newco")
    r = ask(client, account="second", email="DEV@new.example")
    assert r.status_code == 400 and "already has an account" in r.json()["message"]
    verify(client, link_token(client))
    r = ask(signups("open", ip="203.0.113.54"), account="third")
    assert r.status_code == 400 and "already has an account" in r.json()["message"]


def test_failed_email_frees_the_name_and_uses_no_quota(signups):
    client = signups("open")
    r = ask(client, account="newco", email="bounce@new.example")
    assert r.status_code == 503 and "couldn't send" in r.json()["message"]
    assert [ask(client, account="newco", email=f"d{i}@new.example").status_code
            for i in range(3)][0] == 202


def test_invite_signup_needs_a_fresh_code(signups):
    client = signups("invite")
    assert ask(client, account="newco").status_code == 400
    code = accounts.create_invite()
    assert ask(client, account="newco", invite=code).status_code == 202
    other = signups("invite", ip="203.0.113.51")
    again = ask(other, account="other", email="o@new.example", invite=code)
    assert again.status_code == 400 and "already been used" in again.json()["message"]


def test_signups_from_one_address_are_limited_per_day(signups):
    client = signups("open")
    results = [ask(client, account=f"co-{i}", email=f"d{i}@new.example").status_code
               for i in range(4)]
    assert results == [202, 202, 202, 429]
    assert limits.signups_per_ip.seconds == 86400
    assert ask(signups("open", ip="203.0.113.60"), account="co-9",
               email="d9@new.example").status_code == 202


def test_signups_per_day_are_limited_for_everyone(signups, monkeypatch):
    monkeypatch.setattr(limits.signups_total, "limit", 2)
    statuses = [ask(signups("open", ip=f"203.0.113.{70 + i}"), account=f"co-{i}",
                    email=f"d{i}@new.example").status_code for i in range(3)]
    assert statuses == [202, 202, 429]


def test_link_without_server_secret_fails_clearly_and_can_be_used_later(signups, monkeypatch):
    client = signups("open")
    ask(client, account="newco")
    token = link_token(client)
    secret = os.environ["MEMORIA_SECRET_KEY"]
    monkeypatch.delenv("MEMORIA_SECRET_KEY")
    r = verify(client, token)
    assert r.status_code == 503 and "isn't set up" in r.json()["message"]
    monkeypatch.setenv("MEMORIA_SECRET_KEY", secret)
    assert verify(client, token).status_code == 201


def test_only_the_website_may_call_signup_from_a_browser(signups):
    client = signups("open")
    site = {"Origin": "https://www.memoriaagent.site"}
    pre = client.options("/signup", headers={**site, "Access-Control-Request-Method": "POST"})
    assert pre.status_code == 204
    assert pre.headers["access-control-allow-origin"] == "https://www.memoriaagent.site"
    r = client.post("/signup", json={}, headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_signup_refuses_huge_or_broken_requests(signups):
    client = signups("open")
    assert client.post("/signup", content=b"x" * 5000).status_code == 400
    assert client.post("/signup", content=b"[1]").status_code == 400
    assert client.get("/signup").status_code == 405


# ---------- the dashboard must stay on the host computer ----------

def test_dashboard_refuses_requests_through_a_proxy(store):
    with TestClient(dashboard.app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/dashboard/api/memories").status_code == 200
        for header in ("x-forwarded-for", "forwarded", "x-real-ip"):
            r = client.get("/dashboard/api/memories", headers={header: "198.51.100.9"})
            assert r.status_code == 403


def test_dashboard_can_be_turned_off(monkeypatch):
    assert dashboard.handles("/dashboard")
    monkeypatch.setenv("MEMORIA_DASHBOARD", "off")
    assert not dashboard.handles("/dashboard") and not dashboard.handles("/")


def test_dashboard_changes_developer_memories_as_their_author(store, devs):
    mid = save("acme")["memory_id"]
    headers = {"origin": "http://127.0.0.1:8765", "sec-fetch-site": "same-origin"}
    with TestClient(dashboard.app, base_url="http://127.0.0.1:8765") as client:
        r = client.post(f"/dashboard/api/memories/{mid}/sharing", headers=headers,
                        json={"visibility": "shared", "shared_with": ["analyst"]})
        assert r.status_code == 200 and r.json()["account"] == "acme"
        assert client.post(f"/dashboard/api/memories/{mid}/delete",
                           headers=headers).status_code == 200
    assert m.all_memories() == []


# ---------- end to end: a developer's agent over the network ----------

def test_developer_agents_use_memoria_with_api_keys(memoria_url, devs):
    async def scenario():
        async with Memoria(api_key=devs["acme/trader"], url=memoria_url) as mem:
            saved = await mem.save("Octopuses have three hearts.", reason="test", topic="t/sea")
            assert saved["account"] == "acme" and saved["signed_by"] == "acme/trader"
            assert [x["memory_id"] for x in await mem.find("octopuses")] == [saved["memory_id"]]
        async with Memoria(api_key=devs["globex/trader"], url=memoria_url) as mem:
            assert await mem.find("octopuses") == []
            with pytest.raises(RuntimeError, match="No memory with ID"):
                await mem.delete(saved["memory_id"])
        with pytest.raises(Exception):
            async with Memoria(api_key="mem_abcdef123456_wrong", url=memoria_url) as mem:
                await mem.find("octopuses")
    run(scenario())


def test_claiming_another_account_over_the_network_does_nothing(memoria_url, devs):
    """An agent with acme's key that also claims to be globex still only acts as acme."""
    import memoria_client

    class Claim(memoria_client.ApiKey):
        def auth_flow(self, request):
            request.headers["X-Memoria-Account"] = "globex"
            request.headers["X-Memoria-Agent"] = "analyst"
            yield from super().auth_flow(request)

    async def scenario():
        async with memoria_client.session(url=memoria_url, auth=Claim(devs["acme/trader"])) as s:
            result = await s.call_tool("save_memory", {"note": "Mine.", "reason": "r", "topic": "t"})
            saved = memoria_client._result(result)
        assert saved["account"] == "acme" and saved["saved_by"] == "trader"
    run(scenario())


def test_bridge_works_with_an_api_key(memoria_url, devs):
    list_tools, call_tool = memoria_bridge.make_handlers(None, memoria_url,
                                                         api_key=devs["globex/analyst"])

    async def scenario():
        assert len((await list_tools(None, None)).tools) == 5
        result = await call_tool(None, types.CallToolRequestParams(
            name="save_memory", arguments={"note": "Owls hunt.", "reason": "r", "topic": "t"}))
        assert not result.is_error and '"account":"globex"' in result.content[0].text.replace(" ", "")
    run(scenario())
