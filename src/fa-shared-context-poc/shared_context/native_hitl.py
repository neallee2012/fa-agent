"""API-owned native approval records on the existing CAS; no SDK wait or effect execution."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator

from .behavior_auth import BehaviorJwtVerifier, BehaviorPrincipal
from .behavior_operations import _parse, _utc, _write
from .behavior_store import BehaviorStore, ControlChanges, ControlRequest
from .behavior_team_registry import IdentityRegistration, identity_key
from .behavior_wire import Digest, Identifier, WireModel
from .errors import Problem
from .native_hitl_wire import (
    MAX_RULES, HitlStatus, NativeAttentionItem, NativeHitlApprovalReference, NativeHitlClose,
    NativeHitlCommand, NativeHitlDecision, NativeHitlOnceGrant, NativeHitlPending,
    NativeHitlPendingCreate, NativeHitlReadback, NativeHitlRule, NativeHitlRuleChange,
    NativeHitlRuleInput, NativeHitlRules, Revision, NativeHitlAuthorize, NativeHitlAuthorization, NativeHitlEnd,
    NativeHitlTurn, NativeHitlPendingQuery, NativeHitlPendingPage, hitl_key, pending_id,
    NativeHitlAnswer, NativeHitlQuestionCreate, NativeHitlQuestionPage, NativeHitlQuestionPending,
    NativeHitlQuestionReadback, NativeHitlQuestionReference, NativeQuestionRequiredItem, question_id,
    ConnectorConsentCreate, ConnectorConsentDecision, ConnectorConsentPage, ConnectorConsentPending,
    ConnectorConsentReadback, ConnectorConsentReference, NativeConnectRequiredItem, consent_id,
)
from .source_materials import digest
from .native_hitl_audit_wire import MAX_RULE_HISTORY_PAGE, NativeHitlRuleHistoryPage, NativeHitlRuleRevision
from .native_hitl_material import HitlPackedRecord, material_writes, pending_summary, unpack
from .native_routine_wire import routine_proposal_change

_ID = TypeAdapter(Digest)
_COMMAND = TypeAdapter(NativeHitlCommand)
_APPROVE = (NativeHitlRuleChange, NativeHitlDecision)
# A question's answer and a Connect card's click are the Team owner's ordinary reply ("submit"), not Behavior.Approve.
_OWNER = (*_APPROVE, NativeHitlAnswer, ConnectorConsentDecision)
_WAITS = (NativeHitlPendingCreate, NativeHitlQuestionCreate, ConnectorConsentCreate)
# The Agent's own idempotent closing writes: re-running a lost apply cannot grant, decide or restart anything.
_REAPPLY = (NativeHitlEnd, NativeHitlClose)
# The states an Agent's close may replace, and with which observation; a still-pending effect_unknown also
# needs a saved authorization.
_CLOSES = {"pending": {"expired", "interrupted"}, "allow": {"expired", "interrupted", "effect_unknown"}}


class _Scope(WireModel):
    tenant_id: Identifier
    team_id: Identifier


class _RulesHead(_Scope):
    kind: Literal["native_hitl_rules_head"] = "native_hitl_rules_head"
    revision: Revision


class _RulesSnapshot(_Scope):
    kind: Literal["native_hitl_rules_snapshot"] = "native_hitl_rules_snapshot"
    revision: Revision
    rules: Annotated[tuple[NativeHitlRule, ...], Field(max_length=MAX_RULES)]
    intent_id: Digest

    @model_validator(mode="after")
    def unique(self):
        if self.revision == 0 or len({rule.rule_id for rule in self.rules}) != len(self.rules):
            raise ValueError("a stored rule revision is positive and keeps unique stable rule IDs")
        return self


class _Intent(_Scope):
    kind: Literal["native_hitl_intent"] = "native_hitl_intent"
    intent_id: Digest
    principal_oid: Identifier
    principal_kind: Literal["human", "agent"]
    command: NativeHitlCommand
    created_at: datetime
    expires_at: datetime | None
    request: ControlRequest

    _created_utc = field_validator("created_at")(_utc)

    @model_validator(mode="after")
    def deadline(self):
        if isinstance(self.command, _WAITS):
            if self.expires_at is None or _utc(self.expires_at) <= self.created_at:
                raise ValueError("the original pending intent retains its initial deadline")
        elif self.expires_at is not None:
            raise ValueError("only a pending intent allocates a wait deadline")
        return self


class _State(_Scope):
    kind: Literal["native_hitl_state"] = "native_hitl_state"
    pending_id: Digest
    status: HitlStatus
    decision_id: Digest | None = None
    close_id: Digest | None = None


class _Decision(_Scope):
    kind: Literal["native_hitl_decision"] = "native_hitl_decision"
    decision_id: Digest
    owner_oid: Identifier
    command: NativeHitlDecision


class _TurnIndex(_Scope):
    kind: Literal["native_hitl_turn_index"] = "native_hitl_turn_index"
    turn: NativeHitlTurn
    pending_ids: tuple[Digest, ...] = ()
    closed: bool = False
    question_ids: tuple[Digest, ...] = Field(default=(), exclude_if=lambda value: not value)
    consent_ids: tuple[Digest, ...] = Field(default=(), exclude_if=lambda value: not value)


class _QuestionAnswer(_Scope):
    kind: Literal["native_hitl_question_answer"] = "native_hitl_question_answer"
    question_id: Digest
    owner_oid: Identifier
    command: NativeHitlAnswer


class _ConsentDecision(_Scope):
    kind: Literal["native_hitl_consent_decision"] = "native_hitl_consent_decision"
    consent_id: Digest
    owner_oid: Identifier
    command: ConnectorConsentDecision


class _ConsentDeclined(_Scope):
    """The owner's first 「先不用」 for one toolbox in one conversation; later cards for it are refused."""

    kind: Literal["native_hitl_consent_declined"] = "native_hitl_consent_declined"
    agent_id: Identifier
    conversation_id: Identifier
    toolbox_name: str
    consent_id: Digest
    owner_oid: Identifier
    created_at: datetime


class _Authorization(_Scope):
    kind: Literal["native_hitl_authorization"] = "native_hitl_authorization"
    authorization: NativeHitlAuthorization
    intent_id: Digest


def _record(rows, key, model):
    record = rows.get(key)
    if record is None:
        raise Problem(404, "NATIVE_HITL_NOT_FOUND", "the original approval record is unavailable")
    return _parse(record.payload["data"], model)


def _team_scope(principal):
    return dict(tenant_id=principal.tenant_id, team_id=principal.team)


def _owner_registration(principal, teams, rows):
    """The live registry outside a transaction; in one, its row, where a row-less legacy owner keeps its registration."""
    if rows is None:
        return teams.owner(principal.oid)
    key = identity_key(principal.tenant_id, principal.oid)
    if rows[key] is not None:
        return _record(rows, key, IdentityRegistration)
    return teams.legacy_owner if principal.oid == teams.legacy_owner.oid else None


def _readback_conflict(principal, pending, state, decision, grant, grant_key, reference):
    """What differs between a card's saved rows, or None; when several differ, the last comparison names it."""
    pid = pending.pending_id
    invalid = None
    if (state.tenant_id, state.team_id, state.pending_id) != (principal.tenant_id, principal.team, pid):
        invalid = "the state differs from the pending call"
    if decision is not None and (
            decision.tenant_id != principal.tenant_id or decision.team_id != principal.team
            or decision.command.pending_id != pid or decision.command.pending_digest != pending.sha256):
        invalid = "the saved decision differs from the pending call"
    if grant is not None and (grant.grant_id != grant_key or grant.approval != reference
            or grant.action_digest != pending.command.action_digest or grant.expires_at != pending.expires_at
            or grant.decision_id != state.decision_id or grant.tenant_id != principal.tenant_id
            or grant.team_id != principal.team or decision is None or decision.command.choice == "deny"):
        invalid = "the saved grant differs from the original human choice"
    return invalid


