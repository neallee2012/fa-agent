"""Default-off group control records, using the original verified Team entry."""

import hashlib

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .behavior_api import _body, _log_request_failure, _principal_dependency, _response
from .behavior_auth import BehaviorPrincipal
from .behavior_assistant_wire import InstructionSubmit
from .behavior_native_agent_wire import NativeRelayResult
from .behavior_native_turn_api import NativeTurnGateway, configured_native_turns
from .behavior_native_turn_wire import (
    NativeConversationRead, NativeRead, NativeResult, NativeTurnAddress, native_turn_root,
)
from .behavior_team_registry import resolve_agent_scope, team_recipient_ids
from .behavior_wire import WireModel
from .errors import Problem
from .native_agent_api import NativeAgentRelayGateway
from .native_group_wire import (
    NativeGroupAdvance,
    NATIVE_GROUP_PREFIX, NativeGroupActivity, NativeGroupCompletion, NativeGroupContext, NativeGroupFyi,
    NativeGroupListQuery, NativeGroupNextRound,
    NativeGroupOpen, NativeGroupOptions, NativeGroupPost, NativeGroupSignal, NativeGroupStarted,
    NativeGroupTranscript, groups_enabled,
)
from .native_groups import NativeGroupStore, group_post_id
from .native_responses_wire import NATIVE_RESPONSES_PROFILE
from .source_materials import digest
from .storage import TableRepository

PREFIX = NATIVE_GROUP_PREFIX


