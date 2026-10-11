"""One zero-activity pin-v2 window successor; no old capability or budget renewal."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, Annotated

from pydantic import Field, field_validator, model_validator

from . import behavior_migration as m
from .behavior_channels_migration import (
    ChannelMigration, ChannelPlan, ChannelApproval, ChannelHistory, ChannelInstallation, ChannelRetirement,
    CatalogChunk, channel_key, code_sha256, _drained, _required_authority, _verify_channel_history, require,
)
from .behavior_channels_pin_refresh import (
    PinRefreshInstallation, PinRefreshHistory, OperatorCodeDirectory, core_code_sha256, file_bytes,
    current_source_permission,
)
from .behavior_delivery import _Model, _parse, _utc, _ConsumerPage, _material
from .behavior_runtime_rotation import _snapshot_store
from .behavior_runtime_transition import _counter_hash, _payload_hash
from .behavior_server_config import ServerConfigurationV2, ProtectionObservation, parse_document
from .behavior_operations import control_key
from .behavior_workers import WorkerReadiness, worker_key
from .behavior_store import Digest, ControlRequest, GUARD_KEY
from .source_materials import digest, SourceMaterials, AuthenticatedSourceContext
from .source_reviews import DurableHumanReviewVerifier, HumanReviewPolicy
from .memory_governance import MemoryGovernance, ReviewedClaim
from .models import Identifier


class WindowScope(_Model):
    schema_version: Literal["fa-channel-window-scope-v1"] = "fa-channel-window-scope-v1"
    window_id: Identifier
    revision: Literal[2] = 2
    predecessor_configuration_sha256: Digest
    not_before: datetime
    valid_until: datetime
    approved_scope_reference: Identifier
    approved_scope_sha256: Digest
    _times = field_validator("not_before", "valid_until")(_utc)

    @model_validator(mode="after")
    def bounded(self):
        if not timedelta(0) < self.valid_until-self.not_before <= timedelta(hours=4):
            raise ValueError("one explicit new finite window up to four hours required")
        return self


class WindowRetirement(ChannelRetirement):
    schema_version: Literal["fa-channel-window-retirement-v1"] = "fa-channel-window-retirement-v1"
    current_channel_history_sha256: Digest


class WindowWitnesses(_Model):
    sources: Annotated[str, Field(min_length=1, max_length=32768)]
    backups: Annotated[str, Field(min_length=1, max_length=32768)]
    retirement: Annotated[str, Field(min_length=1, max_length=32768)]

    def read(self):
        return {name: parse_document(getattr(self, name).encode("utf-8"),
                    WindowRetirement if name == "retirement" else ProtectionObservation)
                for name in type(self).model_fields}

    @property
    def sha256(self):
        return digest({name: hashlib.sha256(getattr(self, name).encode("utf-8")).hexdigest()
                       for name in type(self).model_fields})


@dataclass(frozen=True)
class WindowEvidenceFiles:
    sources: Path
    backups: Path
    retirement: Path

    def material(self):
        return WindowWitnesses(**{name: file_bytes(getattr(self, name), 32768).decode("utf-8")
                                  for name in WindowWitnesses.model_fields})

    def read(self):
        return self.material().read()


class WindowPreimage(_Model):
    row_id: Identifier
    sha256: Digest
    etag: str
    preimage: Annotated[str, Field(min_length=1, max_length=16384)]


class WindowPlan(ChannelPlan):
    schema_version: Literal["fa-channel-window-plan-v1"] = "fa-channel-window-plan-v1"
    window: WindowScope
    core_code_sha256: Digest
    operator_code_sha256: Digest
    witness_sha256: Digest
    replaced: Annotated[tuple[WindowPreimage, ...], Field(min_length=2, max_length=2)]
    reviewed_source_sha256: Digest
    reviewed_source_valid_until: datetime
    _review_time = field_validator("reviewed_source_valid_until")(_utc)


class WindowApproval(ChannelApproval):
    schema_version: Literal["fa-channel-window-approval-v1"] = "fa-channel-window-approval-v1"
    action: Literal["open-independent-zero-activity-window"] = "open-independent-zero-activity-window"
    window: WindowScope
    core_code_sha256: Digest
    operator_code_sha256: Digest


class WindowHistory(ChannelHistory):
    schema_version: Literal["fa-channel-window-history-v1"] = "fa-channel-window-history-v1"
    plan: WindowPlan
    approval: WindowApproval
    witnesses: WindowWitnesses


class WindowInstallation(ChannelInstallation):
    schema_version: Literal["fa-channel-window-installation-v1"] = "fa-channel-window-installation-v1"
    window_id: Identifier
    revision: Literal[2]
    not_before: datetime
    valid_until: datetime
    _times = field_validator("not_before", "valid_until")(_utc)


def compatible_window(old, new, scope):
    require(type(old) is type(new) is ServerConfigurationV2 and type(scope) is WindowScope,
            "explicit installed v2, proposed v2, and independent window scope required")
    before, after = old.model_dump(mode="json"), new.model_dump(mode="json")
    before["channels"]["valid_until"] = after["channels"]["valid_until"]
    before["channels"]["native_evidence_id"] = after["channels"]["native_evidence_id"]
    require(before == after and old.sha256 != new.sha256
            and old.channels.native_evidence_id != new.channels.native_evidence_id
            and scope.predecessor_configuration_sha256 == old.sha256
            and scope.valid_until == new.channels.valid_until
            and old.channels.valid_until <= scope.not_before < scope.valid_until,
            "only new window expiry and unique native evidence ID may differ; old dates stay immutable")


def propose_window(old, scope, native_evidence_id):
    raw = old.model_dump(mode="json")
    raw["channels"].update(valid_until=scope.valid_until.isoformat(), native_evidence_id=native_evidence_id)
    new = ServerConfigurationV2.model_validate_json(json.dumps(raw))
    compatible_window(old, new, scope)
    return new


def validate_window_evidence(old, new, evidence, scope, *, clock):
    compatible_window(old, new, scope)
    witness = evidence.material()
    values = witness.read()
    local = SimpleNamespace(old=old, new=new, evidence=SimpleNamespace(read=lambda: values), clock=clock)
    _, current, until = ChannelMigration.fresh(local)
    require(witness == evidence.material(), "window witness files changed while reading")
    return values, digest({"current": current, "original_bytes": witness.sha256}), until


def closed_window(store, inventory, old):
    ckey = channel_key("current", old.table.scope.authority_domain)
    pointer = _parse(inventory.rows[ckey].payload["data"], PinRefreshInstallation)
    history = _parse(inventory.rows[pointer.history_id].payload["data"], PinRefreshHistory)
    require(pointer.configuration_sha256 == old.sha256 and pointer.active
            and channel_key("revocation", old.sha256) not in inventory.rows
            and channel_key("requests", old.delivery.entry_id) not in inventory.rows
            and not any(row.payload["data"].get("schema_version") == "fa-teams-metrics-work-v1"
                        for row in inventory.rows.values()),
            "only original unrevoked pin-v2 with zero Teams activity may acquire this window")
    _drained(inventory)
    _required_authority(inventory, old)
    from .teams_personal import PersonalRouteEnrollment
    from .teams_delivery import TeamsRouteAccess
    require(PersonalRouteEnrollment(store, old.channels.personal, clock=lambda: history.applied_at).settings().routes[0].route_id
            == old.channels.route_id, "original signed route differs")
    access = _parse(inventory.rows[control_key("teams-route-access", old.channels.route_id)].payload["data"], TeamsRouteAccess)
    require(access.active and access.route_id == old.channels.route_id
            and access.recipient_id == old.identity.human_oid
            and access.owner_record_ids == (old.bindings[0].owner.owner_record_id,) and not access.artifacts,
            "original zero-activity route ACL was changed or revoked")
    return pointer, history


def validate_late_empty_records(store, inventory, old, pointer, history, *, clock):
    """Classify every post-pin addition; never discard an unknown row or receipt."""
    known_rows = {m.installation_record(old)[0], channel_key("current", old.table.scope.authority_domain),
                  pointer.history_id, pointer.approval_id, control_key("teams-route-access", old.channels.route_id),
                  *(name for name, _ in history.catalog)}
    known_receipts = {history.request.request_id}
    for key, _ in history.catalog:
        chunk = _parse(inventory.rows[key].payload["data"], CatalogChunk)
        for entry in chunk.entries:
            (known_rows if entry.kind == "row" else known_receipts).add(entry.row_id)
    b, policy, profile = old.bindings[0], old.policies[0].policy, old.assistant.profiles[0]
    actor = old.identity.auth().actors[b.agent_oid]
    context = AuthenticatedSourceContext(b.tenant_id, b.team_id, actor.actor, b.ingress_id, policy.allowed_purposes)
    sources = SourceMaterials(store, clock=clock)
    reviewer = DurableHumanReviewVerifier(sources, HumanReviewPolicy(policy_version=old.review_policy_version), clock=clock)
    memory = MemoryGovernance(sources, clock=clock, token_counter=len, review_verifier=reviewer)
    claim = memory.get_current(context, scope=context.scope(policy.source_visibility), subject=b.subject, claim_id=profile.claim_id)
    require(3 <= claim.reference.version <= 2**31 - 1 and claim.human_reviewed,
            "a current approved successor of the pinned claim is required")
    claim_keys, decision_keys = verified_successor_chain(inventory, old, claim, known_rows)
    known_rows.update(claim_keys | decision_keys)
    for key in claim_keys | decision_keys:
        request = _parse(inventory.rows[key].payload["data"]["request"], ControlRequest)
        known_receipts.add(request.request_id)
    for key in set(inventory.rows)-known_rows:
        row, data = inventory.rows[key], inventory.rows[key].payload["data"]
        if data.get("kind") == "delivery_consumer_page":
            page = _parse(data, _ConsumerPage)
            page_key = control_key("delivery-consumer-page", b.owner.root_budget_id, str(page.sequence))
            require(key == page_key and page.root_budget_id == b.owner.root_budget_id and not page.selected
                    and page.offset == 0 and page.active and row.payload["material"] == _material(data).model_dump(mode="json"),
                    "post-pin history supports only immutable original empty control pages")
            cursor = control_key("delivery-consumer-cursor", page.root_budget_id)
            scan = ControlRequest(request_id=control_key("delivery-consumer-scan", page.root_budget_id, str(page.sequence-1)),
                request_digest=digest([page.root_budget_id, page.sequence-1, 1, page.configuration_digest]),
                read_set=(control_key("delivery-index", page.root_budget_id), cursor,
                          control_key("delivery-quota", "root", page.root_budget_id)))
            done = ControlRequest(request_id=control_key("delivery-consumer-page-done", page.root_budget_id, str(page.sequence)),
                                  request_digest=digest(data), read_set=(cursor,))
            require(m._receipt(inventory, scan) is not None and m._receipt(inventory, done) is not None,
                    "each original empty scan must have its complete matching scan/done receipts")
            known_receipts.update((scan.request_id, done.request_id))
        elif "evidence_id" in data and key == worker_key("readiness-evidence", data["evidence_id"]):
            ready = _parse(data, WorkerReadiness)
            contract = old.workers
            require(ready.configuration_digest == digest({"contract": contract.model_dump(mode="json"), "mode": "native"})
                    and ready.control_binding == contract.control_binding and ready.executor_id == contract.executor_id
                    and ready.executor_version == contract.executor_version
                    and row.payload["material"] == _material(data).model_dump(mode="json"),
                    "post-pin worker readiness is outside the original contract")
            request = ControlRequest(
                request_id=worker_key("publish-readiness", contract.workspace_id, ready.evidence_id),
                request_digest=digest(["publish-readiness", data, ready.configuration_digest]),
                read_set=(worker_key("readiness", contract.workspace_id),))
            require(m._receipt(inventory, request) is not None, "worker evidence lacks original publication receipt")
            known_receipts.add(request.request_id)
        else:
            require(False, "unclassified post-pin row; window transition cannot hide unknown history")
        known_rows.add(key)
    require(set(inventory.rows) == known_rows and set(inventory.receipts) == known_receipts,
            "unknown post-pin receipt or row cannot be omitted from the next window")
    return claim


def verified_successor_chain(inventory, config, current, pinned_rows):
    """Read-only full producer replay; historical expiry is not a current grant."""
    from .behavior_recovery_history import _Inventory
    from .behavior_runtime_rotation_history import _SourceHistory

    claims = {}
    for key, row in inventory.rows.items():
        data = row.payload["data"]
        if {"candidate", "reference", "review", "request", "sequence"} <= data.keys():
            value = _parse(data, ReviewedClaim)
            if (value.reference.scope == current.reference.scope
                    and value.reference.subject == current.reference.subject
                    and value.reference.claim_id == current.reference.claim_id):
                require(key == value.reference.key, "retained successor key differs")
                claims[key] = value
    require(len(claims) == current.reference.version,
            "successor chain must retain every original version without jumps or omissions")
    pinned = [value for key, value in claims.items() if key in pinned_rows]
    require(pinned, "successor chain has no original pin-cutoff claim")
    anchor = max(pinned, key=lambda value: value.reference.version)
    require(anchor.reference.version == 2 and current.reference.version > anchor.reference.version,
            "only successors of the original pin-v2 claim2 are supported")
    decisions = {value.review.decision_id for value in claims.values()}
    sources = {ref.key for value in claims.values() for ref in value.candidate.supporting}
    heads = {ref.head_key for value in claims.values() for ref in value.candidate.supporting}
    selected = set(claims) | decisions | sources | heads
    # The complete inventory remains intact. Only producer model discovery is
    # scoped; the original verifier still checks receipts, write images, ordering,
    # source indexes and the complete subject head. The caller rejects all other
    # post-cutoff rows/receipts, including unpaired reviews and revocations.
    verified = _SourceHistory(_Inventory(inventory, config), config, set(inventory.rows)-selected)
    require(set(verified.claims) == set(claims), "verified claim chain differs")
    invariant = {"version", "supersedes", "effective_from", "expires_at"}
    unchanged = anchor.candidate.model_dump(mode="json", exclude=invariant)
    for value in claims.values():
        if value.reference.version > anchor.reference.version:
            require(value.candidate.model_dump(mode="json", exclude=invariant) == unchanged
                    and value.candidate.expires_at is not None,
                    "late review must preserve original content/source/subject with explicit finite dates")
    return set(claims), decisions


class ChannelWindow(ChannelMigration):
    plan_type = WindowPlan
    approval_type = WindowApproval
    history_type = WindowHistory
    installation_type = WindowInstallation
    replace_current_pointer = True

    def __init__(self, store, old, new, evidence, operator_code, scope, *, clock, allow_apply=False, scope_check=None):
        compatible_window(old, new, scope)
        require(isinstance(store, m.MigrationStore) and store._installation_fence is None
                and store._scope == old.table.scope and store._binding == old.delivery.control_binding
                and type(evidence) is WindowEvidenceFiles and type(operator_code) is OperatorCodeDirectory,
                "original isolated authority and typed trusted window inputs required")
        self.store, self.old, self.new, self.evidence = store, old, new, evidence
        self.clock, self.allow_apply, self.operator_code, self.scope = clock, allow_apply, operator_code, scope
        self.scope_check = scope_check
        self._authentication = ContextVar("window_human", default=None)

    @contextmanager
    def authenticated(self, authenticate):
        token = self._authentication.set(authenticate)
        try:
            yield
        finally:
            self._authentication.reset(token)

    def operator(self, principal):
        super().operator(principal)
        check = self._authentication.get()
        if check is not None:
            current = check()
            super().operator(current)
            require(current == principal, "window human changed at final persistent gate")

    def fresh(self):
        if self.scope_check is not None:
            self.scope_check(False)
        return validate_window_evidence(self.old, self.new, self.evidence, self.scope, clock=self.clock)

    def plan(self, principal, *, transition_id):
        self.operator(principal)
        require(transition_id == self.scope.window_id, "route/window identity differs")
        values, evidence_sha, until = self.fresh()
        inventory = self.store.read_inventory()
        view = _snapshot_store(self.store, inventory, self.old)
        m.installed_origin(view, self.old)
        pointer, prior = closed_window(view, inventory, self.old)
        claim = validate_late_empty_records(view, inventory, self.old, pointer, prior, clock=self.clock)
        retired = values["retirement"]
        require(retired.current_channel_history_sha256 == pointer.history_sha256
                and retired.old_code_sha256 == prior.plan.core_code_sha256
                and self.old.channels.valid_until <= _utc(self.clock()),
                "retirement must identify current v2 history/core and its expired original window")
        source_sha, source_until = current_source_permission(self.old, view, clock=self.clock,
                                                            rule_until=self.new.channels.valid_until)
        require(claim.candidate.effective_from <= self.scope.not_before
                and self.scope.valid_until <= source_until, "new window must fit the independent claim3/source validity")
        ckey = channel_key("current", self.old.table.scope.authority_domain)
        replaced_keys = (m.installation_record(self.old)[0], ckey)
        replaced = tuple(WindowPreimage(row_id=key, sha256=_payload_hash(inventory.rows[key].payload),
            etag=inventory.rows[key].etag,
            preimage=json.dumps(inventory.rows[key].payload, ensure_ascii=False, separators=(",", ":")))
            for key in replaced_keys)
        excluded = {GUARD_KEY, *("behavior:v1:row:"+key for key in replaced_keys)}
        return WindowPlan(
            transition_id=transition_id, old_configuration_sha256=self.old.sha256, new_configuration_sha256=self.new.sha256,
            control_binding=self.store._binding, code_sha256=code_sha256(),
            inventory_sha256=digest(dict(inventory.entity_sha256)),
            preserved_sha256=digest({key: value for key, value in inventory.entity_sha256.items() if key not in excluded}),
            counter_sha256=_counter_hash(inventory.rows), evidence_sha256=evidence_sha,
            guard=m.GuardAnchor(binding=self.store._binding, revision=inventory.guard.payload["revision"], etag=inventory.guard.etag),
            valid_until=min(until, source_until), window=self.scope, core_code_sha256=core_code_sha256(),
            operator_code_sha256=self.operator_code.sha256(), witness_sha256=self.evidence.material().sha256,
            replaced=replaced, reviewed_source_sha256=source_sha, reviewed_source_valid_until=source_until,
        )

    def authorize(self, principal, plan, *, decision_id, expires_at):
        require(type(plan) is WindowPlan and plan.window == self.scope, "exact window plan required")
        approved = super().authorize(principal, plan, decision_id=decision_id, expires_at=expires_at)
        return WindowApproval(**approved.model_dump(exclude={"schema_version", "action"}), window=self.scope,
                              core_code_sha256=plan.core_code_sha256, operator_code_sha256=plan.operator_code_sha256)

    def authorized(self, principal, plan, approval, *, historical=False):
        require(type(plan) is WindowPlan and type(approval) is WindowApproval
                and plan.window == approval.window == self.scope
                and plan.core_code_sha256 == approval.core_code_sha256 == core_code_sha256()
                and plan.operator_code_sha256 == approval.operator_code_sha256 == self.operator_code.sha256()
                and plan.witness_sha256 == self.evidence.material().sha256,
                "window version/scope/code/raw evidence changed")
        if self.scope_check is not None:
            self.scope_check(historical)
        if not historical:
            current = current_source_permission(self.old, self.store, clock=self.clock,
                                                 rule_until=self.new.channels.valid_until)
            require(current == (plan.reviewed_source_sha256, plan.reviewed_source_valid_until),
                    "window source/review/owner changed")
        super().authorized(principal, plan, approval, historical=historical)

    def request(self, plan, approval):
        request = super().request(plan, approval)
        return ControlRequest(**request.model_dump(exclude={"read_set"}),
                              read_set=(*request.read_set, channel_key("current", self.old.table.scope.authority_domain)))

    def installation_fields(self, plan):
        return dict(window_id=plan.window.window_id, revision=plan.window.revision,
                    not_before=plan.window.not_before, valid_until=plan.window.valid_until)

    def history_fields(self, plan):
        witnesses = self.evidence.material()
        require(witnesses.sha256 == plan.witness_sha256, "window witnesses changed after approval")
        return {"witnesses": witnesses}


def validate_window_history(old, new, history, pointer):
    require(type(history) is WindowHistory and type(pointer) is WindowInstallation, "explicit new window history required")
    plan, approval = history.plan, history.approval
    compatible_window(old, new, plan.window)
    require(approval.window == plan.window and pointer.window_id == plan.window.window_id
            and pointer.revision == plan.window.revision
            and pointer.not_before == plan.window.not_before and pointer.valid_until == plan.window.valid_until
            and approval.core_code_sha256 == plan.core_code_sha256
            and approval.operator_code_sha256 == plan.operator_code_sha256
            and history.witnesses.sha256 == plan.witness_sha256,
            "window history/approval/validity/evidence bindings differ")
    values = history.witnesses.read()
    local = SimpleNamespace(old=old, new=new, evidence=SimpleNamespace(read=lambda: values), clock=lambda: history.applied_at)
    _, current, until = ChannelMigration.fresh(local)
    require(plan.evidence_sha256 == digest({"current": current, "original_bytes": history.witnesses.sha256})
            and plan.valid_until == min(until, plan.reviewed_source_valid_until),
            "historical window evidence/freshness differs")


def verify_window_predecessor(store, inventory, old, new, history):
    keys = (m.installation_record(old)[0], channel_key("current", old.table.scope.authority_domain))
    require(tuple(image.row_id for image in history.plan.replaced) == keys, "exact two original replaced-row images required")
    for image in history.plan.replaced:
        row = inventory.rows[image.row_id]
        require(row.etag == image.etag and _payload_hash(row.payload) == image.sha256
                and row.payload == json.loads(image.preimage), "old current pointer/install preimage differs")
    view = _snapshot_store(store, inventory, old)
    pointer, prior = closed_window(view, inventory, old)
    # This first successor accepts only the original pin-v2 pointer, before
    # recursion; a fabricated chain of window-v2 predecessors is not traversed.
    original = m.installed_origin(view, old)
    retired = history.witnesses.read()["retirement"]
    require(retired.current_channel_history_sha256 == pointer.history_sha256
            and retired.old_code_sha256 == prior.plan.core_code_sha256, "historical current-v2 retirement binding differs")
    validate_late_empty_records(view, inventory, old, pointer, prior, clock=lambda: history.applied_at)
    require(current_source_permission(old, view, clock=lambda: history.applied_at, rule_until=new.channels.valid_until)
            == (history.plan.reviewed_source_sha256, history.plan.reviewed_source_valid_until),
            "original approved current claim3/source differs")
    return original


def installed_window_origin(store, config, inventory, pointer):
    history = _parse(inventory.rows[pointer.history_id].payload["data"], WindowHistory)
    return _verify_channel_history(store, config, inventory, pointer, history, window=True)
