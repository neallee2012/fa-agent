"""Teams chat relay route (docs/teams-chat-design.md, N2 T1); installed only with ``FA_TEAMS_RELAY_ENABLED=1``.

An allowlisted relay identity forwards one Teams message and the platform's ``x-agent-user-id``. The API compares that
oid with the Agent's Team owner, then runs the owner's ordinary Responses turn: Hosted's ``rid:<RID>`` create-or-read
is the only dedup, and the one new row is a create-once ``TeamsChat`` (platform conversation -> FA CID).
"""

import hashlib
import json
import logging
import os
import re
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Request
from pydantic import Field, ValidationError
from starlette.concurrency import run_in_threadpool

from .behavior_api import _body, _response
from .behavior_assistant_wire import InstructionSubmit
from .behavior_auth import BehaviorPrincipal
from .behavior_native_responses_api import reconcile
from .behavior_native_turn_api import NativeTurnGateway, _BeforeAdmission, configured_native_turns
from .behavior_native_turn_wire import RELAY_PREFIX, NativeRead, NativeResult, NativeTurnStatusRead
from .behavior_team_registry import resolve_agent_scope
from .behavior_wire import WireModel
from .errors import Problem
from .models import ID_PATTERN
from .native_hitl_attention import approval_code, find_approval_ids
from .native_hitl_wire import NativeHitlDecision, NativeHitlPendingQuery, NativeHitlTurn
from .native_responses_wire import NATIVE_RESPONSES_PROFILE

ENABLED_ENV = "FA_TEAMS_RELAY_ENABLED"
# {"<relay agent instance identity oid>": "<FA agent_id>"}; one relay speaks for exactly one Agent.
AGENTS_ENV = "FA_TEAMS_RELAY_AGENTS"
PATH = "/teams-relay/v1/turns"
DECISIONS_PATH = "/teams-relay/v1/decisions"
CHAT_PARTITION = "teams-chat-v1-{tenant}-{team}"
CHAT_SCHEMA = "fa-teams-chat-v1"

TEXT = {
    "refused": "這個助理只回應它的主人。",
    "running": "這一輪還在跑，完成後結果在工作台。",
    "question": "助理有問題要問你，請到工作台回答，結果會在工作台。",
    "connect": "需要連線，請到工作台處理，結果會在工作台。",
    "busy": "Agent 正在忙，請稍後再傳。",
    "failed": "這一輪沒有完成，請到工作台查看。",
    "approval_workbench": "這一步需要你批准，請到工作台處理，批准後結果會在工作台。",
    "no_chat": "找不到這段對話的待批准項目，請到工作台處理。",
    "no_card": "這個代碼沒有對應的待批准項目（可能已處理或過期）。",
    "duplicate": "代碼重複，請到工作台處理。",
    "handled": "這個項目已處理過。",
    "expired": "這個項目已過期。",
    "retry": "這次沒有送成，請再傳一次同樣的指令。",
    "denied": "已拒絕，這一步沒有執行。",
}
log = logging.getLogger(__name__)
_IDENTIFIER = re.compile(ID_PATTERN)


class RelayTurn(WireModel):
    """One Teams message; the relay sends the same body on every poll."""

    activity_id: Annotated[str, Field(min_length=1, max_length=256)]
    chat_id: Annotated[str, Field(min_length=1, max_length=512)]
    user_oid: Annotated[str, Field(max_length=64)] | None = None
    text: Annotated[str, Field(min_length=1)]


class RelayDecision(WireModel):
    """One Teams approval command (「允許 123456」 / 「拒絕 123456」); the relay sends the same body on every poll.

    Only an approval choice exists here: no rule text, question answer or Connect decision can be expressed."""

    activity_id: Annotated[str, Field(min_length=1, max_length=256)]
    chat_id: Annotated[str, Field(min_length=1, max_length=512)]
    user_oid: Annotated[str, Field(max_length=64)] | None = None
    choice: Literal["allow_once", "deny"]
    code: Annotated[str, Field(pattern=r"^[0-9]{6}$")]


class RelayReply(WireModel):
    state: Literal["done", "running", "needs_approval", "busy", "refused", "failed"]
    text: str


def teams_relay_agents(environ) -> dict[str, str] | None:
    """None when off; otherwise the strict relay oid -> agent_id map (fails startup when malformed)."""
    enabled = environ.get(ENABLED_ENV, "")
    if enabled in {"", "0"}:
        return None
    if enabled != "1":
        raise ValueError(ENABLED_ENV + " must be absent, 0 or 1")
    try:
        agents = json.loads(environ[AGENTS_ENV])
    except (KeyError, ValueError):
        raise ValueError(AGENTS_ENV + " must be a JSON object of relay oid to agent_id") from None
    if not isinstance(agents, dict) or not agents or len(set(agents.values())) != len(agents) or not all(
            isinstance(oid, str) and str(UUID(oid)) == oid and isinstance(agent, str)
            and _IDENTIFIER.fullmatch(agent) for oid, agent in agents.items()):
        raise ValueError(AGENTS_ENV + " needs canonical relay oids, each mapped to a distinct agent_id")
    return agents