class NativeGroupGateway:
    def __init__(self, composition, *, enabled, repository=None):
        self.owner = getattr(composition, "_agent_message_owner", None) or composition
        self.enabled, self.repository = enabled, repository
        self.relay = NativeAgentRelayGateway(self.owner, enabled=True, repository=repository)

    def _authorize(self, principal: BehaviorPrincipal, *, write=False, owner=False):
        if not isinstance(principal, BehaviorPrincipal) or principal.tenant_id != str(self.owner.config.identity.tenant_id):
            raise Problem(403, "NATIVE_GROUP_SCOPE_DENIED", "the original verified Team identity is required")
        if owner or principal.kind == "human":
            control = self.owner.runtime.work_policy
            if control is None:
                raise Problem(503, "NATIVE_GROUP_UNAVAILABLE", "the original Team owner is unavailable")
            control._human(principal, "submit" if write else "read")
        else:
            self.relay._sender_scope(principal)
            principal.require(principal.team, "submit" if write else "read")

    def _store(self, principal):
        if not self.enabled:
            raise Problem(503, "NATIVE_GROUP_DISABLED", "native group conversations are not enabled")
        repository = self.repository if self.repository is not None else TableRepository(self.owner.clients[0])
        store = NativeGroupStore(repository, tenant_id=principal.tenant_id, team_id=principal.team)
        if store.partition_key == self.owner.config.table.scope.partition_key:
            raise Problem(503, "NATIVE_GROUP_UNAVAILABLE", "group data cannot share the legacy authority inventory")
        return store

    def options(self, principal):
        self._authorize(principal)
        members = () if not self.enabled else tuple(identifier for identifier, _name in team_recipient_ids(
            self.owner.runtime.store, self.owner.config, sender_oid=principal.oid, team_id=principal.team))
        return NativeGroupOptions(enabled=self.enabled, member_agent_ids=members,
                                  default_agent_id=self.owner.config.targets[0].agent_id)

    def open(self, principal, command):
        self._authorize(principal, write=True, owner=True)
        store = self._store(principal)
        command = NativeGroupOpen.model_validate_json(command.model_dump_json())
        available = {identifier for identifier, _name in team_recipient_ids(
            self.owner.runtime.store, self.owner.config, sender_oid=principal.oid, team_id=principal.team)}
        if not set(command.member_agent_ids).issubset(available):
            raise Problem(403, "NATIVE_GROUP_MEMBER_REQUIRED", "group members must be ready Agents of this Team")
        return store.open(command)

    def read(self, principal, group_id):
        self._authorize(principal)
        group = self._store(principal).read(group_id)
        if principal.kind == "agent":
            scope = self.relay._sender_scope(principal)
            if scope.target.agent_id not in group.member_agent_ids:
                raise Problem(403, "NATIVE_GROUP_MEMBER_REQUIRED", "only a member Agent may read this group")
        return group

    def start(self, principal, group_id):
        self._authorize(principal, write=True, owner=True)
        group = self._store(principal).read(group_id)
        target = self.owner.for_agent(group.orchestrator_agent_id)
        gateway = NativeTurnGateway(target, enabled=configured_native_turns())
        receipt, _ = gateway.submit(principal, InstructionSubmit(
            request_id=group_id, content=group.question), state_profile=NATIVE_RESPONSES_PROFILE)
        return NativeGroupStarted(group_id=group_id, receipt=receipt)

    def context(self, principal, command: NativeRelayResult) -> NativeGroupContext:
        actor = self._agent(principal)
        store = self._store(principal)
        scope = self.relay._sender_scope(principal)
        _, client = self.relay._client(scope)
        receipt = self.relay._original(client, command.request_id)
        if receipt is None or receipt.conversation_id != command.conversation_id:
            raise Problem(403, "NATIVE_GROUP_SCOPE_DENIED", "the original Agent turn is required")
        NativeTurnGateway._same_session(client, receipt)
        self.relay._original_source(principal, receipt)
        root = native_turn_root(receipt)
        try:
            group = store.read(root.request_id)
        except Problem as error:
            if error.body["error"]["code"] in {"NATIVE_GROUP_NOT_FOUND", "NATIVE_GROUP_INVALID"}:
                return NativeGroupContext()
            raise
        if root.agent_id != group.orchestrator_agent_id or actor not in group.member_agent_ids:
            raise Problem(403, "NATIVE_GROUP_SCOPE_DENIED", "the native root differs from the original group")
        original = receipt
        if actor != group.orchestrator_agent_id or receipt.request_id != group.group_id:
            original_scope = resolve_agent_scope(self.owner.runtime.store, self.owner.config, group.orchestrator_agent_id)
            if original_scope.team_id != principal.team:
                raise Problem(403, "NATIVE_GROUP_SCOPE_DENIED", "the group root belongs to another Team")
            _, original_client = self.relay._client(original_scope)
            original = self.relay._original(original_client, group.group_id)
        if (original is None or not original.human_origin or original.agent_id != group.orchestrator_agent_id
                or original.conversation_id != root.conversation_id or original.command.content != group.question
                or original.command.conversation_id is not None or original.state_profile != NATIVE_RESPONSES_PROFILE):
            raise Problem(403, "NATIVE_GROUP_SCOPE_DENIED", "the group must retain its original human Responses root")
        return NativeGroupContext(
            group=group, root=root, role="orchestrator" if actor == group.orchestrator_agent_id else "member")

    def _agent(self, principal):
        self._authorize(principal, write=True)
        if principal.kind != "agent":
            raise Problem(403, "NATIVE_GROUP_AGENT_REQUIRED", "only a verified member Agent may publish or route")
        return self.relay._sender_scope(principal).target.agent_id

    def post(self, principal, group_id, command):
        actor = self._agent(principal)
        if command.kind != "fyi" or command.turn is not None or command.result is not None:
            raise Problem(403, "NATIVE_GROUP_ORIGINAL_RESULT_REQUIRED", "work and results use their original relay paths")
        return self._store(principal).post(group_id, actor, command)

    def advance(self, principal, group_id, command):
        actor = self._agent(principal)
        return self._store(principal).advance(group_id, actor, command)

    def route(self, principal, group_id, command):
        actor = self._agent(principal)
        return self._store(principal).route(group_id, actor, command)

    def read_post(self, principal, group_id, post_id):
        self.read(principal, group_id)
        return self._store(principal).read_post(group_id, post_id)

    def transcript(self, principal, group_id):
        group = self.read(principal, group_id)
        store = self._store(principal)
        return NativeGroupTranscript(group=group, posts=tuple(
            store.read_post(group_id, post.post_id) for post in group.posts))

    def list(self, principal, query):
        self._authorize(principal, owner=True)
        return self._store(principal).list(query.after)

    def opening(self, principal, request_id):
        self._authorize(principal, owner=True)
        return self._store(principal).opening(request_id)

    def activity(self, principal, group_id):
        self._authorize(principal, owner=True)
        group = self._store(principal).read(group_id)
        targets = [(group.orchestrator_agent_id, group_id), *(
            (item.recipient_agent_id, item.message_id) for item in group.deliveries if item.refusal_code is None)]
        turns, missing = [], []
        seen = set()

        def belongs(receipt):
            root = native_turn_root(receipt)
            return root.agent_id == group.orchestrator_agent_id and root.request_id == group_id

        for agent_id, rid in targets:
            gateway = NativeTurnGateway(self.owner.for_agent(agent_id), enabled=configured_native_turns())
            try:
                receipt, _ = gateway.read(principal, NativeRead(request_id=rid))
            except Problem as error:
                if error.status == 404 and error.body["error"]["code"] == "NATIVE_RID_MISSING":
                    missing.append(rid)
                    continue
                raise
            if not belongs(receipt):
                raise Problem(403, "NATIVE_GROUP_SCOPE_DENIED", "activity belongs to another native group root")
            if (agent_id, rid) not in seen:
                turns.append(receipt)
                seen.add((agent_id, rid))
            conversation, _ = gateway.read(principal, NativeConversationRead(conversation_id=receipt.conversation_id))
            current = conversation.inflight_request_id or conversation.last_request_id
            if current is not None and (agent_id, current) not in seen:
                latest, _ = gateway.read(principal, NativeRead(request_id=current))
                if belongs(latest):
                    turns.append(latest)
                    seen.add((agent_id, current))
        return NativeGroupActivity(group_id=group_id, turns=tuple(turns), unconfirmed_request_ids=tuple(missing))

    def turn_fyi(self, principal, command: NativeGroupFyi):
        context = self.context(principal, command)
        if context.group is None:
            raise Problem(403, "NATIVE_GROUP_SCOPE_DENIED", "the original turn is not in a group")
        actor = self._agent(principal)
        _, client = self.relay._client(self.relay._sender_scope(principal))
        original = self.relay._original(client, command.request_id)
        if original.status != "started" or original.cancel_requested:
            raise Problem(409, "NATIVE_GROUP_TURN_CLOSED", "the original turn can no longer publish")
        request_id = digest({"kind": "fa-native-group-fyi-v1", "turn": command.request_id,
                             "tool_call_id": command.tool_call_id})
        group = context.group
        previous = next((post for post in group.posts
                         if post.post_id == group_post_id(group.group_id, actor, request_id)), None)
        saved = self._store(principal).post(group.group_id, actor, NativeGroupPost(
            request_id=request_id, round=group.round if previous is None else previous.round, kind="fyi",
            content=command.content, recipient_agent_id=command.recipient_agent_id))
        if command.recipient_agent_id is not None:
            self._store(principal).route(group.group_id, actor, NativeGroupSignal(
                event_id=saved.post_id, recipient_agent_id=command.recipient_agent_id, kind="fyi",
                payload_sha256=saved.content_sha256))
        return saved

    def turn_advance(self, principal, command: NativeGroupNextRound):
        context = self.context(principal, command)
        if context.group is None or context.role != "orchestrator":
            raise Problem(403, "NATIVE_GROUP_ORCHESTRATOR_REQUIRED", "only the original orchestrator turn may advance")
        _, client = self.relay._client(self.relay._sender_scope(principal))
        original = self.relay._original(client, command.request_id)
        if original.status != "started" or original.cancel_requested:
            raise Problem(409, "NATIVE_GROUP_TURN_CLOSED", "the original turn can no longer advance")
        return self.advance(principal, context.group.group_id, NativeGroupAdvance(
            request_id=digest({"kind": "fa-native-group-round-v1", "turn": command.request_id,
                               "tool_call_id": command.tool_call_id}), expected_round=command.expected_round))

    def complete(self, principal, command: NativeRelayResult):
        context = self.context(principal, command)
        if context.group is None:
            return NativeGroupCompletion(reason="no_group")
        group = context.group
        scope = self.relay._sender_scope(principal)
        _, client = self.relay._client(scope)
        original = self.relay._original(client, command.request_id)
        if (original is None or original.conversation_id != command.conversation_id
                or original.status not in {"completed", "unknown", "busy_refused"}
                or original.status == "completed" and original.result is None):
            raise Problem(409, "NATIVE_GROUP_ORIGINAL_RESULT_REQUIRED", "only the original terminal receipt can publish")
        if original.delegation is not None or original.queued_relay is not None:
            return NativeGroupCompletion(group_id=group.group_id, reason="delegated")
        if original.status == "unknown" and self.relay._continuation_pending(original, client):
            return NativeGroupCompletion(group_id=group.group_id, reason="awaiting_results")
        request_id = digest({"kind": "fa-native-group-result-v1", "agent": original.agent_id,
                             "request_id": original.request_id})
        post_id = group_post_id(group.group_id, original.agent_id, request_id)
        previous = next((post for post in group.posts if post.post_id == post_id), None)
        if previous is not None:
            if previous.turn is None or previous.turn.request_id != original.request_id or previous.result != original.result:
                raise Problem(409, "NATIVE_GROUP_CONFLICT", "the original result publication has changed")
            self._store(principal).read_post(group.group_id, post_id)
            return NativeGroupCompletion(group_id=group.group_id, post=previous, reason="published")
        pending_id, publication_round = None, group.round
        if context.role == "member":
            request = self.relay._reply_request(original, client)
            delivery = None if request is None else next(
                (item for item in group.deliveries if item.message_id == request.source.message_id), None)
            if delivery is None or delivery.recipient_agent_id != original.agent_id:
                raise Problem(403, "NATIVE_GROUP_SCOPE_DENIED", "the result has no original group request")
            pending_id, publication_round = delivery.message_id, delivery.round
        if original.status == "completed":
            text, part = [], 0
            while True:
                page, _ = NativeTurnGateway._invoke(client, NativeResult(
                    conversation_id=original.conversation_id, request_id=original.request_id, part=part))
                if page.result != original.result:
                    raise Problem(409, "NATIVE_RESULT_INCOMPLETE", "the group result differs from the original reference")
                text.append(page.text)
                if page.next_part is None:
                    break
                if page.next_part <= part:
                    raise Problem(409, "NATIVE_RESULT_INCOMPLETE", "the original result cursor did not advance")
                part = page.next_part
            content = "".join(text)
            if (len(content.encode("utf-8")) != original.result.text_bytes
                    or hashlib.sha256(content.encode("utf-8")).hexdigest() != original.result.text_sha256):
                raise Problem(409, "NATIVE_RESULT_INCOMPLETE", "the complete group result differs from its saved digest")
        else:
            content = ("原成員回合未能確認完成（" + (original.failure_code or "NATIVE_TURN_UNKNOWN")
                       + "）。保留原回條，不表示已停止或可以重新交辦。")
        if not content.strip() or len(content.encode("utf-8")) > 8192:
            content = "The original full result is saved; read the attached original result reference."
        store = self._store(principal)
        post = NativeGroupPost(
            request_id=request_id, round=publication_round,
            kind=("blocked" if original.status != "completed" else
                  "final" if context.role == "orchestrator" else "result"),
            content=content, turn=NativeTurnAddress(
                agent_id=original.agent_id, conversation_id=original.conversation_id, request_id=original.request_id),
            result=original.result)
        try:
            saved = store.post(group.group_id, original.agent_id, post, settles=pending_id)
        except Problem as error:
            reasons = {"NATIVE_GROUP_ALLOWANCE": "publication_limit", "NATIVE_GROUP_CLOSED": "closed",
                       "NATIVE_GROUP_ROUND_CHANGED": "old_round", "NATIVE_GROUP_PENDING": "awaiting_results"}
            reason = reasons.get(error.body["error"]["code"])
            if reason is None:
                raise
            return NativeGroupCompletion(group_id=group.group_id, reason=reason)
        return NativeGroupCompletion(group_id=group.group_id, post=saved, reason="published")


