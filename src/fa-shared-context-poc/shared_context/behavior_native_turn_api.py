"""N2 owner-checked forwarding. Native records stay in the Agent, never in an API index."""

import contextlib
import json
import hashlib
import logging
import os
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
from threading import Lock
from time import monotonic

from fastapi import Depends, Query, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTasks
from starlette.concurrency import run_in_threadpool

from .behavior_api import _body, _nonfinite, _principal_dependency, _response, _unique
from .behavior_assistant_wire import InstructionSubmit
from .behavior_foundry import FoundryHttpClient, MAX_NATIVE_TURN_HTTP_BYTES
from .behavior_instruction import conversation_key
from .behavior_native_capture import resolve_capture
from .behavior_timing import phase_timing, timing_id
from .behavior_wire import WireModel
from .behavior_native_turn_wire import (
    NATIVE_REQUEST, NATIVE_STATE_PROFILE, NATIVE_TURN_ERRORS, NativeAgentReceive, NativeRoutineSubmit, NativeCancel, NativeConversation, NativeConversationRead,
    NativeDelivered, NativeHistory, NativeHistoryPage, NativeRead, NativeReceipt, NativeResult,
    NativeResultPage, NativeSubmit, NativeContinuationRead, NativeContinuationSubmit,
    NativeSessionRead, NativeSessionPrepare, NativeSessionPreparation, NativeSessionState, NativeCandidateRead,
    NativeEnvironmentCommand, NativeEnvironmentRetargetCommand, NativeEnvironmentView, NativeSessionFollow,
    NativeSessionRetarget, NativeHitlHistoryRead, MAX_HITL_REVIEW_PAGE,
    NativeEnvironmentWithoutFiles, NativeSessionAbandon, NativeSessionFilesOmission, NativeTurnStatus,
    NativeEnvironmentNotice, NativeUnfinishedRead, NativeUnfinishedSessions, SLOT_NOTICE_KINDS,
    NOTICE_KINDS_HEADER, NOTICE_KINDS_VERSION,
    MAX_RECEIVED_PAGE, NativeReceivedPage, NativeReceivedRead,
    NativePrewarm, NativePrewarmResult, NativePrewarmToolbox, NativePrewarmView, MAX_PREWARM_TOOLBOXES,
)
from .memory_candidate_wire import NativeCandidateView
from .behavior_work_target import selected_targets
from .behavior_text_files import MAX_USER_INPUT_BYTES
from .assistant_image_wire import MAX_IMAGE_BYTES
from .behavior_user_images import NativeUserImageStored, NativeUserImageUpload, stored
from .errors import Problem
from .native_skill_draft_wire import NativeSkillDraftIntentRead, NativeSkillDraftProof
from .native_image_wire import NativeImageContent, NativeImageRead, NativeImagesRead, NativeTurnImages
from .native_append_wire import NativeAppend, NativeAppendRead, NativeAppendReceipt, NativeAppends
from .native_progress_wire import NativeProgressRead, NativeProgressSnapshot, NativeTurnStatusRead
from .native_hitl_wire import NativeHitlReviewHistory
from .native_files_wire import NativeSessionFiles, NativeFilesScope, NativeFilesView
from .native_continuation_wire import NativeContinuationView, NativeContinuationReady, NativeContinuationReadyReceipt

PREFIX = "/work-policy/v1/native-turns"
MAX_SUBMIT_BODY_BYTES = 3 * MAX_USER_INPUT_BYTES + 4096
MAX_IMAGE_UPLOAD_BODY_BYTES = (MAX_IMAGE_BYTES + 2) // 3 * 4 + 4096
# Admission refusals while reading an attached Team source (`read_scoped_sources`) that the shared work-policy list
# does not carry; the original code stays visible so the console can explain it instead of a generic error.
_ATTACHED_SOURCE_ERRORS = {
    "SOURCE_SCOPE_DENIED": "The attached source is outside this Agent's source scope. No work was accepted.",
    "SOURCE_ACCESS_CHANGED": "Access to the attached source changed while it was read. No work was accepted.",
    "SOURCE_REQUIRED_MISSING": "The attached source or one it depends on is missing. No work was accepted.",
    "SOURCE_GRAPH_INVALID": "The attached source has inconsistent dependencies. No work was accepted.",
    "SOURCE_RECORD_INVALID": "The attached source record could not be verified. No work was accepted.",
    "SOURCE_RECEIPT_MISMATCH": "The attached source receipt could not be verified. No work was accepted.",
    "SOURCE_INTEGRITY_ERROR": "The attached source content could not be verified. No work was accepted.",
    "SOURCE_BLOB_UNAVAILABLE": "The attached source content is not readable now. No work was accepted.",
    "BEHAVIOR_UNSAFE_CONTENT": "The attached source content is not allowed by the output policy. No work was accepted.",
}


def configured_native_turns():
    value = os.environ.get("FA_NATIVE_TURNS")
    if value not in (None, NATIVE_STATE_PROFILE):
        raise Problem(503, "NATIVE_TURNS_DISABLED", "FA_NATIVE_TURNS must name the supported state profile")
    return value == NATIVE_STATE_PROFILE


class _BeforeAdmission(Problem):
    """This command was sent to no session (a submit reached no native admission); the same RID and body may be sent
    again."""

    def __init__(self, cause, request_id, *, server_following=False):
        super().__init__(cause.status, cause.body["error"]["code"], str(cause))
        self.body = cause.body
        self.request_id, self.server_following = request_id, server_following


def _not_admitted(error, request_id):
    """Mark a definite refusal before ``rid:<RID>`` exists (4xx, or the whole-Agent version follow); an unknown
    outcome or a taken RID stays as is. It means only that this attempt sent nothing: resend the same RID and body."""
    code = error.body["error"]["code"]
    if (400 <= error.status < 500 or code == "AGENT_VERSION_FOLLOWING") and code != "NATIVE_RID_CONFLICT":
        return _BeforeAdmission(error, request_id)
    return error


class _OmissionBeforeDispatch(Problem):
    def __init__(self, request_id, code, message, *, files_check=None):
        super().__init__(409, code, message, **({} if files_check is None else {"files_check": files_check}))
        self.request_id = request_id


class _NotCurrentReader(Problem):
    """The reader's session read names another environment; only a cached reader is read again."""


# Reads an owner follow-up submit starts while ``sessions.current`` confirms the Agent's session (RTT A): the
# conversation on the kept reader, and the capture from the fixed template and selection this conversation had at
# its last read. Each is used only when it equals what the ordinary order reads; any other outcome (another session,
# another template, a failure) is read again in order. Reads only: nothing is sent to Hosted or written early.
_PREFETCH = ThreadPoolExecutor(max_workers=16, thread_name_prefix="fa-submit-prefetch")
KEPT_CONVERSATIONS = 256


class _SubmitPrefetch:
    def __init__(self, conversation=None, capture=None, basis=None, *, target=None):
        self._conversation, self._capture, self._basis, self._target = conversation, capture, basis, target

    @staticmethod
    def _result(future):
        try:
            return None if future is None else future.result()
        except Exception:
            return None

    def conversation(self, client):
        target = self._target
        if target is None or (target.session_id, target.agent_version) != (
                client.target.session_id, client.target.agent_version):
            return None
        return self._result(self._conversation)

    def capture(self, conversation):
        # ``resolve_capture`` reads only the conversation's ID and capture, so equal ones give an equal result.
        basis = self._basis
        if basis is None or (basis.conversation_id, basis.capture) != (conversation.conversation_id, conversation.capture):
            return None
        return self._result(self._capture)


def _kept_conversations(composition):
    value = getattr(composition, "_native_kept_conversations", None)
    if value is None:
        value = composition.__dict__.setdefault("_native_kept_conversations", (OrderedDict(), Lock()))
    return value


# Typing prewarm (README#typing-prewarm): at most one wake per Agent per this many seconds in one API process, so a
# client that asks on every keystroke still sends Hosted one request a minute. The workbench itself asks every 2 min.
PREWARM_INTERVAL_SECONDS = 60


class _PrewarmBody(WireModel):
    """``{}``: the owner names nothing; the API picks the session and the toolbox versions."""


def _prewarm_toolboxes(runtime):
    """Every toolbox version a capture of this Agent can name now. A capture is the selection ∩ head ∩ these available
    grants plus the Team's enabled connectors, so this superset needs no template, selection or conversation read;
    listing one more is a read only."""
    from .behavior_capabilities import runtime_grants
    from .team_connector import current_team_connector_grants
    try:
        grants = (*runtime_grants(runtime).values(), *current_team_connector_grants(runtime))
    except Problem:
        return ()
    entries = sorted({(grant.toolbox.toolbox, grant.toolbox.version or "") for grant in grants
                      if grant.kind == "mcp-toolbox" and grant.toolbox is not None})
    return tuple(NativePrewarmToolbox(toolbox=name, version=version or None)
                 for name, version in entries[:MAX_PREWARM_TOOLBOXES])


# ADR-0009 §4 steps 5-6: refusals of a row-located submit resolved in the request (never shown as version refusals).
_BOUND_REFUSALS = frozenset({(409, "NATIVE_SESSION_CHANGED"), (403, "NATIVE_TURNS_DISABLED")})
# ADR-0009 §7a (e): the read-time status of an unfinished receipt an earlier (previous) SID accepted.
EARLIER_VERSION_UNFINISHED = "NATIVE_EARLIER_VERSION_UNFINISHED"
# Hosted's mark of a left ``started`` receipt whose progress stopped for N minutes (``native_left_receipts``).
SOURCE_STALLED = "NATIVE_SOURCE_STALLED"


