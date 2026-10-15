"""Teams chat relay route (docs/teams-chat-design.md, N2 T1); installed only with ``FA_TEAMS_RELAY_ENABLED=1``.

An allowlisted relay identity forwards one Teams message and the platform's ``x-agent-user-id``. The API compares that
oid with the Agent's Team owner, then runs the owner's ordinary Responses turn: Hosted's ``rid:<RID>`` create-or-read
is the only dedup, and the one new row is ``TeamsChat`` (platform conversation -> FA CID, created once).

For a relay that sends ``X-FA-Relay-Ack: 1``, a reply it stopped waiting for is recorded in a ``TeamsPending`` row of
its own (the ``TeamsChat`` row never changes shape) and added to the owner's next final reply through that relay, also
in a new conversation; the relay acknowledges what it received and only then is the record cleared
(docs/teams-chat-design.md, 「補上先前的回覆」). A relay without the header gets exactly the replies it always got.
"""

import hashlib
import json
import logging
import os
import re
import time
from functools import partial
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Request
from pydantic import Field, ValidationError
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .behavior_api import _body, _response
from .behavior_assistant_wire import InstructionSubmit
from .behavior_auth import BehaviorPrincipal
from .behavior_native_responses_api import reconcile
from .behavior_native_turn_api import NativeTurnGateway, _BeforeAdmission, configured_native_turns
from .behavior_native_turn_wire import (
    RELAY_PREFIX, SLOT_NOTICE_KINDS, NativeEnvironmentNotice, NativeRead, NativeResult, NativeTurnStatusRead,
)
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
ACKS_PATH = "/teams-relay/v1/acks"
# The relay's promise to acknowledge carried replies. Without it nothing is recorded or added: an older relay never acks,
# so a record would be offered again with every later message.
ACK_HEADER = "x-fa-relay-ack"
CHAT_PARTITION = "teams-chat-v1-{tenant}-{team}"
CHAT_SCHEMA = "fa-teams-chat-v1"
CHAT_KEYS = frozenset({"schema", "key", "agent_id", "conversation_id"})
PENDING_SCHEMA = "fa-teams-pending-v1"
PENDING_KEYS = frozenset({"schema", "key", "agent_id", "pending"})
# Defaults, Neal can change: at most 8 earlier replies wait per owner (the oldest is dropped, it stays in the workbench);
# a reply handed to one message is not handed to another for 120 s (the relay waits 110 s, then acknowledges at once).
PENDING_MAX = 8
LEASE_SECONDS = 120
# A record not carried (or recorded) for a day is dropped from Teams; the reply stays in the workbench.
PENDING_MAX_AGE = 24 * 3600
CAS_ATTEMPTS = 4
TERMINAL = frozenset({"completed", "unknown", "busy_refused"})
# Conservative bound for one Teams reply with earlier replies added, counted as UTF-16 bytes like Teams does; the lowest
# published Teams message size is 28 KB (incoming webhooks), so this keeps a margin for the platform's envelope.
REPLY_UTF16_BYTES = 24_000
CUT_MIN_UTF16_BYTES = 400
CATCH_UP_HEADING = "先前的回覆（剛完成）："
WAITING_HEADING = "先前的訊息還在等你處理："
CATCH_UP_SEPARATOR = "——"
CATCH_UP_CUT = "……（太長，完整內容在工作台）"

TEXT = {
    "refused": "這個助理只回應它的主人。",
    "running": "這一輪還在跑，完成後結果在工作台。",
    "running_catch_up": "這一輪還在跑；完成後，回覆會附在你下一則訊息的回覆前面，也可以先到工作台看。",
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
    "earlier_failed": "（這一則沒有完成，請到工作台查看。）",
    "earlier_busy": "（當時助理在忙，這一則沒有執行；需要的話請再傳一次。）",
    "earlier_more": "還有更早的回覆，會附在你下一則訊息的回覆裡，也可以到工作台查看。",
}
# ADR-0009 §8 sentences, kept exactly as written there (three await Neal's confirmation; the workbench page holds the
# same literals). The relay forwards only ``text``, so the API adds the sentence to a final reply's text.
NOTICE_TEXT = {
    "environment_updated": "助理已更新到新版本，工作檔也一起帶過來了。",
    "environment_switched_without_files": "已換到新版環境；這次沒有搬工作檔，舊環境與檔案都保留。",
    "environment_source_retired": "已換到新的執行環境；舊環境已失效或被平台刪除，這次沒有搬工作檔，之前 VM 裡的工作檔可能已經不在了。",
    "earlier_version_unfinished": "這則訊息是較早的版本接下的，目前環境無法接續；請用新訊息再送一次。",
}
log = logging.getLogger(__name__)
_IDENTIFIER = re.compile(ID_PATTERN)


