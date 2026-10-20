"""API broker for the live desktop viewer: one owner-checked WebSocket relayed to an Agent's Hosted session.

A browser cannot put an Authorization header on a WebSocket, so the 8765 console connects here with the
Team owner's Entra bearer. The API checks once that the caller owns the Agent's Team (ADR-0006 entry
check), that the named Hosted session belongs to that Agent (Foundry scopes sessions to the agent and to
the API identity that created them) and is running, then relays bytes to the official ``invocations_ws``
endpoint with the API managed identity. It never creates or wakes a session and keeps no record.
https://learn.microsoft.com/azure/foundry/agents/how-to/build-voice-agent#connect-a-client
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from urllib.parse import quote, urlencode

from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocket, WebSocketState

from .errors import Problem

PATH = "/desktop/v1/agents/{agent_id}/sessions/{session_id}"
MAX_FRAME_BYTES = 1024 * 1024
OPEN_TIMEOUT_SECONDS = 15
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_SENDABLE = {1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014}
CLEANUP_TIMEOUT_SECONDS = 15  # above the websockets default close_timeout of 10 s

log = logging.getLogger(__name__)


async def _finish(cleanup, timeout=CLEANUP_TIMEOUT_SECONDS):
    """Run ``cleanup`` to the end even if this handler is cancelled, then re-raise the cancel.

    A detached task, because the server's cancel scope (anyio) cancels the handler again at every await.
    Same as behavior_desktop_view._finish, which the API must not import (it is the Hosted side).
    """
    task = asyncio.ensure_future(asyncio.wait_for(cleanup, timeout))
    cancelled = None
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError as error:
            cancelled = error
    if not task.cancelled() and task.exception() is not None:
        log.warning("DESKTOP_VIEW_CLEANUP_FAILED exception_type=%s", type(task.exception()).__name__)
    if cancelled is not None:
        raise cancelled


class FoundryDesktopSessions:
    """The two Foundry calls the broker needs, both with the API's own credential and no retries."""

    def __init__(self, credential):
        self.credential = credential

    def status(self, target, session_id):
        from azure.ai.projects import AIProjectClient
        from azure.core.exceptions import AzureError, ResourceNotFoundError
        try:
            with AIProjectClient(endpoint=target.project_endpoint, credential=self.credential,
                                 retry_total=0, retry_connect=0, retry_read=0, retry_status=0,
                                 connection_timeout=5, read_timeout=10) as project:
                session = project.agents.get_session(target.agent_name, session_id)
        except ResourceNotFoundError:
            raise Problem(404, "DESKTOP_SESSION_UNKNOWN", "this Agent has no such Hosted session") from None
        except AzureError:
            raise Problem(503, "DESKTOP_SESSION_UNAVAILABLE", "the Hosted session could not be read") from None
        if session.agent_session_id != session_id:
            raise Problem(409, "DESKTOP_SESSION_UNKNOWN", "Foundry returned a different session")
        return session.status

    async def connect(self, target, session_id):
        from websockets.asyncio.client import connect
        from .behavior_foundry import FOUNDRY_TOKEN_SCOPE
        token = (await run_in_threadpool(self.credential.get_token, FOUNDRY_TOKEN_SCOPE)).token
        query = urlencode({"api-version": "v1", "agent_session_id": session_id})
        url = ("wss://" + target.project_endpoint.removeprefix("https://")
               + f"/agents/{quote(target.agent_name, safe='')}/endpoint/protocols/invocations_ws?{query}")
        return await connect(url, additional_headers={"Authorization": "Bearer " + token},
                             max_size=MAX_FRAME_BYTES, open_timeout=OPEN_TIMEOUT_SECONDS,
                             compression=None, proxy=None)


