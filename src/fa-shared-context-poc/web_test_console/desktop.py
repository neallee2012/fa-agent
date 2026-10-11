"""8765 side of the live Agent desktop viewer (Grokbot-style noVNC, operable, no input lock).

The page opens a same-origin WebSocket here (cookie + Origin + Host are the local boundary, plus the platform
principal in remote mode, because the HTTP
middleware never sees WebSocket scopes). This relays bytes to the API broker
``/desktop/v1/agents/{agent_id}/sessions/{session}`` with the signed-in owner's bearer, which a browser
WebSocket cannot send itself. The API makes the owner/Team/session checks; nothing here retries or records.
"""

from __future__ import annotations

import contextlib
import re
import secrets
from pathlib import Path
from urllib.parse import quote, urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.websockets import WebSocket, WebSocketState

from shared_context.behavior_desktop_view_api import MAX_FRAME_BYTES, OPEN_TIMEOUT_SECONDS, relay

from .adapter import ConsoleError, require

NOVNC = Path(__file__).with_name("novnc")
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_CLOSE = {401: 4401, 403: 4403, 404: 4404, 422: 4422}


def desktop_url(backend_origin, agent_id, session_id):
    return ("wss://" + backend_origin.removeprefix("https://")
            + f"/desktop/v1/agents/{quote(agent_id, safe='')}/sessions/{quote(session_id, safe='')}")


async def connect(url, token):
    from websockets.asyncio.client import connect as open_websocket
    return await open_websocket(url, additional_headers={"Authorization": "Bearer " + token},
                                max_size=MAX_FRAME_BYTES, open_timeout=OPEN_TIMEOUT_SECONDS,
                                compression=None, proxy=None)


async def _close(websocket, code, reason):
    if websocket.application_state != WebSocketState.DISCONNECTED:
        with contextlib.suppress(Exception):
            await websocket.close(code=code, reason=reason[:120])


def install_desktop_routes(app, *, agents, origin, cookie, cookie_name="fa_console", remote=None):
    root = NOVNC.resolve()

    @app.get("/novnc/{path:path}")
    def novnc(path: str):
        target = (root / path).resolve()
        require(target.is_relative_to(root) and target.suffix == ".js" and target.is_file(),
                "LOCAL_REQUEST_DENIED", 404)
        # Module scripts need an exact JavaScript type; never trust the host's mimetypes registry.
        return Response(target.read_bytes(), media_type="text/javascript; charset=utf-8")

    @app.websocket("/api/agents/{agent_id}/desktop")
    async def desktop(websocket: WebSocket, agent_id: str):
        await websocket.accept()
        try:
            query = websocket.query_params
            require(websocket.headers.get("host") == urlsplit(origin).netloc
                    and websocket.headers.get("origin") == origin
                    and secrets.compare_digest(websocket.cookies.get(cookie_name, ""), cookie),
                    "LOCAL_REQUEST_DENIED", 403)
            if remote is not None:
                remote.admit(websocket.headers)
            session = query.get("session", "")
            require(list(query.keys()) == ["session"] and len(query.getlist("session")) == 1
                    and _SESSION.fullmatch(session) is not None, "DESKTOP_SESSION_REQUIRED", 422)
            require(agents is not None, "DESKTOP_VIEW_UNAVAILABLE", 503)
            console = await run_in_threadpool(agents.resolve, agent_id)
            token = console.vault.get()
            url = desktop_url(console.config.backend_origin, agent_id, session)
        except ConsoleError as error:
            return await _close(websocket, _CLOSE.get(error.status, 4409 if error.status == 409 else 4503),
                                error.code)
        try:
            upstream = await connect(url, token)
        except Exception:
            return await _close(websocket, 4502, "DESKTOP_VIEW_UPSTREAM_REFUSED")
        await relay(websocket, upstream)
