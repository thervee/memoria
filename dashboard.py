"""Memoria dashboard: a web page for managing memories.

Served by the Memoria server itself at http://127.0.0.1:8765/dashboard
It opens straight away, with no password.

Safety rules:
  - Only this laptop can reach it: Memoria listens on 127.0.0.1 only, and
    requests must be addressed to 127.0.0.1 or localhost (this also blocks
    "DNS rebinding" tricks by other websites).
  - Changes (sharing, delete) are only accepted from the dashboard page itself:
    the browser's Origin and Sec-Fetch-Site headers must say so. Other websites
    can't make changes behind your back, and they can't read your memories either,
    because browsers don't hand them the answers.
  - Sharing changes and deletions are made AS the agent that wrote the memory,
    proven with that agent's own key, the same rule agents follow.
  - It shows EVERY account's memories, so it must never be reachable from the
    internet. When Memoria is hosted, a web server in front of it (a proxy) can make
    outside requests look as if they come from this computer, so any request that
    went through a proxy (it carries X-Forwarded-For or Forwarded) is refused.
    Hosts can also turn the dashboard off completely with MEMORIA_DASHBOARD=off.
"""
import json
import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

import identity
import memoria

HERE = Path(__file__).resolve().parent
PAGE_DIR = HERE / "dashboard"
PORT = 8765
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}


# ---------- request checks ----------

def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": True, "message": message}, status_code=status)


def _guard(request: Request) -> JSONResponse | None:
    """Return an error response if this request isn't allowed, else None."""
    if any(h in request.headers for h in ("x-forwarded-for", "forwarded", "x-real-ip")):
        return _error(403, "The dashboard only opens on the computer Memoria runs on.")
    if request.headers.get("host", "") not in ALLOWED_HOSTS:
        return _error(403, "Open the dashboard at http://127.0.0.1:8765/dashboard")
    if request.method != "GET":
        fetch_site = request.headers.get("sec-fetch-site")
        if (request.headers.get("origin") not in ALLOWED_ORIGINS
                or (fetch_site is not None and fetch_site != "same-origin")):
            return _error(403, "Changes are only accepted from the Memoria dashboard page.")
    return None


async def _json_body(request: Request) -> dict:
    body = await request.body()
    if len(body) > 20_000:
        raise ValueError("Request too large.")
    data = json.loads(body or b"{}")
    if not isinstance(data, dict):
        raise ValueError("Bad request.")
    return data


def _act_as_author(memory_id: str) -> tuple[str, str]:
    """The memory's author and account, after proving the author's secret key is here."""
    mem = next((m for m in memoria.all_memories() if m["memory_id"] == memory_id), None)
    if mem is None:
        raise memoria.MemoriaError(f"No memory with ID '{memory_id}'.")
    author, account = mem["saved_by"], mem["account"]
    try:
        identity.sign(identity.principal(account, author), b"memoria-dashboard-check")
    except identity.IdentityError:
        raise memoria.MemoriaError(
            f"This memory was written by '{identity.principal(account, author)}', whose key "
            "isn't on this computer, so it can't be changed from here."
        ) from None
    return author, account


# ---------- pages and API ----------

def _static(name: str, media_type: str):
    async def handler(request: Request):
        blocked = _guard(request)
        if blocked:
            return blocked
        return FileResponse(PAGE_DIR / name, media_type=media_type,
                            headers={"Cache-Control": "no-store"})
    return handler


async def home(request: Request):
    return RedirectResponse("/dashboard")


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
        author, account = _act_as_author(memory_id)
        result = await memoria.share_memory(author, memory_id, str(data.get("visibility", "")),
                                            data.get("shared_with") or [], via="dashboard",
                                            account=account)
    except (memoria.MemoriaError, ValueError) as e:
        return _error(400, str(e))
    return JSONResponse(result)


async def delete(request: Request):
    blocked = _guard(request)
    if blocked:
        return blocked
    memory_id = request.path_params["memory_id"]
    try:
        author, account = _act_as_author(memory_id)
        result = await memoria.delete_memory(author, memory_id, via="dashboard",
                                             account=account)
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
    """True for web addresses that belong to the dashboard (none if it's turned off)."""
    if os.environ.get("MEMORIA_DASHBOARD", "on").strip().lower() == "off":
        return False
    return path == "/" or path == "/dashboard" or path.startswith("/dashboard/")