def notice_text(notice):
    """The §8 sentence Teams shows for ``notice``, or None. A compatible same-session drift
    (``platform_version_changed``) has none: ADR-0009 M4.2 shows it only in the workbench status field, never in chat
    text, so Teams neither shows nor clears it."""
    return NOTICE_TEXT.get(notice.kind)


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


class RelayAck(WireModel):
    """The relay received a final reply that carried these request IDs; their records in this chat can go."""

    chat_id: Annotated[str, Field(min_length=1, max_length=512)]
    user_oid: Annotated[str, Field(max_length=64)] | None = None
    request_ids: Annotated[tuple[Annotated[str, Field(pattern=ID_PATTERN)], ...],
                           Field(min_length=1, max_length=PENDING_MAX + 1)]


class RelayAckResult(WireModel):
    cleared: int


class RelayReply(WireModel):
    state: Literal["done", "running", "needs_approval", "busy", "refused", "failed"]
    text: str
    # Request IDs whose reply this text carries and that this chat still records; the relay acknowledges them.
    confirm: tuple[str, ...] | None = Field(default=None, exclude_if=lambda value: value is None)
    # ADR-0009 §4 end: a final owner reply carries the Agent's pending switch notice (its sentence is also the last
    # line of ``text``, which is all the relay forwards); the route clears it after this reply is written. Never the
    # drift notice: M4.2 shows that only in the workbench status field.
    environment_notice: NativeEnvironmentNotice | None = Field(default=None, exclude_if=lambda value: value is None)


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
    """``tr-``/``tc-``/``tp-`` + 40 hex: 43 characters, inside ``models.ID_PATTERN``."""
    return prefix + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:40]


def _pending(records, scope, relay_oid, agent_id, clock):
    """The owner's late replies for this relay: ``tp-`` + relay, Agent and owner, never another owner's or Team's."""
    return Pending(records, scope, agent_id, _id("tp-", relay_oid, agent_id, scope.human_oid), clock)


def _recorded(pending):
    """The request IDs this owner's relay records; an unreadable record only means a code matches by conversation."""
    try:
        return frozenset(entry["rid"] for entry in pending.entries())
    except Problem as error:
        log.warning("FA_TEAMS_RELAY_CATCH_UP_SKIPPED code=%s", error.body["error"]["code"])
        return frozenset()


def _summary(pending):
    tool = " ".join(str(pending.command.action.tool).split())[:60]
    return "（" + tool + "）" if tool else ""


