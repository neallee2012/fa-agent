"""One already-verified Agent VM's transport and queue; never a ticket or receipt issuer."""

from concurrent.futures import Future, ThreadPoolExecutor
import logging
from threading import Condition, RLock
from typing import Annotated, Protocol
from uuid import uuid4

from pydantic import Field, SecretBytes

from . import behavior_wire as wire
from .behavior_http_diagnostics import _exception_type
from .errors import Problem


class AgentVmBinding(wire.WireModel):
    """API-managed logical session generation, not a PID, turn claim, or liveness proof."""

    team_id: wire.Identifier
    agent_id: wire.Identifier
    hosted_session_id: wire.Identifier
    hosted_agent_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]
    vm_generation: wire.Identifier


class AgentWorkerContext(wire.WireModel):
    """Observed by the authenticated bootstrap/connection, never chosen by a turn payload."""

    vm: AgentVmBinding
    process_id: wire.Identifier
    control_connection_id: wire.Identifier
    caller_oid: wire.Identifier


class AgentTurnAddress(wire.WireModel):
    conversation_id: wire.Identifier
    sdk_session_id: wire.Identifier
    request_id: wire.Identifier


class AgentTurnDelivery(wire.WireModel):
    address: AgentTurnAddress
    payload: Annotated[SecretBytes, Field(min_length=1, max_length=65536)]


class AgentTurnConsumer(Protocol):
    def consume(self, context: AgentWorkerContext, request: AgentTurnDelivery) -> bytes:
        """Return only a fresh CAS start receipt; existing or unknown consumption must raise."""
        ...

    def execute(self, context: AgentWorkerContext, request: AgentTurnDelivery, start_receipt: bytes) -> bytes: ...

    def read(self, context: AgentWorkerContext, address: AgentTurnAddress) -> bytes: ...

    def cancel(self, context: AgentWorkerContext, address: AgentTurnAddress) -> bytes: ...


class AgentWorkerConnection:
    def __init__(self, worker: "AgentWorkerControl", context=None):
        self._worker = worker
        self.context = context or AgentWorkerContext(
            vm=worker.binding, process_id=worker._process_id,
            control_connection_id=uuid4().hex, caller_oid=worker._caller_oid,
        )

    def deliver(self, request: AgentTurnDelivery) -> Future[bytes]:
        with self._worker._lock:
            self._worker._connected(self)
            reply: Future[bytes] = Future()
            self._worker._turns.submit(self._worker._execute, request, reply)
            return reply

    def read(self, address: AgentTurnAddress) -> bytes:
        with self._worker._lock:
            self._worker._connected(self)
            return self._worker.consumer.read(self.context, address)

    def cancel(self, address: AgentTurnAddress) -> bytes:
        with self._worker._lock:
            self._worker._connected(self)
            return self._worker.consumer.cancel(self.context, address)

    def close(self):
        with self._worker._lock:
            if self._worker._connection is self:
                self._worker._connection = None


class AgentWorkerControl:
    def __init__(self, binding: AgentVmBinding, consumer: AgentTurnConsumer, *, process_id: str, caller_oid: str):
        self.binding, self.consumer = binding, consumer
        self._process_id, self._caller_oid = process_id, caller_oid
        self._lock = RLock()
        self._connection_changed = Condition(self._lock)
        self._turns = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fa-agent-turn")
        self._connection: AgentWorkerConnection | None = None
        self._closed = False

    @property
    def caller_oid(self):
        return self._caller_oid

    @property
    def process_id(self):
        return self._process_id

    def connect(self, binding: AgentVmBinding, *, context=None) -> AgentWorkerConnection:
        with self._lock:
            if binding != self.binding:
                raise Problem(403, "AGENT_WORKER_BINDING_CHANGED", "the original Agent VM binding differs")
            if self._closed:
                raise Problem(503, "AGENT_WORKER_UNAVAILABLE", "the original worker is closed")
            if self._connection is not None:
                raise Problem(409, "AGENT_WORKER_CONNECTION_BUSY", "the original VM already has a control connection")
            if context is not None and (context.vm != binding or context.process_id != self.process_id
                                        or context.caller_oid != self.caller_oid):
                raise Problem(403, "AGENT_WORKER_BINDING_CHANGED", "the server control context differs from this worker")
            self._connection = AgentWorkerConnection(self, context)
            self._connection_changed.notify_all()
            return self._connection

    def _connected(self, connection):
        if self._closed or self._connection is not connection:
            raise Problem(503, "AGENT_WORKER_UNAVAILABLE", "the original control connection is no longer active")

    def _execute(self, request, reply):
        waiting = reply.set_running_or_notify_cancel()
        try:
            with self._connection_changed:
                self._connection_changed.wait_for(lambda: self._closed or self._connection is not None)
                self._connected(self._connection)
                context = self._connection.context
                start = self.consumer.consume(context, request)
            result = self.consumer.execute(context, request, start)
        except Exception as error:
            logging.getLogger(__name__).warning(
                "AGENT_WORKER_EXECUTION_UNKNOWN request_id=%s exception_type=%s",
                request.address.request_id, _exception_type(error))
            if waiting:
                reply.set_exception(error)
        else:
            if waiting:
                reply.set_result(result)

    def close(self):
        with self._lock:
            self._closed = True
            self._connection = None
            self._connection_changed.notify_all()
        self._turns.shutdown(wait=True)
