"""Read-only traffic for an existing running Hosted instruction; never an observer result."""

import logging
from time import monotonic

from .behavior_foundry import FoundryHttpClient, native_key
from .behavior_assistant import _Session, key as assistant_key
from .behavior_http_diagnostics import _exception_type
from .errors import Problem

FALLBACK_INTERVAL_SECONDS = 30
# Also remain below the existing 300-second process-local observer activity window.
MAX_INTERVAL_SECONDS = 150


class InstructionSessionKeepalive:
    def __init__(self, composition, binding_id):
        self.composition, self.binding_id = composition, binding_id
        self.target = None
        self.interval = FALLBACK_INTERVAL_SECONDS
        self.last_attempt = None
        self.peer = None
        self.parent = None

    def _current(self):
        c, identity = self.composition, self.binding_id
        if c.closed or c._instruction_stopping or identity in getattr(c, "_instruction_lost", ()):
            return None
        if self.parent is not None and self.parent._current() is None:
            return None
        runtime = c.runtime
        runtime.refresh_bindings(identity)
        binding = runtime.bindings.get(identity)
        if binding is None:
            return None
        workers, native = runtime.dep.workers, runtime.dep.native_launches
        if native is None or not workers.instruction(binding.job_id):
            return None
        job = workers.inspect(binding.job_id)
        if job.cancel_requested or job.binding is None or job.state not in {"starting", "running"}:
            return None
        owner = runtime.resolve_owner(binding.owner.owner_record_id, binding_id=identity)
        if owner != job.owner:
            return None
        session_key = assistant_key("session", assistant_key("session-id", job.binding.start_request_id))
        rows = runtime.store.read_snapshot((session_key,))
        if rows[session_key] is None:
            return None
        session = native._row(rows, session_key, _Session)
        if (session.execution != job.binding or session.state != "running"
                or session.assignment.task.instruction is None):
            return None
        launch = native.launch(native_key("launch-id", job.binding.start_request_id))
        if (launch.phase != "claimed" or launch.native_invocation_id is None
                or launch.start.binding != job.binding or launch.pending_control != "observe"):
            return None
        native._current(runtime.store.read_snapshot(native._keys(launch)), launch, dispatch=True)
        return launch, session

    def _read(self):
        return self.composition.runtime.store.consistent_read(self._current, max_keys=None, row_scoped=True)

    def tick(self):
        self._tick()
        c = self.composition
        if c.config is None or getattr(c.runtime, "mode", None) != "native":
            return
        try:
            observed = c.runtime.store.consistent_read(self._peer, max_keys=None, row_scoped=True)
            if observed is not None:
                agent_id, binding_id = observed
                composition = c.for_agent(agent_id)
                if (self.peer is None or self.peer.composition is not composition
                        or self.peer.binding_id != binding_id):
                    self.peer = InstructionSessionKeepalive(composition, binding_id)
                    self.peer.parent = self
                self.peer._tick()
        except Exception as error:
            logging.getLogger(__name__).warning(
                "HOSTED_SESSION_KEEPALIVE_UNAVAILABLE binding_id=%s phase=peer_read exception_type=%s",
                self.binding_id, _exception_type(error))

    def _peer(self):
        from .behavior_agent_message_api import collaboration_key, _read_root, _receiver_record
        from .behavior_agent_messages import AgentMessageLedger

        c = self.composition
        if getattr(c, "_agent_message_owner", None) is not None:
            return None
        key = collaboration_key(self.binding_id)
        if c.runtime.store.read_snapshot((key,))[key] is None:
            return None
        current = self._current()
        if current is None:
            return None
        launch, session = current
        root = _read_root(c.runtime.store, key)
        if (root.origin.binding_id != self.binding_id or root.origin.execution != launch.start.binding
                or root.origin.session_id != session.assignment.session_id):
            raise Problem(409, "HOSTED_KEEPALIVE_INACTIVE", "the original peer root differs")
        identity = root.directory.agents[1].agent_id
        peer = c.for_agent(identity)
        ledger = AgentMessageLedger(c.runtime.store, root.directory, clock=c.runtime._now)
        record = _receiver_record(root, ledger, peer)
        if record is None:
            return None
        task = record.profile.instruction
        if (task is None or task.peer_work is None or task.peer_work.root_id != root.request.request_id
                or task.peer_work.recipient_agent_id != identity or peer.agent_id != identity):
            raise Problem(409, "HOSTED_KEEPALIVE_INACTIVE", "the original receiver work differs")
        return identity, record.binding.binding_id

    def _tick(self):
        c = self.composition
        if c.config is None or getattr(c.runtime, "mode", None) != "native":
            return
        now = monotonic()
        if self.last_attempt is not None and now - self.last_attempt < self.interval:
            return
        phase = "authority"
        try:
            observed = self._read()
            if observed is None:
                return
            launch, running = observed
            original = next((client for client in c.clients if isinstance(client, FoundryHttpClient)
                             and client.target.executor_id == launch.target.executor_id), None)
            if original is None:
                raise Problem(503, "HOSTED_KEEPALIVE_UNAVAILABLE", "the original session client is unavailable")
            client = original if original.target == launch.target else original.for_target(launch.target)
            if self.target != launch.target:
                self.target = launch.target
                try:
                    seconds = client.session_idle_timeout()
                    self.interval = min(MAX_INTERVAL_SECONDS, seconds / 3)
                except Problem:
                    self.interval = FALLBACK_INTERVAL_SECONDS
                    logging.getLogger(__name__).warning(
                        "HOSTED_SESSION_KEEPALIVE_INTERVAL_FALLBACK binding_id=%s interval_seconds=%s",
                        self.binding_id, self.interval)
            recent = [value for value in (self.last_attempt, client.last_session_request()) if value is not None]
            if recent and now - max(recent) < self.interval:
                return
            self.last_attempt = now
            phase = "session_status"
            platform = client.session_status()
            if platform.status != "active":
                logging.getLogger(__name__).info(
                    "HOSTED_SESSION_KEEPALIVE_SKIPPED binding_id=%s active=false", self.binding_id)
                return

            def current():
                observation = self._read()
                actual = None if observation is None else observation[0]
                if actual is None or (
                    actual.start.binding, actual.target, actual.native_invocation_id,
                    actual.claim_id, actual.process_id, actual.process_generation,
                ) != (
                    launch.start.binding, launch.target, launch.native_invocation_id,
                    launch.claim_id, launch.process_id, launch.process_generation,
                ):
                    raise Problem(409, "HOSTED_KEEPALIVE_INACTIVE", "the original instruction is no longer active")

            phase = "ingress"
            client.keepalive(launch.native_invocation_id, launch.launch_id, before_send=current)
            c.runtime.dep.native_launches.assistant.record_instruction_activity(running)
            logging.getLogger(__name__).info(
                "HOSTED_SESSION_KEEPALIVE_CONFIRMED binding_id=%s interval_seconds=%s", self.binding_id, self.interval)
        except Exception as error:
            self.last_attempt = monotonic()
            logging.getLogger(__name__).warning(
                "HOSTED_SESSION_KEEPALIVE_UNAVAILABLE binding_id=%s phase=%s exception_type=%s",
                self.binding_id, phase, _exception_type(error))