def _id(prefix, *parts):
    """``tr-``/``tc-`` + 40 hex: 43 characters, inside ``models.ID_PATTERN``."""
    return prefix + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:40]


def _summary(pending):
    tool = " ".join(str(pending.command.action.tool).split())[:60]
    return "（" + tool + "）" if tool else ""


class TeamsRelay:
    def __init__(self, composition, agents, records, hitl=None):
        self.composition, self.agents, self.records, self.hitl = composition, agents, records, hitl

    def turn(self, relay_oid, turn: RelayTurn) -> RelayReply:
        agent_id = self.agents[relay_oid]
        scope = resolve_agent_scope(self.composition.runtime.store, self.composition.config, agent_id)
        if turn.user_oid is None or turn.user_oid != scope.human_oid:
            return RelayReply(state="refused", text=TEXT["refused"])
        # The accepted trust edge: the allowlisted relay vouches for the platform oid, which equals the Team owner.
        owner = BehaviorPrincipal(str(scope.tenant_id), scope.human_oid, scope.team_id, "human", None,
                                  frozenset({"read", "submit"}))
        gateway = NativeTurnGateway(self.composition.for_agent(agent_id), enabled=configured_native_turns())
        chat = Chat(self.records, scope, agent_id, _id("tc-", relay_oid, turn.chat_id))
        rid = _id(RELAY_PREFIX, relay_oid, turn.chat_id, turn.activity_id)
        try:
            original, _ = gateway.read(owner, NativeRead(request_id=rid))
        except Problem as error:
            # NATIVE_SESSION_CHANGED: the selected version has no session yet, so nothing was read. The submit of the
            # same deterministic RID answers it (503 not_dispatched: the relay polls and sends it again).
            if error.body["error"]["code"] not in {"NATIVE_RID_MISSING", "NATIVE_SESSION_CHANGED"}:
                raise
            original = self._submit(gateway, owner, rid, chat.conversation_id(), turn.text)
            if original is None:
                return RelayReply(state="running", text=TEXT["running"])
        else:
            if original.command.content != turn.text:
                raise Problem(409, "NATIVE_RID_CONFLICT", "the Teams message differs from its original turn")
            if original.status == "accepted":
                # A Responses POST that the first attempt could not confirm is sent again with the same RID.
                original, _ = reconcile(gateway, owner, original.command)
        if original.command.conversation_id is None and original.status != "busy_refused":
            # Also repairs a crash between admission and this write: the receipt keeps the CID it opened.
            chat.remember(original.conversation_id)
        return self._reply(gateway, owner, original)

    @staticmethod
    def _submit(gateway, owner, rid, conversation_id, text):
        try:
            command = InstructionSubmit(request_id=rid, content=text, conversation_id=conversation_id)
        except ValidationError:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "the Teams message is not one bounded text turn") from None
        try:
            original, _ = gateway.submit(owner, command, state_profile=NATIVE_RESPONSES_PROFILE)
        except _BeforeAdmission as error:
            # Nothing was admitted; the relay's next poll sends the same RID and body again.
            if error.status >= 500 or error.server_following:
                return None
            raise
        except Problem as error:
            # A concurrent first send of this message was admitted first, maybe already with this chat's new CID.
            if error.body["error"]["code"] != "NATIVE_RID_CONFLICT":
                raise
            original, _ = gateway.read(owner, NativeRead(request_id=rid))
            if original.command.content != text:
                raise
        return original

    def decision(self, relay_oid, command: RelayDecision) -> RelayReply:
        """Approve or deny one approval card by its code; never reaches the model, never changes rules."""
        agent_id = self.agents[relay_oid]
        scope = resolve_agent_scope(self.composition.runtime.store, self.composition.config, agent_id)
        if command.user_oid is None or command.user_oid != scope.human_oid:
            return RelayReply(state="refused", text=TEXT["refused"])
        approver = BehaviorPrincipal(str(scope.tenant_id), scope.human_oid, scope.team_id, "human", None,
                                     frozenset({"read", "approve"}))
        chat = Chat(self.records, scope, agent_id, _id("tc-", relay_oid, command.chat_id))
        conversation_id = chat.conversation_id()
        if conversation_id is None or self.hitl is None:
            return RelayReply(state="failed", text=TEXT["no_chat"])
        result = self._decide(approver, agent_id, conversation_id, command)
        if isinstance(result, RelayReply):
            return result
        owner = BehaviorPrincipal(str(scope.tenant_id), scope.human_oid, scope.team_id, "human", None,
                                  frozenset({"read", "submit"}))
        gateway = NativeTurnGateway(self.composition.for_agent(agent_id), enabled=configured_native_turns())
        original, _ = gateway.read(owner, NativeRead(request_id=result.request_id))
        reply = self._reply(gateway, owner, original)
        if command.choice == "deny" and reply.state == "running":
            return RelayReply(state="done", text=TEXT["denied"])
        return reply

    def _decide(self, approver, agent_id, conversation_id, command):
        """The original turn once this choice is recorded on the one matching card, else a fixed refusal reply."""
        found = []
        for pid in find_approval_ids(self.hitl, approver, command.code):
            readback = self.hitl.read(approver, pid)
            call = readback.approval.call
            if (call.agent_id, call.conversation_id) == (agent_id, conversation_id):
                found.append(readback)
        if not found:
            return RelayReply(state="failed", text=TEXT["no_card"])
        if len(found) > 1:
            return RelayReply(state="failed", text=TEXT["duplicate"])
        readback = found[0]
        if readback.pending.command.action.category == "routine":
            # An allow on a routine card also writes its daily definition (the workbench route does that); not here.
            return RelayReply(state="failed", text=TEXT["approval_workbench"])
        for attempt in range(2):
            outcome = self._settled(readback, command.choice)
            if outcome is not None:
                return outcome
            rules = self.hitl.rules(approver)
            pid = readback.approval.pending_id
            lost = False
            try:
                # One decision ID per Teams message and rules version: polls repeat it, a rules change or a message
                # typed again makes a new one.
                self.hitl.submit(approver, NativeHitlDecision(
                    request_id=_id("td-", pid, command.chat_id, command.activity_id, command.choice,
                                   str(rules.revision), rules.etag or ""),
                    pending_id=pid, pending_digest=readback.approval.pending_digest, choice=command.choice,
                    expected_rules_revision=rules.revision, expected_rules_etag=rules.etag))
            except Problem as error:
                # A concurrent decision or a rules change: read the card again and judge its saved state.
                code = error.body["error"]["code"]
                lost = error.status == 412 and code == "BEHAVIOR_CONFLICT"
                if not (code.startswith("NATIVE_HITL_") or lost) or (attempt and not lost):
                    raise
            readback = self.hitl.read(approver, pid)
            if lost and self._settled(readback, command.choice) is None:
                # Another write won the same commit and nothing was saved; this message's ID stays read-only.
                return RelayReply(state="failed", text=TEXT["retry"])
        return self._settled(readback, command.choice) or RelayReply(state="failed", text=TEXT["handled"])

    @staticmethod
    def _settled(readback, choice):
        """None while the card still waits; its original turn when this same choice is saved; else a fixed reply.

        The saved choice decides, so a card that Hosted later closed as expired still answers its first choice."""
        saved = readback.decision.choice if readback.decision is not None else None
        if saved is not None:
            # A workbench "allow by rule" answers a Teams 「允許」 like an allow once.
            if {"allow_rule": "allow_once"}.get(saved, saved) != choice:
                return RelayReply(state="failed", text=TEXT["handled"])
            call = readback.approval.call
            return NativeHitlTurn(agent_id=call.agent_id, conversation_id=call.conversation_id,
                                  request_id=call.request_id)
        if readback.status == "pending":
            return RelayReply(state="failed", text=TEXT["expired"]) if readback.expired or readback.turn_closed else None
        if readback.status in ("expired", "interrupted"):
            return RelayReply(state="failed", text=TEXT["expired"])
        # effect_unknown or allow without a decision: a rule already let the call run.
        return RelayReply(state="failed", text=TEXT["handled"])

    def _reply(self, gateway, owner, original):
        state = {"busy_refused": "busy", "unknown": "failed", "completed": "done"}.get(original.status)
        if state == "done":
            return RelayReply(state="done", text=self._result(gateway, owner, original))
        if state is None:
            waiting = self._waiting(owner, original)
            if waiting is not None:
                text = self._approval_text(owner, original) if waiting == "needs_approval" else TEXT[waiting]
                return RelayReply(state="needs_approval", text=text)
            state = "running"
        return RelayReply(state=state, text=TEXT[state])

    @staticmethod
    def _result(gateway, owner, original):
        status, _ = gateway.read(owner, NativeTurnStatusRead(
            conversation_id=original.conversation_id, request_id=original.request_id))
        if status.text is not None:
            return status.text
        parts, part = [], 0
        while part is not None:
            page, _ = gateway.read(owner, NativeResult(
                conversation_id=original.conversation_id, request_id=original.request_id, part=part))
            parts.append(page.text)
            part = page.next_part
        text, result = "".join(parts), status.receipt.result
        raw = text.encode("utf-8")
        if len(raw) != result.text_bytes or hashlib.sha256(raw).hexdigest() != result.text_sha256:
            raise Problem(502, "NATIVE_RESULT_INCOMPLETE", "the complete original result length or hash differs")
        return text

    def _waiting(self, owner, original):
        """The TEXT key of an open approval card, question or Connect card on this turn, else None."""
        if self.hitl is None:
            return None
        query = NativeHitlPendingQuery(turn=NativeHitlTurn(
            agent_id=original.agent_id, conversation_id=original.conversation_id, request_id=original.request_id))
        reads = (("needs_approval", self.hitl.pending), ("question", self.hitl.questions),
                 ("connect", self.hitl.consents))
        return next((key for key, read in reads if read(owner, query).attention), None)

    def _approval_text(self, owner, original):
        """One command pair per open approval card; routine cards stay workbench-only."""
        query = NativeHitlPendingQuery(turn=NativeHitlTurn(
            agent_id=original.agent_id, conversation_id=original.conversation_id, request_id=original.request_id))
        lines = []
        for item in self.hitl.pending(owner, query).attention:
            pid = item.approval.pending_id
            pending = self.hitl.read(owner, pid).pending
            if pending.command.action.category == "routine":
                continue
            code = approval_code(pid)
            lines.append(f"這一步需要你批准{_summary(pending)}。允許請回覆「允許 {code}」，拒絕請回覆「拒絕 {code}」；"
                         "也可以到工作台處理。")
        return "\n".join(lines) or TEXT["approval_workbench"]


