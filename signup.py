"""Developer sign-up, with email verification.

    POST /signup          {"account": "acme", "email": "dev@acme.example",
                           "turnstile": "<Cloudflare Turnstile token>", "invite": "inv_..."}
        Checks the Turnstile result, reserves the account name and emails a link to
        <MEMORIA_SITE_URL>/verify.html?token=...   Nothing is created yet.
        (invite only when MEMORIA_SIGNUP=invite)
    POST /signup/verify   {"token": "<the secret from the link>"}
        Creates the account and one API key (agent "main") and returns the key.
        Each link works once, within 24 hours, so the key is shown only once.
    GET /signup/config
        {"open": true, "turnstile_site_key": "..."} for the website's sign-up form.
        The site key is public (Turnstile's widget shows it to every visitor).

OFF unless the host turns it on:
  MEMORIA_SIGNUP=off      (default) nobody can sign up; the host creates accounts
                          with manage_accounts.py
  MEMORIA_SIGNUP=invite   developers need an invite code from the host
                          (manage_accounts.py invite); each code works once
  MEMORIA_SIGNUP=open     anyone can sign up

Sign-up also needs these settings (Railway variables), or it refuses to run:
  TURNSTILE_SITE_KEY      Cloudflare Turnstile's site key (public; the form loads it)
  TURNSTILE_SECRET_KEY    Cloudflare Turnstile's secret key (the "are you human?" check)
  RESEND_API_KEY          Resend's API key, for sending the verification emails
  MEMORIA_EMAIL_FROM      optional, default "Memoria <noreply@memoriaagent.site>"
  MEMORIA_SITE_URL        optional, default "https://www.memoriaagent.site"
  MEMORIA_SIGNUP_ORIGINS  optional: websites allowed to call sign-up from the browser,
                          default "https://www.memoriaagent.site,https://memoriaagent.site"

Spam protection: the Turnstile check; 3 sign-ups per internet address per day and
50 in total per day (MEMORIA_SIGNUPS_PER_IP / MEMORIA_SIGNUPS_PER_DAY); and one
account per email address. New accounts get the normal limits (accounts.DEFAULT_LIMITS).
"""
import asyncio
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import accounts
import identity
import limits

EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[a-z]{2,}$", re.IGNORECASE)
MAX_BODY = 4_000
TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
RESEND_URL = "https://api.resend.com/emails"
USER_AGENT = "memoria-server/1.0"


def _setting(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _site_url() -> str:
    return _setting("MEMORIA_SITE_URL", "https://www.memoriaagent.site").rstrip("/")


def _allowed_origins() -> set[str]:
    raw = _setting("MEMORIA_SIGNUP_ORIGINS",
                   "https://www.memoriaagent.site,https://memoriaagent.site")
    return {o.strip().rstrip("/") for o in raw.split(",") if o.strip()}


def handles(path: str) -> bool:
    return path in ("/signup", "/signup/verify", "/signup/config")


def _cors(scope) -> list:
    """Let the Memoria website call sign-up from visitors' browsers (and no other site)."""
    origin = dict(scope.get("headers", [])).get(b"origin", b"").decode(errors="replace")
    if origin and origin.rstrip("/") in _allowed_origins():
        return [(b"access-control-allow-origin", origin.encode()), (b"vary", b"Origin")]
    return [(b"vary", b"Origin")]


async def _answer(send, status: int, data: dict, headers=()) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"cache-control", b"no-store"), *headers]})
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


# ---------- the outside services (replaced in the tests) ----------

