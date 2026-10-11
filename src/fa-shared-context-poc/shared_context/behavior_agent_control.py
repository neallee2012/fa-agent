"""Bounded HTTP control channel; dispatch rights and durable receipts belong to T1."""

from collections import deque
from concurrent.futures import Future
import json
import logging
from threading import Condition, RLock
from time import monotonic
from typing import Annotated, Literal
from uuid import uuid4

import httpx
from pydantic import Field, SecretBytes

from . import behavior_wire as wire
from .behavior_assistant_wire import Empty
from .behavior_agent_worker import AgentTurnAddress, AgentTurnDelivery, AgentVmBinding, AgentWorkerContext
from .behavior_http_diagnostics import _exception_type
from .errors import Problem

PREFIX = "/behavior/v1/agent-worker"
CONNECTION_HEADER = "x-behavior-control-connection"
MAX_FRAME_BYTES = 96 * 1024


class _Hello(wire.WireModel):
    vm: AgentVmBinding
    process_id: wire.Identifier


class _Command(wire.WireModel):
    command_id: wire.Identifier
    action: Literal["turn", "read", "cancel"]
    address: AgentTurnAddress
    payload: Annotated[bytes, Field(max_length=65536, repr=False)] = b""


class _Reply(wire.WireModel):
    command_id: wire.Identifier
    value: Annotated[bytes, Field(max_length=65536, repr=False)] = b""
    unknown: bool = False


class _Peer:
    def __init__(self, context, observed_at):
        self.context = context
        self.observed_at = observed_at
        self.changed = Condition(RLock())
        self.queue = deque()
        self.pending = {}
        self.closed = False
        self.reading = False

    def put(self, command):
        with self.changed:
            if self.closed:
                raise Problem(503, "AGENT_WORKER_UNAVAILABLE", "the original control channel is closed")
            if len(self.pending) >= (32 if command.action == "turn" else 64):
                raise Problem(503, "AGENT_WORKER_CONTROL_BUSY", "the original control channel is full")
            reply = Future()
            self.pending[command.command_id] = reply
            if command.action == "turn":
                self.queue.append(command)
            else:
                self.queue.appendleft(command)
            self.changed.notify_all()
            return reply

    def next(self, seconds):
        with self.changed:
            if self.reading:
                raise Problem(409, "AGENT_WORKER_CONNECTION_BUSY", "only one original control reader is allowed")
            self.reading = True
            try:
                self.changed.wait_for(lambda: self.closed or self.queue, timeout=seconds)
                if self.closed:
                    raise Problem(503, "AGENT_WORKER_UNAVAILABLE", "the original control channel is closed")
                return self.queue.popleft() if self.queue else None
            finally:
                self.reading = False

    def reply(self, value):
        with self.changed:
            pending = self.pending.pop(value.command_id, None)
            if pending is None or self.closed:
                raise Problem(409, "AGENT_WORKER_REPLY_UNKNOWN", "the original transport request is unavailable")
            if not pending.done():
                if value.unknown:
                    pending.set_exception(Problem(503, "AGENT_WORKER_EXECUTION_UNKNOWN", "read the original turn receipt"))
                else:
                    pending.set_result(value.value)

    def close(self):
        with self.changed:
            self.closed = True
            self.queue.clear()
            for pending in self.pending.values():
                if not pending.done():
                    pending.set_exception(Problem(503, "AGENT_WORKER_EXECUTION_UNKNOWN", "read the original turn receipt"))
            self.pending.clear()
            self.changed.notify_all()