class NativeTurnGateway:
    def __init__(self, composition, *, enabled):
        self.composition, self.enabled = composition, enabled
        self._prewarmed, self._prewarm_lock = None, Lock()

    def prewarm(self, principal):
        """The owner is typing (README#typing-prewarm): one request to this Agent's current session, which wakes its
        compute (Hosted provisions compute when a session is referenced), naming its toolbox versions so Hosted lists
        them before the turn. No RID, record, queue position, model call or session change, and a send never waits
        for it. Only the entry check can refuse; anything after it answers ``skipped``."""
        control, base = self._context(principal, "submit", prepared=False)
        now = monotonic()
        with self._prewarm_lock:
            if self._prewarmed is not None and now - self._prewarmed < PREWARM_INTERVAL_SECONDS:
                return NativePrewarmResult(state="recent"), None
            self._prewarmed = now
        from .behavior_native_sessions import native_sessions
        sessions = native_sessions(self.composition)
        log = logging.getLogger(__name__)
        try:
            toolboxes = _prewarm_toolboxes(control.runtime)
            # The kept reader or one platform read: it creates and follows nothing, and unlike sessions.current() it
            # takes no session lock, so a send started meanwhile is never held behind the platform waking the compute.
            # Waking compute is the point, so the selector is read now: an old version's session is never woken.
            client = sessions.reader(base, checked_since=monotonic())
            value, _ = self._invoke(client, NativePrewarm(toolboxes=toolboxes))
        except Problem as error:
            if sessions.binding_moved(error):
                # The kept reader is not the Agent's session any more; a timeout keeps it (the send reads it as is).
                sessions.forget()
            log.warning("FA_NATIVE_PREWARM outcome=skipped code=%s", error.body["error"]["code"])
            return NativePrewarmResult(state="skipped"), None
        log.warning("FA_NATIVE_PREWARM outcome=sent toolboxes=%s", value.toolboxes)
        return NativePrewarmResult(state="sent", toolboxes=value.toolboxes), None

    def _context(self, principal, action, *, prepared=True):
        control = self.composition.runtime.work_policy
        if control is None:
            raise Problem(503, "NATIVE_TURNS_DISABLED", "the original owner and Agent settings are unavailable")
        control._human(principal, action)
        if not self.enabled:
            raise Problem(503, "NATIVE_TURNS_DISABLED", "native turns are not enabled on this API")
        policy = control.policy
        if action == "submit":
            current = control.installed()
            if current != policy or not current.active or not current.read_enabled:
                raise Problem(403, "WORK_POLICY_REVOKED", "current original submission rights are required")
        targets = selected_targets(control.store, self.composition.config, agent_id=policy.agent_id)
        if len(targets) != 1 or targets[0].agent_oid != policy.template.agent_oid:
            raise Problem(403, "NATIVE_STATE_SCOPE", "one original server-selected Agent is required")
        target = targets[0]
        clients = [client for client in self.composition.clients
                   if isinstance(client, FoundryHttpClient) and client.target.executor_id == target.executor_id]
        if len(clients) != 1:
            raise Problem(503, "NATIVE_EXECUTION_UNAVAILABLE", "the original Foundry transport is unavailable")
        client = clients[0].for_target(target)
        if prepared:
            # Image upload and append take this path (cancel and delivery go through ``_turn_context``): they deliver
            # to the Agent's current session, so the selector is read in-request.
            from .behavior_native_sessions import native_sessions
            client = native_sessions(self.composition).current(client, checked_since=monotonic())
        return control, client

    def _turn_context(self, principal, action, request_id):
        """A cancel or delivery acknowledgement goes to the session that holds its turn.

        That is the Agent's current session (selector read in this request), or, when ``current`` refuses because the
        selected version has no session yet or pending work keeps the Agent on its earlier session, the session the
        Agent record still names while platform metadata says it is active (``NativeSessions.turn_session``).
        Otherwise nothing is sent: the refusal carries ``not_dispatched`` and the same command may be sent again."""
        from .behavior_native_sessions import native_sessions
        control, client = self._context(principal, action, prepared=False)
        sessions = native_sessions(self.composition)
        try:
            return control, sessions.current(client, checked_since=monotonic())
        except Problem as error:
            if error.body["error"]["code"] not in {"NATIVE_SESSION_CHANGED", "AGENT_VERSION_FOLLOWING"}:
                raise
            holder = sessions.turn_session(client)
            if holder is None:
                raise _BeforeAdmission(error, request_id) from None
            return control, holder

    def _read_context(self, principal):
        from .behavior_native_sessions import native_sessions
        control, client = self._context(principal, "read", prepared=False)
        return control, native_sessions(self.composition).reader(client, stale_read=True)

    def _reading(self, principal, operation):
        """One idempotent read on the cached reader; when the binding moved, the platform is read once more.

        A selector that cannot be read now keeps a recently confirmed reader (``SELECTOR_STALE_READ_SECONDS``)."""
        from .behavior_native_sessions import native_sessions
        sessions = native_sessions(self.composition)
        retry = sessions.reader_cached()
        control, client = self._context(principal, "read", prepared=False)
        relocated, relocate = None, True
        while True:
            reader = relocated or sessions.reader(client, stale_read=True)
            relocated = None
            try:
                return operation(control, reader)
            except Problem as error:
                if relocate:
                    # ADR-0009 phase 3: the row-located SID woke on another compatible version, or the row is behind
                    # Hosted's record; the same read goes once more to where the row (now repaired) points.
                    relocate, relocated = False, sessions.relocate(reader, error)
                    if relocated is not None:
                        continue
                if not (sessions.moved(error) and retry):
                    raise
                retry = False

    @staticmethod
    def _legacy(control, cid):
        if cid is not None:
            key = conversation_key(cid)
            if control.store.read_snapshot((key,))[key] is not None:
                raise Problem(409, "NATIVE_STATE_SCOPE", "an existing Table conversation must keep its original route")

    @staticmethod
    def _same_session(client, original):
        """A record read through ``client`` belongs to its session. ADR-0009 phase 3: a conversation is judged by the
        binding row that located ``client``: its execution SID being one of the row's previous SIDs while ``client``
        is the row's current SID means the Agent switched environments, and the new session continues it (Hosted's
        own continuation rules decide). Receipts and preparations stay strict: an ``accepted`` receipt of another
        SID is never sent to this one (boundary 7)."""
        session_id = (original.execution_hosted_session_id if isinstance(original, NativeConversation)
                      and original.execution_hosted_session_id is not None else original.hosted_session_id)
        if session_id == client.target.session_id:
            return
        row = getattr(client, "_binding_row", None)
        if (isinstance(original, NativeConversation) and row is not None
                and row.hosted_session_id == client.target.session_id and row.earlier(session_id)):
            return
        raise Problem(409, "NATIVE_SESSION_CHANGED", "the original native session differs; no replacement was submitted")

    @staticmethod
    def _accounted(control, result):
        value, _ = result
        receipt = value.receipt if isinstance(value, NativeTurnStatus) else value
        if isinstance(receipt, NativeReceipt) and receipt.status in {"completed", "unknown", "busy_refused"}:
            from .behavior_instruction_cost import record_agent_turn_usage
            record_agent_turn_usage(control, rid=receipt.request_id, usage=receipt.usage)
        return result

    @staticmethod
    def _invoke(client, command):
        response = client.invoke_native(command)
        model = (NativeSessionState if isinstance(command, NativeSessionRead) else
                 NativeFilesView if isinstance(command, NativeSessionFiles) else
                 NativeContinuationView if isinstance(command, NativeContinuationRead) else
                 NativeContinuationReadyReceipt if isinstance(command, NativeContinuationReady) else
                 NativeEnvironmentView if isinstance(command, (NativeSessionFollow, NativeSessionRetarget, NativeSessionFilesOmission,
                                                               NativeSessionAbandon)) else
                 NativeUnfinishedSessions if isinstance(command, NativeUnfinishedRead) else
                 NativeSessionPreparation if isinstance(command, NativeSessionPrepare) else
                 NativeConversation if isinstance(command, NativeConversationRead) else
                 NativeHistoryPage if isinstance(command, NativeHistory) else
                 NativeResultPage if isinstance(command, NativeResult) else
                 NativeTurnImages if isinstance(command, NativeImagesRead) else
                 NativeImageContent if isinstance(command, NativeImageRead) else
                 NativeProgressSnapshot if isinstance(command, NativeProgressRead) else
                 NativeTurnStatus if isinstance(command, NativeTurnStatusRead) else
                 NativeHitlReviewHistory if isinstance(command, NativeHitlHistoryRead) else
                 NativeReceivedPage if isinstance(command, NativeReceivedRead) else
                 NativeAppends if isinstance(command, NativeAppendRead) and command.append_id is None else
                 NativeAppendReceipt if isinstance(command, (NativeAppend, NativeAppendRead)) else
                 NativeUserImageStored if isinstance(command, NativeUserImageUpload) else
                 NativeSkillDraftProof if isinstance(command, NativeSkillDraftIntentRead) else
                 NativePrewarmView if isinstance(command, NativePrewarm) else
                 NativeCandidateView if isinstance(command, NativeCandidateRead) else NativeReceipt)
        try:
            json.loads(response.body, object_pairs_hook=_unique, parse_constant=_nonfinite)
            value = model.model_validate_json(response.body)
        except (ValueError, TypeError):
            raise Problem(502, "NATIVE_STATE_INVALID", "the native response does not match its fixed schema") from None
        if isinstance(value, NativeHistoryPage):
            scope = value.conversation
            numbers = [turn.turn_number for turn in value.turns]
            if (len(value.turns) > command.limit or numbers != sorted(set(numbers))
                    or len({turn.request_id for turn in value.turns}) != len(value.turns)
                    or any(turn.conversation_id != command.conversation_id
                           or turn.result.turn_number != turn.turn_number
                           or not command.after_turn < turn.turn_number <= scope.last_turn for turn in value.turns)
                    or value.next_after_turn is not None and (
                        not numbers or value.next_after_turn != numbers[-1])):
                raise Problem(502, "NATIVE_STATE_INVALID", "native history does not match its original page")
        elif isinstance(value, NativeTurnStatus):
            scope = value.receipt
            if value.text is not None:
                raw = value.text.encode("utf-8")
                if (len(raw) != scope.result.text_bytes
                        or hashlib.sha256(raw).hexdigest() != scope.result.text_sha256):
                    raise Problem(502, "NATIVE_RESULT_INCOMPLETE", "the complete original result length or hash differs")
        else:
            scope = value
        if (hasattr(scope, "agent_id") and scope.agent_id != client.target.agent_id
                or hasattr(command, "conversation_id") and scope.conversation_id != command.conversation_id
                or not isinstance(command, (
                    NativeSessionPrepare, NativeSessionFollow, NativeContinuationRead, NativeSessionFilesOmission,
                    NativeSessionAbandon))
                and hasattr(command, "request_id") and getattr(scope, "request_id", None) != command.request_id):
            raise Problem(502, "NATIVE_STATE_SCOPE", "the native response belongs to another Agent or request")
        if isinstance(command, NativeContinuationRead) and value.original_request_id != command.request_id:
            raise Problem(502, "NATIVE_STATE_SCOPE", "the continuation view belongs to another original request")
        if isinstance(command, (NativeSubmit, NativeAgentReceive, NativeRoutineSubmit, NativeContinuationSubmit)):
            if (value.command != command.command or value.capture != command.capture
                    or value.source != getattr(command, "source", None) or value.submit_digest != command.sha256
                    or value.routine != getattr(command, "routine", None)
                    or value.continuation != getattr(command, "continuation", None)):
                raise Problem(409, "NATIVE_CAPTURE_CHANGED", "the response differs from the original submitted material")
            if value.status not in {"completed", "unknown", "busy_refused"}:
                # Hosted's create-or-read may return a finished original of an earlier session; it executes nothing.
                NativeTurnGateway._same_session(client, value)
        if isinstance(command, NativeSessionPrepare):
            NativeTurnGateway._same_session(client, value)
        if isinstance(command, (NativeSessionFollow, NativeSessionRetarget)) and (
                value.state not in {"pending", "ready"} or value.request_id is None
                or value.previous_hosted_session_id != command.expected_session_id
                or value.hosted_session_id != client.target.session_id
                or value.hosted_agent_version != client.target.agent_version):
            raise Problem(502, "NATIVE_STATE_SCOPE", "the environment response differs from the approved source and target")
        if isinstance(command, NativeSessionFilesOmission) and (
                value.request_id != command.follow.request_id
                or value.previous_hosted_session_id != command.follow.expected_session_id
                or value.hosted_session_id != client.target.session_id
                or value.hosted_agent_version != client.target.agent_version
                or command.stage != "check" and not (
                    value.files_skip_request_id == command.request_id and value.files_migrated is False
                    or command.stage == "confirm" and value.files_confirmation_pending == command.request_id
                    or command.stage == "read" and command.request_id in {
                        value.files_confirmation_saved, value.files_confirmation_absent})):
            raise Problem(502, "NATIVE_STATE_SCOPE", "the file omission differs from the original confirmed operation")
        if isinstance(command, NativeSessionFiles):
            if any(getattr(value, name) != getattr(command, name) for name in NativeFilesScope.model_fields):
                raise Problem(502, "NATIVE_STATE_SCOPE", "the file progress differs from its original operation")
            if value.progress is not None and any(
                    getattr(value.progress.manifest, name) != getattr(command, name)
                    for name in NativeFilesScope.model_fields):
                raise Problem(502, "NATIVE_STATE_SCOPE", "the manifest differs from its original operation")
            if value.send_allowed and command.stage != "begin":
                raise Problem(502, "NATIVE_STATE_INVALID", "a readback cannot authorize a file upload")
            if value.send_allowed and (value.progress is None
                                       or value.progress.next_index != command.index
                                       or value.progress.sending != command.send_token):
                raise Problem(502, "NATIVE_STATE_INVALID", "the file send claim did not read back")
        if isinstance(command, NativeResult) and value.part != command.part:
            raise Problem(502, "NATIVE_RESULT_INCOMPLETE", "the original result part differs")
        if isinstance(command, NativeImageRead) and value.entry.version_id != command.version_id:
            raise Problem(502, "IMAGE_DELIVERY_CONFLICT", "the image differs from the requested original version")
        if isinstance(command, NativeHitlHistoryRead) and (
                len(value.entries) > command.limit
                or value.next_after is not None and value.next_after == command.after):
            raise Problem(502, "NATIVE_STATE_INVALID", "the original review page did not advance")
        if isinstance(command, NativeReceivedRead) and (
                len(value.items) > command.limit
                or len({item.request_id for item in value.items}) != len(value.items)
                or value.next_after is not None and value.next_after == command.after):
            raise Problem(502, "NATIVE_STATE_INVALID", "the original received page did not advance")
        if (isinstance(command, (NativeAppend, NativeAppendRead)) and command.append_id is not None
                and value.append_id != command.append_id):
            raise Problem(502, "NATIVE_STATE_SCOPE", "the append response differs from the original control")
        if isinstance(value, NativeResultPage) and value.complete:
            raw = value.text.encode("utf-8")
            if len(raw) != value.result.text_bytes or hashlib.sha256(raw).hexdigest() != value.result.text_sha256:
                raise Problem(502, "NATIVE_RESULT_INCOMPLETE", "the complete original result length or hash differs")
        if isinstance(command, NativeDelivered) and (
                value.result is None or value.result.result_digest != command.result_digest or not value.delivered):
            raise Problem(409, "NATIVE_DELIVERY_CONFLICT", "the original completed result was not acknowledged")
        return value, response

    def read(self, principal, command):
        try:
            command = NATIVE_REQUEST.validate_json(command.model_dump_json())
        except (ValueError, AttributeError):
            raise Problem(422, "NATIVE_REQUEST_INVALID", "one original typed native command is required") from None
        if not isinstance(command, (NativeRead, NativeConversationRead, NativeHistory, NativeResult, NativeSessionRead,
                                    NativeCandidateRead, NativeImagesRead, NativeImageRead, NativeAppendRead,
                                    NativeProgressRead, NativeTurnStatusRead, NativeHitlHistoryRead,
                                    NativeContinuationRead, NativeReceivedRead)):
            raise Problem(422, "NATIVE_REQUEST_INVALID", "this route only reads existing native records")
        # Reads need no Table-route check: Hosted holds only its own records and CIDs are server-made.
        control, value, response, reader_sid = self._reading(principal, lambda control, client: (
            control, *self._invoke(client, command), client.target.session_id))
        value = self._earlier_version_notice(value, reader_sid)
        if isinstance(value, NativeReceipt) and value.delegation is not None and value.notification_error is None:
            value = self._peer_delivery_status(principal, value)
        if (isinstance(value, NativeTurnStatus) and value.receipt.delegation is not None
                and value.receipt.notification_error is None):
            value = value.model_copy(update={"receipt": self._peer_delivery_status(principal, value.receipt)})
        return self._accounted(control, (value, response))

    def _earlier_version_notice(self, value, reader_sid):
        """ADR-0009 §7a (e), decided by the API (Hosted has no binding row), for a receipt one of the binding row's
        previous SIDs accepted. Only what can no longer continue carries "這則訊息是較早的版本接下的……" (the owner
        sends it again as a new message): a ``started`` one left on a SID the platform had retired, and one Hosted
        marked after its progress stopped for N minutes (``NATIVE_SOURCE_STALLED``). Anything else is reported as it
        is (boundary 7: never ask for a resend of work that may still finish): Hosted adopts a never-started receipt
        of any earlier SID, retired or not, on the read itself (M9; it then names the reader's SID, and one whose
        adoption is unconfirmed is adopted on a later read), and a ``started`` one on an idle source is ordinary
        progress until it completes (its answer) or is marked. A SID neither current nor previous is undecidable and
        reported as before. The row is read only for a receipt of another SID than the reader's, so an ordinary
        in-flight poll reads nothing more.

        The answer is a read-time view, never written back (the receipt keeps its bytes and its RID). An unfinished
        one reads ``unknown`` / ``NATIVE_EARLIER_VERSION_UNFINISHED``, a state every poller (the workbench too) already
        treats as final, so nothing polls it as accepted/started forever, retries it, or dispatches it to the session
        it left; without the transport-only ``dispatch`` and the started-only ``preview``. A marked one keeps its own
        ``unknown`` and failure code and only gains the notice."""
        receipt = value.receipt if isinstance(value, NativeTurnStatus) else value
        if (not isinstance(receipt, NativeReceipt) or receipt.hosted_session_id == reader_sid
                or receipt.status not in {"accepted", "started"} and (
                    receipt.status, receipt.failure_code) != ("unknown", SOURCE_STALLED)):
            return value
        from .behavior_native_sessions import native_sessions
        row = native_sessions(self.composition)._binding("notice", lambda store: store.read(receipt.agent_id))
        if row is None or not row.earlier(receipt.hosted_session_id):
            return value
        notice = NativeEnvironmentNotice(kind="earlier_version_unfinished")
        if receipt.status == "unknown":
            return self._with_receipt(value, receipt.model_copy(update={"environment_notice": notice}))
        if receipt.status != "started" or not row.retired(receipt.hosted_session_id):
            return value
        receipt = receipt.model_copy(update={
            "status": "unknown", "failure_code": EARLIER_VERSION_UNFINISHED, "dispatch": None,
            "environment_notice": notice})
        return self._with_receipt(value, receipt)

    @staticmethod
    def _with_receipt(value, receipt):
        return (value.model_copy(update={"receipt": receipt, "preview": None}) if isinstance(value, NativeTurnStatus)
                else receipt)

    def system_prompt(self, principal, command):
        """Only the original receipt is read (no usage, peer or ledger write); its prompt is rebuilt from it here."""
        from .behavior_system_prompt_view import turn_view
        value, _ = self._reading(principal, lambda _control, client: self._invoke(client, command))
        if not isinstance(value, NativeReceipt) or value.request_id != command.request_id:
            raise Problem(502, "NATIVE_STATE_INVALID", "the original native receipt is required")
        return turn_view(value), None

    def _peer_delivery_status(self, principal, original):
        owner = getattr(self.composition, "_agent_message_owner", None) or self.composition
        delegated = original.delegation
        try:
            peer = owner.for_agent(delegated.recipient_agent_id)
            gateway = NativeTurnGateway(peer, enabled=self.enabled)
            child, _response = gateway._reading(
                principal, lambda _control, client: gateway._invoke(client, NativeRead(request_id=delegated.message_id)))
            source = child.source
            if (child.submit_digest != delegated.submit_digest or source is None or source.kind != "agent_request"
                    or source.sender.agent_id != original.agent_id
                    or source.sender.conversation_id != original.conversation_id
                    or source.sender.request_id != original.request_id):
                raise Problem(409, "NATIVE_STATE_SCOPE", "the peer notification belongs to another original request")
            code = child.notification_error
        except Problem as error:
            logging.getLogger(__name__).warning("NATIVE_PEER_STATUS_UNCONFIRMED code=%s", error.body["error"]["code"])
            code = "NATIVE_PEER_STATUS_UNCONFIRMED"
        return (original if code is None else NativeReceipt.model_validate_json(
            original.model_copy(update={"notification_error": code}).model_dump_json()))

    def prepare(self, principal, command):
        from .behavior_native_sessions import native_sessions
        _, client = self._context(principal, "submit", prepared=False)
        value = native_sessions(self.composition).prepare(
            client, command, before_send=lambda: self._context(principal, "submit", prepared=False))
        return value, None

    def environment(self, principal, command=None, *, automatic=False):
        from .behavior_native_sessions import native_sessions
        _, client = self._context(principal, "read" if command is None else "submit", prepared=False)
        sessions = native_sessions(self.composition)
        operation = sessions.advance if automatic else sessions.follow
        value = (sessions.environment_view(client) if command is None else
                 operation(client, command, before_send=lambda: self._context(principal, "submit", prepared=False)))
        if command is not None:
            self._ready_handoffs(principal, value)
        return value, None

    def _ready_handoffs(self, principal, value):
        manifest = getattr(value, "files_manifest_sha256", None)
        if value.state != "ready" or manifest is None:
            return
        try:
            from .behavior_native_sessions import native_sessions
            # A handoff delivers work: a selector read made now, never the read window or a stale confirmation.
            _, client = self._context(principal, "read", prepared=False)
            client = native_sessions(self.composition).reader(client, checked_since=monotonic())
            if (client.target.session_id, client.target.agent_version) != (
                    value.hosted_session_id, value.hosted_agent_version):
                raise Problem(409, "NATIVE_SESSION_CHANGED", "the ready environment is no longer current")
            self._context(principal, "submit", prepared=False)
            self._signal_ready_handoffs(client, value)
        except Problem as error:
            logging.getLogger(__name__).warning(
                "NATIVE_CONTINUATION_HANDOFF_PENDING code=%s", error.body["error"]["code"])

    @staticmethod
    def _signal_ready_handoffs(client, value):
        from .behavior_native_turn_wire import continuation_ready_id

        manifest = getattr(value, "files_manifest_sha256", None)
        if value.state != "ready" or manifest is None:
            return
        if (client.target.session_id, client.target.agent_version) != (value.hosted_session_id, value.hosted_agent_version):
            raise Problem(409, "NATIVE_SESSION_CHANGED", "the ready environment is no longer current")
        NativeTurnGateway._invoke(client, NativeContinuationReady(
            request_id=continuation_ready_id(value.request_id, value.hosted_session_id, manifest),
            environment_request_id=value.request_id, hosted_session_id=value.hosted_session_id,
            files_manifest_sha256=manifest))

    def retarget_environment(self, principal, command):
        from .behavior_native_sessions import native_sessions
        _, client = self._context(principal, "submit", prepared=False)
        result = native_sessions(self.composition).retarget(
            client, command, before_send=lambda: self._context(principal, "submit", prepared=False))
        self._ready_handoffs(principal, result)
        return result, None

    def observe_environment(self, principal, command, *, automatic=False):
        from .behavior_native_sessions import native_sessions
        _, client = self._context(principal, "read", prepared=False)
        sessions = native_sessions(self.composition)
        value = (sessions.observe_automatic(client, command, before_send=lambda: self._context(principal, "read", prepared=False))
                 if automatic else sessions.observe_environment(client, command))
        return value, None

    def without_files(self, principal, command, *, reconcile=False, preview=False, resume=False, observe_only=False):
        from .behavior_native_sessions import native_sessions
        action = "read" if preview or observe_only else "submit"
        _, client = self._context(principal, action, prepared=False)
        result = native_sessions(self.composition).without_files(
            client, command, reconcile=reconcile or observe_only, preview=preview, resume=resume, observe_only=observe_only,
            before_send=lambda: self._context(principal, action, prepared=False))
        return result, None

    def observe_preparation(self, principal):
        def observing(_control, client):
            state, response = self._invoke(client, NativeSessionRead())
            self._same_session(client, state)
            return state, response

        state, response = self._reading(principal, observing)
        if state.preparation is None:
            raise Problem(409, "NATIVE_SESSION_CHANGED", "the current reader has no confirmed preparation")
        # A workbench older than ``actual_version`` decodes this strictly (agent_onboarding); it is not its fact.
        return state.model_copy(update={"actual_version": None}), response

    def observe_absent_admission(self, principal, command):
        from .behavior_native_turn_wire import NativeAdmissionObservation

        def current_reader(_control, client):
            before, _ = self._invoke(client, NativeSessionRead())
            if before.hosted_session_id != client.target.session_id:
                raise _NotCurrentReader(409, "NATIVE_SESSION_CHANGED", "the reader is not the Agent's environment")
            return client, before

        try:
            client, before = self._reading(principal, current_reader)
        except _NotCurrentReader:
            raise Problem(404, "NATIVE_RID_MISSING", "the original admission cannot yet be classified") from None
        environment = before.environment
        if (environment is None or environment.state != "ready" or before.admission_request_id is not None
                or before.following is not None and before.following.state == "pending"):
            raise Problem(404, "NATIVE_RID_MISSING", "the original admission cannot yet be classified")
        try:
            self._invoke(client, command)
        except Problem as error:
            if error.status != 404 or error.body["error"]["code"] != "NATIVE_RID_MISSING":
                raise
        else:
            raise Problem(409, "NATIVE_RID_CONFLICT", "the original receipt exists; read it instead")
        after, _ = self._invoke(client, NativeSessionRead())
        if before != after:
            raise Problem(503, "NATIVE_ADMISSION_PENDING", "the Agent changed during the original admission lookup")
        return NativeAdmissionObservation(
            agent_id=client.target.agent_id, request_id=command.request_id,
            environment_request_id=environment.request_id, hosted_session_id=before.hosted_session_id), None

    def reconcile(self, principal, command):
        command = InstructionSubmit.model_validate_json(command.model_dump_json())
        value, response = self.read(principal, NativeRead(request_id=command.request_id))
        if value.command != command:
            raise Problem(409, "NATIVE_RID_CONFLICT", "the original request ID has different retained input")
        return value, response

    def submit(self, principal, command, *, state_profile=NATIVE_STATE_PROFILE, original=None, response=None,
               defer=None, owner_notice=False, notice_kinds=False):
        """Owner submit: entry check once, then Hosted's ``rid:<RID>`` create-or-read is the only dedup.

        A first attempt does not pre-read the RID. Only when Hosted reports that the RID already names other
        material is the original read, so a retry keeps its saved capture. ``original``/``response`` are what the
        caller (reconcile) already read in this request. ``defer`` is only passed by the Responses ``/submit`` route.
        A definite refusal of a first attempt before ``rid:<RID>`` exists carries ``not_dispatched`` with the RID,
        so the client may send the same RID and body again unchanged.

        ``owner_notice``: this is the owner's own workbench response, which carries the Agent's pending slot notice
        (ADR-0009 §4 end); other callers (group start, reconcile) never carry it. ``notice_kinds``: the bridge sent
        ``NOTICE_KINDS_HEADER``; without it only the drift notice is carried, as before phase 2c.
        """
        command = InstructionSubmit.model_validate_json(command.model_dump_json())
        rid = command.request_id
        timed = timing_id(rid)
        # The selected version comes from a selector read made in this request (once, shared with the prefetch).
        started = monotonic()
        try:
            with phase_timing("api_submit_context", job_id=timed):
                control, reader = self._context(principal, "submit", prepared=False)
                self._legacy(control, command.conversation_id)
        except Problem as error:
            raise (error if original is not None else _not_admitted(error, rid)) from None
        from .behavior_native_sessions import native_sessions
        sessions = native_sessions(self.composition)
        if defer is not None:
            queue = defer

            def defer(send):
                # A refused after-reply POST forgets the cached reader as a refused synchronous one did.
                queue(partial(send, refused=sessions.moved))
        prefetch = (self._prefetch(control, sessions, reader, principal, command, checked_since=started)
                    if original is None and command.conversation_id is not None else None)
        try:
            with phase_timing("api_submit_session", job_id=timed):
                client = sessions.current(reader, checked_since=started)
        except Problem as error:
            if error.body["error"]["code"] == "NATIVE_SESSION_CHANGED":
                # A submit never bootstraps: it follows only an Agent prepared earlier, seen either as prepared state
                # naming the new version's session (``following_candidate``) or as an existing earlier session.
                # A reconcile of an accepted RID is not marked.
                if original is not None:
                    raise
                try:
                    prepared = sessions.following_candidate is not None or self._prepared_before(sessions, reader)
                except Problem as unconfirmed:
                    code = unconfirmed.body["error"]["code"]
                    if code == "AGENT_VERSION_FOLLOWING" or (
                            unconfirmed.status == 503 and code == "NATIVE_ENVIRONMENT_UNAVAILABLE"):
                        # Only another version's session exists, or the session listing could not be read (or the
                        # selected session is past its bounded page): nothing is invoked or followed from here, and
                        # the same RID and body are sent again later. It is not "never prepared".
                        raise _BeforeAdmission(unconfirmed, rid) from None
                    prepared = False
                raise (self._follow_before_admission(principal, reader, sessions, error, rid)
                       if prepared else _BeforeAdmission(error, rid)) from None
            if error.body["error"]["code"] == "AGENT_VERSION_FOLLOWING" and original is None:
                # The selected version is not this API build's (deploy in progress): nothing is sent or followed here.
                raise _BeforeAdmission(error, rid) from None
            raise
        if original is None:
            try:
                sessions.admission(client, request_id=rid, resolved=True)
                capture, previous = self._prepare(control, client, principal, command, state_profile, timed=timed,
                                                  prefetch=prefetch)
            except Problem as refusal:
                # Refused before reaching Hosted: only a retry of an already accepted RID continues as before.
                if refusal.status >= 500 and refusal.body["error"]["code"] != "AGENT_VERSION_FOLLOWING":
                    raise
                try:
                    original, response = self._existing(client, rid)
                except Problem:
                    raise refusal from None
                if original is None:
                    if refusal.body["error"]["code"] == "AGENT_VERSION_FOLLOWING":
                        raise self._follow_before_admission(principal, reader, sessions, refusal, rid) from None
                    raise _not_admitted(refusal, rid) from None
            else:
                from .behavior_native_responses_api import submit_native_turn
                try:
                    with phase_timing("api_submit_hosted", job_id=timed):
                        submitted = submit_native_turn(
                            self.composition, client, command, capture, conversation=previous, defer=defer)
                    if command.conversation_id is None:
                        self._keep_admitted(client, submitted[0])
                    return self._bound(sessions, client, self._accounted(control, submitted), owner_notice,
                                       notice_kinds)
                except Problem as conflict:
                    if getattr(client, "_binding_row", None) is not None and (
                            conflict.status, conflict.body["error"]["code"]) in _BOUND_REFUSALS:
                        # ADR-0009 §4 steps 5-6: the row located this session; its refusal is resolved here.
                        sent, result = self._after_refusal(
                            control, principal, reader, sessions, client, command, capture, previous, conflict,
                            state_profile=state_profile, defer=defer)
                        return self._bound(sessions, sent, result, owner_notice, notice_kinds)
                    actual = conflict.body["error"].get("actual_version")
                    if conflict.body["error"]["code"] == "NATIVE_SESSION_CHANGED" and actual is not None:
                        # A newer image reported the VM's own version: record it (ADR-0009 §4 step 6). This also
                        # claims an Agent that had no row, so the same RID sent again is located by the row.
                        sessions.observed_binding(client, version=actual)
                        if (conflict.body["error"].get("reason") == "version"
                                and conflict.body["error"].get("not_dispatched") is True):
                            # Nothing was dispatched: the client sends the same RID and body again (no follow).
                            sessions.forget()
                            raise _BeforeAdmission(conflict, rid, server_following=True) from None
                    if conflict.body["error"]["code"] != "NATIVE_RID_CONFLICT":
                        # Not resent here; the client's retry of this same RID reads the binding again.
                        sessions.moved(conflict)
                        if conflict.body["error"].get("not_dispatched") is True:
                            # Hosted refused before rid:<RID> because the Agent is changing versions: this request
                            # moves it on, so every resend that is told to wait has advanced the follow once.
                            code = conflict.body["error"]["code"]
                            if code in {"NATIVE_SESSION_CHANGED", "AGENT_VERSION_FOLLOWING"}:
                                raise self._follow_before_admission(
                                    principal, reader, sessions, conflict, rid,
                                    refused_session_id=(client.target.session_id
                                                        if code == "AGENT_VERSION_FOLLOWING" else None)) from None
                            raise _not_admitted(conflict, rid) from None
                        raise
                    original, response = self._existing(client, rid)
                    if original is None:
                        raise
        return self._bound(sessions, client, self._retained(
            control, client, command, original, state_profile, response=response, defer=defer), owner_notice,
            notice_kinds)

    def _after_refusal(self, control, principal, reader, sessions, client, command, capture, previous, conflict, *,
                       state_profile, defer):
        """ADR-0009 §4 steps 5-6 for a submit the binding row located: (client that answered, result), or a raised
        refusal the client resends unchanged (``server_following``), or, when nothing proves the RID was not taken,
        the original refusal (boundary 7: never resent blind). The binding row is read again first.

        - The row names another SID now: the same RID and body go there once if nothing was dispatched; otherwise
          the RID is read through the new SID first (its original answers; a never-started one Hosted adopts; one
          left ``accepted`` on the old SID is reported, never resent).
        - 403 ``NATIVE_TURNS_DISABLED``, same SID: the VM woke on another profile (§9). No refusal is added: it is
          logged and marked, and the retained follow runs; the client resends the same RID (``server_following``)
          only while that follow is pending or moved the Agent off this SID, otherwise today's response stands.
        - 409, same SID, V_obs = V_rec: not drift. Hosted's Agent record moved on without the row (M3): the switch is
          read from this SID's own state, written to the row, and the same RID goes to the new SID.
        - 409, same SID, V_obs ≠ V_rec: drift. Recorded (a VM report now, a metadata guess once the VM accepts it);
          compatible: the same RID goes to V_obs once (read first when the refusal may have dispatched).

        A definite refusal none of these resolves runs the retained follow as well; both follows name this SID as the
        refusing one (``refused_session_id``), so a follow that keeps the Agent on it asks for no resend."""
        rid = command.request_id
        detail = conflict.body["error"]
        definite = conflict.status == 403 or detail.get("not_dispatched") is True

        def resend_later(refusal):
            if not definite:
                return conflict
            return self._follow_before_admission(principal, reader, sessions, refusal, rid,
                                                 refused_session_id=client.target.session_id)

        row = sessions.bound_row(client, fresh=True)
        if row is None:
            raise resend_later(conflict) from None
        send = partial(self._send_once, control, principal, reader, sessions, command, capture, previous,
                       definite=definite, state_profile=state_profile, defer=defer)
        if row.hosted_session_id != client.target.session_id:
            return send(sessions._bound_client(client, row))
        if conflict.status == 403:
            actual = detail.get("actual_version")
            if isinstance(actual, str) and actual != client.target.agent_version:
                # A newer image names the version it woke on: a VM fact, recorded as drift (§4 step 6).
                sessions._observe_drift(client.for_target(client.target.model_copy(update={"agent_version": actual})),
                                        row, "vm")
            sessions.guard.mark_incompatible(client.target.agent_id, client.target.session_id)
            logging.getLogger(__name__).warning(
                "NATIVE_SESSION_CALL_INCOMPATIBLE operation=submit session_id=%s reason=%s enforced=False",
                client.target.session_id, detail.get("reason"))
            raise self._follow_before_admission(principal, reader, sessions, Problem(
                503, "AGENT_VERSION_FOLLOWING", "the Agent's session does not accept this profile; nothing was sent"),
                rid, refused_session_id=client.target.session_id) from None
        observed = sessions.observed_version(client, conflict)
        if observed is None:
            raise resend_later(conflict) from None
        if observed[0] == client.target.agent_version:
            logging.getLogger(__name__).warning("NATIVE_BINDING_DIVERGED session_id=%s", client.target.session_id)
            try:
                repaired = sessions._repair(client, sessions._state(client), row)
            except Problem as unread:
                logging.getLogger(__name__).warning("NATIVE_BINDING_REPAIR_UNREAD code=%s", unread.body["error"]["code"])
                repaired = None
            if repaired is None:
                raise resend_later(conflict) from None
            return send(repaired[0])
        drifted = sessions._drifted(client, conflict, row)
        if drifted is None:
            raise resend_later(conflict) from None
        moved, _row, source = drifted
        confirm = (partial(sessions._observe_drift, moved, _row, source) if source == "metadata-active" else None)
        return send(moved, confirm=confirm)

    def _send_once(self, control, principal, reader, sessions, command, capture, previous, client, *, definite,
                   state_profile, defer, confirm=None):
        """The same RID and body to ``client`` once (ADR-0009 §4 step 6; Hosted's ``rid:<RID>`` create-or-read keeps
        it to one execution). When the refusal may have dispatched, the RID is read through ``client`` first and
        only ``NATIVE_RID_MISSING`` sends it. ``confirm`` runs once ``client``'s VM answered (old-image drift)."""
        rid = command.request_id
        if not definite:
            original, response = self._existing(client, rid)
            if confirm is not None:
                confirm()
            if original is not None:
                if original.status == "accepted" and original.hosted_session_id != client.target.session_id:
                    # Accepted by an earlier SID and not adopted (yet): reported as it is, never resent (§7a (e)).
                    return client, self._accounted(control, (
                        self._earlier_version_notice(original, client.target.session_id), response))
                return client, self._retained(control, client, command, original, state_profile,
                                              response=response, defer=defer)
        from .behavior_native_responses_api import submit_native_turn
        try:
            submitted = submit_native_turn(self.composition, client, command, capture, conversation=previous,
                                           defer=defer)
        except Problem as again:
            detail = again.body["error"]
            if detail.get("not_dispatched") is True and detail["code"] in {
                    "NATIVE_SESSION_CHANGED", "AGENT_VERSION_FOLLOWING", "NATIVE_TURNS_DISABLED"}:
                # One resend per RID only (§4): the retained follow takes it from here; the client resends.
                sessions.forget()
                raise self._follow_before_admission(principal, reader, sessions, again, rid) from None
            raise
        if confirm is not None and definite:
            confirm()
        if command.conversation_id is None:
            self._keep_admitted(client, submitted[0])
        return client, self._accounted(control, submitted)

    @staticmethod
    def _bound(sessions, client, result, owner_notice, notice_kinds=False):
        """Hosted answered, so the VM accepted ``client.target.agent_version``: record it. Only an owner response
        carries the pending slot notice (the drift notice only, unless the bridge knows the phase 2c kinds); the
        owner's HTTP route clears it after the response is written (``clear_delivered_notice``, ADR-0009 §4 end),
        never this call. ADR-0009 phase 3: ``client`` is the client that answered, so after a switch inside this
        request (``_after_refusal``'s new SID, ``_repair``'s ``record_switch``) its row is the post-switch row with
        that switch's notice; a row that already records ``client``'s SID at its version is not read again."""
        value, upstream = result
        located = getattr(client, "_binding_row", None)
        if (located is not None and located.hosted_session_id == client.target.session_id
                and located.observed.agent_version == client.target.agent_version):
            row = located
        else:
            row = sessions.observed_binding(client, version=client.target.agent_version)
        notice = (None if row is None or row.hosted_session_id != client.target.session_id
                  else row.notice(switch=notice_kinds))
        if owner_notice and notice is not None and isinstance(value, NativeReceipt):
            value = value.model_copy(update={"environment_notice": notice})
        return value, upstream

    def clear_delivered_notice(self, value):
        """ADR-0009 §4 end, run only after the owner response carrying ``value.environment_notice`` was written: CAS
        clear of the binding row's notice slot, and only while it still holds that notice. A failed clear logs and
        leaves it pending, so the next owner response shows it again (at least once)."""
        notice = getattr(value, "environment_notice", None)
        if isinstance(value, NativeReceipt) and notice is not None:
            self.clear_notice(value.agent_id, notice)

    def clear_notice(self, agent_id, notice):
        """``clear_delivered_notice`` for an owner reply that is not a receipt (a Teams final reply)."""
        if notice.kind not in SLOT_NOTICE_KINDS:
            return
        from .behavior_native_sessions import native_sessions
        native_sessions(self.composition)._binding("clear", lambda store: store.clear_notice(agent_id, notice))

    def owner_notice(self, agent_id):
        """The binding row's pending slot notice for a final owner reply that did not come from a submit (Teams'
        read path). Reads the row only: no session call; an unreadable row carries nothing."""
        from .behavior_native_sessions import native_sessions
        row = native_sessions(self.composition)._binding("notice", lambda store: store.read(agent_id))
        return None if row is None else row.notice()

    def _prepared_before(self, sessions, reader):
        """The selected version's existing session (version reader, else the legacy one) names a preparation.

        Platform metadata finds it and one read-only state read confirms it, as /prepare does; nothing is created,
        so a never-prepared Agent keeps its original refusal even when a deploy-time follow created a session.
        A session found only on another version is never invoked: 503 ``AGENT_VERSION_FOLLOWING``."""
        from .behavior_native_sessions import (
            _latest_version_reader, _read_platform_session, _require_selected_version,
        )

        if sessions.bound_row(reader) is not None:
            # ADR-0009 §7 phase 2 item 2, done here: an Agent with a binding row was prepared (no session is read).
            return True
        client_id = self.composition.config.identity.managed_identity_client_id
        selected = sessions.desired_version or sessions.selected_version(reader)
        target = _latest_version_reader(reader, client_id, selected)
        if target is None or target.agent_version != selected:
            legacy = _read_platform_session(sessions.candidate(reader))
            if legacy is not None and (target is None or legacy.agent_version == selected):
                target = legacy
        if target is None:
            return False
        _require_selected_version(target, selected)
        return sessions._state(reader.for_target(target)).preparation is not None

    def _follow_before_admission(self, principal, reader, sessions, refusal, request_id, *, refused_session_id=None):
        """The submit itself moves the Agent to its current version (one bounded ``advance``, like the deploy-time
        and peer follow) and returns the refusal to raise. Nothing was admitted, so the same RID and body may be
        sent again; the system policy inside ``advance`` already decides any file omission. ``server_following``
        only when that ``advance`` made or kept progress; a failure keeps its own code. ``refused_session_id``:
        Hosted's own 503 also covers its task recovery, which no follow moves, so it asks to wait only while the
        follow is pending or once it moved the Agent off the refusing session."""
        from .native_environment_sync import automatic_environment_command

        try:
            operation = automatic_environment_command(reader.target, sessions.selected_version(reader))
            view = sessions.advance(reader, operation, before_send=lambda: self._context(principal, "submit", prepared=False))
        except Problem as failure:
            # The system policy raises an unobservable task as a no-files refusal; no person can override it either.
            if (failure.body["error"].get("files_check") or {}).get("category") != "task_access_denied":
                return _BeforeAdmission(failure, request_id)
            view = None
        # Same stops as the deploy-time follow: neither resolves by sending the same message again.
        if view is not None and view.files_reconcile_required:
            return _BeforeAdmission(Problem(503, "NATIVE_FILES_UNKNOWN",
                                            "the original file effect requires reconciliation"), request_id)
        if view is None or view.reason == "task_access_denied":
            return _BeforeAdmission(Problem(403, "NATIVE_ENVIRONMENT_UNAVAILABLE",
                                            "the original task observation was denied"), request_id)
        self._ready_handoffs(principal, view)
        return _BeforeAdmission(refusal, request_id, server_following=(
            refused_session_id is None or view.state == "pending" or view.hosted_session_id != refused_session_id))

    def _existing(self, client, request_id):
        try:
            return self._invoke(client, NativeRead(request_id=request_id))
        except Problem as error:
            if error.body["error"]["code"] == "NATIVE_RID_MISSING":
                return None, None
            raise

    def _prefetch(self, control, sessions, reader, principal, command, *, checked_since):
        """Start the follow-up's conversation read and capture while ``sessions.current`` confirms the session."""
        # The submit's selector read, taken without the session lock so the conversation read starts early;
        # ``sessions.current`` reuses it and raises any refusal.
        with contextlib.suppress(Problem):
            sessions.reader(reader, checked_since=checked_since)
        kept_reader = sessions.cached_reader(reader)
        conversation = None
        if kept_reader is not None:
            read = NativeConversationRead(conversation_id=command.conversation_id)
            conversation = _PREFETCH.submit(copy_context().run, lambda: self._invoke(kept_reader, read)[0])
        kept, lock = _kept_conversations(self.composition)
        with lock:
            basis = kept.get((reader.target.agent_id, command.conversation_id))
        capture = None
        if basis is not None:
            authority = getattr(getattr(self.composition, "assistant", None), "authority", None)
            capture = _PREFETCH.submit(copy_context().run, resolve_capture, control, authority, principal, command,
                                       conversation=basis)
        return _SubmitPrefetch(conversation, capture, basis,
                               target=None if kept_reader is None else kept_reader.target)

    def _keep_admitted(self, client, value):
        """A new conversation's admitted receipt names its CID and the capture it keeps, the next submit's basis."""
        if not isinstance(value, NativeReceipt) or value.status == "busy_refused":
            return
        try:
            conversation = NativeConversation(
                state_profile=value.state_profile, agent_id=value.agent_id, hosted_session_id=value.hosted_session_id,
                conversation_id=value.conversation_id, last_turn=0, last_request_id=None,
                capture=value.capture, capture_digest=value.capture_digest)
        except ValueError:
            return
        self._keep_conversation(client, conversation)

    def _keep_conversation(self, client, conversation):
        kept, lock = _kept_conversations(self.composition)
        with lock:
            kept[(client.target.agent_id, conversation.conversation_id)] = conversation
            kept.move_to_end((client.target.agent_id, conversation.conversation_id))
            while len(kept) > KEPT_CONVERSATIONS:
                kept.popitem(last=False)

    def _prepare(self, control, client, principal, command, state_profile, *, timed=None, prefetch=None):
        previous = None
        if command.conversation_id is not None:
            with phase_timing("api_submit_conversation", job_id=timed):
                previous = None if prefetch is None else prefetch.conversation(client)
                if previous is None:
                    previous, _ = self._invoke(client, NativeConversationRead(conversation_id=command.conversation_id))
            if previous.state_profile != state_profile:
                raise Problem(409, "NATIVE_STATE_SCOPE", "the original CID must keep its selected state profile")
            self._same_session(client, previous)
            if previous.continuation_blocker is not None:
                raise Problem(409, previous.continuation_blocker, "the original conversation cannot resume on this environment")
            self._keep_conversation(client, previous)
        authority = getattr(getattr(self.composition, "assistant", None), "authority", None)
        with phase_timing("api_submit_capture", job_id=timed):
            capture = None if prefetch is None or previous is None else prefetch.capture(previous)
            if capture is None:
                capture = resolve_capture(control, authority, principal, command, conversation=previous)
        from .behavior_native_responses_api import turn_profile
        if turn_profile(capture, conversation=previous) != state_profile:
            raise Problem(409, "NATIVE_STATE_SCOPE", "the server-selected template uses another state profile")
        return capture, previous

    def _retained(self, control, client, command, original, state_profile, *, response=None, defer=None):
        if original.state_profile != state_profile:
            raise Problem(409, "NATIVE_STATE_SCOPE", "the original RID must keep its selected state profile")
        if original.command != command:
            raise Problem(409, "NATIVE_RID_CONFLICT", "the original request ID has different retained input")
        if original.status != "accepted":
            return self._accounted(control, (original, response))
        self._same_session(client, original)
        from .behavior_native_responses_api import submit_native_turn
        return self._accounted(control, submit_native_turn(
            self.composition, client, command, original.capture, original=original, defer=defer))

    def upload_image(self, principal, command):
        """Store one attached PNG with the Agent before the turn that names it; repeating it is the same item."""
        command = NativeUserImageUpload.model_validate_json(command.model_dump_json())
        _, client = self._context(principal, "submit")
        value, response = self._invoke(client, command)
        if value != stored(client.target.agent_id, command.png_base64):
            raise Problem(502, "NATIVE_STATE_INVALID", "the stored image differs from the uploaded bytes")
        return value, response

    def change(self, principal, command):
        try:
            command = NATIVE_REQUEST.validate_json(command.model_dump_json())
        except (ValueError, AttributeError):
            raise Problem(422, "NATIVE_REQUEST_INVALID", "one original typed native command is required") from None
        if not isinstance(command, (NativeCancel, NativeDelivered)):
            raise Problem(422, "NATIVE_REQUEST_INVALID", "only cancellation or delivery acknowledgement is supported")
        control, client = self._turn_context(
            principal, "acknowledge" if isinstance(command, NativeDelivered) else "read", command.request_id)
        self._legacy(control, command.conversation_id)
        return self._accounted(control, self._invoke(client, command))

    def append(self, principal, command):
        command = NativeAppend.model_validate_json(command.model_dump_json())
        control, client = self._context(principal, "submit")
        self._legacy(control, command.conversation_id)
        original, _ = self._invoke(client, NativeRead(request_id=command.request_id))
        if (original.conversation_id != command.conversation_id
                or original.submit_digest != command.expected_submit_digest):
            raise Problem(409, "NATIVE_RID_CONFLICT", "the append must name the original conversation and turn version")
        if original.status in {"accepted", "started"}:
            self._same_session(client, original)
        self._context(principal, "submit", prepared=False)
        return self._invoke(client, command)