def _post(url: str, data: bytes, headers: dict) -> dict:
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(request, timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode(errors="replace")
        raise RuntimeError(f"{url} answered {e.code}: {detail}") from None


async def check_turnstile(token: str, ip: str) -> bool:
    """True if Cloudflare says this visitor passed the Turnstile check."""
    if not token:
        return False
    form = urllib.parse.urlencode({"secret": _setting("TURNSTILE_SECRET_KEY"),
                                   "response": token, "remoteip": ip}).encode()
    answer = await asyncio.to_thread(
        _post, TURNSTILE_URL, form, {"Content-Type": "application/x-www-form-urlencoded"})
    return answer.get("success") is True


async def send_email(to: str, subject: str, text: str, html: str) -> None:
    body = json.dumps({"from": _setting("MEMORIA_EMAIL_FROM",
                                        "Memoria <noreply@memoriaagent.site>"),
                       "to": [to], "subject": subject, "text": text, "html": html}).encode()
    await asyncio.to_thread(_post, RESEND_URL, body, {
        "Authorization": f"Bearer {_setting('RESEND_API_KEY')}",
        "Content-Type": "application/json"})


def _verification_email(account: str, link: str) -> tuple[str, str, str]:
    hours = accounts.SIGNUP_LINK_HOURS
    subject = "Confirm your Memoria sign-up"
    text = (f"Someone, hopefully you, asked for a Memoria developer account called "
            f"'{account}' with this email address.\n\n"
            f"Open this link to confirm it and get your API key:\n{link}\n\n"
            f"The link works once and expires in {hours} hours. If you didn't ask for "
            f"this, ignore this email: nothing will be created.\n\nMemoria")
    html = (f"<p>Someone, hopefully you, asked for a Memoria developer account called "
            f"<strong>{account}</strong> with this email address.</p>"
            f'<p><a href="{link}">Confirm and get your API key</a></p>'
            f"<p>The link works once and expires in {hours} hours. If you didn't ask for "
            f"this, ignore this email: nothing will be created.</p><p>Memoria</p>")
    return subject, text, html


# ---------- the two requests ----------

async def app(scope, receive, send) -> None:
    cors = _cors(scope)
    if scope["method"] == "OPTIONS":  # the browser asking whether the website may call us
        return await send_preflight(send, cors)
    mode = accounts.signup_mode()
    if scope["path"] == "/signup/config":
        site_key = _setting("TURNSTILE_SITE_KEY")
        return await _answer(send, 200, {"open": mode != "off" and bool(site_key),
                                         "invite": mode == "invite",
                                         "turnstile_site_key": site_key}, cors)
    if mode == "off":
        return await _answer(send, 404, _error("Sign-up is closed. Ask the host for an account."),
                             cors)
    if scope["method"] != "POST":
        return await _answer(send, 405, _error("Send a POST request with JSON."), cors)
    try:
        data = json.loads(await _read_body(receive) or b"{}")
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        return await _answer(send, 400, _error("Send JSON like {\"account\": \"acme\", "
                                               "\"email\": \"you@example.com\"}."), cors)
    if scope["path"] == "/signup/verify":
        return await _verify(send, data, cors)
    return await _start(scope, send, data, mode, cors)


async def send_preflight(send, cors) -> None:
    await send({"type": "http.response.start", "status": 204, "headers": [
        *cors, (b"access-control-allow-methods", b"POST"),
        (b"access-control-allow-headers", b"content-type"),
        (b"access-control-max-age", b"600")]})
    await send({"type": "http.response.body", "body": b""})


async def _start(scope, send, data: dict, mode: str, cors) -> None:
    if not _setting("TURNSTILE_SECRET_KEY") or not _setting("RESEND_API_KEY"):
        return await _answer(send, 503, _error("Sign-up isn't set up on this server yet."), cors)

    # Spam protection: 3 sign-ups per address per day, and a daily total for everyone.
    ip = limits.client_ip(scope)
    wait = limits.signups_per_ip.blocked_for(ip) or limits.signups_total.blocked_for("everyone")
    if wait:
        seconds = max(1, round(wait))
        return await _answer(send, 429, _error(f"Too many sign-ups. Try again in {seconds} "
                                               "seconds."),
                             [*cors, (b"retry-after", str(seconds).encode())])

    account = str(data.get("account", "")).strip().lower()
    email = str(data.get("email", "")).strip()
    invite = str(data.get("invite", "")).strip()
    if not EMAIL.match(email):
        return await _answer(send, 400, _error("Please give a valid email address."), cors)
    try:
        human = await check_turnstile(str(data.get("turnstile", "")).strip(), ip)
    except Exception as e:
        print(f"Memoria sign-up: Turnstile check failed: {e}", file=sys.stderr)
        return await _answer(send, 503, _error("We couldn't check you're human just now. "
                                               "Please try again in a minute."), cors)
    if not human:
        return await _answer(send, 400, _error("Please complete the \"I'm human\" check "
                                               "and try again."), cors)
    try:
        secret = accounts.start_signup(account, email, invite if mode == "invite" else "")
    except accounts.AccountError as e:
        return await _answer(send, 400, _error(str(e)), cors)

    link = f"{_site_url()}/verify.html?token={secret}"
    try:
        await send_email(email, *_verification_email(account, link))
    except Exception as e:
        accounts.cancel_signup(secret)
        print(f"Memoria sign-up: email to new developer not sent: {e}", file=sys.stderr)
        return await _answer(send, 503, _error("We couldn't send the email just now. "
                                               "Please try again in a minute."), cors)
    limits.signups_per_ip.hit(ip)
    limits.signups_total.hit("everyone")
    return await _answer(send, 202, {
        "account": account,
        "message": f"Check your email: we sent a link to {email}. Open it within "
                   f"{accounts.SIGNUP_LINK_HOURS} hours to get your API key.",
    }, cors)


async def _verify(send, data: dict, cors) -> None:
    try:
        account, agent, api_key = accounts.finish_signup(str(data.get("token", "")))
    except accounts.AccountError as e:
        return await _answer(send, 400, _error(str(e)), cors)
    except identity.IdentityError:
        return await _answer(send, 503, _error("Sign-up isn't set up on this server yet."), cors)
    return await _answer(send, 201, {
        "account": account, "agent": agent, "api_key": api_key,
        "message": "Save this API key now: it is shown only once.",
    }, cors)
