"""Live desktop viewer on the Hosted ``/invocations_ws`` route: attach-only, byte relay, no input lock."""

import asyncio
import os
import queue
import socket
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from shared_context import behavior_desktop_capability as capability
from shared_context import behavior_desktop_view as view
from shared_context.behavior_hosted import create_host
from shared_context.behavior_wire import HostedIdentity
from test_behavior_desktop import ALLOWED_ENV
from test_desktop_capability import GRANT, fake_desktop, session  # noqa: F401

IDENTITY = HostedIdentity(agent_id="0a0a0a0a-0a0a-4a0a-8a0a-0a0a0a0a0a0a", agent_name="OFFLINE-agent",
                          agent_version="1", session_id="OFFLINE-session")
ENV = {"DISPLAY": ":7", "PATH": "/usr/bin", "HOME": "/tmp/fa-desktop-x"}


class FakeVnc:
    """An ``x11vnc -inetd`` stand-in on ONE duplex stream: the test feeds what it sends and reads what it got."""

    def __init__(self):
        self.output, self.input = queue.Queue(), queue.Queue()
        self.buffer, self.eof, self.returncode, self.signals, self.closed = b"", False, None, [], False
        stream = SimpleNamespace(read=self._read, write=self._write, drain=self._drain, close=self._close)
        self.child = view.VncChild(self, stream, stream)

    def _close(self):
        self.closed = True

    async def _read(self, size):
        while not self.buffer:
            if self.eof or self.returncode is not None:
                return b""
            try:
                item = self.output.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.005)
                continue
            if item is None:
                self.eof = True
                return b""
            self.buffer += item
        chunk, self.buffer = self.buffer[:size], self.buffer[size:]
        return chunk

    def _write(self, data):
        if self.returncode is not None:
            raise BrokenPipeError
        self.input.put(bytes(data))

    async def _drain(self):
        await asyncio.sleep(0)

    def terminate(self):
        self.signals.append("terminate")
        self.returncode = -15

    def kill(self):
        self.signals.append("kill")
        self.returncode = -9

    async def wait(self):
        return self.returncode


def client_for(*, env=ENV, process=None, spawned=None):
    app = create_host(SimpleNamespace(identity=IDENTITY))

    async def spawn(value):
        if spawned is not None:
            spawned.append(value)
        return None if process is None else process.child
    view.install_desktop_view(app, capability=SimpleNamespace(current_env=lambda: env), spawn=spawn)
    return TestClient(app)


def closed(socket):
    with pytest.raises(WebSocketDisconnect) as error:
        socket.receive_bytes()
    return error.value.code, error.value.reason


def test_without_a_running_desktop_the_viewer_is_told_and_nothing_starts():
    spawned = []
    with client_for(env=None, spawned=spawned) as client, client.websocket_connect("/invocations_ws") as socket:
        assert closed(socket) == (4404, "DESKTOP_NOT_RUNNING")
    assert spawned == []


def test_a_missing_x11vnc_is_reported_without_touching_the_desktop():
    with client_for(process=None) as client, client.websocket_connect("/invocations_ws") as socket:
        assert closed(socket) == (4503, "DESKTOP_VIEW_UNAVAILABLE")


def test_bytes_flow_both_ways_in_bounded_frames_and_a_closed_desktop_ends_the_view():
    vnc, spawned = FakeVnc(), []
    with client_for(process=vnc, spawned=spawned) as client, client.websocket_connect("/invocations_ws") as socket:
        vnc.output.put(b"RFB 003.008\n")
        assert socket.receive_bytes() == b"RFB 003.008\n"
        socket.send_bytes(b"RFB 003.008\n")
        socket.send_bytes(b"\x05\x00\x00\x00\x00\x00\x00\x10")
        assert vnc.input.get(timeout=5) == b"RFB 003.008\n"
        assert vnc.input.get(timeout=5) == b"\x05\x00\x00\x00\x00\x00\x00\x10"
        frame = bytes(range(256)) * 600
        vnc.output.put(frame)
        received = b""
        while len(received) < len(frame):
            chunk = socket.receive_bytes()
            assert len(chunk) <= view.MAX_FRAME_BYTES
            received += chunk
        assert received == frame
        vnc.output.put(None)
        assert closed(socket) == (1000, "DESKTOP_CLOSED")
    assert spawned == [ENV] and vnc.returncode is not None and vnc.closed


def test_a_viewer_leaving_stops_its_own_x11vnc():
    vnc = FakeVnc()
    with client_for(process=vnc) as client:
        with client.websocket_connect("/invocations_ws") as socket:
            socket.send_bytes(b"hello")
            assert vnc.input.get(timeout=5) == b"hello"
        for _ in range(200):
            if vnc.signals:
                break
            time.sleep(0.01)
    assert vnc.signals[:1] == ["terminate"]