class AgentControlRelay:
    def __init__(self, *, wait_seconds=20, clock=monotonic):
        if not 0 <= wait_seconds <= 25:
            raise ValueError("control long-poll duration must be between zero and 25 seconds")
        self.wait_seconds = wait_seconds
        self.clock = clock
        self._lock = RLock()
        self._peers = {}

    def connect(self, binding, process_id, caller_oid):
        with self._lock:
            key = (binding.team_id, binding.agent_id)
            previous = self._peers.get(key)
            if previous is not None and self.clock() - previous.observed_at >= 60:
                self.disconnect(previous)
            if key in self._peers:
                raise Problem(409, "AGENT_WORKER_CONNECTION_BUSY", "this Agent already has a control connection")
            context = AgentWorkerContext(vm=binding, process_id=process_id,
                                         control_connection_id=uuid4().hex, caller_oid=caller_oid)
            self._peers[key] = _Peer(context, self.clock())
            return context

    def peer(self, binding, *, connection_id=None, caller_oid=None):
        with self._lock:
            peer = self._peers.get((binding.team_id, binding.agent_id))
            if peer is not None and self.clock() - peer.observed_at >= 60:
                self.disconnect(peer)
                peer = None
            if (peer is None or peer.context.vm != binding or peer.closed
                    or connection_id is not None and peer.context.control_connection_id != connection_id
                    or caller_oid is not None and peer.context.caller_oid != caller_oid):
                raise Problem(409, "AGENT_WORKER_BINDING_CHANGED", "the original Agent VM connection differs")
            if connection_id is not None:
                peer.observed_at = self.clock()
            return peer

    def deliver(self, binding, request):
        return self.peer(binding).put(_Command(command_id=uuid4().hex, action="turn",
            address=request.address, payload=request.payload.get_secret_value()))

    def read(self, binding, address):
        return self.peer(binding).put(_Command(command_id=uuid4().hex, action="read", address=address))

    def cancel(self, binding, address):
        return self.peer(binding).put(_Command(command_id=uuid4().hex, action="cancel", address=address))

    def disconnect(self, peer):
        with self._lock:
            key = (peer.context.vm.team_id, peer.context.vm.agent_id)
            if self._peers.get(key) is peer:
                del self._peers[key]
            peer.close()

    def close(self):
        with self._lock:
            peers = tuple(self._peers.values())
        for peer in peers:
            self.disconnect(peer)


def install_control_routes(app, relay, verifier, expected_vm):
    from fastapi import Request
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import Response
    from .behavior_api import _body, _principal_dependency, _response

    verified = _principal_dependency(verifier)

    def identity(request):
        principal = verified(request)
        if principal.kind != "agent":
            raise Problem(403, "AGENT_WORKER_BINDING_CHANGED", "only a verified Agent can connect its worker")
        binding = expected_vm(principal)
        principal.require(binding.team_id, "execute")
        return principal, binding

    def connected(request):
        principal, binding = identity(request)
        values = request.headers.getlist(CONNECTION_HEADER)
        if len(values) != 1:
            raise Problem(403, "AGENT_WORKER_BINDING_CHANGED", "one original connection header is required")
        return relay.peer(binding, connection_id=values[0], caller_oid=principal.oid)

    @app.post(PREFIX + "/connect")
    async def connect(request: Request):
        principal, binding = identity(request)
        hello = await _body(request, _Hello)
        if hello.vm != binding:
            raise Problem(403, "AGENT_WORKER_BINDING_CHANGED", "the requested VM is not this Agent's current VM")
        return _response(relay.connect(binding, hello.process_id, principal.oid))

    @app.get(PREFIX + "/next")
    async def next_command(request: Request):
        peer = connected(request)
        if request.query_params:
            raise Problem(422, "BEHAVIOR_INVALID_REQUEST", "control reads do not take a body or query")
        async for chunk in request.stream():
            if chunk:
                raise Problem(422, "BEHAVIOR_INVALID_REQUEST", "control reads do not take a body or query")
        command = await run_in_threadpool(peer.next, relay.wait_seconds)
        return Response(status_code=204, headers={"Cache-Control": "no-store"}) if command is None else _response(command)

    @app.post(PREFIX + "/reply")
    async def reply(request: Request):
        peer = connected(request)
        value = await _body(request, _Reply, max_body_bytes=MAX_FRAME_BYTES)
        peer.reply(value)
        return _response({})

    @app.post(PREFIX + "/close")
    async def close(request: Request):
        peer = connected(request)
        await _body(request, Empty)
        relay.disconnect(peer)
        return _response({})