def _knows_notice_kinds(request) -> bool:
    """The workbench bridge decodes the phase 2c notice kinds (``NOTICE_KINDS_HEADER``, ADR-0009 §8)."""
    return request.headers.getlist(NOTICE_KINDS_HEADER) == [NOTICE_KINDS_VERSION]


def install_native_turn_routes(app, composition, *, enabled):
    gateway = NativeTurnGateway(composition, enabled=enabled)
    identity = _principal_dependency(composition.verifier)

    async def reply(call, *, read=False, owner=False, background=None):
        """``owner``: the owner's own submit route from a bridge that knows the phase 2c notice kinds; a slot notice
        its receipt carries is cleared by a background task, which Starlette runs only after the whole body was sent
        (``background``: the route's own task list). Without that bridge nothing is cleared, as before phase 2c."""
        try:
            value, upstream = await run_in_threadpool(call)
        except Problem as error:
            from .behavior_native_sessions import _RetiredSession
            from .behavior_work_policy_api import _SAFE_WORK_ERRORS
            code = error.body["error"]["code"]
            transport_errors = {
                "FOUNDRY_INVOCATION_UNKNOWN", "FOUNDRY_RESPONSE_INVALID", "FOUNDRY_NATIVE_BINDING_MISMATCH",
                "FOUNDRY_CREDENTIAL_DISABLED", "FOUNDRY_CREDENTIAL_UNAVAILABLE",
            }
            # Reading again is safe: a lost Table read, or a lost commit of this read's RID-keyed usage entry.
            if (code not in NATIVE_TURN_ERRORS | transport_errors and code not in _SAFE_WORK_ERRORS
                    and (read or code not in _ATTACHED_SOURCE_ERRORS) and not isinstance(error, _BeforeAdmission)
                    and not (read and code in {"BEHAVIOR_READ_FAILED", "BEHAVIOR_COMMIT_UNKNOWN"})):
                raise
            logging.getLogger(__name__).warning("NATIVE_TURN_REFUSED code=%s", code)
            following = isinstance(error, _BeforeAdmission) and error.server_following
            # A disabled lane or a retired session does not clear by reading again.
            retry = (read and error.status >= 500 and code != "NATIVE_TURNS_DISABLED"
                     and not isinstance(error, _RetiredSession))
            return JSONResponse(status_code=error.status, headers={"Cache-Control": "no-store"}, content={"error": {
                "code": code, "message": (
                    "The Agent is moving to its current version; this message was not accepted yet. "
                    "Send the same request ID and body again shortly." if following else
                    "The entire Agent is changing environments; no new work was accepted."
                    if code == "AGENT_VERSION_FOLLOWING" else _SAFE_WORK_ERRORS.get(code, _ATTACHED_SOURCE_ERRORS.get(
                        code, "This read did not complete; reading again is safe." if retry else
                        "The original native command could not be completed; no automatic retry."))),
                # A read changes nothing, so a transient 5xx (platform or Hosted read timeout) is polled again.
                "automatic_retry": retry,
                "reconcile_original": not isinstance(error, _BeforeAdmission) and code in {
                    "NATIVE_TURN_UNKNOWN", "NATIVE_STATE_STORE_UNKNOWN", "SDK_STATE_UPLOAD_UNKNOWN",
                    "FOUNDRY_INVOCATION_UNKNOWN", "NATIVE_BOOTSTRAP_UNKNOWN", "NATIVE_RESPONSE_UNCONFIRMED",
                    "NATIVE_APPEND_UNKNOWN", "BEHAVIOR_COMMIT_UNKNOWN",
                },
                **({"not_dispatched": True, "request_id": error.request_id, **({"server_following": True} if following else {})}
                   if isinstance(error, _BeforeAdmission) else {}),
                **({"confirmation_not_dispatched": True, "request_id": error.request_id}
                   if isinstance(error, _OmissionBeforeDispatch) else {}),
                **({"files_check": error.body["error"]["files_check"]} if "files_check" in error.body["error"] else {}),
            }})
        response = _response(value, max_bytes=MAX_NATIVE_TURN_HTTP_BYTES)
        response.status_code = 200 if upstream is None else upstream.status_code
        response.headers["Cache-Control"] = "no-store"
        if upstream is not None:
            response.headers["x-agent-invocation-id"] = upstream.invocation_id
        notice = getattr(value, "environment_notice", None)
        if owner and isinstance(value, NativeReceipt) and notice is not None and notice.kind in SLOT_NOTICE_KINDS:
            tasks = background if background is not None else BackgroundTasks()
            tasks.add_task(gateway.clear_delivered_notice, value)
            response.background = tasks
        return response

    async def reply_read(call):
        return await reply(call, read=True)

    @app.post(PREFIX + "/prepare")
    async def prepare(request: Request, principal=Depends(identity)):
        from .behavior_wire import Command
        command = await _body(request, Command)
        return await reply(lambda: gateway.prepare(principal, command))

    @app.get(PREFIX + "/preparation")
    async def preparation(request: Request, principal=Depends(identity)):
        await read_request(request)
        return await reply_read(lambda: gateway.environment(principal))

    @app.post(PREFIX + "/prepare/reconcile")
    async def reconcile_preparation(request: Request, principal=Depends(identity)):
        from .behavior_wire import Command
        await _body(request, Command)
        return await reply(lambda: gateway.observe_preparation(principal))

    @app.get(PREFIX + "/environment")
    async def environment(request: Request, principal=Depends(identity)):
        await read_request(request)
        return await reply_read(lambda: gateway.environment(principal))

    @app.post(PREFIX + "/environment")
    async def follow_environment(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeEnvironmentCommand)
        return await reply(lambda: gateway.environment(principal, command))

    @app.post(PREFIX + "/environment/retarget")
    async def retarget_environment(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeEnvironmentRetargetCommand)
        return await reply(lambda: gateway.retarget_environment(principal, command))

    @app.post(PREFIX + "/environment/reconcile")
    async def reconcile_environment(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeEnvironmentCommand)
        return await reply(lambda: gateway.observe_environment(principal, command))

    @app.post(PREFIX + "/environment/advance")
    async def advance_environment(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeEnvironmentCommand)
        return await reply(lambda: gateway.environment(principal, command, automatic=True))

    @app.post(PREFIX + "/environment/observe-automatic")
    async def observe_automatic_environment(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeEnvironmentCommand)
        return await reply(lambda: gateway.observe_environment(principal, command, automatic=True))

    @app.post(PREFIX + "/environment/without-files")
    async def without_files(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeEnvironmentWithoutFiles)
        return await reply(lambda: gateway.without_files(principal, command))

    @app.post(PREFIX + "/environment/without-files/preview")
    async def preview_without_files(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeEnvironmentWithoutFiles)
        return await reply(lambda: gateway.without_files(principal, command, preview=True))

    @app.post(PREFIX + "/environment/without-files/reconcile")
    async def reconcile_without_files(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeEnvironmentWithoutFiles)
        return await reply(lambda: gateway.without_files(principal, command, reconcile=True))

    @app.post(PREFIX + "/environment/without-files/observe")
    async def observe_without_files(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeEnvironmentWithoutFiles)
        return await reply(lambda: gateway.without_files(principal, command, observe_only=True))

    @app.post(PREFIX + "/environment/without-files/continue")
    async def continue_without_files(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeEnvironmentWithoutFiles)
        return await reply(lambda: gateway.without_files(principal, command, resume=True))

    @app.get(PREFIX + "/requests/{request_id}/admission-observation")
    async def admission_observation(request_id: str, request: Request, principal=Depends(identity)):
        await read_request(request)
        return await reply_read(lambda: gateway.observe_absent_admission(principal, NativeRead(request_id=request_id)))

    @app.get(PREFIX + "/conversations/{conversation_id}/attachment-sources")
    async def attachments(conversation_id: str, request: Request, principal=Depends(identity)):
        await read_request(request)
        try:
            command = NativeConversationRead(conversation_id=conversation_id)
        except ValueError:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "one original conversation ID is required") from None
        def read():
            from .behavior_attachment_catalog import read_attachment_catalog

            def reading(control, client):
                gateway._legacy(control, conversation_id)
                return control, *gateway._invoke(client, command)

            control, original, upstream = gateway._reading(principal, reading)
            catalog = read_attachment_catalog(control, principal, conversation_id=conversation_id,
                                               native_template=original.capture.template)
            return catalog, upstream
        return await reply_read(read)

    async def read_request(request, *, queries=()):
        if (set(request.query_params) - set(queries)
                or any(len(request.query_params.getlist(key)) != 1 for key in request.query_params)
                or await request.body()):
            raise Problem(422, "NATIVE_REQUEST_INVALID", "this read accepts only its original selectors")

    @app.post(PREFIX + "/submit")
    async def submit(request: Request, principal=Depends(identity)):
        command = await _body(request, InstructionSubmit, max_body_bytes=MAX_SUBMIT_BODY_BYTES)
        known = _knows_notice_kinds(request)
        return await reply(lambda: gateway.submit(principal, command, owner_notice=True, notice_kinds=known),
                           owner=known)

    @app.post(PREFIX + "/prewarm")
    async def prewarm(request: Request, principal=Depends(identity)):
        # README#typing-prewarm: an empty body; the API decides the session and the toolbox versions.
        await _body(request, _PrewarmBody)
        return await reply(lambda: gateway.prewarm(principal))

    @app.post(PREFIX + "/images")
    async def upload_image(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeUserImageUpload, max_body_bytes=MAX_IMAGE_UPLOAD_BODY_BYTES)
        return await reply(lambda: gateway.upload_image(principal, command))

    @app.post(PREFIX + "/reconcile")
    async def reconcile(request: Request, principal=Depends(identity)):
        command = await _body(request, InstructionSubmit, max_body_bytes=MAX_SUBMIT_BODY_BYTES)
        return await reply(lambda: gateway.reconcile(principal, command))

    @app.post(PREFIX + "/append")
    async def append_turn(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeAppend, max_body_bytes=MAX_SUBMIT_BODY_BYTES)
        return await reply(lambda: gateway.append(principal, command))

    @app.get(PREFIX + "/conversations/{conversation_id}/requests/{request_id}/appends/{append_id}")
    async def read_append(conversation_id: str, request_id: str, append_id: str, request: Request,
                          principal=Depends(identity)):
        await read_request(request)
        command = NativeAppendRead(conversation_id=conversation_id, request_id=request_id, append_id=append_id)
        return await reply_read(lambda: gateway.read(principal, command))

    @app.get(PREFIX + "/conversations/{conversation_id}/requests/{request_id}/appends")
    async def read_appends(conversation_id: str, request_id: str, request: Request, principal=Depends(identity)):
        await read_request(request)
        command = NativeAppendRead(conversation_id=conversation_id, request_id=request_id)
        return await reply_read(lambda: gateway.read(principal, command))

    @app.get(PREFIX + "/conversations/{conversation_id}/requests/{request_id}/progress")
    async def read_progress(conversation_id: str, request_id: str, request: Request, principal=Depends(identity)):
        await read_request(request)
        command = NativeProgressRead(conversation_id=conversation_id, request_id=request_id)
        return await reply_read(lambda: gateway.read(principal, command))

    @app.get(PREFIX + "/conversations/{conversation_id}/requests/{request_id}/status")
    async def read_status(conversation_id: str, request_id: str, request: Request, principal=Depends(identity)):
        # One poll for one RID: receipt, public progress and, once completed, the single-page reply.
        await read_request(request)
        try:
            command = NativeTurnStatusRead(conversation_id=conversation_id, request_id=request_id)
        except ValueError:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "one original CID and RID are required") from None
        return await reply_read(lambda: gateway.read(principal, command))

    @app.get(PREFIX + "/conversations/{conversation_id}/requests/{request_id}/hitl-reviews")
    async def read_hitl_reviews(
        conversation_id: str, request_id: str, request: Request, after: str | None = Query(None),
        limit: int = Query(MAX_HITL_REVIEW_PAGE, ge=1, le=MAX_HITL_REVIEW_PAGE), principal=Depends(identity),
    ):
        await read_request(request, queries=("after", "limit"))
        try:
            command = NativeHitlHistoryRead(
                conversation_id=conversation_id, request_id=request_id, after=after, limit=limit)
        except ValueError:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "native review history selectors are invalid") from None
        return await reply_read(lambda: gateway.read(principal, command))

    from .behavior_native_responses_api import (
        PREFIX as RESPONSES_PREFIX, cancel as cancel_response, history as response_history, reconcile as reconcile_response,
    )
    from .native_responses_wire import NATIVE_RESPONSES_PROFILE

    @app.post(RESPONSES_PREFIX + "/submit")
    async def submit_response(request: Request, principal=Depends(identity)):
        command = await _body(request, InstructionSubmit, max_body_bytes=MAX_SUBMIT_BODY_BYTES)
        after_reply = BackgroundTasks()
        known = _knows_notice_kinds(request)
        response = await reply(lambda: gateway.submit(
            principal, command, state_profile=NATIVE_RESPONSES_PROFILE, defer=after_reply.add_task, owner_notice=True,
            notice_kinds=known), owner=known, background=after_reply)
        if after_reply.tasks:
            response.background = after_reply
        return response

    @app.post(RESPONSES_PREFIX + "/reconcile")
    async def reconcile_response_turn(request: Request, principal=Depends(identity)):
        command = await _body(request, InstructionSubmit, max_body_bytes=MAX_SUBMIT_BODY_BYTES)
        return await reply(lambda: reconcile_response(gateway, principal, command))

    @app.post(RESPONSES_PREFIX + "/cancel")
    async def cancel_response_turn(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeCancel)
        return await reply(lambda: cancel_response(gateway, principal, command))

    @app.get(RESPONSES_PREFIX + "/conversations/{conversation_id}/history")
    async def stored_conversation_history(
        conversation_id: str, request: Request, after: str | None = Query(None),
        limit: int = Query(20, ge=1, le=100), principal=Depends(identity),
    ):
        await read_request(request, queries=("after", "limit"))
        return await reply_read(lambda: response_history(
            gateway, principal, "conversation", conversation_id, after=after, limit=limit))

    @app.get(RESPONSES_PREFIX + "/responses/{response_id}/history")
    async def stored_response_history(
        response_id: str, request: Request, after: str | None = Query(None),
        limit: int = Query(20, ge=1, le=100), principal=Depends(identity),
    ):
        await read_request(request, queries=("after", "limit"))
        return await reply_read(lambda: response_history(
            gateway, principal, "response", response_id, after=after, limit=limit))

    from .behavior_native_stream_api import install_native_stream_routes
    install_native_stream_routes(app, gateway, identity, read_request, reply_read, RESPONSES_PREFIX)

    @app.get(PREFIX + "/requests/{request_id}")
    async def receipt(request_id: str, request: Request, principal=Depends(identity)):
        await read_request(request)
        try:
            command = NativeRead(request_id=request_id)
        except ValueError:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "one original request ID is required") from None
        return await reply_read(lambda: gateway.read(principal, command))

    @app.get(PREFIX + "/requests/{request_id}/continuation")
    async def continuation(request_id: str, request: Request, principal=Depends(identity)):
        await read_request(request)
        try:
            command = NativeContinuationRead(request_id=request_id)
        except ValueError:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "one original request ID is required") from None
        return await reply_read(lambda: gateway.read(principal, command))

    @app.get(PREFIX + "/requests/{request_id}/system-prompt")
    async def turn_system_prompt(request_id: str, request: Request, principal=Depends(identity)):
        await read_request(request)
        try:
            command = NativeRead(request_id=request_id)
        except ValueError:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "one original request ID is required") from None
        return await reply_read(lambda: gateway.system_prompt(principal, command))

    @app.get(PREFIX + "/conversations/{conversation_id}")
    async def conversation(conversation_id: str, request: Request, principal=Depends(identity)):
        await read_request(request)
        try:
            command = NativeConversationRead(conversation_id=conversation_id)
        except ValueError:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "one original conversation ID is required") from None
        return await reply_read(lambda: gateway.read(principal, command))

    @app.get(PREFIX + "/received")
    async def received(request: Request, after: str | None = Query(None),
                       limit: int = Query(MAX_RECEIVED_PAGE, ge=1, le=MAX_RECEIVED_PAGE), principal=Depends(identity)):
        await read_request(request, queries=("after", "limit"))
        try:
            command = NativeReceivedRead(after=after, limit=limit)
        except ValueError:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "received page selectors are invalid") from None
        return await reply_read(lambda: gateway.read(principal, command))

    @app.get(PREFIX + "/conversations/{conversation_id}/history")
    async def history(conversation_id: str, request: Request, after_turn: int = Query(0, ge=0),
                      limit: int = Query(20, ge=1, le=100), principal=Depends(identity)):
        await read_request(request, queries=("after_turn", "limit"))
        try:
            command = NativeHistory(conversation_id=conversation_id, after_turn=after_turn, limit=limit)
        except ValueError:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "native history selectors are invalid") from None
        return await reply_read(lambda: gateway.read(principal, command))

    @app.get(PREFIX + "/conversations/{conversation_id}/requests/{request_id}/result")
    async def result(conversation_id: str, request_id: str, request: Request, part: int = Query(0, ge=0),
                     principal=Depends(identity)):
        await read_request(request, queries=("part",))
        try:
            command = NativeResult(conversation_id=conversation_id, request_id=request_id, part=part)
        except ValueError:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "native result selectors are invalid") from None
        return await reply_read(lambda: gateway.read(principal, command))

    @app.get(PREFIX + "/conversations/{conversation_id}/requests/{request_id}/memory-candidate")
    async def memory_candidate(conversation_id: str, request_id: str, request: Request, principal=Depends(identity)):
        # ADR-0007 C5: the original human inspects an inactive suggestion; reading never shares or activates it.
        await read_request(request)
        try:
            command = NativeCandidateRead(conversation_id=conversation_id, request_id=request_id)
        except ValueError:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "one original conversation and request ID are required") from None
        return await reply_read(lambda: gateway.read(principal, command))

    @app.get(PREFIX + "/conversations/{conversation_id}/requests/{request_id}/images")
    async def images(conversation_id: str, request_id: str, request: Request, principal=Depends(identity)):
        await read_request(request)
        try:
            command = NativeImagesRead(conversation_id=conversation_id, request_id=request_id)
        except ValueError:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "one original CID and RID are required") from None
        return await reply_read(lambda: gateway.read(principal, command))

    @app.get(PREFIX + "/conversations/{conversation_id}/requests/{request_id}/images/{version_id}")
    async def image(conversation_id: str, request_id: str, version_id: str, request: Request, principal=Depends(identity)):
        await read_request(request)
        try:
            command = NativeImageRead(conversation_id=conversation_id, request_id=request_id, version_id=version_id)
        except ValueError:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "one original CID, RID and image version are required") from None
        return await reply_read(lambda: gateway.read(principal, command))

    @app.post(PREFIX + "/cancel")
    async def cancel(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeCancel)
        return await reply(lambda: gateway.change(principal, command))

    @app.post(PREFIX + "/delivered")
    async def delivered(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeDelivered)
        return await reply(lambda: gateway.change(principal, command))
