"""Visitors using www.memoriaagent.site/mcp (forwarded by Vercel) get their own limits."""
import asyncio

import pytest

import limits
import server

SECRET = "test-proxy-secret-1234567890"
VERCEL = "18.199.163.242"   # what Railway sees for every website visitor


def scope(visitor_ip=None, secret=None, key="mem_000000000000_wrong"):
    headers = [(b"authorization", f"Bearer {key}".encode()),
               (b"x-forwarded-for", f"{visitor_ip or '1.1.1.1'}, {VERCEL}".encode())]
    if secret is not None:
        headers += [(b"x-memoria-proxy-secret", secret.encode()),
                    (b"x-memoria-client-ip", (visitor_ip or "").encode())]
    return {"type": "http", "path": "/mcp", "method": "POST", "headers": headers,
            "client": ("10.0.0.1", 5000)}


@pytest.fixture
def online(monkeypatch, store):
    monkeypatch.setenv("MEMORIA_TRUST_PROXY", "1")  # as on Railway
    monkeypatch.setenv("MEMORIA_PROXY_SECRET", SECRET)


def status(sc):
    seen, sent = {}, []

    async def inner_app(s, receive, send):
        seen["headers"] = dict(s["headers"])

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)
    asyncio.run(server.LoginCheck(inner_app)(sc, receive, send))
    return (sent[0]["status"] if sent else 200), seen.get("headers")


def test_the_real_address_is_used_when_the_secret_matches(online):
    assert limits.client_ip(scope("203.0.113.9", SECRET)) == "203.0.113.9"


@pytest.mark.parametrize("secret, ip, env", [
    ("wrong-secret", "203.0.113.9", SECRET),   # someone guessing the secret
    ("", "203.0.113.9", SECRET),                 # no secret sent
    (SECRET, "203.0.113.9", ""),                 # the server has no secret set
    (SECRET, "not-an-ip", SECRET),               # nonsense address
])
def test_otherwise_the_proxys_address_is_used_as_before(online, monkeypatch, secret, ip, env):
    monkeypatch.setenv("MEMORIA_PROXY_SECRET", env)
    assert limits.client_ip(scope(ip, secret)) == VERCEL


def test_one_visitors_wrong_keys_dont_lock_out_other_visitors(online):
    for _ in range(limits.failed_logins.limit):
        assert status(scope("203.0.113.9", SECRET))[0] == 401   # wrong key each time
    assert status(scope("203.0.113.9", SECRET))[0] == 429      # that visitor is now locked out
    assert status(scope("198.51.100.7", SECRET))[0] == 401     # another visitor is not


def test_without_the_secret_everyone_shares_the_lockout(online, monkeypatch):
    """What happened before: every website visitor looked like Vercel."""
    monkeypatch.setenv("MEMORIA_PROXY_SECRET", "")
    for _ in range(limits.failed_logins.limit):
        status(scope("203.0.113.9"))
    assert status(scope("198.51.100.7"))[0] == 429


def test_the_secret_is_not_passed_on(online, devs):
    code, seen = status(scope("203.0.113.9", SECRET, key=devs["acme/trader"]))
    assert code == 200
    assert b"x-memoria-proxy-secret" not in seen and b"x-memoria-client-ip" not in seen
