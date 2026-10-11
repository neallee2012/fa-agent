"""Live view of this Hosted process's desktop over the official ``/invocations_ws`` route.

noVNC in the browser speaks RFB. This handler only relays bytes between the WebSocket and one
``x11vnc -inetd`` child (stdio, no listening port) attached to the desktop that a granted turn already
started. It never starts, replaces or closes that desktop, and like Grokbot it adds no input lock: the
user may operate the desktop while the Agent does. Frames stay far below the platform's 1 MB limit.
https://learn.microsoft.com/azure/foundry/agents/how-to/build-voice-agent#implement-the-websocket-handler
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import socket
from dataclasses import dataclass

from starlette.websockets import WebSocket, WebSocketState

from .behavior_desktop_capability import DESKTOP_CAPABILITY

MAX_FRAME_BYTES = 64 * 1024
# RFC 6455 private-use close codes; the viewer explains them, nothing retries automatically.
CLOSE_NOT_RUNNING = 4404
CLOSE_UNAVAILABLE = 4503
# -nocursorshape: noVNC accepts cursor-shape updates but not cursor-position updates, so by default it only shows the
# shape under the viewer's own mouse and the desktop pointer the Agent moves stays invisible. Drawing the pointer
# into the framebuffer makes it visible to every viewer (x11vnc(1) -cursor, -nocursorshape, -cursorpos).
COMMAND = ("x11vnc", "-inetd", "-q", "-nopw", "-norc", "-nocmds", "-noremote", "-nocursorshape")

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VncChild:
    process: object
    reader: object
    writer: object


async def _spawn(env):
    """``-inetd`` uses fd 0 as ONE bidirectional socket (as inetd provides), so stdin/stdout share a socketpair."""
    executable = shutil.which("x11vnc", path=env.get("PATH"))
    if executable is None:
        return None
    parent, child = socket.socketpair()
    try:
        process = await asyncio.create_subprocess_exec(
            executable, *COMMAND[1:], "-display", env["DISPLAY"], env=env,
            stdin=child, stdout=child, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    except BaseException:
        parent.close()
        raise
    finally:
        child.close()
    try:
        reader, writer = await asyncio.open_connection(sock=parent, limit=MAX_FRAME_BYTES)
    except BaseException:
        parent.close()
        await _stop(process)
        raise
    return VncChild(process, reader, writer)


async def _close(websocket, code, reason):
    if websocket.application_state != WebSocketState.DISCONNECTED:
        with contextlib.suppress(Exception):
            await websocket.close(code=code, reason=reason)


async def _stop(process):
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 2)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


async def view_desktop(websocket: WebSocket, *, capability=DESKTOP_CAPABILITY, spawn=_spawn) -> None:
    env = await asyncio.to_thread(capability.current_env)
    if env is None:
        return await _close(websocket, CLOSE_NOT_RUNNING, "DESKTOP_NOT_RUNNING")
    try:
        vnc = await spawn(env)
    except OSError:
        vnc = None
    if vnc is None:
        return await _close(websocket, CLOSE_UNAVAILABLE, "DESKTOP_VIEW_UNAVAILABLE")

    async def to_viewer():
        while chunk := await vnc.reader.read(MAX_FRAME_BYTES):
            await websocket.send_bytes(chunk)
        return 1000, "DESKTOP_CLOSED"

    async def to_desktop():
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return None
            data = message.get("bytes")
            if data is None:
                return 1003, "RFB_BINARY_ONLY"
            try:
                vnc.writer.write(data)
                await vnc.writer.drain()
            except (BrokenPipeError, ConnectionResetError):
                return 1000, "DESKTOP_CLOSED"

    tasks = [asyncio.ensure_future(to_viewer()), asyncio.ensure_future(to_desktop())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        outcome = next(iter(done)).result()
    except Exception as error:
        log.warning("DESKTOP_VIEW_RELAY_ENDED exception_type=%s", type(error).__name__)
        outcome = 1011, "DESKTOP_VIEW_FAILED"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        vnc.writer.close()
        await _stop(vnc.process)
    if outcome is not None:
        await _close(websocket, *outcome)


def install_desktop_view(app, **options) -> None:
    """Register the single ``/invocations_ws`` handler of this Hosted app (one viewer = one x11vnc)."""
    @app.ws_handler
    async def desktop(websocket: WebSocket) -> None:
        await view_desktop(websocket, **options)