def _next_rules(principal, rules, intent_id, operation, rule_id=None, rule=None):
    """The rule list one change leaves and the stable ID it touched; the writer and the history reader share it."""
    current = list(rules)
    found = next((index for index, value in enumerate(current) if value.rule_id == rule_id), None)
    if operation == "add":
        if len(current) >= MAX_RULES:
            raise Problem(409, "NATIVE_HITL_RULE_LIMIT", "the Team already has 20 rules; allow once or edit a rule")
        rule_id = hitl_key("rule", principal.tenant_id, principal.team, intent_id)
        current.append(NativeHitlRule(rule_id=rule_id, **rule.model_dump()))
    else:
        if found is None:
            raise Problem(409, "NATIVE_HITL_RULE_MISSING", "the selected stable rule ID is not current")
        if operation == "delete":
            current.pop(found)
        else:
            current[found] = NativeHitlRule(rule_id=rule_id, **rule.model_dump())
    return tuple(current), rule_id


def _unknown():
    return Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "read the original HITL command; do not resubmit an unknown write")


class NativeHitlService:
    def __init__(self, store: BehaviorStore, verifier: BehaviorJwtVerifier, *, resolve_agent, clock,
                 ttl_seconds=600, interactive_ttl_seconds=86400):
        if not isinstance(verifier, BehaviorJwtVerifier) or any(
                type(value) is not int or not 1 <= value <= 2**31 - 1 for value in (ttl_seconds, interactive_ttl_seconds)):
            raise ValueError("native HITL requires the existing JWT verifier and positive wait TTLs")
        self.store, self.verifier = store, verifier
        self.resolve_agent, self.clock, self.ttl_seconds = resolve_agent, clock, ttl_seconds
        self.wait_seconds = {"background": ttl_seconds, "interactive": interactive_ttl_seconds}

    def _now(self):
        return _utc(self.clock())

    def _load(self, record, key, model):
        if record is None:
            raise Problem(404, "NATIVE_HITL_NOT_FOUND", "the original approval record is unavailable")
        data = unpack(record, key, self.store.read_record)
        if data.get("kind") != model.model_fields["kind"].default:
            # A question is never read as an approval (or the reverse), whichever ID a caller supplies.
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the original record has another kind")
        return _parse(data, model)

    def _authorize(self, principal, *, owner=False, write=False, rows=None, approve=None):
        if not isinstance(principal, BehaviorPrincipal) or principal.tenant_id != self.verifier.auth.tenant_id:
            raise Problem(403, "NATIVE_HITL_SCOPE_DENIED", "the verified original Team identity is required")
        approve = owner if approve is None else approve
        principal.require(principal.team, "approve" if approve and write else "submit" if write else "read")
        if owner and (principal.kind != "human" or principal.actor_id is not None):
            raise Problem(403, "NATIVE_HITL_OWNER_ONLY", "only the delegated Team owner may record rules or decisions")
        teams = self.verifier.teams
        if principal.kind == "human":
            if not self._is_current_owner(principal, teams, rows):
                raise Problem(403, "NATIVE_HITL_OWNER_ONLY", "the current original Team owner is required")
        elif principal.kind == "agent":
            if not self._is_registered_agent(principal, teams, rows):
                raise Problem(403, "NATIVE_HITL_AGENT_ONLY", "an active registered Agent of this Team is required")
        else:
            raise Problem(403, "NATIVE_HITL_SCOPE_DENIED", "a verified Team identity is required")

    def _is_current_owner(self, principal, teams, rows):
        if teams is None:
            return (principal.actor_id is None and principal.oid == self.verifier.settings.human_oid
                    and principal.team == self.verifier.settings.team)
        value = _owner_registration(principal, teams, rows)
        return (principal.actor_id is None and value is not None and value.active and value.kind == "owner"
                and (value.tenant_id, value.oid, value.team_id) == (principal.tenant_id, principal.oid, principal.team))

    def _is_registered_agent(self, principal, teams, rows):
        actor = self.verifier.auth.actors.get(principal.oid)
        if actor is not None:
            return (actor.actor, actor.team) == (principal.actor_id, principal.team)
        if teams is None:
            return False
        key = identity_key(principal.tenant_id, principal.oid)
        value = teams.lookup(principal.oid) if rows is None else (
            None if rows[key] is None else _record(rows, key, IdentityRegistration))
        return (value is not None and value.active and value.kind == "agent"
                and (value.tenant_id, value.oid, value.team_id, value.agent_id)
                == (principal.tenant_id, principal.oid, principal.team, principal.actor_id))

    def _agent(self, principal, call):
        if principal.kind != "agent":
            raise Problem(403, "NATIVE_HITL_AGENT_ONLY", "only the original Agent may propose or close its call")
        scope = self.resolve_agent(call.agent_id)
        if (str(scope.tenant_id), scope.team_id, str(scope.identity.oid), scope.identity.actor) != (
                principal.tenant_id, principal.team, principal.oid, principal.actor_id):
            raise Problem(403, "NATIVE_HITL_SCOPE_DENIED", "the call belongs to another Agent or Team")

    @staticmethod
    def _declined_key(principal, call, toolbox_name):
        return hitl_key("consent-declined", principal.tenant_id, principal.team, call.agent_id,
                        call.conversation_id, toolbox_name)

    @staticmethod
    def _head_key(principal):
        return hitl_key("rules", principal.tenant_id, principal.team)

    @staticmethod
    def _snapshot_key(principal, revision):
        return hitl_key("rules-revision", principal.tenant_id, principal.team, str(revision))

    def _rules(self, principal, rows=None):
        head_key = self._head_key(principal)
        row = self.store.read_record(head_key) if rows is None else rows[head_key]
        if row is None:
            return NativeHitlRules(team_id=principal.team, revision=0, etag=None)
        head = _parse(row.payload["data"], _RulesHead)
        key = self._snapshot_key(principal, head.revision)
        record = self.store.read_record(key) if rows is None else rows.get(key)
        snapshot = _record({key: record}, key, _RulesSnapshot)
        if (head.tenant_id, head.team_id, snapshot.tenant_id, snapshot.team_id, snapshot.revision) != (
                principal.tenant_id, principal.team, principal.tenant_id, principal.team, head.revision):
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the retained rules have another scope or revision")
        return NativeHitlRules(team_id=principal.team, revision=head.revision, etag=row.etag, rules=snapshot.rules)

    def rules(self, principal):
        def read():
            self._authorize(principal)
            return self._rules(principal)
        return self.store.consistent_read(read)

    def had_waits(self, principal, turn):
        """Read-only, for hints: whether this original turn ever recorded an approval card or a question."""
        key = self._index_key(principal, turn)
        row = self.store.read_record(key)
        if row is None:
            return False
        index = _record({key: row}, key, _TurnIndex)
        return bool(index.pending_ids or index.question_ids or index.consent_ids)

    def original_turn(self, record_id, *, question=False, consent=False):
        """Read-only, for hints: the original turn of a stored approval card, question or Connect card."""
        model = ConnectorConsentPending if consent else NativeHitlQuestionPending if question else NativeHitlPending
        value = self._load(self.store.read_record(record_id), record_id, model)
        return self._turn(value.command.consent.call if consent else
                          value.command.question.call if question else value.command.action.call)

    def category(self, pid):
        """Read-only, for the API's routine decision step: one stored card's action category, or None if absent."""
        row = self.store.read_record(pid)
        return None if row is None else self._load(row, pid, NativeHitlPending).command.action.category

    def _intent_key(self, principal, command):
        return hitl_key("intent", principal.tenant_id, principal.team, principal.oid, command.request_id)

    @staticmethod
    def _turn(call):
        return NativeHitlTurn(agent_id=call.agent_id, conversation_id=call.conversation_id, request_id=call.request_id)

    @staticmethod
    def _index_key(principal, turn):
        return hitl_key("turn", principal.tenant_id, principal.team, turn.agent_id, turn.conversation_id, turn.request_id)

    def _index(self, principal, turn, rows):
        key = self._index_key(principal, turn)
        value = (_TurnIndex(tenant_id=principal.tenant_id, team_id=principal.team, turn=turn)
                 if rows.get(key) is None else _record(rows, key, _TurnIndex))
        if (value.tenant_id, value.team_id, value.turn) != (principal.tenant_id, principal.team, turn):
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the pending reference index has another original turn")
        return key, value

    def _request(self, principal, command, *, legacy=False):
        key = self._intent_key(principal, command)
        reads = [identity_key(principal.tenant_id, principal.oid), key, self._head_key(principal)]
        revision = (command.review.rules_revision if isinstance(command, (NativeHitlPendingCreate, NativeHitlAuthorize)) else
                    command.expected_rules_revision if isinstance(command, (NativeHitlRuleChange, NativeHitlDecision))
                    else None)
        if revision:
            reads.append(self._snapshot_key(principal, revision))
        if isinstance(command, (NativeHitlPendingCreate, NativeHitlAuthorize)):
            pid = pending_id(principal.tenant_id, principal.team, command.action.call)
        elif isinstance(command, (NativeHitlDecision, NativeHitlClose)):
            pid = command.pending_id
        else:
            pid = None
        if pid is not None:
            reads.extend((pid, hitl_key("state", principal.tenant_id, principal.team, pid),
                          hitl_key("grant", principal.tenant_id, principal.team, pid)))
        reads.extend(self._wait_reads(principal, command, legacy))
        if not legacy:
            reads.extend(self._current_reads(principal, command, pid))
        return ControlRequest(
            request_id=hitl_key("apply", principal.tenant_id, principal.team, key),
            request_digest=digest([principal.tenant_id, principal.team, principal.oid, command.model_dump(mode="json")]),
            read_set=tuple(reads),
        )

    def _wait_reads(self, principal, command, legacy):
        """Keys of a question or Connect card. A saved intent keeps this order: _original compares it again."""
        if isinstance(command, NativeHitlQuestionCreate):
            qid = question_id(principal.tenant_id, principal.team, command.question.call)
            return (qid, hitl_key("answer", principal.tenant_id, principal.team, qid),
                    self._index_key(principal, self._turn(command.question.call)))
        if isinstance(command, NativeHitlAnswer):
            return self._answer_reads(principal, command)
        if isinstance(command, ConnectorConsentCreate):
            cid = consent_id(principal.tenant_id, principal.team, command.consent.call)
            reads = (cid, hitl_key("consent-decision", principal.tenant_id, principal.team, cid),
                     self._index_key(principal, self._turn(command.consent.call)))
            return reads if legacy else (
                *reads, self._declined_key(principal, command.consent.call, command.consent.toolbox_name))
        if isinstance(command, ConnectorConsentDecision):
            return self._click_reads(principal, command, legacy)
        return ()

    def _answer_reads(self, principal, command):
        reads = (command.question_id, hitl_key("answer", principal.tenant_id, principal.team, command.question_id))
        row = self.store.read_record(command.question_id)
        if row is None:
            return reads
        question = self._load(row, command.question_id, NativeHitlQuestionPending)
        return (*reads, self._index_key(principal, self._turn(question.command.question.call)))

    def _click_reads(self, principal, command, legacy):
        reads = (command.consent_id,
                 hitl_key("consent-decision", principal.tenant_id, principal.team, command.consent_id))
        row = self.store.read_record(command.consent_id)
        if row is None:
            return reads
        asked = self._load(row, command.consent_id, ConnectorConsentPending).command.consent
        reads = (*reads, self._index_key(principal, self._turn(asked.call)))
        return reads if legacy else (*reads, self._declined_key(principal, asked.call, asked.toolbox_name))

    def _current_reads(self, principal, command, pid):
        """Keys an intent saved before the claim point and the turn index existed never read (legacy)."""
        reads = () if pid is None else (hitl_key("authorization", principal.tenant_id, principal.team, pid),)
        if isinstance(command, NativeHitlEnd):
            return (*reads, self._index_key(principal, command.turn))
        if isinstance(command, (NativeHitlPendingCreate, NativeHitlAuthorize)):
            return (*reads, self._index_key(principal, self._turn(command.action.call)))
        if isinstance(command, (NativeHitlDecision, NativeHitlClose)):
            row = self.store.read_record(pid)
            if row is not None:
                pending = self._load(row, pid, NativeHitlPending)
                return (*reads, self._index_key(principal, self._turn(pending.command.action.call)))
        return reads

    def _original(self, principal, command, record):
        value = self._load(record, self._intent_key(principal, command), _Intent)
        if (value.intent_id != self._intent_key(principal, command) or value.command != command
                or (value.tenant_id, value.team_id, value.principal_oid, value.principal_kind) != (
                    principal.tenant_id, principal.team, principal.oid, principal.kind)
                or value.request not in (
                    self._request(principal, command), self._request(principal, command, legacy=True))):
            raise Problem(409, "NATIVE_HITL_COMMAND_CONFLICT", "this original command ID has different input or identity")
        return value

    def reconcile(self, principal, command):
        command = _COMMAND.validate_json(command.model_dump_json())
        owner = isinstance(command, _OWNER)

        def read():
            self._authorize(principal, owner=owner)
            record = self.store.read_record(self._intent_key(principal, command))
            if record is None:
                raise _unknown()
            return self.store.reconcile(self._original(principal, command, record).request)
        return self.store.consistent_read(read)

    def submit(self, principal, command):
        """Only the first acknowledged intent can apply its control mutation; never an external action."""
        command = _COMMAND.validate_json(command.model_dump_json())
        owner = isinstance(command, _OWNER)
        self._authorize(principal, owner=owner, write=True, approve=isinstance(command, _APPROVE))
        key = self._intent_key(principal, command)

        def apply(rows):
            original = self._original(principal, command, rows[key])
            return self._changes(principal, original, rows)

        existing = self.store.read_record(key)
        if existing is not None:
            original = self._original(principal, command, existing)
            if not isinstance(command, _REAPPLY):
                return self.reconcile(principal, command)
            # An End/Close whose apply was lost (412, crash) re-runs under its original receipt ID; a saved receipt is
            # returned as is. A decision recorded since meets the ordinary Close rule: a deny refuses it (409), an
            # allow becomes the close's status with the decision record kept, as a fresh close of an unrun allow does.
            return self.store.transact(original.request, apply)
        request = self._request(principal, command)
        now = self._now()
        intent = _Intent(
            tenant_id=principal.tenant_id, team_id=principal.team, intent_id=key, principal_oid=principal.oid,
            principal_kind=principal.kind, command=command, created_at=now, request=request,
            expires_at=now + timedelta(seconds=self.wait_seconds[command.wait]) if isinstance(command, _WAITS) else None,
        )
        reserve = ControlRequest(request_id=key, request_digest=request.request_digest, read_set=request.read_set)
        fresh = False

        def reserve_changes(rows):
            nonlocal fresh
            self._changes(principal, intent, rows)
            fresh = True
            return ControlChanges(writes=material_writes(key, intent),
                                  result={"intent_id": key, "purpose": "reconcile-original-not-execution-permission"})

        self.store.transact(reserve, reserve_changes)
        if not fresh:
            if not isinstance(command, _REAPPLY):
                return self.reconcile(principal, command)
            # Another call saved this intent first: re-run under the request it saved, never one recomputed here.
            request = self._original(principal, command, self.store.read_record(key)).request
        return self.store.transact(request, apply)

    def _version(self, principal, command, rows):
        head_key = self._head_key(principal)
        row = rows[head_key]
        revision = 0 if row is None else _parse(row.payload["data"], _RulesHead).revision
        expected = (command.review.rules_revision if isinstance(command, (NativeHitlPendingCreate, NativeHitlAuthorize))
                    else command.expected_rules_revision)
        if revision != expected or not isinstance(command, NativeHitlPendingCreate) and (
                (None if row is None else row.etag) != command.expected_rules_etag):
            raise Problem(409, "NATIVE_HITL_RULES_CHANGED", "read the current rules and reassess the original action")
        return self._rules(principal, rows)

    def _rule_writes(self, principal, rules, intent_id, *, operation, rule_id=None, rule=None):
        current, rule_id = _next_rules(principal, rules.rules, intent_id, operation, rule_id, rule)
        revision = rules.revision + 1
        scope = _team_scope(principal)
        snapshot = _RulesSnapshot(**scope, revision=revision, rules=current, intent_id=intent_id)
        return (
            _write(self._snapshot_key(principal, revision), snapshot, "create"),
            _write(self._head_key(principal), _RulesHead(**scope, revision=revision),
                   "create" if rules.revision == 0 else "replace"),
        ), revision, rule_id

    def _original_agent(self, principal, call, value, refusal):
        if principal.kind == "agent":
            self._agent(principal, call)
            if principal.oid != value.agent_oid:
                raise Problem(403, "NATIVE_HITL_SCOPE_DENIED", refusal)

    def _pending(self, principal, pid, rows):
        value = self._load(rows.get(pid), pid, NativeHitlPending)
        call = value.command.action.call
        if (value.tenant_id, value.team_id, value.pending_id) != (principal.tenant_id, principal.team, pid):
            raise Problem(403, "NATIVE_HITL_SCOPE_DENIED", "the pending call belongs to another Team")
        if pid != pending_id(principal.tenant_id, principal.team, call):
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the pending key does not identify its original call")
        self._original_agent(principal, call, value, "only the original Agent may read or close this pending call")
        return value

    def _question(self, principal, qid, rows):
        value = self._load(rows.get(qid), qid, NativeHitlQuestionPending)
        call = value.command.question.call
        if (value.tenant_id, value.team_id, value.question_id) != (principal.tenant_id, principal.team, qid):
            raise Problem(403, "NATIVE_HITL_SCOPE_DENIED", "the question belongs to another Team")
        if qid != question_id(principal.tenant_id, principal.team, call):
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the question key does not identify its original wait")
        self._original_agent(principal, call, value, "only the original Agent may read this question")
        return value

    @staticmethod
    def _question_reference(question):
        return NativeHitlQuestionReference(
            question_id=question.question_id, question_digest=question.sha256, call=question.command.question.call)

    @classmethod
    def _question_item(cls, question):
        asked = question.command.question
        return NativeQuestionRequiredItem(
            item_id=question.question_id, question=cls._question_reference(question), text=asked.question,
            choices=asked.choices, allow_freeform=asked.allow_freeform,
            created_at=question.created_at, expires_at=question.expires_at, wait=question.command.wait)

    def _answer(self, principal, question):
        akey = hitl_key("answer", principal.tenant_id, principal.team, question.question_id)
        row = self.store.read_record(akey)
        if row is None:
            return None
        value = _parse(row.payload["data"], _QuestionAnswer)
        if ((value.tenant_id, value.team_id, value.question_id) != (principal.tenant_id, principal.team, question.question_id)
                or value.command.question_digest != question.sha256):
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the saved answer differs from the original question")
        return value.command

    def read_question(self, principal, qid):
        try:
            _ID.validate_python(qid, strict=True)
        except ValueError:
            raise Problem(422, "NATIVE_HITL_INVALID_REQUEST", "one original question ID is required") from None

        def read():
            self._authorize(principal)
            question = self._question(principal, qid, {qid: self.store.read_record(qid)})
            answer = self._answer(principal, question)
            turn = self._turn(question.command.question.call)
            ikey = self._index_key(principal, turn)
            _, index = self._index(principal, turn, {ikey: self.store.read_record(ikey)})
            now = self._now()
            expired = now >= question.expires_at
            open_ = answer is None and not expired and not index.closed
            return NativeHitlQuestionReadback(
                question=question, reference=self._question_reference(question),
                status="pending" if answer is None else "answered", answer=answer,
                attention=self._question_item(question) if open_ else None,
                observed_at=now, expired=expired, turn_closed=index.closed)
        return self.store.consistent_read(read)

    def questions(self, principal, query: NativeHitlPendingQuery):
        def read():
            self._authorize(principal)
            scope = self.resolve_agent(query.turn.agent_id)
            if (str(scope.tenant_id), scope.team_id) != (principal.tenant_id, principal.team):
                raise Problem(403, "NATIVE_HITL_SCOPE_DENIED", "the original turn belongs to another Team")
            if principal.kind == "agent":
                self._agent(principal, query.turn)
            key = self._index_key(principal, query.turn)
            _, index = self._index(principal, query.turn, {key: self.store.read_record(key)})
            end = query.after + query.limit
            references, attention = [], []
            for qid in index.question_ids[query.after:end]:
                question = self._question(principal, qid, {qid: self.store.read_record(qid)})
                if self._turn(question.command.question.call) != query.turn:
                    raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the question index names another original turn")
                references.append(self._question_reference(question))
                if not index.closed and self._now() < question.expires_at and self._answer(principal, question) is None:
                    attention.append(self._question_item(question))
            return NativeHitlQuestionPage(
                turn=query.turn, references=tuple(references), attention=tuple(attention), closed=index.closed,
                next_after=end if end < len(index.question_ids) else None)
        return self.store.consistent_read(read)

    def _consent(self, principal, cid, rows):
        value = self._load(rows.get(cid), cid, ConnectorConsentPending)
        call = value.command.consent.call
        if (value.tenant_id, value.team_id, value.consent_id) != (principal.tenant_id, principal.team, cid):
            raise Problem(403, "NATIVE_HITL_SCOPE_DENIED", "the Connect card belongs to another Team")
        if cid != consent_id(principal.tenant_id, principal.team, call):
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the Connect card key does not identify its original wait")
        self._original_agent(principal, call, value, "only the original Agent may read this Connect card")
        return value

    @staticmethod
    def _consent_reference(consent):
        return ConnectorConsentReference(
            consent_id=consent.consent_id, consent_digest=consent.sha256, call=consent.command.consent.call)

    @classmethod
    def _consent_item(cls, consent):
        asked = consent.command.consent
        return NativeConnectRequiredItem(
            item_id=consent.consent_id, consent=cls._consent_reference(consent), need=asked.need,
            connector_id=asked.connector_id, server_label=asked.server_label, step=asked.step,
            created_at=consent.created_at, expires_at=consent.expires_at, wait=consent.command.wait)

    def _consent_decision(self, principal, consent):
        row = self.store.read_record(hitl_key("consent-decision", principal.tenant_id, principal.team, consent.consent_id))
        if row is None:
            return None
        value = _parse(row.payload["data"], _ConsentDecision)
        if ((value.tenant_id, value.team_id, value.consent_id) != (principal.tenant_id, principal.team, consent.consent_id)
                or value.command.consent_digest != consent.sha256):
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the saved click differs from the original Connect card")
        return value.command

    def read_consent(self, principal, cid):
        """The private readback: the only response that carries the consent links (owner or the original Agent)."""
        try:
            _ID.validate_python(cid, strict=True)
        except ValueError:
            raise Problem(422, "NATIVE_HITL_INVALID_REQUEST", "one original Connect card ID is required") from None

        def read():
            self._authorize(principal)
            consent = self._consent(principal, cid, {cid: self.store.read_record(cid)})
            decision = self._consent_decision(principal, consent)
            turn = self._turn(consent.command.consent.call)
            ikey = self._index_key(principal, turn)
            _, index = self._index(principal, turn, {ikey: self.store.read_record(ikey)})
            now = self._now()
            expired = now >= consent.expires_at
            open_ = decision is None and not expired and not index.closed
            return ConnectorConsentReadback(
                consent=consent, reference=self._consent_reference(consent),
                status="pending" if decision is None else decision.choice, decision=decision,
                attention=self._consent_item(consent) if open_ else None,
                observed_at=now, expired=expired, turn_closed=index.closed)
        return self.store.consistent_read(read)

    def consents(self, principal, query: NativeHitlPendingQuery):
        def read():
            self._authorize(principal)
            scope = self.resolve_agent(query.turn.agent_id)
            if (str(scope.tenant_id), scope.team_id) != (principal.tenant_id, principal.team):
                raise Problem(403, "NATIVE_HITL_SCOPE_DENIED", "the original turn belongs to another Team")
            if principal.kind == "agent":
                self._agent(principal, query.turn)
            key = self._index_key(principal, query.turn)
            _, index = self._index(principal, query.turn, {key: self.store.read_record(key)})
            end = query.after + query.limit
            references, attention = [], []
            for cid in index.consent_ids[query.after:end]:
                consent = self._consent(principal, cid, {cid: self.store.read_record(cid)})
                if self._turn(consent.command.consent.call) != query.turn:
                    raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the Connect card index names another original turn")
                references.append(self._consent_reference(consent))
                if (not index.closed and self._now() < consent.expires_at
                        and self._consent_decision(principal, consent) is None):
                    attention.append(self._consent_item(consent))
            return ConnectorConsentPage(
                turn=query.turn, references=tuple(references), attention=tuple(attention), closed=index.closed,
                next_after=end if end < len(index.consent_ids) else None)
        return self.store.consistent_read(read)

    @staticmethod
    def _reference(pending):
        return NativeHitlApprovalReference(
            pending_id=pending.pending_id, pending_digest=pending.sha256, call=pending.command.action.call)

    def _changes(self, principal, intent, rows):
        command = intent.command
        self._authorize(principal, owner=isinstance(command, _OWNER), write=True, rows=rows,
                        approve=isinstance(command, _APPROVE))
        now = self._now()
        result = {"request_id": command.request_id, "kind": command.kind,
                  "purpose": "readback-only-not-execution-permission"}
        return self._CHANGES[type(command)](self, principal, intent, rows, now, result)

    def _end_changes(self, principal, intent, rows, now, result):
        command = intent.command
        self._agent(principal, command.turn)
        key, index = self._index(principal, command.turn, rows)
        closed = index.model_copy(update={"closed": True})
        return ControlChanges(
            writes=(_write(key, closed, "create" if rows[key] is None else "replace"),),
            result={**result, "turn": command.turn.model_dump(mode="json"), "closed": True})

    def _rule_changes(self, principal, intent, rows, now, result):
        command = intent.command
        rules = self._version(principal, command, rows)
        writes, revision, rule_id = self._rule_writes(
            principal, rules, intent.intent_id, operation=command.operation, rule_id=command.rule_id, rule=command.rule)
        return ControlChanges(writes=writes, result={**result, "rules_revision": revision, "rule_id": rule_id})

    def _pending_changes(self, principal, intent, rows, now, result):
        command = intent.command
        scope = _team_scope(principal)
        self._agent(principal, command.action.call)
        rules = self._version(principal, command, rows)
        ikey, index = self._index(principal, self._turn(command.action.call), rows)
        if index.closed:
            raise Problem(409, "NATIVE_HITL_TURN_CLOSED", "the original turn no longer has a live approval wait")
        current = {rule.rule_id for rule in rules.rules}
        if not set(command.review.matched_rule_ids) <= current or not command.review.rule_reasons.keys() <= current:
            raise Problem(409, "NATIVE_HITL_RULES_CHANGED", "reviewed rule IDs must exist in the current revision")
        pid = pending_id(principal.tenant_id, principal.team, command.action.call)
        if command.action.category == "routine":
            # The card shows the draft an owner's allow writes; only a valid draft that asks the owner is a card.
            if command.review.source != "default":
                raise Problem(422, "NATIVE_HITL_INVALID_REQUEST", "a routine proposal always asks the owner directly")
            try:
                routine_proposal_change(command.action, pid)
            except Problem:
                raise Problem(422, "NATIVE_HITL_INVALID_REQUEST", "the routine proposal draft is invalid") from None
        akey = hitl_key("authorization", principal.tenant_id, principal.team, pid)
        if rows[pid] is not None or rows[akey] is not None:
            raise Problem(409, "NATIVE_HITL_CALL_EXISTS", "read the original pending call; never restart its wait")
        pending = NativeHitlPending(
            **scope, pending_id=pid, agent_oid=principal.oid, command=command,
            created_at=intent.created_at, expires_at=intent.expires_at)
        if now >= pending.expires_at:
            raise Problem(410, "NATIVE_HITL_EXPIRED", "the original waiting window has expired")
        state = _State(**scope, pending_id=pid, status="pending")
        return ControlChanges(writes=(
            *material_writes(pid, pending),
            _write(hitl_key("state", principal.tenant_id, principal.team, pid), state, "create"),
            _write(ikey, index.model_copy(update={"pending_ids": (*index.pending_ids, pid)}),
                   "create" if rows[ikey] is None else "replace"),
        ), result={**result, "pending_id": pid, "pending_digest": pending.sha256, "status": "pending"})

    def _question_changes(self, principal, intent, rows, now, result):
        command = intent.command
        scope = _team_scope(principal)
        call = command.question.call
        self._agent(principal, call)
        ikey, index = self._index(principal, self._turn(call), rows)
        if index.closed:
            raise Problem(409, "NATIVE_HITL_TURN_CLOSED", "the original turn no longer has a live question wait")
        qid = question_id(principal.tenant_id, principal.team, call)
        if rows[qid] is not None:
            raise Problem(409, "NATIVE_HITL_CALL_EXISTS", "read the original question; never restart its wait")
        pending = NativeHitlQuestionPending(
            **scope, question_id=qid, agent_oid=principal.oid, command=command,
            created_at=intent.created_at, expires_at=intent.expires_at)
        if now >= pending.expires_at:
            raise Problem(410, "NATIVE_HITL_EXPIRED", "the original waiting window has expired")
        return ControlChanges(writes=(
            _write(qid, pending, "create"),
            _write(ikey, index.model_copy(update={"question_ids": (*index.question_ids, qid)}),
                   "create" if rows[ikey] is None else "replace"),
        ), result={**result, "question_id": qid, "question_digest": pending.sha256, "status": "pending"})

    def _consent_changes(self, principal, intent, rows, now, result):
        command = intent.command
        scope = _team_scope(principal)
        call = command.consent.call
        self._agent(principal, call)
        ikey, index = self._index(principal, self._turn(call), rows)
        if index.closed:
            raise Problem(409, "NATIVE_HITL_TURN_CLOSED", "the original turn no longer has a live Connect wait")
        cid = consent_id(principal.tenant_id, principal.team, call)
        if rows[cid] is not None:
            raise Problem(409, "NATIVE_HITL_CALL_EXISTS", "read the original Connect card; never restart its wait")
        if rows[self._declined_key(principal, call, command.consent.toolbox_name)] is not None:
            raise Problem(409, "NATIVE_HITL_HUMAN_DENIED", "the owner declined this connector in this conversation")
        pending = ConnectorConsentPending(
            **scope, consent_id=cid, agent_oid=principal.oid, command=command,
            created_at=intent.created_at, expires_at=intent.expires_at)
        if now >= pending.expires_at:
            raise Problem(410, "NATIVE_HITL_EXPIRED", "the original waiting window has expired")
        return ControlChanges(writes=(
            _write(cid, pending, "create"),
            _write(ikey, index.model_copy(update={"consent_ids": (*index.consent_ids, cid)}),
                   "create" if rows[ikey] is None else "replace"),
        ), result={**result, "consent_id": cid, "consent_digest": pending.sha256, "status": "pending"})

    def _click_changes(self, principal, intent, rows, now, result):
        command = intent.command
        scope = _team_scope(principal)
        consent = self._consent(principal, command.consent_id, rows)
        if command.consent_digest != consent.sha256:
            raise Problem(409, "NATIVE_HITL_ACTION_CHANGED", "the click must name the original complete Connect card")
        dkey = hitl_key("consent-decision", principal.tenant_id, principal.team, consent.consent_id)
        if rows[dkey] is not None:
            raise Problem(409, "NATIVE_HITL_STATE_CONFLICT", "the original Connect card already has its one click")
        if now >= consent.expires_at:
            raise Problem(410, "NATIVE_HITL_EXPIRED", "an expired Connect card cannot resume")
        _, index = self._index(principal, self._turn(consent.command.consent.call), rows)
        if index.closed:
            raise Problem(409, "NATIVE_HITL_TURN_CLOSED", "the original turn has ended")
        decision = _ConsentDecision(**scope, consent_id=consent.consent_id, owner_oid=principal.oid, command=command)
        writes = [_write(dkey, decision, "create")]
        if command.choice == "decline":
            asked = consent.command.consent
            mkey = self._declined_key(principal, asked.call, asked.toolbox_name)
            if rows[mkey] is None:  # an earlier decline in this conversation keeps its row
                writes.append(_write(mkey, _ConsentDeclined(
                    **scope, agent_id=asked.call.agent_id, conversation_id=asked.call.conversation_id,
                    toolbox_name=asked.toolbox_name, consent_id=consent.consent_id, owner_oid=principal.oid,
                    created_at=now), "create"))
        return ControlChanges(writes=tuple(writes), result={
            **result, "consent_id": consent.consent_id, "status": command.choice})

    def _answer_changes(self, principal, intent, rows, now, result):
        command = intent.command
        scope = _team_scope(principal)
        question = self._question(principal, command.question_id, rows)
        if command.question_digest != question.sha256:
            raise Problem(409, "NATIVE_HITL_QUESTION_CHANGED", "the answer must name the original complete question")
        akey = hitl_key("answer", principal.tenant_id, principal.team, question.question_id)
        if rows[akey] is not None:
            raise Problem(409, "NATIVE_HITL_STATE_CONFLICT", "the original question already has its one answer")
        if now >= question.expires_at:
            raise Problem(410, "NATIVE_HITL_EXPIRED", "an expired question cannot receive a new answer")
        _, index = self._index(principal, self._turn(question.command.question.call), rows)
        if index.closed:
            raise Problem(409, "NATIVE_HITL_TURN_CLOSED", "the original SDK wait has ended")
        asked = question.command.question
        if command.was_freeform and not asked.allow_freeform or not command.was_freeform and command.answer not in asked.choices:
            raise Problem(422, "NATIVE_HITL_ANSWER_INVALID", "pick one of the original choices or a permitted own reply")
        answer = _QuestionAnswer(**scope, question_id=question.question_id, owner_oid=principal.oid, command=command)
        return ControlChanges(writes=(_write(akey, answer, "create"),), result={
            **result, "question_id": question.question_id, "status": "answered"})

    def _bound_state(self, principal, command, rows):
        """(pending, state key, state) of the original card a decision or close names, in this fixed order."""
        pending = self._pending(principal, command.pending_id, rows)
        if command.pending_digest != pending.sha256:
            raise Problem(409, "NATIVE_HITL_ACTION_CHANGED", "the decision must bind the original complete pending action")
        skey = hitl_key("state", principal.tenant_id, principal.team, pending.pending_id)
        state = _record(rows, skey, _State)
        if (state.tenant_id, state.team_id, state.pending_id) != (
                principal.tenant_id, principal.team, pending.pending_id):
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the approval state has another scope")
        return pending, skey, state

    def _close_changes(self, principal, intent, rows, now, result):
        command = intent.command
        pending, skey, state = self._bound_state(principal, command, rows)
        self._agent(principal, pending.command.action.call)
        # The authorization row is read only for a still-pending card closing as effect_unknown.
        allowed = (command.status in _CLOSES.get(state.status, ())
                   or state.status == "pending" and command.status == "effect_unknown"
                   and rows[hitl_key("authorization", principal.tenant_id, principal.team, pending.pending_id)] is not None)
        if not allowed or command.status == "expired" and now < pending.expires_at:
            raise Problem(409, "NATIVE_HITL_STATE_CONFLICT", "this observation cannot replace the original decision or outcome")
        closed = _State(**_team_scope(principal), pending_id=pending.pending_id, status=command.status,
                        decision_id=state.decision_id, close_id=intent.intent_id)
        return ControlChanges(writes=(_write(skey, closed, "replace"),),
                              result={**result, "pending_id": pending.pending_id, "status": command.status})

    def _refuse_undecidable(self, principal, command, pending, rows, now):
        """Raise for a still-pending card whose wait expired, ended or was authorized, or a rule it may not get."""
        if now >= pending.expires_at:
            raise Problem(410, "NATIVE_HITL_EXPIRED", "an expired call cannot receive a new human decision")
        _, index = self._index(principal, self._turn(pending.command.action.call), rows)
        if index.closed:
            raise Problem(409, "NATIVE_HITL_TURN_CLOSED", "the original SDK wait has ended")
        if rows[hitl_key("authorization", principal.tenant_id, principal.team, pending.pending_id)] is not None:
            raise Problem(409, "NATIVE_HITL_ALREADY_AUTHORIZED", "the original call already crossed its authorization point")
        if command.choice == "allow_rule" and pending.command.review.source == "toolbox":
            # The toolbox's require_approval=always means every call is approved again; no Team rule replaces it.
            raise Problem(422, "NATIVE_HITL_INVALID_REQUEST", "a toolbox tool is approved once per call, not by a rule")
        if command.choice == "allow_rule" and pending.command.action.category == "routine":
            # Each schedule is the owner's own click on its card; no Team rule may approve future schedules.
            raise Problem(422, "NATIVE_HITL_INVALID_REQUEST", "a routine proposal is approved once per card, not by a rule")

    def _decision_changes(self, principal, intent, rows, now, result):
        command = intent.command
        scope = _team_scope(principal)
        pending, skey, state = self._bound_state(principal, command, rows)
        if state.status != "pending":
            raise Problem(409, "NATIVE_HITL_STATE_CONFLICT", "the original pending call already has a decision or observation")
        self._refuse_undecidable(principal, command, pending, rows, now)
        rules = self._version(principal, command, rows)
        revision, writes = rules.revision, ()
        if command.choice == "allow_rule":
            writes, revision, rule_id = self._rule_writes(
                principal, rules, intent.intent_id, operation="add", rule=command.rule)
            result["rule_id"] = rule_id
        decision_id = hitl_key("decision", principal.tenant_id, principal.team, intent.intent_id)
        decision = _Decision(**scope, decision_id=decision_id, owner_oid=principal.oid, command=command)
        status = "deny" if command.choice == "deny" else "allow"
        writes += (
            _write(decision_id, decision, "create"),
            _write(skey, _State(**scope, pending_id=pending.pending_id, status=status, decision_id=decision_id), "replace"),
        )
        if status == "allow":
            grant_id = hitl_key("grant", principal.tenant_id, principal.team, pending.pending_id)
            grant = NativeHitlOnceGrant(
                **scope, grant_id=grant_id, approval=self._reference(pending),
                action_digest=pending.command.action_digest, decision_id=decision_id,
                rules_revision=revision, expires_at=pending.expires_at)
            writes += (_write(grant_id, grant, "create"),)
        return ControlChanges(writes=writes, result={
            **result, "pending_id": pending.pending_id, "status": status, "rules_revision": revision,
        })

    @staticmethod
    def _review_asks(command, rules):
        """Whether a passing review still needs the owner; raises when the current review refuses the action."""
        matched = {rule.rule_id: rule for rule in rules.rules}
        review = command.review
        if not review.rule_reasons.keys() <= matched.keys():
            raise Problem(409, "NATIVE_HITL_RULES_CHANGED", "reviewed rule IDs must exist in the current revision")
        if (not set(review.matched_rule_ids) <= set(matched) or review.outcome == "deny" or review.takeover_required):
            raise Problem(403, "NATIVE_HITL_REVIEW_DENIED", "current review or human takeover refuses this action")
        return (review.outcome == "ask" or command.action.category == "routine"
                or any(matched[key].behavior == "ask_first" for key in review.matched_rule_ids))

    def _live_wait_status(self, principal, command, pid, rows, now):
        """The status of a saved card for this exact action; a terminal or expired wait cannot be claimed."""
        previous = self._pending(principal, pid, rows)
        if previous.command.action != command.action or previous.command.action_digest != command.action_digest:
            raise Problem(409, "NATIVE_HITL_ACTION_CHANGED", "the original pending call cannot authorize different input")
        status = _record(rows, hitl_key("state", principal.tenant_id, principal.team, pid), _State).status
        if status not in {"pending", "allow"} or now >= previous.expires_at:
            raise Problem(403, "NATIVE_HITL_PERMISSION_REQUIRED", "a terminal original wait cannot be reopened by another rule")
        return status

    def _once_grant(self, principal, command, pid, rows, now):
        """The (grant ID, deadline) of the owner's once decision this claim names."""
        if command.approval is None:
            raise Problem(403, "NATIVE_HITL_PERMISSION_REQUIRED", "a current original human once decision is required")
        pending = self._pending(principal, pid, rows)
        if (command.approval != self._reference(pending)
                or pending.command.action != command.action or pending.command.action_digest != command.action_digest):
            raise Problem(409, "NATIVE_HITL_ACTION_CHANGED", "approval cannot authorize different original input")
        state = _record(rows, hitl_key("state", principal.tenant_id, principal.team, pid), _State)
        if state.status != "allow":
            raise Problem(403, "NATIVE_HITL_PERMISSION_REQUIRED", "the original human did not allow this live call")
        grant_id = hitl_key("grant", principal.tenant_id, principal.team, pid)
        grant = _record(rows, grant_id, NativeHitlOnceGrant)
        if grant.approval != command.approval or grant.action_digest != command.action_digest:
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the once grant names another action")
        if now >= grant.expires_at:
            raise Problem(410, "NATIVE_HITL_EXPIRED", "the original once decision has expired")
        return grant_id, grant.expires_at

    def _authorize_effect(self, principal, intent, rows, now, result):
        command = intent.command
        principal.require(principal.team, "consume")
        self._agent(principal, command.action.call)
        rules = self._version(principal, command, rows)
        _, index = self._index(principal, self._turn(command.action.call), rows)
        if index.closed:
            raise Problem(409, "NATIVE_HITL_TURN_CLOSED", "the original turn is closed")
        ask = self._review_asks(command, rules)
        pid = pending_id(principal.tenant_id, principal.team, command.action.call)
        akey = hitl_key("authorization", principal.tenant_id, principal.team, pid)
        if rows[akey] is not None:
            raise Problem(409, "NATIVE_HITL_ALREADY_AUTHORIZED", "read the original authorization; never acquire another claim")
        grant_id, expires = None, now + timedelta(seconds=self.ttl_seconds)
        if rows[pid] is not None:
            status = self._live_wait_status(principal, command, pid, rows, now)
            ask = ask or status == "allow"
        if ask or command.approval is not None:
            grant_id, expires = self._once_grant(principal, command, pid, rows, now)
        authorization = NativeHitlAuthorization(
            authorization_id=akey, call=command.action.call, action_digest=command.action_digest,
            review=command.review, effect_token=command.effect_token, approval=command.approval, grant_id=grant_id,
            expires_at=expires)
        record = _Authorization(tenant_id=principal.tenant_id, team_id=principal.team,
                                authorization=authorization, intent_id=intent.intent_id)
        return ControlChanges(writes=(_write(akey, record, "create"),),
                              result={**result, "authorization": authorization.model_dump(mode="json")})

    # One handler per command kind, after _changes' shared identity check and its one clock read.
    _CHANGES = {
        NativeHitlEnd: _end_changes, NativeHitlRuleChange: _rule_changes, NativeHitlPendingCreate: _pending_changes,
        NativeHitlAuthorize: _authorize_effect, NativeHitlQuestionCreate: _question_changes,
        ConnectorConsentCreate: _consent_changes, ConnectorConsentDecision: _click_changes,
        NativeHitlAnswer: _answer_changes, NativeHitlClose: _close_changes, NativeHitlDecision: _decision_changes,
    }

    def read(self, principal, pid):
        try:
            _ID.validate_python(pid, strict=True)
        except ValueError:
            raise Problem(422, "NATIVE_HITL_INVALID_REQUEST", "one original approval ID is required") from None

        def read():
            self._authorize(principal)
            row = self.store.read_record(pid)
            pending = self._pending(principal, pid, {pid: row})
            state_key = hitl_key("state", principal.tenant_id, principal.team, pid)
            state = _record({state_key: self.store.read_record(state_key)}, state_key, _State)
            decision = None if state.decision_id is None else _record(
                {state.decision_id: self.store.read_record(state.decision_id)}, state.decision_id, _Decision)
            grant_key = hitl_key("grant", principal.tenant_id, principal.team, pid)
            grant_row = self.store.read_record(grant_key)
            grant = None if grant_row is None else _parse(grant_row.payload["data"], NativeHitlOnceGrant)
            reference = self._reference(pending)
            invalid = _readback_conflict(principal, pending, state, decision, grant, grant_key, reference)
            now = self._now()
            expired = now >= pending.expires_at
            ikey = self._index_key(principal, self._turn(pending.command.action.call))
            _, index = self._index(principal, self._turn(pending.command.action.call),
                                   {ikey: self.store.read_record(ikey)})
            akey = hitl_key("authorization", principal.tenant_id, principal.team, pid)
            arow = self.store.read_record(akey)
            authorization = None if arow is None else _parse(arow.payload["data"], _Authorization).authorization
            attention = None if state.status != "pending" or expired or index.closed or authorization is not None else NativeAttentionItem(
                item_id=pid, approval=reference, created_at=pending.created_at, expires_at=pending.expires_at)
            value = NativeHitlReadback(
                pending=pending, approval=reference, status=state.status,
                decision=None if decision is None else decision.command, grant=grant, attention=attention,
                observed_at=now, expired=expired, current_rules_revision=self._rules(principal).revision,
                authorization=authorization, turn_closed=index.closed)
            return value, invalid
        # An owner commit can interleave the state and grant GETs. Confirm the read-set before judging their relation.
        value, invalid = self.store.consistent_read(read)
        if invalid is not None:
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", invalid)
        return value

    def _pending_projection(self, principal, pid, row):
        """One card's attention item from its own row; a packed card's checked projection, never its material parts."""
        if row is not None and row.payload["data"].get("kind") == "native_hitl_packed_record":
            packed = _parse(row.payload["data"], HitlPackedRecord)
            item = packed.pending
            if (packed.tenant_id, packed.team_id, packed.record_id, packed.record_kind) != (
                    principal.tenant_id, principal.team, pid, "native_hitl_pending") or item is None:
                raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the pending projection belongs to another scope")
            if (item.item_id != pid or item.approval.pending_digest != packed.sha256
                    or item.approval.pending_id != pending_id(
                        principal.tenant_id, principal.team, item.approval.call)):
                raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the pending projection reference differs")
            if principal.kind == "agent":
                self._agent(principal, item.approval.call)
                if packed.agent_oid != principal.oid:
                    raise Problem(403, "NATIVE_HITL_SCOPE_DENIED", "the original Agent must read its own pending call")
            return item
        return pending_summary(self._pending(principal, pid, {pid: row}))

    def _snapshot(self, principal, revision):
        key = self._snapshot_key(principal, revision)
        row = self.store.read_record(key)
        if row is None:
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "a saved rule revision is missing")
        snapshot = _parse(row.payload["data"], _RulesSnapshot)
        if (snapshot.tenant_id, snapshot.team_id, snapshot.revision) != (principal.tenant_id, principal.team, revision):
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the saved rule revision has another scope or number")
        return snapshot

    def _revision_entry(self, principal, snapshot, previous):
        """Replays the saved intent on the previous list; any difference from the saved snapshot is invalid, not guessed."""
        invalid = Problem(409, "NATIVE_HITL_RECORD_INVALID", "the saved rule revision differs from its original change")
        row = self.store.read_record(snapshot.intent_id)
        if row is None:
            raise invalid
        intent = self._load(row, snapshot.intent_id, _Intent)
        command = intent.command
        if (intent.intent_id != snapshot.intent_id or (intent.tenant_id, intent.team_id) != (
                principal.tenant_id, principal.team) or intent.principal_kind != "human"
                or not isinstance(command, (NativeHitlRuleChange, NativeHitlDecision))
                or command.expected_rules_revision != snapshot.revision - 1):
            raise invalid
        allow_rule = isinstance(command, NativeHitlDecision)
        if allow_rule and command.choice != "allow_rule":
            raise invalid
        operation, rule_id = ("add", None) if allow_rule else (command.operation, command.rule_id)
        try:
            expected, rule_id = _next_rules(principal, previous, intent.intent_id, operation, rule_id, command.rule)
        except Problem:
            raise invalid from None
        if expected != snapshot.rules:
            raise invalid
        before = next((NativeHitlRuleInput(text=value.text, behavior=value.behavior)
                       for value in previous if value.rule_id == rule_id), None)
        source = {}
        if allow_rule:
            card = self.store.read_record(command.pending_id)
            if card is None:
                raise invalid
            source = dict(source_pending_id=command.pending_id,
                          source_call=self._pending_projection(principal, command.pending_id, card).approval.call)
        return NativeHitlRuleRevision(
            revision=snapshot.revision, intent_id=intent.intent_id, changed_at=intent.created_at,
            changed_by_oid=intent.principal_oid, change="allow_rule" if allow_rule else operation, rule_id=rule_id,
            rule=command.rule, previous_rule=before, **source)

    def rule_history(self, principal, *, before=None, limit=MAX_RULE_HISTORY_PAGE):
        """Owner-only, newest first: each saved revision with who changed what, when, and from which card."""
        if (type(limit) is not int or not 1 <= limit <= MAX_RULE_HISTORY_PAGE
                or before is not None and (type(before) is not int or before < 1)):
            raise Problem(422, "NATIVE_HITL_INVALID_REQUEST", "rule history takes a bounded page before one revision")

        def read():
            self._authorize(principal, owner=True)
            head = self._rules(principal).revision
            if before is not None and before > head + 1:
                raise Problem(422, "NATIVE_HITL_INVALID_REQUEST", "rule history starts at a saved revision")
            start = head if before is None else before - 1
            end = max(0, start - limit)
            snapshots = {revision: self._snapshot(principal, revision) for revision in range(start, max(end, 1) - 1, -1)}
            entries = tuple(self._revision_entry(
                principal, snapshots[revision], snapshots[revision - 1].rules if revision > 1 else ())
                for revision in range(start, end, -1))
            return NativeHitlRuleHistoryPage(team_id=principal.team, head_revision=head, entries=entries,
                                             next_before=end + 1 if end else None)
        return self.store.consistent_read(read)

    def pending(self, principal, query: NativeHitlPendingQuery):
        def read():
            self._authorize(principal)
            scope = self.resolve_agent(query.turn.agent_id)
            if (str(scope.tenant_id), scope.team_id) != (principal.tenant_id, principal.team):
                raise Problem(403, "NATIVE_HITL_SCOPE_DENIED", "the original turn belongs to another Team")
            if principal.kind == "agent":
                self._agent(principal, query.turn)
            key = self._index_key(principal, query.turn)
            _, index = self._index(principal, query.turn, {key: self.store.read_record(key)})
            end = query.after + query.limit
            references, attention = [], []
            for pid in index.pending_ids[query.after:end]:
                item = self._pending_projection(principal, pid, self.store.read_record(pid))
                if self._turn(item.approval.call) != query.turn:
                    raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the pending index names another original turn")
                skey = hitl_key("state", principal.tenant_id, principal.team, pid)
                state = _record({skey: self.store.read_record(skey)}, skey, _State)
                akey = hitl_key("authorization", principal.tenant_id, principal.team, pid)
                reference = item.approval
                references.append(reference)
                if (not index.closed and state.status == "pending" and self._now() < item.expires_at
                        and self.store.read_record(akey) is None):
                    attention.append(item)
            return NativeHitlPendingPage(
                turn=query.turn, references=tuple(references), attention=tuple(attention), closed=index.closed,
                next_after=end if end < len(index.pending_ids) else None)
        return self.store.consistent_read(read)