def test_text_frames_are_refused_because_rfb_is_binary():
    vnc = FakeVnc()
    with client_for(process=vnc) as client, client.websocket_connect("/invocations_ws") as socket:
        socket.send_text("hello")
        assert closed(socket) == (1003, "RFB_BINARY_ONLY")
    assert vnc.signals[:1] == ["terminate"] and vnc.input.empty()


def test_x11vnc_runs_on_the_owned_display_over_one_duplex_socket(monkeypatch):
    calls, opened = [], []

    async def exec_(*args, **kwargs):
        calls.append((args, kwargs))
        return "process"

    async def open_connection(*, sock, limit):
        opened.append((sock, limit))
        return "reader", "writer"
    monkeypatch.setattr(view.shutil, "which", lambda name, path=None: "/usr/bin/" + name if path else None)
    monkeypatch.setattr(view.asyncio, "create_subprocess_exec", exec_)
    monkeypatch.setattr(view.asyncio, "open_connection", open_connection)
    assert asyncio.run(view._spawn(ENV)) == view.VncChild("process", "reader", "writer")
    (args, kwargs), = calls
    assert args == ("/usr/bin/x11vnc", "-inetd", "-q", "-nopw", "-norc", "-nocmds", "-noremote", "-nocursorshape",
                    "-display", ":7")
    assert kwargs["env"] is ENV and kwargs["start_new_session"] is True
    # inetd semantics: fd 0 is one bidirectional socket, so stdin and stdout are the same socketpair end.
    assert isinstance(kwargs["stdin"], socket.socket) and kwargs["stdout"] is kwargs["stdin"]
    assert kwargs["stdin"].fileno() == -1 and opened[0][1] == view.MAX_FRAME_BYTES
    opened[0][0].close()
    monkeypatch.setattr(view.shutil, "which", lambda name, path=None: None)
    assert asyncio.run(view._spawn(ENV)) is None


INETD_ECHO = "import os; data = os.read(0, 64); os.write(0, b'RFB ' + data)"


@pytest.mark.skipif(sys.platform != "linux", reason="inetd fd semantics need a POSIX child process")
def test_a_real_child_writing_to_fd0_reaches_the_viewer(monkeypatch):
    monkeypatch.setattr(view.shutil, "which", lambda name, path=None: sys.executable)
    monkeypatch.setattr(view, "COMMAND", ("python", "-c", INETD_ECHO))

    async def run():
        vnc = await view._spawn({**ENV, "PATH": os.environ.get("PATH", "")})
        vnc.writer.write(b"003.008\n")
        await vnc.writer.drain()
        reply = await asyncio.wait_for(vnc.reader.read(64), 10)
        vnc.writer.close()
        await view._stop(vnc.process)
        return reply
    assert asyncio.run(run()) == b"RFB 003.008\n"


def test_the_viewer_attaches_to_the_turns_desktop_and_never_starts_one(tmp_path, fake_desktop):
    assert capability.DESKTOP_CAPABILITY.current_env() is None
    specs, _ = capability.DESKTOP_CAPABILITY(session(tmp_path), GRANT)
    assert capability.DESKTOP_CAPABILITY.current_env() is None
    assert not any(args[0] == "Xvfb" for args, _ in fake_desktop.spawned)
    observe = next(spec.handler for spec in specs if spec.name == "desktop_observe")
    asyncio.run(observe({"arguments": {}}))
    env = capability.DESKTOP_CAPABILITY.current_env()
    assert env["DISPLAY"] == ":7" and set(env) <= ALLOWED_ENV
    capability.DESKTOP_CAPABILITY.release(session(tmp_path))
    assert capability.DESKTOP_CAPABILITY.current_env() is None


def test_the_hosted_entry_registers_the_viewer(monkeypatch):
    import behavior_assistant_main
    hosts = []

    class Host:
        def ws_handler(self, fn):
            hosts.append(fn)
            return fn

        def run(self):
            hosts.append("run")
    monkeypatch.setattr(behavior_assistant_main, "disable_dumpable", lambda: None)
    monkeypatch.setattr(behavior_assistant_main.AssistantHostedRuntime, "from_environment", classmethod(lambda cls: None))
    monkeypatch.setattr(behavior_assistant_main, "create_host",
                        lambda runtime, *, responses_handler_factory=None: Host())
    behavior_assistant_main.main()
    assert callable(hosts[0]) and hosts[1] == "run"