class Chat:
    """``TeamsChat``: one platform conversation -> one FA CID, created once in the Agent's Team partition."""

    def __init__(self, records, scope, agent_id, key):
        self.records, self.agent_id, self.key = records, agent_id, key
        self.partition = CHAT_PARTITION.format(tenant=scope.tenant_id, team=scope.team_id)

    def conversation_id(self):
        try:
            stored = self.records.get(self.partition, self.key).payload
        except Problem as error:
            if error.body["error"]["code"] != "NOT_FOUND":
                raise
            return None
        if (not isinstance(stored, dict) or set(stored) != {"schema", "key", "agent_id", "conversation_id"}
                or (stored["schema"], stored["key"], stored["agent_id"]) != (CHAT_SCHEMA, self.key, self.agent_id)
                or not isinstance(stored["conversation_id"], str)
                or not _IDENTIFIER.fullmatch(stored["conversation_id"])):
            raise Problem(502, "TEAMS_CHAT_INVALID", "the stored Teams chat does not match its key")
        return stored["conversation_id"]

    def remember(self, conversation_id):
        try:
            self.records.create(self.partition, self.key, {
                "schema": CHAT_SCHEMA, "key": self.key, "agent_id": self.agent_id, "conversation_id": conversation_id})
        except Problem as error:
            # The first message of this chat (or a later repair) already wrote it; the original row stays.
            if error.body["error"]["code"] != "ALREADY_EXISTS":
                raise