def _decode(raw, model):
    def unique(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("duplicate control field")
        return value
    try:
        json.loads(raw, object_pairs_hook=unique)
        return model.model_validate_json(raw)
    except (ValueError, TypeError):
        raise Problem(502, "AGENT_WORKER_CONTROL_INVALID", "the original control response is invalid") from None


class AgentControlClient:
    def __init__(self, api_url, worker, *, token_provider, transport=None):
        url = httpx.URL(api_url)
        if (url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment
                or url.path not in ("", "/") or url.port not in (None, 443)):
            raise ValueError("the worker requires its fixed HTTPS API origin")
        self.worker, self.token_provider = worker, token_provider
        self.http = httpx.Client(base_url=api_url.rstrip("/"), transport=transport,
                                 timeout=30, follow_redirects=False, trust_env=False)
        self.connection = None

    def _request(self, method, action, value=None, *, context=None):
        token = self.token_provider()
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept-Encoding": "identity"}
        context = context or (None if self.connection is None else self.connection.context)
        if context is not None:
            headers[CONNECTION_HEADER] = context.control_connection_id
        try:
            with self.http.stream(method, PREFIX + "/" + action, headers=headers,
                                  content=None if value is None else value.model_dump_json().encode()) as response:
                if (response.headers.get("content-encoding", "identity") != "identity"
                        or response.headers.get("cache-control") != "no-store"):
                    raise Problem(502, "AGENT_WORKER_CONTROL_INVALID", "control responses must be private unencoded data")
                raw = bytearray()
                for part in response.iter_bytes():
                    raw.extend(part)
                    if len(raw) > MAX_FRAME_BYTES:
                        raise Problem(502, "AGENT_WORKER_CONTROL_INVALID", "control response exceeds its byte bound")
                if response.status_code not in (200, 204):
                    raise Problem(503, "AGENT_WORKER_CONTROL_UNKNOWN", "control response is not a turn receipt",
                                  upstream_status=response.status_code)
                if (response.status_code == 204 and raw or response.status_code == 200
                        and response.headers.get("content-type", "").split(";")[0] != "application/json"):
                    raise Problem(502, "AGENT_WORKER_CONTROL_INVALID", "the original control response shape differs")
                return None if response.status_code == 204 else bytes(raw)
        except httpx.HTTPError:
            raise Problem(503, "AGENT_WORKER_CONTROL_UNKNOWN", "control transport failed; read the original receipt") from None

    def connect(self):
        if self.connection is not None:
            raise Problem(409, "AGENT_WORKER_CONNECTION_BUSY", "the worker already has a control connection")
        hello = _Hello(vm=self.worker.binding, process_id=self.worker.process_id)
        context = _decode(self._request("POST", "connect", hello), AgentWorkerContext)
        self.connection = self.worker.connect(self.worker.binding, context=context)

    def _reply(self, connection, command_id, pending):
        if self.connection is not connection:
            logging.getLogger(__name__).warning("AGENT_WORKER_REPLY_UNCONFIRMED original_connection_closed=true")
            return
        try:
            result = pending.result()
            reply = _Reply(command_id=command_id, value=result)
        except Exception:
            reply = _Reply(command_id=command_id, unknown=True)
        try:
            self._request("POST", "reply", reply, context=connection.context)
        except Exception as error:
            logging.getLogger(__name__).warning("AGENT_WORKER_REPLY_UNCONFIRMED exception_type=%s", _exception_type(error))

    def receive_once(self):
        if self.connection is None:
            raise Problem(503, "AGENT_WORKER_UNAVAILABLE", "the original worker is not connected")
        connection = self.connection
        raw = self._request("GET", "next", context=connection.context)
        if raw is None:
            return
        if self.connection is not connection:
            raise Problem(409, "AGENT_WORKER_BINDING_CHANGED", "the original receiving connection changed")
        command = _decode(raw, _Command)
        if command.action == "turn":
            pending = connection.deliver(AgentTurnDelivery(address=command.address, payload=SecretBytes(command.payload)))
            pending.add_done_callback(lambda done: self._reply(connection, command.command_id, done))
        else:
            pending = Future()
            try:
                pending.set_result(getattr(connection, command.action)(command.address))
            except Exception as error:
                pending.set_exception(error)
            self._reply(connection, command.command_id, pending)

    def run(self, stopping):
        try:
            while not stopping.is_set():
                self.receive_once()
        except Exception as error:
            logging.getLogger(__name__).warning(
                "AGENT_WORKER_CONTROL_DISCONNECTED exception_type=%s", _exception_type(error))
            raise
        finally:
            self.disconnect()

    def disconnect(self):
        if self.connection is not None:
            try:
                self._request("POST", "close", Empty())
            except Exception as error:
                logging.getLogger(__name__).warning("AGENT_WORKER_CLOSE_UNCONFIRMED exception_type=%s", _exception_type(error))
            self.connection.close()
            self.connection = None

    def close(self):
        self.disconnect()
        self.http.close()