def _authorize(composition, websocket, agent_id, session_id, sessions):
    from .behavior_auth import BehaviorPrincipal
    from .behavior_team_registry import resolve_agent_scope
    if len(websocket.headers.getlist("authorization")) != 1:
        raise Problem(401, "BEHAVIOR_UNAUTHENTICATED", "one authorization header is required")
    try:
        principal = composition.verifier(websocket)
    except Problem:
        raise
    except Exception:
        raise Problem(503, "BEHAVIOR_DEPENDENCY_UNAVAILABLE", "authentication dependency failed") from None
    if not isinstance(principal, BehaviorPrincipal) or principal.kind != "human" or principal.actor_id is not None:
        raise Problem(403, "DESKTOP_VIEW_DENIED", "only the Team owner may view an Agent desktop")
    scope = resolve_agent_scope(composition.runtime.store, composition.config, agent_id)
    principal.require(scope.team_id, "read")
    if _SESSION_ID.fullmatch(session_id) is None:
        raise Problem(422, "DESKTOP_SESSION_UNKNOWN", "one Hosted session ID is required")
    if sessions.status(scope.target, session_id) != "active":
        raise Problem(409, "DESKTOP_VM_NOT_RUNNING", "the Hosted session is not running; it is not woken")
    return scope.target


def _close_code(status):
    return {401: 4401, 403: 4403, 404: 4404, 409: 4409, 422: 4422}.get(status, 4503)


async def _close(websocket, code, reason):
    if websocket.application_state != WebSocketState.DISCONNECTED:
        with contextlib.suppress(Exception):
            await websocket.close(code=code, reason=reason[:120])


def _upstream_outcome(upstream):
    code, reason = upstream.close_code, upstream.close_reason or ""
    if code in _SENDABLE or code is not None and 3000 <= code <= 4999:
        return code, reason
    return 1011, "DESKTOP_VIEW_UPSTREAM_CLOSED"


async def relay(websocket, upstream):
    """Bytes both ways until one side closes; the Hosted close code/reason reaches the viewer."""
    from websockets.exceptions import ConnectionClosed

    async def to_viewer():
        try:
            async for message in upstream:
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(message)
        except ConnectionClosed:
            pass
        return _upstream_outcome(upstream)

    async def to_desktop():
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return None
            data = message.get("bytes")
            if data is None:
                return 1003, "RFB_BINARY_ONLY"
            try:
                await upstream.send(data)
            except ConnectionClosed:
                # Hosted closed first (e.g. 1001 after ~30 min) while the viewer was sending.
                return _upstream_outcome(upstream)

    async def cleanup():
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await upstream.close()

    tasks = [asyncio.ensure_future(to_viewer()), asyncio.ensure_future(to_desktop())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        outcome = next(iter(done)).result()
    except Exception as error:
        log.warning("DESKTOP_VIEW_RELAY_ENDED exception_type=%s", type(error).__name__)
        outcome = 1011, "DESKTOP_VIEW_FAILED"
    finally:
        await _finish(cleanup())
    if outcome is not None:
        await _close(websocket, *outcome)


def install_desktop_view_routes(app, composition, *, sessions=None):
    def foundry():
        if sessions is not None:
            return sessions
        from .behavior_foundry import FoundryHttpClient
        clients = [client for client in composition.clients if isinstance(client, FoundryHttpClient)]
        if not clients or clients[0].credential is None:
            raise Problem(503, "DESKTOP_VIEW_UNAVAILABLE", "the API Foundry credential is unavailable")
        return FoundryDesktopSessions(clients[0].credential)

    @app.websocket(PATH)
    async def desktop(websocket: WebSocket, agent_id: str, session_id: str):
        await websocket.accept()
        try:
            adapter = foundry()
            target = await run_in_threadpool(_authorize, composition, websocket, agent_id, session_id, adapter)
        except Problem as error:
            code = error.body["error"]["code"]
            log.warning("DESKTOP_VIEW_REFUSED code=%s", code)
            return await _close(websocket, _close_code(error.status), code)
        try:
            upstream = await adapter.connect(target, session_id)
        except Exception as error:
            log.warning("DESKTOP_VIEW_UPSTREAM_REFUSED exception_type=%s status=%s", type(error).__name__,
                        getattr(getattr(error, "response", None), "status_code", None))
            return await _close(websocket, 4502, "DESKTOP_VIEW_UPSTREAM_REFUSED")
        await relay(websocket, upstream)
