"""One irreversible SDK-entry gate in the original private executor journal."""

import logging
from threading import Event, current_thread

from .behavior_code_identity import code_sha256
from .behavior_wire import SdkNoStart, SdkStartBinding, SdkStartOwner, SdkStartRecord, SdkVmNoStart, SdkVmStartOwner
from .errors import Problem

# Categorical refusal codes only; Hosted diagnostics may log these, never gate material.
SDK_START_GATE_ERRORS = frozenset({
    "SDK_START_PROFILE_DENIED", "SDK_START_GATE_REQUIRED", "SDK_START_GATE_REPLAY", "SDK_START_GATE_UNKNOWN",
    "SDK_START_GATE_CODE_CHANGED", "SDK_START_GATE_MISSING", "SDK_START_GATE_INVALID", "SDK_START_GATE_FENCED",
})


class SdkStartGate:
    def __init__(self, journal, binding: SdkStartBinding, owner: SdkStartOwner | SdkVmStartOwner):
        self.journal = journal
        self.binding = SdkStartBinding.model_validate(binding.model_dump())
        self.owner = (SdkVmStartOwner if isinstance(owner, SdkVmStartOwner)
                      else SdkStartOwner).model_validate(owner.model_dump())
        self.thread = current_thread()
        self.entry_thread = self.thread
        self.uncertain = False
        self._turn_completed = Event()
        self._code()
        writing = False
        try:
            with journal.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE IF NOT EXISTS assistant_sdk_start_gates "
                           "(session_id TEXT PRIMARY KEY, body TEXT NOT NULL)")
                if db.execute("SELECT 1 FROM assistant_sdk_start_gates WHERE session_id=?",
                              (binding.session_id,)).fetchone() is not None:
                    raise Problem(409, "SDK_START_GATE_REPLAY", "the original SDK entry gate cannot be reserved again")
                value = SdkStartRecord(schema_version="fa-sdk-start-gate-record-" + self.binding.version,
                                       binding=self.binding, owner=self.owner, state="open", refusal_code=None)
                writing = True
                db.execute("INSERT INTO assistant_sdk_start_gates VALUES (?, ?)",
                           (binding.session_id, value.model_dump_json()))
        except Exception:
            if writing:
                self.uncertain = True
                logging.getLogger(__name__).warning("SDK_START_GATE_WRITE_UNKNOWN transition=reserve")
            raise

    def _code(self):
        if self.uncertain:
            raise Problem(503, "SDK_START_GATE_UNKNOWN", "the original journal write remains unresolved")
        if self.binding.code_sha256 != code_sha256():
            raise Problem(403, "SDK_START_GATE_CODE_CHANGED", "SDK entry requires its exact assigned installed code")

    def _read(self, db):
        row = db.execute("SELECT body FROM assistant_sdk_start_gates WHERE session_id=?",
                         (self.binding.session_id,)).fetchone()
        if row is None:
            raise Problem(409, "SDK_START_GATE_MISSING", "the original acknowledged SDK entry gate is required")
        try:
            value = SdkStartRecord.model_validate_json(row[0])
        except ValueError:
            raise Problem(403, "SDK_START_GATE_INVALID", "unknown or invalid SDK gate material") from None
        if value.binding != self.binding or value.owner != self.owner:
            raise Problem(403, "SDK_START_GATE_FENCED", "original SDK assignment and supervisor differ")
        return value

    def _transition(self, state, refusal_code=None):
        self._code()
        self._active_turn()
        writing = False
        try:
            with self.journal.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                previous = self._read(db)
                if previous.state != "open":
                    raise Problem(409, "SDK_START_GATE_FENCED", "the SDK entry gate is already consumed or closed")
                value = SdkStartRecord(schema_version="fa-sdk-start-gate-record-" + self.binding.version,
                    binding=self.binding, owner=self.owner, state=state, refusal_code=refusal_code)
                writing = True
                db.execute("UPDATE assistant_sdk_start_gates SET body=? WHERE session_id=?",
                           (value.model_dump_json(), self.binding.session_id))
        except Exception:
            if writing:
                self.uncertain = True
                logging.getLogger(__name__).warning("SDK_START_GATE_WRITE_UNKNOWN transition=%s", state)
            raise
        return value

    def allow_entry_from(self, thread):
        """The owned dispatch runs its SDK on the resident loop thread it blocks on (ADR-0005 PoC).

        Completion is still observed on the original owned dispatch thread.
        """
        if current_thread() is not self.thread:
            raise Problem(403, "SDK_START_GATE_FENCED", "only the original owned dispatch may delegate its SDK entry")
        self._active_turn()
        self.entry_thread = thread

    def enter(self, sdk_session_id):
        if sdk_session_id != self.binding.sdk_session_id or current_thread() is not self.entry_thread:
            raise Problem(403, "SDK_START_GATE_FENCED", "only the original owned dispatch may enter its SDK")
        self._transition("entry_attempted")

    def close(self, refusal_code):
        self._transition("closed_before_entry", refusal_code)

    def observe_closed(self):
        if isinstance(self.owner, SdkVmStartOwner):
            raise Problem(403, "SDK_START_GATE_FENCED", "a VM turn must use its own completion observation")
        self._code()
        if self.thread is current_thread() or self.thread.ident is None:
            return None
        self.thread.join(timeout=0)
        if self.thread.is_alive():
            return None
        with self.journal.connect() as db:
            value = self._read(db)
        return (SdkNoStart(schema_version="fa-sdk-no-start-" + self.binding.version, gate=value, owned_thread_completed=True)
                if value.state == "closed_before_entry" else None)

    def _active_turn(self):
        if isinstance(self.owner, SdkVmStartOwner) and self._turn_completed.is_set():
            raise Problem(409, "SDK_START_GATE_FENCED", "the original VM turn has already completed")

    def finish_turn(self):
        if not isinstance(self.owner, SdkVmStartOwner) or current_thread() is not self.thread:
            raise Problem(403, "SDK_START_GATE_FENCED", "only the original VM dispatch may finish this turn")
        self._turn_completed.set()

    def observe_turn_closed(self):
        self._code()
        if not isinstance(self.owner, SdkVmStartOwner):
            raise Problem(403, "SDK_START_GATE_FENCED", "this is not an admitted VM turn")
        if not self._turn_completed.is_set():
            return None
        with self.journal.connect() as db:
            value = self._read(db)
        return SdkVmNoStart(gate=value, owned_turn_completed=True) if value.state == "closed_before_entry" else None


def sdk_owner_for_started_turn(started):
    """Use only a successful T1 begin reply; this DTO is not a substitute for that CAS."""
    from .behavior_turn_wire import StartedTurn, wire_fingerprint

    started = StartedTurn.model_validate_json(started.model_dump_json())
    assignment = started.ticket.assignment
    binding = assignment.sdk_start_binding
    instruction = assignment.task.instruction
    if (binding is None or instruction is None or binding.job_id != started.start.job_id
            or binding.profile != instruction.sdk_start_profile
            or binding.task_digest != wire_fingerprint(assignment.task)
            or binding.profile_digest != assignment.task.profile_digest
            or started.start.ticket.ticket_sha256 != started.ticket.sha256
            or binding.session_id != assignment.session_id or binding.sdk_session_id != started.start.address.sdk_session_id
            or started.start.address != started.ticket.address or started.start.worker.vm != started.ticket.vm
            or started.start.worker.caller_oid != started.ticket.agent_oid):
        raise Problem(403, "SDK_START_PROFILE_DENIED", "the real admitted turn and original SDK binding are required")
    return SdkVmStartOwner(supervisor_id=started.start.worker.process_id, ticket_id=started.start.ticket.ticket_id,
                           start_receipt_sha256=started.start.sha256)
