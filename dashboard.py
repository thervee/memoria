"""Memoria dashboard: a password-protected web page for managing memories.

Served by the Memoria server itself at http://127.0.0.1:8765/dashboard
(only this laptop can open it).

Safety rules:
  - The password is never stored, only a salted scrypt hash, in
    keys/dashboard_password.json (git-ignored, readable only by your Windows account).
  - After logging in, the browser gets a random session cookie (12 hours).
  - Requests must come to 127.0.0.1/localhost, and changes must come from
    the dashboard page itself, so other websites can't use it behind your back.
  - Sharing changes and deletions are made AS the agent that wrote the memory,
    proven with that agent's own key, the same rule agents follow.
"""
import asyncio
import json
import secrets
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

import dashboard_password as password_store
import identity
import memoria

HERE = Path(__file__).resolve().parent
PAGE_DIR = HERE / "dashboard"
PORT = 8765
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}
COOKIE = "memoria_session"
SESSION_SECONDS = 12 * 3600

_sessions: dict[str, float] = {}   # session cookie -> expiry time
_failed_logins: list[float] = []   # times of recent wrong passwords


# ---------- request checks ----------

def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": True, "message": message}, status_code=status)


def _logged_in(request: Request) -> bool:
    expiry = _sessions.get(request.cookies.get(COOKIE, ""))
    if expiry and expiry > time.time():
        return True
    _sessions.pop(request.cookies.get(COOKIE, ""), None)
    return False


def _guard(request: Request, need_login: bool = True) -> JSONResponse | None:
    """Return an error response if this request isn't allowed, else None."""
    if request.headers.get("host", "") not in ALLOWED_HOSTS:
        return _error(403, "Open the dashboard at http://127.0.0.1:8765/dashboard")
    if request.method != "GET" and request.headers.get("origin") not in ALLOWED_ORIGINS:
        return _error(403, "Changes are only accepted from the Memoria dashboard page.")
    if need_login and not _logged_in(request):
        return _error(401, "Please log in.")
    return None


async def _json_body(request: Request) -> dict:
    body = await request.body()
    if len(body) > 20_000:
        raise ValueError("Request too large.")
    data = json.loads(body or b"{}")
    if not isinstance(data, dict):
        raise ValueError("Bad request.")
    return data


def _act_as_author(memory_id: str) -> str:
    """The memory's author, after proving its secret key is on this laptop."""
    mem = next((m for m in memoria.all_memories() if m["memory_id"] == memory_id), None)
    if mem is None:
        raise memoria.MemoriaError(f"No memory with ID '{memory_id}'.")
    author = mem["saved_by"]
    try:
        identity.check_token(identity.make_token(author))  # same login check agents pass
    except identity.IdentityError:
        raise memoria.MemoriaError(
            f"This memory was written by '{author}', whose key isn't on this laptop, "
            "so it can't be changed from here."
        ) from None
    return author


# ---------- pages and API ----------

def _static(name: str, media_type: str):
    async def handler(request: Request):
        blocked = _guard(request, need_login=False)
        if blocked:
            return blocked
        return FileResponse(PAGE_DIR / name, media_type=media_type,
                            headers={"Cache-Control": "no-store"})
    return handler


async def home(request: Request):
    return RedirectResponse("/dashboard")


async def session(request: Request):
    blocked = _guard(request, need_login=False)
    return blocked or JSONResponse({"logged_in": _logged_in(request),
                                    "password_set": password_store.is_set()})


async def login(request: Request):
    blocked = _guard(request, need_login=False)
    if blocked:
        return blocked
    now = time.time()
    _failed_logins[:] = [t for t in _failed_logins if now - t < 60]
    if len(_failed_logins) >= 5:
        return _error(429, "Too many wrong passwords. Wait a minute and try again.")
    if not password_store.is_set():
        return _error(400, "No password set yet. Double-click set_dashboard_password.bat first.")
    try:
        password = str((await _json_body(request)).get("password", ""))
    except ValueError as e:
        return _error(400, str(e))
    if not await asyncio.to_thread(password_store.check, password):
        _failed_logins.append(now)
        await asyncio.sleep(1)  # slow down guessing
        return _error(401, "Wrong password.")
    token = secrets.token_urlsafe(32)
    _sessions[token] = now + SESSION_SECONDS
    response = JSONResponse({"logged_in": True})
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                        samesite="strict", path="/dashboard")
    return response


async def logout(request: Request):
    blocked = _guard(request, need_login=False)
    if blocked:
        return blocked
    _sessions.pop(request.cookies.get(COOKIE, ""), None)
    response = JSONResponse({"logged_in": False})
    response.delete_cookie(COOKIE, path="/dashboard")
    return response


async def list_memories(request: Request):
    blocked = _guard(request)
    if blocked:
        return blocked
    agents = sorted(json.loads(identity.REGISTRY.read_text(encoding="utf-8")).keys()) \
        if identity.REGISTRY.exists() else []
    return JSONResponse({"memories": memoria.all_memories(), "agents": agents})


async def memory_detail(request: Request):
    blocked = _guard(request)
    if blocked:
        return blocked
    detail = memoria.memory_versions(request.path_params["memory_id"])
    return JSONResponse(detail) if detail else _error(404, "That memory no longer exists.")


async def change_sharing(request: Request):
    blocked = _guard(request)
    if blocked:
        return blocked
    memory_id = request.path_params["memory_id"]
    try:
        data = await _json_body(request)
        author = _act_as_author(memory_id)
        result = await memoria.share_memory(author, memory_id, str(data.get("visibility", "")),
                                            data.get("shared_with") or [], via="dashboard")
    except (memoria.MemoriaError, ValueError) as e:
        return _error(400, str(e))
    return JSONResponse(result)


async def delete(request: Request):
    blocked = _guard(request)
    if blocked:
        return blocked
    memory_id = request.path_params["memory_id"]
    try:
        author = _act_as_author(memory_id)
        result = await memoria.delete_memory(author, memory_id, via="dashboard")
    except memoria.MemoriaError as e:
        return _error(400, str(e))
    return JSONResponse(result)


SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; "
                                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

_app = Starlette(routes=[
    Route("/", home),
    Route("/dashboard", _static("index.html", "text/html")),
    Route("/dashboard/", home),
    Route("/dashboard/app.css", _static("app.css", "text/css")),
    Route("/dashboard/app.js", _static("app.js", "text/javascript")),
    Route("/dashboard/api/session", session),
    Route("/dashboard/api/login", login, methods=["POST"]),
    Route("/dashboard/api/logout", logout, methods=["POST"]),
    Route("/dashboard/api/memories", list_memories),
    Route("/dashboard/api/memories/{memory_id}", memory_detail),
    Route("/dashboard/api/memories/{memory_id}/sharing", change_sharing, methods=["POST"]),
    Route("/dashboard/api/memories/{memory_id}/delete", delete, methods=["POST"]),
])


async def app(scope, receive, send):
    """The dashboard, with security headers added to every response."""
    async def send_with_headers(message):
        if message["type"] == "http.response.start":
            extra = [(k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
            message = dict(message, headers=list(message.get("headers", [])) + extra)
        await send(message)
    await _app(scope, receive, send_with_headers)


def handles(path: str) -> bool:
    """True for web addresses that belong to the dashboard."""
    return path == "/" or path == "/dashboard" or path.startswith("/dashboard/")