class TeamsRelay:
    def __init__(self, composition, agents, records, hitl=None, clock=time.time):
        self.composition, self.agents, self.records, self.hitl = composition, agents, records, hitl
        self.clock = clock

    def turn(self, relay_oid, turn: RelayTurn, *, catch_up=False) -> RelayReply:
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
        reply = self._reply(gateway, owner, original)
        reply = self._settle(gateway, owner, _pending(self.records, scope, relay_oid, agent_id, self.clock), rid, reply,
                             original.request_id, original.status in TERMINAL, catch_up=catch_up)
        return self._noticed(gateway, agent_id, reply)

    @staticmethod
    def _noticed(gateway, agent_id, reply):
        """ADR-0009 §4 end: only a final reply (the one Teams shows) carries the pending switch notice, read from the
        binding row here, on the read path too; a running poll's text is never shown, so it carries nothing. A drift
        notice has no Teams sentence (M4.2) and stays pending for the workbench."""
        if reply.state in {"running", "refused"}:
            return reply
        notice = gateway.owner_notice(agent_id)
        sentence = None if notice is None else notice_text(notice)
        if sentence is None:
            return reply
        return reply.model_copy(update={"text": reply.text + "\n\n" + sentence, "environment_notice": notice})

    def clear_notice(self, relay_oid, notice):
        """After the final reply carrying ``notice`` was written to the relay (ADR-0009 §4 end)."""
        agent_id = self.agents[relay_oid]
        NativeTurnGateway(self.composition.for_agent(agent_id), enabled=configured_native_turns()).clear_notice(
            agent_id, notice)

    @staticmethod
    def _submit(gateway, owner, rid, conversation_id, text):
        try:
            command = InstructionSubmit(request_id=rid, content=text, conversation_id=conversation_id)
        except ValidationError:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "the Teams message is not one bounded text turn") from None
        try:
            # The first poll's reply is rarely final: the slot notice is added to the final reply instead (_noticed).
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

    def decision(self, relay_oid, command: RelayDecision, *, catch_up=False) -> RelayReply:
        """Approve or deny one approval card by its code; never reaches the model, never changes rules."""
        agent_id = self.agents[relay_oid]
        scope = resolve_agent_scope(self.composition.runtime.store, self.composition.config, agent_id)
        if command.user_oid is None or command.user_oid != scope.human_oid:
            return RelayReply(state="refused", text=TEXT["refused"])
        approver = BehaviorPrincipal(str(scope.tenant_id), scope.human_oid, scope.team_id, "human", None,
                                     frozenset({"read", "approve"}))
        chat = Chat(self.records, scope, agent_id, _id("tc-", relay_oid, command.chat_id))
        conversation_id = chat.conversation_id()
        pending = _pending(self.records, scope, relay_oid, agent_id, self.clock)
        # A code shown with an earlier message (another conversation's card, carried here) works where it is shown.
        recorded = _recorded(pending) if catch_up else frozenset()
        if self.hitl is None or (conversation_id is None and not recorded):
            return RelayReply(state="failed", text=TEXT["no_chat"])
        result = self._decide(approver, agent_id, conversation_id, command, recorded)
        owner = BehaviorPrincipal(str(scope.tenant_id), scope.human_oid, scope.team_id, "human", None,
                                  frozenset({"read", "submit"}))
        gateway = NativeTurnGateway(self.composition.for_agent(agent_id), enabled=configured_native_turns())
        key = _id(RELAY_PREFIX, relay_oid, command.chat_id, command.activity_id)
        if isinstance(result, RelayReply):
            # A fixed answer about the code (no card, duplicate, handled, expired) stays exactly that.
            return result
        original, _ = gateway.read(owner, NativeRead(request_id=result.request_id))
        reply, done = self._reply(gateway, owner, original), original.status in TERMINAL
        if command.choice == "deny" and reply.state == "running":
            reply, done = RelayReply(state="done", text=TEXT["denied"]), False
        return self._settle(gateway, owner, pending, key, reply, original.request_id, done, catch_up=catch_up,
                            first=True)

    def ack(self, relay_oid, command: RelayAck) -> RelayAckResult:
        """Clear the records of replies the relay received; IDs this owner's relay does not record are ignored."""
        agent_id = self.agents[relay_oid]
        scope = resolve_agent_scope(self.composition.runtime.store, self.composition.config, agent_id)
        if command.user_oid is None or command.user_oid != scope.human_oid:
            raise Problem(403, "TEAMS_RELAY_NOT_OWNER", "only the owner's chat records can be acknowledged")
        pending = _pending(self.records, scope, relay_oid, agent_id, self.clock)
        cleared = []

        def clear(entries):
            kept = [entry for entry in entries if entry["rid"] not in command.request_ids]
            cleared[:] = [entry["rid"] for entry in entries if entry["rid"] in command.request_ids]
            return kept

        pending.change(clear)
        return RelayAckResult(cleared=len(cleared))

    def _settle(self, gateway, owner, pending, key, reply, main_rid, main_done, *, catch_up, first=False):
        """Record a turn whose reply Teams has not got; add the earlier replies that ended to a final reply.

        ``key`` is the Teams message being answered (its ``tr-`` ID) and ``main_rid`` the turn whose state ``reply``
        shows. The relay shows a running text only when it stops waiting, so a ``running`` reply carries nothing; it
        says the reply comes with the next message. ``catch_up`` is false for a relay that never acknowledges: it gets
        ``reply`` unchanged and nothing is recorded. ``reply`` comes first (``first``, a decision's result) or when it
        asks for approval, so its codes stay at the top; otherwise the earlier replies come first, in time order.

        Extra reads are bounded without more state: a running poll reads nothing more; a final reply (once per Teams
        message, again only when its response was lost) reads at most ``PENDING_MAX`` recorded turns, skipping those
        handed to another message in the last ``LEASE_SECONDS``: one receipt read each, plus the result pages of an
        ended one or the card reads of a waiting one.
        """
        if not catch_up or reply.state == "refused":
            return reply
        now = int(self.clock())
        recorded = True
        if main_rid is not None and not main_done:
            try:
                pending.record(main_rid, key, now)
            except Problem as error:
                # The turn and its reply are unaffected; only catching up on it later may not happen.
                log.warning("FA_TEAMS_RELAY_CATCH_UP_SKIPPED code=%s", error.body["error"]["code"])
                recorded = False
        if reply.state == "running":
            # Promise the next message only when this turn is recorded; otherwise the plain text (workbench) holds.
            return reply.model_copy(update={"text": TEXT["running_catch_up"]}) if recorded else reply
        try:
            entries = pending.entries()
        except Problem as error:
            # Nothing is cleared without this read: the reply goes out alone and every record stays for later.
            log.warning("FA_TEAMS_RELAY_CATCH_UP_SKIPPED code=%s", error.body["error"]["code"])
            return reply
        main = main_rid if main_done and any(entry["rid"] == main_rid for entry in entries) else None
        first = first or reply.state == "needs_approval"
        ready, waiting = [], []
        for entry in entries:
            # Only an ended reply is ever handed to a message, so one handed elsewhere recently is not read again.
            item = (self._earlier(gateway, owner, entry["rid"])
                    if entry["rid"] != main_rid and _available(entry, key, now) else None)
            if item is not None:
                (ready if item[3] else waiting).append(item[:3])
        _, shown = _compose(ready, waiting, reply.text, first=first)
        wanted = set(shown) | ({main} if main else set())
        granted = set()

        def lease(entries):
            granted.clear()
            leased = []
            for entry in entries:
                if entry["rid"] in wanted and (entry["rid"] == main or _available(entry, key, now)):
                    granted.add(entry["rid"])
                    entry = {**entry, "to": key, "at": now}
                leased.append(entry)
            return leased

        if wanted:
            try:
                pending.change(lease)
            except Problem as error:
                # The earlier replies wait for a later message; this message's own reply still goes out.
                log.warning("FA_TEAMS_RELAY_CATCH_UP_SKIPPED code=%s", error.body["error"]["code"])
                granted.clear()
        text, shown = _compose([item for item in ready if item[0] in granted], waiting, reply.text, first=first,
                               more=len(wanted - {main}) < len(ready))
        confirm = tuple(rid for rid in (main, *shown) if rid in granted)
        if text == reply.text and not confirm:
            return reply
        return reply.model_copy(update={"text": text, **({"confirm": confirm} if confirm else {})})

    def _earlier(self, gateway, owner, rid):
        """``(rid, message, text, ended)`` for a recorded turn that ended or waits for the owner, else None.

        An ended turn's text is its reply. A waiting one's is its approval (or question / Connect) text: never in
        ``confirm``, so it stays recorded and is shown again until it is handled and its reply comes."""
        try:
            original, _ = gateway.read(owner, NativeRead(request_id=rid))
            ended = original.status in TERMINAL
            if original.status == "completed":
                body = self._result(gateway, owner, original)
            elif ended:
                body = TEXT["earlier_failed" if original.status == "unknown" else "earlier_busy"]
            else:
                waiting = self._waiting(owner, original)
                if waiting is None:
                    return None
                body = self._approval_text(owner, original) if waiting == "needs_approval" else TEXT[waiting]
        except Problem as error:
            # An unreadable earlier turn never blocks this message's reply; the next final reply tries it again.
            log.warning("FA_TEAMS_RELAY_CATCH_UP_UNREADABLE code=%s", error.body["error"]["code"])
            return None
        message = " ".join(original.command.content.split())
        return rid, message if len(message) <= 30 else message[:30] + "…", body, ended

    def _decide(self, approver, agent_id, conversation_id, command, recorded=frozenset()):
        """The original turn once this choice is recorded on the one matching card, else a fixed refusal reply.

        A card matches when it is this Agent's and belongs to this chat's conversation or to a turn this owner's relay
        records (``recorded``): its code was shown with a later message, maybe in a new conversation."""
        found = []
        for pid in find_approval_ids(self.hitl, approver, command.code):
            readback = self.hitl.read(approver, pid)
            call = readback.approval.call
            if call.agent_id == agent_id and (call.conversation_id == conversation_id or call.request_id in recorded):
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
        if original.status == "unknown" and (original.environment_notice is not None
                                             and original.environment_notice.kind == "earlier_version_unfinished"):
            # ADR-0009 §7a (e): say why and that a new message is needed, as the workbench does.
            return RelayReply(state="failed", text=NOTICE_TEXT["earlier_version_unfinished"])
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


