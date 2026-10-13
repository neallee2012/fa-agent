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
    NativeHitlRules, Revision, NativeHitlAuthorize, NativeHitlAuthorization, NativeHitlEnd,
    NativeHitlTurn, NativeHitlPendingQuery, NativeHitlPendingPage, hitl_key, pending_id,
    NativeHitlAnswer, NativeHitlQuestionCreate, NativeHitlQuestionPage, NativeHitlQuestionPending,
    NativeHitlQuestionReadback, NativeHitlQuestionReference, NativeQuestionRequiredItem, question_id,
    ConnectorConsentCreate, ConnectorConsentDecision, ConnectorConsentPage, ConnectorConsentPending,
    ConnectorConsentReadback, ConnectorConsentReference, NativeConnectRequiredItem, consent_id,
)
from .source_materials import digest
from .native_hitl_material import HitlMaterialPart, HitlPackedRecord, material_writes, pending_summary, unpack
from .native_routine_wire import routine_proposal_change

_ID = TypeAdapter(Digest)
_COMMAND = TypeAdapter(NativeHitlCommand)
_APPROVE = (NativeHitlRuleChange, NativeHitlDecision)
# A question's answer and a Connect card's click are the Team owner's ordinary reply ("submit"), not Behavior.Approve.
_OWNER = (*_APPROVE, NativeHitlAnswer, ConnectorConsentDecision)
_WAITS = (NativeHitlPendingCreate, NativeHitlQuestionCreate, ConnectorConsentCreate)


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
            if teams is None:
                valid = (principal.actor_id is None and principal.oid == self.verifier.settings.human_oid
                         and principal.team == self.verifier.settings.team)
            else:
                key = identity_key(principal.tenant_id, principal.oid)
                value = (teams.owner(principal.oid) if rows is None else
                         teams.legacy_owner if rows[key] is None and principal.oid == teams.legacy_owner.oid else
                         None if rows[key] is None else _record(rows, key, IdentityRegistration))
                valid = (principal.actor_id is None and value is not None and value.active and value.kind == "owner"
                         and (value.tenant_id, value.oid, value.team_id)
                         == (principal.tenant_id, principal.oid, principal.team))
            if not valid:
                raise Problem(403, "NATIVE_HITL_OWNER_ONLY", "the current original Team owner is required")
        elif principal.kind == "agent":
            actor = self.verifier.auth.actors.get(principal.oid)
            if actor is not None:
                valid = (actor.actor, actor.team) == (principal.actor_id, principal.team)
            elif teams is not None:
                key = identity_key(principal.tenant_id, principal.oid)
                value = teams.lookup(principal.oid) if rows is None else (
                    None if rows[key] is None else _record(rows, key, IdentityRegistration))
                valid = (value is not None and value.active and value.kind == "agent"
                         and (value.tenant_id, value.oid, value.team_id, value.agent_id)
                         == (principal.tenant_id, principal.oid, principal.team, principal.actor_id))
            else:
                valid = False
            if not valid:
                raise Problem(403, "NATIVE_HITL_AGENT_ONLY", "an active registered Agent of this Team is required")
        else:
            raise Problem(403, "NATIVE_HITL_SCOPE_DENIED", "a verified Team identity is required")

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
        if isinstance(command, NativeHitlQuestionCreate):
            qid = question_id(principal.tenant_id, principal.team, command.question.call)
            reads.extend((qid, hitl_key("answer", principal.tenant_id, principal.team, qid),
                          self._index_key(principal, self._turn(command.question.call))))
        elif isinstance(command, NativeHitlAnswer):
            reads.extend((command.question_id, hitl_key("answer", principal.tenant_id, principal.team, command.question_id)))
            row = self.store.read_record(command.question_id)
            if row is not None:
                question = self._load(row, command.question_id, NativeHitlQuestionPending)
                reads.append(self._index_key(principal, self._turn(question.command.question.call)))
        elif isinstance(command, ConnectorConsentCreate):
            cid = consent_id(principal.tenant_id, principal.team, command.consent.call)
            reads.extend((cid, hitl_key("consent-decision", principal.tenant_id, principal.team, cid),
                          self._index_key(principal, self._turn(command.consent.call))))
            if not legacy:
                reads.append(self._declined_key(principal, command.consent.call, command.consent.toolbox_name))
        elif isinstance(command, ConnectorConsentDecision):
            reads.extend((command.consent_id,
                          hitl_key("consent-decision", principal.tenant_id, principal.team, command.consent_id)))
            row = self.store.read_record(command.consent_id)
            if row is not None:
                consent = self._load(row, command.consent_id, ConnectorConsentPending)
                reads.append(self._index_key(principal, self._turn(consent.command.consent.call)))
                if not legacy:
                    reads.append(self._declined_key(
                        principal, consent.command.consent.call, consent.command.consent.toolbox_name))
        if not legacy:
            if pid is not None:
                reads.append(hitl_key("authorization", principal.tenant_id, principal.team, pid))
            if isinstance(command, NativeHitlEnd):
                reads.append(self._index_key(principal, command.turn))
            elif isinstance(command, (NativeHitlPendingCreate, NativeHitlAuthorize)):
                reads.append(self._index_key(principal, self._turn(command.action.call)))
            elif isinstance(command, (NativeHitlDecision, NativeHitlClose)):
                pending_row = self.store.read_record(pid)
                if pending_row is not None:
                    pending = self._load(pending_row, pid, NativeHitlPending)
                    reads.append(self._index_key(principal, self._turn(pending.command.action.call)))
        return ControlRequest(
            request_id=hitl_key("apply", principal.tenant_id, principal.team, key),
            request_digest=digest([principal.tenant_id, principal.team, principal.oid, command.model_dump(mode="json")]),
            read_set=tuple(reads),
        )

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
        existing = self.store.read_record(key)
        if existing is not None:
            self._original(principal, command, existing)
            return self.reconcile(principal, command)
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
            return self.reconcile(principal, command)

        def apply(rows):
            original = self._original(principal, command, rows[key])
            return self._changes(principal, original, rows)
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
        current = list(rules.rules)
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
        revision = rules.revision + 1
        scope = dict(tenant_id=principal.tenant_id, team_id=principal.team)
        snapshot = _RulesSnapshot(**scope, revision=revision, rules=tuple(current), intent_id=intent_id)
        return (
            _write(self._snapshot_key(principal, revision), snapshot, "create"),
            _write(self._head_key(principal), _RulesHead(**scope, revision=revision),
                   "create" if rules.revision == 0 else "replace"),
        ), revision, rule_id

    def _pending(self, principal, pid, rows):
        value = self._load(rows.get(pid), pid, NativeHitlPending)
        call = value.command.action.call
        if (value.tenant_id, value.team_id, value.pending_id) != (principal.tenant_id, principal.team, pid):
            raise Problem(403, "NATIVE_HITL_SCOPE_DENIED", "the pending call belongs to another Team")
        if pid != pending_id(principal.tenant_id, principal.team, call):
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the pending key does not identify its original call")
        if principal.kind == "agent":
            self._agent(principal, call)
            if principal.oid != value.agent_oid:
                raise Problem(403, "NATIVE_HITL_SCOPE_DENIED", "only the original Agent may read or close this pending call")
        return value

    def _question(self, principal, qid, rows):
        value = self._load(rows.get(qid), qid, NativeHitlQuestionPending)
        call = value.command.question.call
        if (value.tenant_id, value.team_id, value.question_id) != (principal.tenant_id, principal.team, qid):
            raise Problem(403, "NATIVE_HITL_SCOPE_DENIED", "the question belongs to another Team")
        if qid != question_id(principal.tenant_id, principal.team, call):
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the question key does not identify its original wait")
        if principal.kind == "agent":
            self._agent(principal, call)
            if principal.oid != value.agent_oid:
                raise Problem(403, "NATIVE_HITL_SCOPE_DENIED", "only the original Agent may read this question")
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
        if principal.kind == "agent":
            self._agent(principal, call)
            if principal.oid != value.agent_oid:
                raise Problem(403, "NATIVE_HITL_SCOPE_DENIED", "only the original Agent may read this Connect card")
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
        scope = dict(tenant_id=principal.tenant_id, team_id=principal.team)
        result = {"request_id": command.request_id, "kind": command.kind,
                  "purpose": "readback-only-not-execution-permission"}
        if isinstance(command, NativeHitlEnd):
            self._agent(principal, command.turn)
            key, index = self._index(principal, command.turn, rows)
            closed = index.model_copy(update={"closed": True})
            return ControlChanges(
                writes=(_write(key, closed, "create" if rows[key] is None else "replace"),),
                result={**result, "turn": command.turn.model_dump(mode="json"), "closed": True})
        if isinstance(command, NativeHitlRuleChange):
            rules = self._version(principal, command, rows)
            writes, revision, rule_id = self._rule_writes(
                principal, rules, intent.intent_id, operation=command.operation, rule_id=command.rule_id, rule=command.rule)
            return ControlChanges(writes=writes, result={**result, "rules_revision": revision, "rule_id": rule_id})
        if isinstance(command, NativeHitlPendingCreate):
            self._agent(principal, command.action.call)
            rules = self._version(principal, command, rows)
            ikey, index = self._index(principal, self._turn(command.action.call), rows)
            if index.closed:
                raise Problem(409, "NATIVE_HITL_TURN_CLOSED", "the original turn no longer has a live approval wait")
            if not set(command.review.matched_rule_ids) <= {rule.rule_id for rule in rules.rules}:
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
        if isinstance(command, NativeHitlAuthorize):
            return self._authorize_effect(principal, intent, rows, now, result)
        if isinstance(command, NativeHitlQuestionCreate):
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
        if isinstance(command, ConnectorConsentCreate):
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
        if isinstance(command, ConnectorConsentDecision):
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
        if isinstance(command, NativeHitlAnswer):
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
        pending = self._pending(principal, command.pending_id, rows)
        if command.pending_digest != pending.sha256:
            raise Problem(409, "NATIVE_HITL_ACTION_CHANGED", "the decision must bind the original complete pending action")
        skey = hitl_key("state", principal.tenant_id, principal.team, pending.pending_id)
        state = _record(rows, skey, _State)
        if (state.tenant_id, state.team_id, state.pending_id) != (
                principal.tenant_id, principal.team, pending.pending_id):
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the approval state has another scope")
        if isinstance(command, NativeHitlClose):
            self._agent(principal, pending.command.action.call)
            allowed = (state.status == "pending" and command.status in {"expired", "interrupted"}
                       or state.status == "allow" and command.status in {"expired", "interrupted", "effect_unknown"}
                       or state.status == "pending" and command.status == "effect_unknown"
                       and rows[hitl_key("authorization", principal.tenant_id, principal.team, pending.pending_id)] is not None)
            if not allowed or command.status == "expired" and now < pending.expires_at:
                raise Problem(409, "NATIVE_HITL_STATE_CONFLICT", "this observation cannot replace the original decision or outcome")
            closed = _State(**scope, pending_id=pending.pending_id, status=command.status,
                            decision_id=state.decision_id, close_id=intent.intent_id)
            return ControlChanges(writes=(_write(skey, closed, "replace"),),
                                  result={**result, "pending_id": pending.pending_id, "status": command.status})
        if state.status != "pending":
            raise Problem(409, "NATIVE_HITL_STATE_CONFLICT", "the original pending call already has a decision or observation")
        if now >= pending.expires_at:
            raise Problem(410, "NATIVE_HITL_EXPIRED", "an expired call cannot receive a new human decision")
        ikey = self._index_key(principal, self._turn(pending.command.action.call))
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

    def _authorize_effect(self, principal, intent, rows, now, result):
        command = intent.command
        principal.require(principal.team, "consume")
        self._agent(principal, command.action.call)
        rules = self._version(principal, command, rows)
        _, index = self._index(principal, self._turn(command.action.call), rows)
        if index.closed:
            raise Problem(409, "NATIVE_HITL_TURN_CLOSED", "the original turn is closed")
        matched = {rule.rule_id: rule for rule in rules.rules}
        review = command.review
        if (not set(review.matched_rule_ids) <= set(matched) or review.outcome == "deny" or review.takeover_required):
            raise Problem(403, "NATIVE_HITL_REVIEW_DENIED", "current review or human takeover refuses this action")
        ask = (review.outcome == "ask" or command.action.category == "routine"
               or any(matched[key].behavior == "ask_first" for key in review.matched_rule_ids))
        pid = pending_id(principal.tenant_id, principal.team, command.action.call)
        akey = hitl_key("authorization", principal.tenant_id, principal.team, pid)
        if rows[akey] is not None:
            raise Problem(409, "NATIVE_HITL_ALREADY_AUTHORIZED", "read the original authorization; never acquire another claim")
        grant_id, expires = None, now + timedelta(seconds=self.ttl_seconds)
        if rows[pid] is not None:
            previous = self._pending(principal, pid, rows)
            if previous.command.action != command.action or previous.command.action_digest != command.action_digest:
                raise Problem(409, "NATIVE_HITL_ACTION_CHANGED", "the original pending call cannot authorize different input")
            status = _record(rows, hitl_key("state", principal.tenant_id, principal.team, pid), _State).status
            if status not in {"pending", "allow"} or now >= previous.expires_at:
                raise Problem(403, "NATIVE_HITL_PERMISSION_REQUIRED", "a terminal original wait cannot be reopened by another rule")
            ask = ask or status == "allow"
        if ask or command.approval is not None:
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
            expires = grant.expires_at
            if now >= expires:
                raise Problem(410, "NATIVE_HITL_EXPIRED", "the original once decision has expired")
        authorization = NativeHitlAuthorization(
            authorization_id=akey, call=command.action.call, action_digest=command.action_digest,
            review=review, effect_token=command.effect_token, approval=command.approval, grant_id=grant_id,
            expires_at=expires)
        record = _Authorization(tenant_id=principal.tenant_id, team_id=principal.team,
                                authorization=authorization, intent_id=intent.intent_id)
        return ControlChanges(writes=(_write(akey, record, "create"),),
                              result={**result, "authorization": authorization.model_dump(mode="json")})

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
                row = self.store.read_record(pid)
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
                else:
                    item = pending_summary(self._pending(principal, pid, {pid: row}))
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


