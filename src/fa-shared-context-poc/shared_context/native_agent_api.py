"""Native-only Agent relay. The receiver's own N1 RID is its acceptance and execution authority."""

from __future__ import annotations

import logging
import os
from time import monotonic

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .behavior_api import _body, _log_request_failure, _principal_dependency, _response
from .behavior_agent_message_wire import AgentMessageRecipient
from .behavior_auth import BehaviorPrincipal
from .behavior_foundry import FoundryHttpClient, MAX_NATIVE_TURN_HTTP_BYTES
from .behavior_native_capture import resolve_agent_capture
from .behavior_native_agent_wire import (
    NATIVE_AGENT_PREFIX as PREFIX, NativePeerDirectory, NativeRelayAcceptance,
    NativeRelayReadResult, NativeRelayResult, NativeRelaySend, relay_acceptance_scope,
)
from .behavior_native_turn_api import NativeTurnGateway
from .behavior_native_responses_api import submit_native_turn
from .behavior_native_sessions import native_sessions
from .behavior_native_turn_wire import (
    NativeAgentCommand, NativeAgentReceive, NativeConversationRead, NativeRead, NativeReceipt, NativeResult,
)
from .behavior_server_config import ServerAgentScope
from .behavior_team_registry import resolve_agent_scope, resolve_team_recipient, team_recipient_ids
from .behavior_work_target import selected_targets
from .errors import Problem
from .native_agent_relay import (
    message_identifier, message_material_digest, outcome_reference, prepare_outcome, prepare_request, prepare_result,
    queued_command_matches, queued_request_matches,
)
from .native_relay_quota import NativeRelayQuota
from .storage import TableRepository

