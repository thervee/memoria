"""Spam protection: simple "no more than N in the last M seconds" limits.

Used by the server to slow down anyone sending too many requests:
  - per internet address (IP): every request, before checking any key
  - per internet address: wrong API keys or login passes (stops key guessing)
  - per agent: requests with a valid key (each account's requests_per_minute)
  - per account: all of an account's agents together (account_requests_per_minute)
  - per internet address and overall: sign-ups
  - per internet address and per account: requests for a new agent's API key
  - per internet address: the pay page's requests (billing.py)

The counts are kept in memory, so they start again when Memoria restarts. That's
fine for slowing down spam; the daily write limits (memoria.py) are kept on disk.
Settings can be changed in .env (see the defaults below).
"""
import os
import time
from collections import deque

import paths


def _setting(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


class Window:
    """Allows at most `limit` events per `seconds` for each key (e.g. an IP address)."""

    def __init__(self, limit: int, seconds: float):
        self.limit, self.seconds = limit, seconds
        self._events: dict[str, deque] = {}

    def _recent(self, key: str, now: float) -> deque:
        events = self._events.setdefault(key, deque())
        while events and events[0] <= now - self.seconds:
            events.popleft()
        return events

    def blocked_for(self, key: str, limit: int | None = None) -> float:
        """Seconds until `key` may act again (0 if it may act now). Doesn't count anything."""
        now = time.monotonic()
        events = self._recent(key, now)
        if len(events) < (limit or self.limit):
            return 0.0
        return max(0.1, events[0] + self.seconds - now)

    def hit(self, key: str) -> None:
        self._recent(key, time.monotonic()).append(time.monotonic())
        if len(self._events) > 50_000:  # don't let one flood use up memory
            self._tidy()

    def allow(self, key: str, limit: int | None = None) -> float:
        """Count one event if allowed. Returns 0 if allowed, else seconds to wait."""
        wait = self.blocked_for(key, limit)
        if not wait:
            self.hit(key)
        return wait

    def _tidy(self) -> None:
        now = time.monotonic()
        for key in list(self._events):
            if not self._recent(key, now):
                del self._events[key]

    def reset(self) -> None:
        self._events.clear()


per_ip = Window(_setting("MEMORIA_IP_LIMIT", 120), 60)              # requests per minute
failed_logins = Window(_setting("MEMORIA_FAILED_LOGIN_LIMIT", 10), 600)  # per 10 minutes
per_agent = Window(60, 60)  # the real limit comes from each account (requests_per_minute)
per_account = Window(120, 60)  # likewise (account_requests_per_minute)
signups_per_ip = Window(_setting("MEMORIA_SIGNUPS_PER_IP", 3), 86400)    # per day
signups_total = Window(_setting("MEMORIA_SIGNUPS_PER_DAY", 50), 86400)   # per day, everyone
key_requests_per_ip = Window(_setting("MEMORIA_KEY_REQUESTS_PER_IP", 5), 86400)  # per day
key_emails_per_account = Window(_setting("MEMORIA_KEY_EMAILS_PER_ACCOUNT", 5), 86400)  # per day
billing_per_ip = Window(_setting("MEMORIA_BILLING_PER_IP", 30), 60)  # pay page requests per minute
MAX_REQUEST_BYTES = _setting("MEMORIA_MAX_REQUEST_BYTES", 64_000)


def _trust_proxy() -> bool:
    """On unless told otherwise when online (Railway always sits behind a proxy)."""
    setting = os.environ.get("MEMORIA_TRUST_PROXY", "").strip().lower()
    if setting:
        return setting in ("1", "true", "yes", "on")
    return paths.ONLINE


def client_ip(scope) -> str:
    """The internet address a request came from.

    When Memoria is hosted behind a web server that handles HTTPS (a "reverse proxy"),
    every request seems to come from that web server. Set MEMORIA_TRUST_PROXY=1 then,
    and the real address is read from the X-Forwarded-For header the proxy adds.
    Only do that behind a proxy: otherwise anyone could send a fake header.
    """
    if _trust_proxy():
        for name, value in scope.get("headers", []):
            if name.lower() == b"x-forwarded-for":
                # The proxy adds the real address at the end; earlier parts could be faked.
                return value.decode(errors="replace").split(",")[-1].strip()
    client = scope.get("client")
    return client[0] if client else "unknown"


def is_this_computer(ip: str) -> bool:
    """Requests from this computer itself (this laptop's own agents) skip the address limits.

    Never when behind a proxy, because then every request arrives from this computer.
    """
    return not _trust_proxy() and ip in ("127.0.0.1", "::1", "localhost")


def reset_all() -> None:
    for window in (per_ip, failed_logins, per_agent, per_account, signups_per_ip, signups_total,
                   key_requests_per_ip, key_emails_per_account, billing_per_ip):
        window.reset()