def _available(entry, key, now):
    """A recorded reply this message may carry: already handed to it, or not handed to another one recently."""
    return entry["to"] == key or now - entry["at"] >= LEASE_SECONDS


def _size(text):
    return len(text.encode("utf-16-le"))


def _compose(items, waiting, reply, *, first=False, more=False):
    """Earlier messages still waiting for the owner, then each earlier reply under its message in recorded order (with a
    heading each), a separator and this message's reply; with ``first`` this message's reply leads instead.

    Returns the text and the request IDs of the replies it carries. Earlier replies that do not fit within
    ``REPLY_UTF16_BYTES`` wait for the next message (``more`` says some already do); one that alone is too long is cut
    with a workbench pointer. Waiting texts are short and always shown whole."""
    held = "\n".join([WAITING_HEADING, "\n\n".join("「" + message + "」\n" + body for _, message, body in waiting)]
                     ) if waiting else ""
    budget = REPLY_UTF16_BYTES - _size("\n".join((held, "", CATCH_UP_HEADING, "", TEXT["earlier_more"], "",
                                                  CATCH_UP_SEPARATOR, reply)))
    blocks, shown = [], []
    for rid, message, body in items:
        block = "「" + message + "」\n" + body
        if _size(block) + 4 > budget:
            room = budget - _size("「" + message + "」\n" + CATCH_UP_CUT) - 4
            if blocks or room < CUT_MIN_UTF16_BYTES:
                break
            cut = body.encode("utf-16-le")[:room - room % 2].decode("utf-16-le", errors="ignore")
            block = "「" + message + "」\n" + cut + CATCH_UP_CUT
        blocks.append(block)
        shown.append(rid)
        budget -= _size(block) + 4
    more = ["", TEXT["earlier_more"]] if more or len(blocks) < len(items) else []
    sections = [held] if held else []
    if blocks:
        sections.append("\n".join([CATCH_UP_HEADING, "\n\n".join(blocks), *more]))
    if not sections:
        return reply, []
    earlier = "\n\n".join(sections)
    if first:
        return "\n".join([reply, "", CATCH_UP_SEPARATOR, earlier]), shown
    return "\n".join([earlier, "", CATCH_UP_SEPARATOR, reply]), shown


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
        if (not isinstance(stored, dict) or set(stored) != CHAT_KEYS
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


class Pending:
    """``TeamsPending``: the turns whose reply the owner's Teams has not got yet, for one relay and its Agent.

    A row of its own next to ``TeamsChat`` (same Team partition), so a ``TeamsChat`` row keeps the shape every API
    version reads. Keyed by relay, Agent and owner rather than by platform conversation, so a late reply still comes
    with the next message after ``/foundry_new_preview`` started a new conversation. ``pending`` keeps recorded order:
    ``rid``, the Teams message ``to`` that last carried (or waited for) it and ``at`` when. It changes only by ETag
    compare-and-swap; once created the row stays (an empty list when nothing waits)."""

    def __init__(self, records, scope, agent_id, key, clock=time.time):
        self.records, self.agent_id, self.key, self.clock = records, agent_id, key, clock
        self.partition = CHAT_PARTITION.format(tenant=scope.tenant_id, team=scope.team_id)

    def _load(self):
        try:
            record = self.records.get(self.partition, self.key)
        except Problem as error:
            if error.body["error"]["code"] != "NOT_FOUND":
                raise
            return None, None
        stored = record.payload
        if (not isinstance(stored, dict) or set(stored) != PENDING_KEYS
                or (stored["schema"], stored["key"], stored["agent_id"]) != (PENDING_SCHEMA, self.key, self.agent_id)
                or not _valid_pending(stored["pending"])):
            raise Problem(502, "TEAMS_PENDING_INVALID", "the stored Teams pending replies do not match their key")
        # Not carried or recorded for PENDING_MAX_AGE (e.g. kept through a relay rollback): no longer 「剛完成」, and the
        # workbench has had it all along. A row written under a higher cap keeps its newest, as recording does. The
        # next change writes the shorter list.
        now = int(self.clock())
        kept = [entry for entry in stored["pending"] if now - entry["at"] < PENDING_MAX_AGE][-PENDING_MAX:]
        if len(kept) < len(stored["pending"]):
            # Repeats on every read until a change writes the shorter list, so info; record() warns once per drop.
            log.info("FA_TEAMS_RELAY_PENDING_TRIMMED count=%d", len(stored["pending"]) - len(kept))
            stored = {**stored, "pending": kept}
        return stored, record.etag

    def entries(self):
        stored, _ = self._load()
        return [] if stored is None else [dict(entry) for entry in stored["pending"]]

    def change(self, update):
        """Replace ``pending`` with ``update(pending)`` by compare-and-swap, creating the row on its first change."""
        for _ in range(CAS_ATTEMPTS):
            stored, etag = self._load()
            before = [] if stored is None else [dict(entry) for entry in stored["pending"]]
            after = update([dict(entry) for entry in before])
            if after == before:
                return after
            payload = {"schema": PENDING_SCHEMA, "key": self.key, "agent_id": self.agent_id, "pending": after}
            try:
                if stored is None:
                    self.records.create(self.partition, self.key, payload)
                else:
                    self.records.replace(self.partition, self.key, payload, etag)
            except Problem as error:
                # Another poll, message or acknowledgement wrote first: read it again and redo the change.
                if error.body["error"]["code"] not in {"ETAG_CONFLICT", "ALREADY_EXISTS"}:
                    raise
                continue
            return after
        raise Problem(503, "TEAMS_CHAT_BUSY", "the Teams chat kept changing; send the same request again")

    def record(self, rid, key, now):
        """Keep ``rid`` until a reply carrying it is acknowledged; ``key`` is the message waiting for it now."""

        def add(entries):
            if any(entry["rid"] == rid for entry in entries):
                return entries
            entries.append({"rid": rid, "to": key, "at": now})
            if len(entries) > PENDING_MAX:
                # Bounded row: the oldest waiting reply is dropped from Teams; it stays in the workbench.
                log.warning("FA_TEAMS_RELAY_PENDING_DROPPED count=%d", len(entries) - PENDING_MAX)
            return entries[-PENDING_MAX:]

        self.change(add)


def _valid_pending(entries):
    """Shape only: the cap can change between writes, so a longer list is trimmed on read rather than refused."""
    return (isinstance(entries, list)
            and len({entry.get("rid") for entry in entries if isinstance(entry, dict)}) == len(entries)
            and all(isinstance(entry, dict) and set(entry) == {"rid", "to", "at"}
                    and all(isinstance(entry[name], str) and _IDENTIFIER.fullmatch(entry[name]) for name in ("rid", "to"))
                    and type(entry["at"]) is int for entry in entries))


def _acknowledges(request):
    return request.headers.getlist(ACK_HEADER) == ["1"]


def install_teams_relay_routes(app, composition, *, environ=None, records=None, clock=time.time) -> None:
    agents = teams_relay_agents(os.environ if environ is None else environ)
    if agents is None:
        return
    if records is None and getattr(app.state, "team_data", None) is not None:
        records = app.state.team_data.records
    if records is None or composition.runtime.work_policy is None:
        raise ValueError(ENABLED_ENV + " requires native turns and the Team data Table")
    service = TeamsRelay(composition, agents, records, hitl=getattr(app.state, "native_hitl", None), clock=clock)
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
        reply = await run_in_threadpool(partial(service.turn, catch_up=_acknowledges(request)), relay_oid, turn)
        log.warning("FA_TEAMS_RELAY agent_id=%s state=%s", agents[relay_oid], reply.state)
        response = _response(reply)
        if reply.environment_notice is not None and reply.environment_notice.kind in SLOT_NOTICE_KINDS:
            # Starlette runs it only after the whole reply was sent to the relay (先送、後清).
            response.background = BackgroundTask(service.clear_notice, relay_oid, reply.environment_notice)
        return response

    @app.post(DECISIONS_PATH)
    async def relay_decision(request: Request):
        relay_oid = await relay_identity(request)
        command = await _body(request, RelayDecision)
        reply = await run_in_threadpool(partial(service.decision, catch_up=_acknowledges(request)), relay_oid, command)
        log.warning("FA_TEAMS_RELAY_DECISION agent_id=%s choice=%s state=%s", agents[relay_oid], command.choice, reply.state)
        return _response(reply)

    @app.post(ACKS_PATH)
    async def relay_ack(request: Request):
        relay_oid = await relay_identity(request)
        command = await _body(request, RelayAck)
        result = await run_in_threadpool(service.ack, relay_oid, command)
        log.warning("FA_TEAMS_RELAY_ACK agent_id=%s cleared=%d", agents[relay_oid], result.cleared)
        return _response(result)