class NativeAgentRelayGateway:
    def __init__(self, composition, *, enabled, repository=None):
        self.owner = getattr(composition, "_agent_message_owner", None) or composition
        self.enabled, self.repository = enabled, repository

    def _sender_scope(self, principal: BehaviorPrincipal) -> ServerAgentScope:
        if not self.enabled:
            raise Problem(503, "NATIVE_TURNS_DISABLED", "native Agent messaging is not enabled")
        principal.require(principal.team, "submit")
        config = self.owner.config
        if principal.kind != "agent" or principal.tenant_id != str(config.identity.tenant_id):
            raise Problem(403, "NATIVE_AGENT_SOURCE_INVALID", "a verified Agent identity is required")
        target = next((target for target in config.targets if target.agent_oid == principal.oid), None)
        identifier = target.agent_id if target is not None else principal.actor_id
        scope = resolve_agent_scope(self.owner.runtime.store, config, identifier)
        if (str(scope.identity.oid) != principal.oid or scope.identity.actor != principal.actor_id
                or scope.team_id != principal.team):
            raise Problem(403, "NATIVE_AGENT_SOURCE_INVALID", "the verified Agent differs from its server scope")
        return scope

    def _client(self, scope, *, prepared=True, dispatch=False):
        """``dispatch``: work is delivered to this Agent's session, so (like an owner submit) the endpoint selector
        is read in this request rather than reused from the last ``SELECTOR_CHECK_SECONDS``."""
        composition = self.owner.for_agent(scope.target.agent_id)
        control = composition.runtime.work_policy
        targets = selected_targets(control.store, composition.config, agent_id=scope.target.agent_id)
        if len(targets) != 1 or targets[0].agent_oid != scope.identity.oid:
            raise Problem(403, "NATIVE_STATE_SCOPE", "one original server-selected Agent target is required")
        clients = [client for client in composition.clients
                   if isinstance(client, FoundryHttpClient) and client.target.executor_id == targets[0].executor_id]
        if len(clients) != 1:
            raise Problem(503, "NATIVE_EXECUTION_UNAVAILABLE", "the original native transport is unavailable")
        original = clients[0].for_target(targets[0])
        if not prepared:
            return composition, original
        return composition, native_sessions(composition).current(
            original, checked_since=monotonic() if dispatch else None)

    def _quota(self, principal):
        repository = self.repository
        if repository is None:
            repository = TableRepository(self.owner.clients[0])
        quota = NativeRelayQuota(repository, tenant_id=principal.tenant_id, team_id=principal.team)
        if quota.partition_key == self.owner.config.table.scope.partition_key:
            raise Problem(503, "NATIVE_RELAY_QUOTA_INVALID", "relay metadata must not share the legacy inventory partition")
        return quota

    def _original_source(self, principal, receipt, *, client=None):
        if receipt.continuation is not None:
            if client is None:
                _, client = self._client(self._sender_scope(principal))
            return self._original_source(principal, self._recovery_source(receipt, client), client=client)
        source = receipt.source
        if source is None:
            return
        identifier = source.message_id if source.kind == "agent_request" else source.in_reply_to
        original = next((item for item in self._quota(principal).read(source.root_id).messages
                         if item.message_id == identifier), None)
        if original is None:
            raise Problem(403, "AGENT_MESSAGE_ROOT_DENIED", "the inherited root has no original relay reservation")
        if source.kind == "agent_request":
            if original.payload_sha256 != message_material_digest(receipt):
                raise Problem(403, "AGENT_MESSAGE_ROOT_DENIED", "the received request differs from its original reservation")
        else:
            peer = self._recipient(principal, source.sender.agent_id)
            _, client = self._client(peer)
            result = self._original(client, source.sender.request_id)
            request = None if result is None else self._reply_request(result, client)
            if (result is None or result.conversation_id != source.sender.conversation_id
                    or request is None or request.source.message_id != source.in_reply_to
                    or message_material_digest(request) != original.payload_sha256
                    or request.source.sender.agent_id != receipt.agent_id
                    or request.source.sender.conversation_id != receipt.conversation_id
                    or source.continuation is not None and source.continuation != request.source.sender
                    or request.source.root_id != source.root_id or request.source.root != source.root
                    or (source.kind == "agent_result" and (
                        result.status != "completed" or result.delegation is not None or result.result != source.result))
                    or (source.kind == "agent_notice" and outcome_reference(result) != source.outcome)):
                raise Problem(403, "AGENT_MESSAGE_ROOT_DENIED", "the wake must retain its original request and result")

    def _recipient(self, principal, agent_id):
        store, config = self.owner.runtime.store, self.owner.config
        scope = resolve_team_recipient(store, config, agent_id, sender_oid=principal.oid, team_id=principal.team)
        ready = team_recipient_ids(store, config, sender_oid=principal.oid, team_id=principal.team)
        if agent_id not in {identifier for identifier, _name in ready}:
            raise Problem(409, "AGENT_MESSAGE_RECIPIENT_NOT_READY", "the resolved receiver is not ready")
        return scope

    def directory(self, principal):
        self._sender_scope(principal)
        return NativePeerDirectory(agents=tuple(
            AgentMessageRecipient(agent_id=identifier, display_name=name)
            for identifier, name in team_recipient_ids(
                self.owner.runtime.store, self.owner.config, sender_oid=principal.oid, team_id=principal.team)))

    @staticmethod
    def _original(client, rid):
        try:
            return NativeTurnGateway._invoke(client, NativeRead(request_id=rid))[0]
        except Problem as error:
            if error.status == 404 and error.body["error"]["code"] == "NATIVE_RID_MISSING":
                return None
            raise

    def _recovery_source(self, receipt, client):
        from .behavior_native_turn_wire import check_continuation_link
        from .native_continuation_wire import MAX_AUTOMATIC_CONTINUATIONS
        current = receipt
        for _ in range(MAX_AUTOMATIC_CONTINUATIONS):
            if current.continuation is None:
                return current
            original = self._original(client, current.continuation.continuation_of)
            if original is None:
                raise Problem(409, "NATIVE_CONTINUATION_CONFLICT", "the original continuation source is missing")
            check_continuation_link(current, original)
            current = original
        if current.continuation is not None:
            raise Problem(409, "NATIVE_CONTINUATION_LIMIT", "the original continuation chain exceeds its bound")
        return current

    @staticmethod
    def _continuation_pending(receipt, client):
        from .behavior_native_turn_wire import NativeContinuationRead
        if not receipt.continuation_available:
            return False
        try:
            view, _ = NativeTurnGateway._invoke(client, NativeContinuationRead(request_id=receipt.request_id))
        except Problem as error:
            if error.body["error"]["code"] == "NATIVE_CONTINUATION_MISSING":
                return False
            raise
        return view.owns_outcome

    def _reply_request(self, receipt, client):
        """Walk only this Agent/CID's verified delegation continuations, not a new conversation or task."""
        current, seen = self._recovery_source(receipt, client), set()
        while current.source is not None and current.source.kind != "agent_request":
            link = current.source.continuation
            if link is None:
                return None
            if (link.agent_id != receipt.agent_id or link.conversation_id != receipt.conversation_id
                    or link.request_id in seen):
                raise Problem(403, "AGENT_MESSAGE_REPLY_DENIED", "the original delegation continuation differs")
            seen.add(link.request_id)
            previous = self._original(client, link.request_id)
            if (previous is None or previous.conversation_id != receipt.conversation_id
                    or previous.delegation is None
                    or previous.delegation.message_id != current.source.in_reply_to
                    or previous.delegation.recipient_agent_id != current.source.sender.agent_id
                    or previous.delegation.conversation_id != current.source.sender.conversation_id):
                raise Problem(403, "AGENT_MESSAGE_REPLY_DENIED", "the result has no original delegation receipt")
            current = self._recovery_source(previous, client)
        return current if current.source is not None and current.source.kind == "agent_request" else None

    @staticmethod
    def _accepted(receipt: NativeReceipt, *, source=None):
        return NativeRelayAcceptance(
            message_id=receipt.request_id, recipient_agent_id=receipt.agent_id,
            conversation_id=receipt.conversation_id, submit_digest=receipt.submit_digest, status=receipt.status,
            state_profile=receipt.state_profile, response_id=receipt.response_id, dispatch=receipt.dispatch,
            source_conversation_id=None if source is None else source.conversation_id,
            source_request_id=None if source is None else source.request_id)

    def _sender_command(self, principal, command):
        scope = self._sender_scope(principal)
        changed = None
        try:
            composition, client = self._client(scope)
        except Problem as error:
            if error.body["error"]["code"] != "NATIVE_SESSION_CHANGED":
                raise
            changed = error
            composition, base = self._client(scope, prepared=False)
            client = native_sessions(composition).reader(base)
        NativeTurnGateway._legacy(composition.runtime.work_policy, command.conversation_id)
        sender = self._original(client, command.request_id)
        if sender is None or sender.conversation_id != command.conversation_id:
            raise Problem(403, "NATIVE_AGENT_SOURCE_INVALID", "the original native sender turn is required")
        if not queued_command_matches(sender, command):
            if changed is not None:
                raise changed
            NativeTurnGateway._same_session(client, sender)
        self._original_source(principal, sender, client=client)
        return scope, client, sender

    def send(self, principal: BehaviorPrincipal, command: NativeRelaySend) -> NativeRelayAcceptance:
        command = NativeRelaySend.model_validate_json(command.model_dump_json())
        sender_scope, sender_client, sender = self._sender_command(principal, command)
        identifier = message_identifier(principal, sender, command.tool_call_id)
        try:
            recipient = self._recipient(principal, command.message.recipient_agent_id)
            peer, client = self._client(recipient, dispatch=True)
            original = self._original(client, identifier)
            if sender.queued_relay is not None and original is None and not queued_request_matches(sender, command):
                raise Problem(403, "NATIVE_AGENT_SOURCE_INVALID", "only the original queued command may be delivered")
            if sender.delegation is not None and (
                    original is None
                    or relay_acceptance_scope(sender.delegation) != relay_acceptance_scope(self._accepted(original))):
                raise Problem(503, "NATIVE_RELAY_OUTCOME_UNKNOWN",
                              "the retained acceptance requires its original receiver record; never send another message")
        except Problem as error:
            code = error.body["error"]["code"]
            if code == "NATIVE_SESSION_CHANGED":
                raise Problem(503, "AGENT_VERSION_FOLLOWING",
                              "the recipient needs its published environment; retain the original message") from None
            if code not in {
                "AGENT_MESSAGE_RECIPIENT_NOT_READY",
                "NATIVE_TURNS_DISABLED", "NATIVE_EXECUTION_UNAVAILABLE",
            }:
                raise
            logging.getLogger(__name__).warning("NATIVE_RELAY_RECIPIENT_UNAVAILABLE cause=%s", code)
            raise Problem(
                error.status, "NATIVE_RELAY_RECIPIENT_UNAVAILABLE",
                "the recipient cannot currently receive; this attempt added no delivery; prior outcomes remain unconfirmed",
            ) from None
        if original is None:
            client = native_sessions(peer).admission(client, request_id=identifier, resolved=True)
        quota = self._quota(principal)

        def dispatch(retained):
            capture = (retained.capture if retained is not None else resolve_agent_capture(
                peer.runtime.work_policy, peer.assistant.authority, principal, recipient,
                NativeAgentCommand(request_id=identifier, content=command.message.content)))
            message = prepare_request(principal, sender_scope, sender, recipient, capture, command.message,
                                      tool_call_id=command.tool_call_id, original=retained)
            material = message_material_digest(message)
            if retained is None:
                quota.reserve_message(message.source.root_id, identifier, material)
            else:
                allowed = next((item for item in quota.read(message.source.root_id).messages
                                if item.message_id == identifier), None)
                if allowed is None or allowed.payload_sha256 != material:
                    raise Problem(403, "AGENT_MESSAGE_ROOT_DENIED", "the original relay allowance must name this message")
                if retained.status != "accepted":
                    return self._accepted(retained)
            groups = None
            if os.environ.get("FA_NATIVE_GROUPS_ENABLED") == "1":
                from .native_groups import NativeGroupStore
                groups = NativeGroupStore(quota.repository, tenant_id=principal.tenant_id, team_id=principal.team)
                groups.relay_request(message)
            try:
                received, _ = submit_native_turn(
                    peer, client, message.command, message.capture, source=message.source, original=retained,
                    before_send=lambda: principal.require(recipient.team_id, "submit"))
            except Problem as error:
                code = error.body["error"]["code"]
                # Hosted refuses a lane it has not enabled with 403 before creating any receipt; the API's own
                # platform 403 (no automatic_retry flag) is a credential problem, not a member refusal.
                if (groups is not None and retained is None
                        and (error.status == 409 and code in {"NATIVE_TURNS_DISABLED", "NATIVE_SESSION_CHANGED"}
                             or error.status == 403 and code == "NATIVE_TURNS_DISABLED"
                             and error.body["error"].get("automatic_retry") is False)
                        and self._original(client, identifier) is None):
                    groups.refused_request(message, code)
                raise
            NativeTurnGateway._accounted(peer.runtime.work_policy, (received, None))
            return self._accepted(received)

        try:
            return dispatch(original)
        except Problem as error:
            if (original is not None or error.status != 409
                    or error.body["error"]["code"] != "NATIVE_RID_CONFLICT"):
                raise
            retained = self._original(client, identifier)
            if retained is None:
                raise
            return dispatch(retained)

    def environment(self, principal, command, *, observe=False):
        from .native_environment_sync import automatic_environment_command

        command = NativeRelaySend.model_validate_json(command.model_dump_json())
        _sender_scope, _sender_client, sender = self._sender_command(principal, command)
        if ((sender.status != "started" or sender.queued_relay is not None)
                and not queued_request_matches(sender, command) or sender.cancel_requested):
            raise Problem(403, "NATIVE_AGENT_SOURCE_INVALID", "the original queued sender is no longer executing")
        recipient = self._recipient(principal, command.message.recipient_agent_id)
        peer, client = self._client(recipient, prepared=False)
        sessions = native_sessions(peer)
        operation = automatic_environment_command(client.target, sessions.selected_version(client))

        def current():
            self._sender_scope(principal)
            if self._recipient(principal, command.message.recipient_agent_id) != recipient:
                raise Problem(403, "NATIVE_STATE_SCOPE", "the original recipient scope changed")
            policy = peer.runtime.work_policy.installed()
            if policy != peer.runtime.work_policy.policy or not policy.active or not policy.read_enabled:
                raise Problem(403, "WORK_POLICY_REVOKED", "the recipient's original policy is no longer active")

        current()
        view = (sessions.observe_automatic(client, operation, before_send=current) if observe else
                sessions.advance(client, operation, before_send=current))
        if not observe and view.state == "ready" and view.files_manifest_sha256 is not None:
            current()
            try:
                # A ready handoff delivers work: the selector is read in this request.
                NativeTurnGateway._signal_ready_handoffs(sessions.current(client, checked_since=monotonic()), view)
            except Problem as error:
                logging.getLogger(__name__).warning(
                    "NATIVE_CONTINUATION_HANDOFF_PENDING code=%s", error.body["error"]["code"])
        return view

    def continue_original(self, principal, command: NativeRelayResult):
        from .behavior_native_capture import resolve_continuation_capture
        from .behavior_native_turn_wire import NativeContinuationRead
        from .native_continuation_wire import CONTINUATION_STOP_STATUS

        scope = self._sender_scope(principal)
        composition, client = self._client(scope, dispatch=True)
        control = composition.runtime.work_policy
        NativeTurnGateway._legacy(control, command.conversation_id)
        source = self._original(client, command.request_id)
        if source is None or source.conversation_id != command.conversation_id or source.status != "unknown":
            raise Problem(409, "NATIVE_CONTINUATION_CONFLICT", "the original interrupted input is required")
        view, _ = NativeTurnGateway._invoke(client, NativeContinuationRead(request_id=source.request_id))
        if (view.state == "blocked" or view.request_id is None or view.continuation is None
                or view.continuation.source_submit_digest != source.submit_digest):
            raise Problem(CONTINUATION_STOP_STATUS.get(view.failure_code, 409),
                          view.failure_code or "NATIVE_CONTINUATION_CONFLICT",
                          "the original recovery intent does not permit a new input")
        existing = self._original(client, view.request_id)
        if existing is not None and existing.status != "accepted":
            return NativeTurnGateway._accounted(control, (existing, None))[0]
        conversation, _ = NativeTurnGateway._invoke(client, NativeConversationRead(
            conversation_id=source.conversation_id))
        resumed = source.command.model_copy(update={
            "request_id": view.request_id, "conversation_id": source.conversation_id})
        fresh = resolve_continuation_capture(
            control, composition.assistant.authority, principal, source, resumed, conversation)

        def current():
            self._sender_scope(principal)
            policy = control.installed()
            if policy != control.policy or not policy.active or not policy.read_enabled:
                raise Problem(403, "WORK_POLICY_REVOKED", "the original Agent policy is no longer active")
            if source.routine is not None:
                from .native_routine_wire import NativeRoutineCheck
                from .native_routines import NativeRoutineService
                NativeRoutineService(self.owner, enabled=self.enabled).check(principal, NativeRoutineCheck(
                    request_id=source.routine.request_id, conversation_id=source.conversation_id,
                    definition_sha256=source.routine.occurrence.definition_sha256))

        return NativeTurnGateway._accounted(control, submit_native_turn(
            composition, client, resumed, existing.capture if existing is not None else fresh,
            source=source.source, routine=source.routine, continuation=view.continuation,
            original=existing, conversation=conversation,
            before_send=current))[0]

    def result(self, principal: BehaviorPrincipal, command: NativeRelayResult) -> NativeRelayAcceptance:
        return self._return_outcome(principal, command, completed=True)

    def outcome(self, principal: BehaviorPrincipal, command: NativeRelayResult) -> NativeRelayAcceptance:
        return self._return_outcome(principal, command, completed=False)

    def _return_outcome(self, principal, command, *, completed):
        command = NativeRelayResult.model_validate_json(command.model_dump_json())
        scope = self._sender_scope(principal)
        sender_composition, sender_client = self._client(scope)
        NativeTurnGateway._legacy(sender_composition.runtime.work_policy, command.conversation_id)
        sender = self._original(sender_client, command.request_id)
        if (sender is None or sender.conversation_id != command.conversation_id
                or sender.delegation is not None or sender.queued_relay is not None
                or (completed and (sender.status != "completed" or sender.result is None))
                or (not completed and sender.status not in {"unknown", "busy_refused"})):
            raise Problem(403, "AGENT_MESSAGE_REPLY_DENIED", "the original final peer outcome is required")
        if not completed and self._continuation_pending(sender, sender_client):
            raise Problem(409, "NATIVE_CONTINUATION_PENDING", "the original result awaits its bounded continuation")
        request = self._reply_request(sender, sender_client)
        if request is None:
            raise Problem(403, "AGENT_MESSAGE_REPLY_DENIED", "this outcome has no upstream peer request")
        source = request.source
        admitted = next((item for item in self._quota(principal).read(source.root_id).messages
                         if item.message_id == source.message_id), None)
        if admitted is None or admitted.payload_sha256 != message_material_digest(request):
            raise Problem(403, "AGENT_MESSAGE_ROOT_DENIED", "the result must belong to the original relayed request")
        if os.environ.get("FA_NATIVE_GROUPS_ENABLED") == "1":
            from .native_group_api import NativeGroupGateway
            try:
                NativeGroupGateway(self.owner, enabled=True, repository=self.repository).complete(principal, command)
            except Problem as error:
                logging.getLogger(__name__).warning(
                    "NATIVE_GROUP_PUBLICATION_PENDING code=%s", error.body["error"]["code"])
        recipient = self._recipient(principal, source.sender.agent_id)
        target, client = self._client(recipient, dispatch=True)
        NativeTurnGateway._legacy(target.runtime.work_policy, source.sender.conversation_id)
        original = self._original(client, source.message_id)
        conversation = None
        if original is None:
            client = native_sessions(target).admission(client, request_id=source.message_id, resolved=True)
            conversation = NativeTurnGateway._invoke(
                client, NativeConversationRead(conversation_id=source.sender.conversation_id))[0]
            NativeTurnGateway._same_session(client, conversation)
            capture = resolve_agent_capture(
                target.runtime.work_policy, target.assistant.authority, principal, recipient,
                NativeAgentCommand(request_id=source.message_id, conversation_id=source.sender.conversation_id,
                                   model=conversation.capture.model.id, content="Original peer result notification."),
                conversation=conversation)
        else:
            capture = original.capture
        if completed:
            page = NativeTurnGateway._invoke(
                sender_client, NativeResult(conversation_id=sender.conversation_id, request_id=sender.request_id))[0]
            if page.result != sender.result:
                raise Problem(409, "NATIVE_RESULT_INCOMPLETE", "the result page differs from the completed original turn")
            message = prepare_result(principal, scope, sender, recipient, capture, sender.result,
                                     page.text if page.complete else None, request=request)
        else:
            message = prepare_outcome(principal, scope, sender, recipient, capture, request=request)
        received, _ = submit_native_turn(
            target, client, message.command, message.capture, source=message.source,
            original=original, conversation=conversation,
            before_send=lambda: principal.require(recipient.team_id, "submit"))
        if original is not None and original.status != "accepted":
            return self._accepted(received, source=sender)
        NativeTurnGateway._accounted(target.runtime.work_policy, (received, None))
        return self._accepted(received, source=sender)

    def read_result(self, principal: BehaviorPrincipal, command: NativeRelayReadResult):
        command = NativeRelayReadResult.model_validate_json(command.model_dump_json())
        scope = self._sender_scope(principal)
        composition, client = self._client(scope)
        NativeTurnGateway._legacy(composition.runtime.work_policy, command.conversation_id)
        turn = self._original(client, command.request_id)
        if (turn is None or turn.conversation_id != command.conversation_id or turn.source is None
                or turn.source.kind != "agent_result" or command.part >= turn.source.result.text_parts):
            raise Problem(403, "AGENT_MESSAGE_READ_DENIED", "only this turn's original peer result is readable")
        self._original_source(principal, turn)
        source = turn.source
        peer_scope = self._recipient(principal, source.sender.agent_id)
        _, peer_client = self._client(peer_scope)
        page = NativeTurnGateway._invoke(peer_client, NativeResult(
            conversation_id=source.sender.conversation_id, request_id=source.sender.request_id, part=command.part))[0]
        if page.result != source.result:
            raise Problem(409, "NATIVE_RESULT_INCOMPLETE", "the original received result reference changed")
        return page


