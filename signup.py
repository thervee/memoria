"""Developer sign-up: POST /signup creates an account and its first API key.

OFF unless the host turns it on in .env:
  MEMORIA_SIGNUP=off      (default) nobody can sign up; the host creates accounts
                          with manage_accounts.py
  MEMORIA_SIGNUP=invite   developers need an invite code from the host
                          (manage_accounts.py invite); each code works once
  MEMORIA_SIGNUP=open     anyone can sign up

Spam protection: each internet address may try 3 times an hour, and at most 50
accounts are made per day in total (MEMORIA_SIGNUPS_PER_IP / MEMORIA_SIGNUPS_PER_DAY).
New accounts get the normal limits (accounts.DEFAULT_LIMITS).

The request:   {"account": "acme", "email": "dev@acme.example", "agent": "main",
                "invite": "inv_..."}            (agent is optional; invite only if needed)
The answer:    {"account": "acme", "agent": "main", "api_key": "mem_..."}
The API key is shown only this once.
"""
import json
import re

import accounts
import identity
import limits

EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[a-z]{2,}$", re.IGNORECASE)
MAX_BODY = 2_000


def handles(path: str) -> bool:
    return path == "/signup"


async def _answer(send, status: int, data: dict, headers=()) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), *headers]})
    await send({"type": "http.response.body", "body": json.dumps(data).encode()})


async def _read_body(receive) -> bytes:
    body = b""
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        body += message.get("body", b"")
        if len(body) > MAX_BODY:
            raise ValueError("Request too large.")
        if not message.get("more_body"):
            break
    return body


def _error(message: str) -> dict:
    return {"error": True, "message": message}


async def app(scope, receive, send) -> None:
    mode = accounts.signup_mode()
    if mode == "off":
        return await _answer(send, 404, _error("Sign-up is closed. Ask the host for an account."))
    if scope["method"] != "POST":
        return await _answer(send, 405, _error("Send a POST request with JSON."))

    # Spam protection: a few tries per address, and a daily total for everyone.
    ip = limits.client_ip(scope)
    wait = limits.signups_per_ip.allow(ip) or limits.signups_total.blocked_for("everyone")
    if wait:
        seconds = max(1, round(wait))
        return await _answer(send, 429, _error(f"Too many sign-ups. Try again in {seconds} "
                                               "seconds."),
                             [(b"retry-after", str(seconds).encode())])

    try:
        data = json.loads(await _read_body(receive) or b"{}")
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        return await _answer(send, 400, _error("Send JSON like {\"account\": \"acme\", "
                                               "\"email\": \"you@example.com\"}."))
    account = str(data.get("account", "")).strip().lower()
    email = str(data.get("email", "")).strip()
    agent = str(data.get("agent") or "main").strip().lower()
    invite = str(data.get("invite", "")).strip()
    if not EMAIL.match(email):
        return await _answer(send, 400, _error("Please give a valid email address."))

    try:
        if mode == "invite":
            accounts.check_invite(invite)  # fail before making the account
        accounts.create_account(account, email=email)
        if mode == "invite":
            accounts.use_invite(invite, account)
        api_key = accounts.create_key(account, agent)
    except accounts.AccountError as e:
        return await _answer(send, 400, _error(str(e)))
    except identity.IdentityError:
        return await _answer(send, 503, _error("Sign-up isn't set up on this server yet."))
    limits.signups_total.hit("everyone")
    return await _answer(send, 201, {
        "account": account, "agent": agent, "api_key": api_key,
        "message": "Keep this API key safe: it is shown only once.",
    })