def install_teams_relay_routes(app, composition, *, environ=None, records=None) -> None:
    agents = teams_relay_agents(os.environ if environ is None else environ)
    if agents is None:
        return
    if records is None and getattr(app.state, "team_data", None) is not None:
        records = app.state.team_data.records
    if records is None or composition.runtime.work_policy is None:
        raise ValueError(ENABLED_ENV + " requires native turns and the Team data Table")
    service = TeamsRelay(composition, agents, records, hitl=getattr(app.state, "native_hitl", None))
    tokens = composition.verifier.tokens

    async def relay_identity(request):
        if len(request.headers.getlist("authorization")) != 1:
            raise Problem(401, "BEHAVIOR_UNAUTHENTICATED", "one authorization header is required")
        claims = await run_in_threadpool(tokens.verified_claims, request)
        relay_oid = claims["oid"]
        if relay_oid not in agents or "scp" in claims or claims.get("idtyp", "app") != "app":
            log.warning("FA_TEAMS_RELAY_DENIED")
            raise Problem(403, "TEAMS_RELAY_DENIED", "an allowlisted app-only relay identity is required")
        return relay_oid

    @app.post(PATH)
    async def relay_turn(request: Request):
        relay_oid = await relay_identity(request)
        turn = await _body(request, RelayTurn)
        reply = await run_in_threadpool(service.turn, relay_oid, turn)
        log.warning("FA_TEAMS_RELAY agent_id=%s state=%s", agents[relay_oid], reply.state)
        return _response(reply)

    @app.post(DECISIONS_PATH)
    async def relay_decision(request: Request):
        relay_oid = await relay_identity(request)
        command = await _body(request, RelayDecision)
        reply = await run_in_threadpool(service.decision, relay_oid, command)
        log.warning("FA_TEAMS_RELAY_DECISION agent_id=%s choice=%s state=%s", agents[relay_oid], command.choice, reply.state)
        return _response(reply)