def install_native_agent_routes(app, composition, *, enabled, repository=None):
    from .realtime_push import agent_id_of, realtime_publisher
    gateway = NativeAgentRelayGateway(composition, enabled=enabled, repository=repository)
    identity = _principal_dependency(composition.verifier)
    publisher = realtime_publisher(composition)

    def relayed(principal, event_type):
        """Hint after the recipient's original turn is admitted (send) or the peer's terminal outcome woke the
        original sender (result/outcome); IDs only, both Agents in the verified sender's Team."""
        if publisher is None:
            return None

        def after(accepted):
            ids = dict(agent_id=accepted.recipient_agent_id, conversation_id=accepted.conversation_id,
                       request_id=accepted.message_id, sender_agent_id=agent_id_of(composition, principal))
            if event_type == "agent.received":
                publisher.notify(principal.team, event_type, **ids, status=accepted.status)
            else:
                publisher.notify(principal.team, event_type, **ids, source_request_id=accepted.source_request_id)
        return after

    async def reply(call, *, accepted=False, after=None):
        try:
            result = await run_in_threadpool(call)
        except Problem as error:
            code = error.body["error"]["code"]
            if error.status == 503 and code == "NATIVE_RESPONSE_UNCONFIRMED":
                logging.getLogger(__name__).warning("NATIVE_AGENT_RESPONSE_UNCONFIRMED")
                return JSONResponse(status_code=503, headers={"Cache-Control": "no-store"}, content={"error": {
                    "code": code, "message": "The original Responses handoff is unconfirmed.",
                    "reconcile_original": True, "automatic_retry": False,
                }})
            if error.status != 503 or code not in {"AGENT_VERSION_FOLLOWING", "NATIVE_ADMISSION_PENDING"}:
                raise
            logging.getLogger(__name__).warning("NATIVE_AGENT_VERSION_PENDING code=%s", code)
            return JSONResponse(status_code=503, headers={"Cache-Control": "no-store"}, content={"error": {
                "code": code,
                "message": "The recipient is changing environments; retry only the original message.",
                "accepted": False, "automatic_retry": False,
            }})
        response = _response(result, max_bytes=MAX_NATIVE_TURN_HTTP_BYTES)
        response.headers["Cache-Control"] = "no-store"
        response.status_code = 202 if accepted else 200
        if after is not None:
            after(result)
        return response

    @app.get(PREFIX + "/recipients")
    async def directory(principal=Depends(identity)):
        return await reply(lambda: gateway.directory(principal))

    @app.post(PREFIX + "/send")
    async def send(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeRelaySend, max_body_bytes=16 * 1024)
        try:
            return await reply(lambda: gateway.send(principal, command), accepted=True,
                               after=relayed(principal, "agent.received"))
        except Problem as error:
            code = error.body["error"]["code"]
            if error.status == 409 and code == "NATIVE_GROUP_REPLY_REQUIRED":
                from .native_group_wire import NATIVE_GROUP_FAILURE_GUIDANCE
                _log_request_failure(request, code)
                return JSONResponse(status_code=409, headers={"Cache-Control": "no-store"}, content={"error": {
                    "code": code, "message": NATIVE_GROUP_FAILURE_GUIDANCE[code],
                    "automatic_retry": False, "reconcile_original": True,
                }})
            if code != "NATIVE_RELAY_RECIPIENT_UNAVAILABLE":
                raise
            return JSONResponse(status_code=error.status, headers={"Cache-Control": "no-store"}, content={"error": {
                "code": code,
                "message": "The recipient cannot currently receive; this attempt added no delivery. "
                           "Reconcile the same message ID; prior outcomes are unconfirmed.",
                "automatic_retry": False, "reconcile_original": True,
            }})

    @app.post(PREFIX + "/continue")
    async def continue_original(request: Request, principal=Depends(identity)):
        from .native_continuation_wire import CONTINUATION_STOP_STATUS
        command = await _body(request, NativeRelayResult)
        try:
            return await reply(lambda: gateway.continue_original(principal, command), accepted=True)
        except Problem as error:
            code = error.body["error"]["code"]
            if error.status != CONTINUATION_STOP_STATUS.get(code):
                raise
            _log_request_failure(request, code)
            return JSONResponse(status_code=error.status, headers={"Cache-Control": "no-store"}, content={"error": {
                "code": code, "message": "The original continuation was refused; reconcile its original admission.",
                "automatic_retry": False, "reconcile_original": True,
            }})

    @app.post(PREFIX + "/environment")
    async def recipient_environment(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeRelaySend, max_body_bytes=16 * 1024)
        return await reply(lambda: gateway.environment(principal, command))

    @app.post(PREFIX + "/environment/observe")
    async def observe_recipient_environment(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeRelaySend, max_body_bytes=16 * 1024)
        return await reply(lambda: gateway.environment(principal, command, observe=True))

    @app.post(PREFIX + "/result")
    async def result(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeRelayResult)
        return await reply(lambda: gateway.result(principal, command), accepted=True,
                           after=relayed(principal, "agent.replied"))

    @app.post(PREFIX + "/outcome")
    async def outcome(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeRelayResult)
        return await reply(lambda: gateway.outcome(principal, command), accepted=True,
                           after=relayed(principal, "agent.replied"))

    @app.post(PREFIX + "/read-result")
    async def read_result(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeRelayReadResult)
        return await reply(lambda: gateway.read_result(principal, command))