def install_native_group_routes(app, composition, *, repository=None, environ=None):
    gateway = NativeGroupGateway(composition, enabled=groups_enabled(environ), repository=repository)
    router = APIRouter()
    identity = _principal_dependency(composition.verifier)

    async def reply(request, call, *, created=False):
        try:
            value = await run_in_threadpool(call)
        except Problem as error:
            code = error.body["error"]["code"]
            if not code.startswith("NATIVE_GROUP_"):
                raise
            _log_request_failure(request, code)
            return JSONResponse(status_code=error.status, headers={"Cache-Control": "no-store"}, content={"error": {
                "code": code, "message": "Read the original group request and its saved state.",
                "automatic_retry": False, "reconcile_original": code == "NATIVE_GROUP_UNKNOWN",
            }})
        response = _response(value)
        response.status_code = 201 if created else 200
        return response

    async def no_body(request):
        if request.query_params:
            raise Problem(422, "NATIVE_GROUP_INVALID", "this group read takes no query or body")
        async for chunk in request.stream():
            if chunk:
                raise Problem(422, "NATIVE_GROUP_INVALID", "this group read takes no query or body")

    @router.get(PREFIX + "/options")
    async def options(request: Request, principal=Depends(identity)):
        await no_body(request)
        return await reply(request, lambda: gateway.options(principal))

    @router.post(PREFIX + "/groups")
    async def create(request: Request, principal=Depends(identity)):
        gateway._authorize(principal, write=True, owner=True)
        command = await _body(request, NativeGroupOpen)
        return await reply(request, lambda: gateway.open(principal, command), created=True)

    @router.post(PREFIX + "/list")
    async def list_groups(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeGroupListQuery)
        return await reply(request, lambda: gateway.list(principal, command))

    @router.get(PREFIX + "/openings/{request_id}")
    async def opening(request: Request, request_id: str, principal=Depends(identity)):
        await no_body(request)
        return await reply(request, lambda: gateway.opening(principal, request_id))

    @router.post(PREFIX + "/context")
    async def context(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeRelayResult)
        return await reply(request, lambda: gateway.context(principal, command))

    @router.post(PREFIX + "/complete")
    async def complete(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeRelayResult)
        return await reply(request, lambda: gateway.complete(principal, command))

    @router.post(PREFIX + "/fyi")
    async def fyi(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeGroupFyi)
        return await reply(request, lambda: gateway.turn_fyi(principal, command))

    @router.post(PREFIX + "/next-round")
    async def next_round(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeGroupNextRound)
        return await reply(request, lambda: gateway.turn_advance(principal, command))

    @router.get(PREFIX + "/groups/{group_id}")
    async def read(request: Request, group_id: str, principal=Depends(identity)):
        await no_body(request)
        return await reply(request, lambda: gateway.read(principal, group_id))

    @router.get(PREFIX + "/groups/{group_id}/transcript")
    async def transcript(request: Request, group_id: str, principal=Depends(identity)):
        await no_body(request)
        return await reply(request, lambda: gateway.transcript(principal, group_id))

    @router.get(PREFIX + "/groups/{group_id}/activity")
    async def activity(request: Request, group_id: str, principal=Depends(identity)):
        await no_body(request)
        return await reply(request, lambda: gateway.activity(principal, group_id))

    @router.post(PREFIX + "/groups/{group_id}/start")
    async def start(request: Request, group_id: str, principal=Depends(identity)):
        await _body(request, WireModel)
        return await reply(request, lambda: gateway.start(principal, group_id))

    @router.get(PREFIX + "/groups/{group_id}/posts/{post_id}")
    async def read_post(request: Request, group_id: str, post_id: str, principal=Depends(identity)):
        await no_body(request)
        return await reply(request, lambda: gateway.read_post(principal, group_id, post_id))

    @router.post(PREFIX + "/groups/{group_id}/posts")
    async def post(request: Request, group_id: str, principal=Depends(identity)):
        gateway._agent(principal)
        command = await _body(request, NativeGroupPost)
        return await reply(request, lambda: gateway.post(principal, group_id, command), created=True)

    @router.post(PREFIX + "/groups/{group_id}/rounds")
    async def advance(request: Request, group_id: str, principal=Depends(identity)):
        gateway._agent(principal)
        command = await _body(request, NativeGroupAdvance)
        return await reply(request, lambda: gateway.advance(principal, group_id, command))

    @router.post(PREFIX + "/groups/{group_id}/signals")
    async def route(request: Request, group_id: str, principal=Depends(identity)):
        gateway._agent(principal)
        command = await _body(request, NativeGroupSignal)
        return await reply(request, lambda: gateway.route(principal, group_id, command))

    app.include_router(router)