def validate_native_hitl_history(inventory, config):
    """Classify only this API's typed records; retained choices never resume an SDK wait."""
    from .behavior_team_registry import TeamOwnership, inventory_agent_scope, team_key

    models = {model.model_fields["kind"].default: model for model in (
        _RulesHead, _RulesSnapshot, _Intent, _State, _Decision, NativeHitlPending, NativeHitlOnceGrant,
        _TurnIndex, _Authorization, HitlMaterialPart, NativeHitlQuestionPending, _QuestionAnswer,
        ConnectorConsentPending, _ConsentDecision, _ConsentDeclined,
    )}
    values = {}
    for key, row in inventory.rows.items():
        data = row.payload["data"]
        kind = data.get("kind")
        if not isinstance(kind, str) or not kind.startswith("native_hitl_"):
            continue
        if kind == "native_hitl_packed_record":
            data = unpack(row, key, inventory.rows.get)
            kind = data.get("kind")
        if kind not in models:
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "unknown retained native approval schema")
        value = _parse(data, models[kind])
        if value.tenant_id != config.identity.tenant_id:
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "retained HITL data belongs to another tenant")
        tenant, team = value.tenant_id, value.team_id
        if team == config.identity.team_id:
            owner_oid = config.identity.human_oid
        else:
            tkey = team_key(tenant, team)
            ownership = _record(inventory.rows, tkey, TeamOwnership)
            if (ownership.tenant_id, ownership.team_id) != (tenant, team):
                raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "retained HITL data has no original Team owner")
            owner_oid = ownership.owner_oid
        if isinstance(value, _RulesHead):
            expected = hitl_key("rules", tenant, team)
            if value.revision == 0:
                raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "a stored rule head requires a published revision")
        elif isinstance(value, _RulesSnapshot):
            expected = hitl_key("rules-revision", tenant, team, str(value.revision))
        elif isinstance(value, _Intent):
            expected = hitl_key("intent", tenant, team, value.principal_oid, value.command.request_id)
            is_owner = isinstance(value.command, _OWNER)
            if (value.intent_id != expected or (value.principal_kind == "human") != is_owner
                    or is_owner and value.principal_oid != owner_oid):
                raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the retained command has no matching original principal")
        elif isinstance(value, _TurnIndex):
            expected = hitl_key("turn", tenant, team, value.turn.agent_id, value.turn.conversation_id, value.turn.request_id)
        elif isinstance(value, _Authorization):
            expected = hitl_key("authorization", tenant, team, pending_id(tenant, team, value.authorization.call))
            if value.authorization.authorization_id != expected:
                raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the retained effect authorization identity differs")
        elif isinstance(value, HitlMaterialPart):
            expected = hitl_key("material", tenant, team, value.parent_id, str(value.index))
            parent = _record(inventory.rows, value.parent_id, HitlPackedRecord)
            if ((parent.tenant_id, parent.team_id) != (tenant, team) or value.index >= len(parent.parts)
                    or parent.parts[value.index] != key):
                raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the material part has no original parent")
        elif isinstance(value, NativeHitlPending):
            expected = pending_id(tenant, team, value.command.action.call)
            scope = inventory_agent_scope(inventory, config, value.command.action.call.agent_id)
            if (str(scope.tenant_id), scope.team_id, str(scope.identity.oid)) != (tenant, team, value.agent_oid):
                raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the original pending Agent differs")
        elif isinstance(value, NativeHitlQuestionPending):
            expected = question_id(tenant, team, value.command.question.call)
            scope = inventory_agent_scope(inventory, config, value.command.question.call.agent_id)
            if (str(scope.tenant_id), scope.team_id, str(scope.identity.oid)) != (tenant, team, value.agent_oid):
                raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the original question Agent differs")
        elif isinstance(value, ConnectorConsentPending):
            expected = consent_id(tenant, team, value.command.consent.call)
            scope = inventory_agent_scope(inventory, config, value.command.consent.call.agent_id)
            if (str(scope.tenant_id), scope.team_id, str(scope.identity.oid)) != (tenant, team, value.agent_oid):
                raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the original Connect card Agent differs")
        elif isinstance(value, _ConsentDecision):
            expected = hitl_key("consent-decision", tenant, team, value.consent_id)
            if value.owner_oid != owner_oid:
                raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the original click has no matching Team owner")
        elif isinstance(value, _ConsentDeclined):
            expected = hitl_key("consent-declined", tenant, team, value.agent_id, value.conversation_id, value.toolbox_name)
            if value.owner_oid != owner_oid:
                raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the original decline has no matching Team owner")
        elif isinstance(value, _QuestionAnswer):
            expected = hitl_key("answer", tenant, team, value.question_id)
            if value.owner_oid != owner_oid:
                raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the original answer has no matching Team owner")
        elif isinstance(value, _State):
            expected = hitl_key("state", tenant, team, value.pending_id)
        elif isinstance(value, _Decision):
            intent = hitl_key("intent", tenant, team, value.owner_oid, value.command.request_id)
            expected = hitl_key("decision", tenant, team, intent)
            if value.owner_oid != owner_oid or value.decision_id != expected:
                raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the original human decision differs")
        else:
            expected = hitl_key("grant", tenant, team, value.approval.pending_id)
            if value.grant_id != expected:
                raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "the original once grant differs")
        if key != expected:
            raise Problem(409, "NATIVE_HITL_RECORD_INVALID", "retained HITL key differs from its original identity")
        values[key] = value
    return values
