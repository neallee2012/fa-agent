"""Explicit channel-history v2: two evidence pins, one original guarded EGT."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Literal
import hashlib
import json

from pydantic import Field, field_validator

from . import behavior_migration as m
from .behavior_channels_migration import (
    ChannelApproval, ChannelEvidenceFiles, ChannelHistory, ChannelInstallation, ChannelMigration, ChannelPlan,
    _verify_channel_history, channel_key, require,
)
from .behavior_delivery import _Model, _parse, _utc
from .behavior_operations import control_key, OwnerRecord, PolicyRecord
from .behavior_runtime import RuntimePolicy, runtime_key
from .behavior_runtime_transition import _payload_hash
from .behavior_server_config import ProtectionObservation, ServerConfiguration, ServerConfigurationV2, parse_document
from .behavior_store import ControlRequest, Digest, _Row
from .source_materials import (
    InventoryProtectionVerifier, SourceMaterials, SourceReference, AuthenticatedSourceContext, digest,
)
from .source_reviews import DurableHumanReviewVerifier, HumanReviewPolicy
from .memory_governance import MemoryGovernance
from .models import Identifier

LANES = ("sources", "backups")


def file_bytes(path, limit=512 * 1024):
    path = Path(path)
    require(not any(node.is_symlink() or getattr(node, "is_junction", lambda: False)()
                    for node in (path, *path.parents)), "code/witness path cannot use links")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, "code/witness exceeds its bound")
    return raw


def core_code_sha256():
    directory = Path(__file__).resolve().parent
    return digest({path.name: hashlib.sha256(file_bytes(path)).hexdigest()
                   for path in sorted(directory.glob("*.py"))})


@dataclass(frozen=True)
class OperatorCodeDirectory:
    directory: Path

    def sha256(self):
        paths = sorted(Path(self.directory).glob("*.py"))
        require(0 < len(paths) <= 128, "complete actual operator Python directory required")
        return digest({path.name: hashlib.sha256(file_bytes(path)).hexdigest() for path in paths})


class WitnessHashes(_Model):
    sources: Digest
    backups: Digest
    retirement: Digest
    previous_sources: Digest
    previous_backups: Digest


class PinWitnesses(_Model):
    sources: Annotated[str, Field(min_length=1, max_length=32768)]
    backups: Annotated[str, Field(min_length=1, max_length=32768)]
    retirement: Annotated[str, Field(min_length=1, max_length=32768)]
    previous_sources: Annotated[str, Field(min_length=1, max_length=32768)]
    previous_backups: Annotated[str, Field(min_length=1, max_length=32768)]

    def fingerprints(self):
        return WitnessHashes(**{name: hashlib.sha256(getattr(self, name).encode("utf-8")).hexdigest()
                               for name in type(self).model_fields})

    def current(self):
        from .behavior_channels_migration import ChannelRetirement
        return {name: parse_document(getattr(self, name).encode("utf-8"),
                                    ChannelRetirement if name == "retirement" else ProtectionObservation)
                for name in (*LANES, "retirement")}


@dataclass(frozen=True)
class PinRefreshEvidenceFiles(ChannelEvidenceFiles):
    previous_sources: Path
    previous_backups: Path

    def material(self):
        return PinWitnesses(**{name: file_bytes(getattr(self, name), 32768).decode("utf-8")
                               for name in PinWitnesses.model_fields})

    def read(self):
        return self.material().current()


class PinPreimage(_Model):
    lane: Literal["sources", "backups"]
    row_id: Identifier
    preimage: Annotated[str, Field(min_length=1, max_length=16384)]
    etag: Annotated[str, Field(min_length=1, max_length=1024)]
    sha256: Digest


class PinRefreshPlan(ChannelPlan):
    schema_version: Literal["fa-channel-pin-plan-v2"] = "fa-channel-pin-plan-v2"
    core_code_sha256: Digest
    operator_code_sha256: Digest
    old_pins: Annotated[tuple[PinPreimage, ...], Field(min_length=2, max_length=2)]
    witness_sha256: WitnessHashes
    reviewed_source_sha256: Digest
    reviewed_source_valid_until: datetime
    _review_time = field_validator("reviewed_source_valid_until")(_utc)


class PinRefreshApproval(ChannelApproval):
    schema_version: Literal["fa-channel-pin-approval-v2"] = "fa-channel-pin-approval-v2"
    action: Literal["enable-bounded-teams-metrics-with-protection-pin-refresh"] = (
        "enable-bounded-teams-metrics-with-protection-pin-refresh"
    )
    core_code_sha256: Digest
    operator_code_sha256: Digest


class PinRefreshHistory(ChannelHistory):
    schema_version: Literal["fa-channel-pin-history-v2"] = "fa-channel-pin-history-v2"
    plan: PinRefreshPlan
    approval: PinRefreshApproval
    witnesses: PinWitnesses


class PinRefreshInstallation(ChannelInstallation):
    schema_version: Literal["fa-channel-pin-installation-v2"] = "fa-channel-pin-installation-v2"


def compatible(old, new):
    require(type(old) is ServerConfiguration and type(new) is ServerConfigurationV2,
            "explicit original v1 and channel v2 configurations required")
    expected = old.model_dump(mode="json")
    for lane in LANES:
        before, after = getattr(old, lane).protection, getattr(new, lane).protection
        require(before.evidence_sha256 != after.evidence_sha256, "both actual evidence pins must change explicitly")
        expected[lane]["protection"]["evidence_sha256"] = after.evidence_sha256
    require(expected == new.predecessor().model_dump(mode="json"),
            "only channels and the two protection evidence_sha256 fields may change")


def validate_witnesses(old, new, witnesses):
    current = witnesses.current()
    previous = {lane: parse_document(getattr(witnesses, "previous_" + lane).encode("utf-8"), ProtectionObservation)
                for lane in LANES}
    validate_protection_pairs(old, new, previous, {lane: current[lane] for lane in LANES})
    return current


def validate_protection_pairs(old, new, previous_values, current):
    compatible(old, new)
    require(set(previous_values) == set(current) == set(LANES), "both exact source/backup witness pairs required")
    for lane in LANES:
        previous = previous_values[lane]
        expected = getattr(old, lane)
        require(type(previous) is ProtectionObservation and type(current[lane]) is ProtectionObservation
                and previous.configuration == expected.protection
                and set(previous.inventory_coverage or ()) == set(old.evidence.inventory_scopes)
                and previous.blob_service.account_resource_id == expected.account_resource_id
                and previous.management_policy.account_resource_id == expected.account_resource_id,
                "historical witness must bind the exact original installed pin/account/inventory")
        InventoryProtectionVerifier(previous.state, clock=lambda p=previous: p.observed_at).verify(previous.configuration)
        fresh = current[lane]
        require(fresh.configuration == getattr(new, lane).protection, "new observation differs from proposed pin")
        InventoryProtectionVerifier(fresh.state, clock=lambda p=fresh: p.observed_at).verify(fresh.configuration)
        require(previous.blob_service.canonical_payload() == fresh.blob_service.canonical_payload()
                and previous.management_policy.canonical_payload() == fresh.management_policy.canonical_payload(),
                "pin-only transition cannot alter native retention policy")


def propose_protected_channels(old, channels, previous, current):
    """Local typed proposal only; no retirement, authorization, or freshness grant."""
    require(set(previous) == set(current) == set(LANES), "both exact witness pairs required")
    raw = old.model_dump(mode="json")
    raw.update(schema_version="fa-behavior-server-v2", channels=channels.model_dump(mode="json"))
    for lane in LANES:
        raw[lane]["protection"] = current[lane].configuration.model_dump(mode="json")
    new = ServerConfigurationV2.model_validate_json(json.dumps(raw))
    validate_protection_pairs(old, new, previous, current)
    return new


def validate_images(old, plan):
    require(tuple(image.lane for image in plan.old_pins) == LANES
            and tuple(image.row_id for image in plan.old_pins) == m.protection_keys(old),
            "exact ordered source/backup pin preimages required")
    for image in plan.old_pins:
        raw = json.loads(image.preimage)
        row = _parse(raw, _Row)
        require(row.row_id == image.row_id and row.binding == old.workers.control_binding
                and row.data == getattr(old, image.lane).protection.model_dump(mode="json")
                and row.material is None and _payload_hash(raw) == image.sha256,
                "old pin preimage/hash/binding differs")


def propose_pin_configuration(old, channels, evidence):
    require(type(evidence) is PinRefreshEvidenceFiles, "explicit pin-refresh witness files required")
    witnesses = evidence.material()
    current = witnesses.current()
    previous = {lane: parse_document(getattr(witnesses, "previous_" + lane).encode("utf-8"), ProtectionObservation)
                for lane in LANES}
    return propose_protected_channels(old, channels, previous, {lane: current[lane] for lane in LANES})


def validate_pin_evidence(old, new, evidence, *, clock):
    witnesses = evidence.material()
    current = validate_witnesses(old, new, witnesses)
    local = SimpleNamespace(old=old, new=new, evidence=evidence, clock=clock)
    values, observed, until = ChannelMigration.fresh(local, _protection_configuration=new.predecessor())
    require(values == current and witnesses == evidence.material(), "witness files changed during verification")
    return values, digest({"current": observed, "raw_witnesses": witnesses.fingerprints().model_dump()}), until


def pin_request(old, new, plan, approval):
    original = ChannelMigration.request(SimpleNamespace(old=old, new=new), plan, approval)
    return ControlRequest(**original.model_dump(exclude={"read_set"}),
                          read_set=(*original.read_set, *m.protection_keys(old)))


def current_source_permission(config, store, *, clock, rule_until):
    """Read the fixed template without granting its expired runtime permission."""
    b, profile, policy = config.bindings[0], config.assistant.profiles[0], config.policies[0].policy
    keys = (control_key("owner", b.owner.owner_record_id), control_key("policy", b.owner.policy_id),
            runtime_key("policy", b.binding_id))
    rows = store.read_snapshot(keys)
    require(all(rows.values()), "original owner/policy rows are required")
    owner, grant, runtime = (_parse(rows[key].payload["data"], model)
                            for key, model in zip(keys, (OwnerRecord, PolicyRecord, RuntimePolicy)))
    require(owner.active and grant.active and runtime.active
            and all(getattr(owner, field) == getattr(b.owner, field) for field in type(b.owner).model_fields)
            and runtime.model_dump(mode="json") == policy.model_dump(mode="json")
            and runtime.operation_policy_revision == grant.revision,
            "original template ownership/policy has been revoked or changed")
    actor = config.identity.auth().actors[b.agent_oid]
    context = AuthenticatedSourceContext(b.tenant_id, b.team_id, actor.actor, b.ingress_id, policy.allowed_purposes)
    sources = SourceMaterials(store, clock=clock)
    reference = SourceReference(scope=context.scope(policy.source_visibility), **profile.source.model_dump())
    source = sources.read_exact(context, reference, require_current=True)
    reviewer = DurableHumanReviewVerifier(sources, HumanReviewPolicy(policy_version=config.review_policy_version), clock=clock)
    memory = MemoryGovernance(sources, clock=clock, token_counter=len, review_verifier=reviewer)
    claim = memory.get_current(context, scope=reference.scope, subject=b.subject, claim_id=profile.claim_id)
    require(source.event.blob is None and not source.event.necessary and claim.human_reviewed
            and claim.review.reviewer_id == config.identity.human_oid
            and reference in claim.candidate.supporting, "fixed inline source and current designated human review required")
    fingerprint = digest({
        "owner_policy": {key: row.payload for key, row in rows.items()},
        "source": sources.evidence_digest({reference.key: source}),
        "claim": claim.model_dump(mode="json"),
    })
    until = min(value for value in (rule_until, source.event.expires_at, claim.candidate.expires_at) if value is not None)
    return fingerprint, until


class ChannelPinRefresh(ChannelMigration):
    plan_type = PinRefreshPlan
    approval_type = PinRefreshApproval
    history_type = PinRefreshHistory
    installation_type = PinRefreshInstallation

    def __init__(self, store, old, new, evidence, operator_code, *, clock, allow_apply=False, scope_check=None):
        compatible(old, new)
        require(type(evidence) is PinRefreshEvidenceFiles and type(operator_code) is OperatorCodeDirectory,
                "trusted complete historical/current witness files and actual operator code required")
        unchanged = ServerConfigurationV2.model_validate_json(json.dumps({
            **old.model_dump(mode="json"), "schema_version": "fa-behavior-server-v2",
            "channels": new.channels.model_dump(mode="json"),
        }))
        super().__init__(store, old, unchanged, ChannelEvidenceFiles(evidence.sources, evidence.backups, evidence.retirement),
                         clock=clock, allow_apply=allow_apply)
        self.new, self.evidence, self.operator_code = new, evidence, operator_code
        self.scope_check = scope_check
        self._authentication = ContextVar("channel_pin_human", default=None)

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
            require(current == principal, "delegated human scope changed before persistence")

    def fresh(self):
        if self.scope_check is not None:
            self.scope_check(False)
        return validate_pin_evidence(self.old, self.new, self.evidence, clock=self.clock)

    def replacement_data(self):
        result = super().replacement_data()
        result.update({key: getattr(self.new, lane).protection.model_dump(mode="json")
                       for lane, key in zip(LANES, m.protection_keys(self.old))})
        return result

    def plan(self, principal, *, transition_id):
        planned = super().plan(principal, transition_id=transition_id)
        source_sha, source_until = current_source_permission(
            self.old, self.store, clock=self.clock, rule_until=self.new.channels.valid_until)
        inventory = self.store.read_inventory()
        require(digest(dict(inventory.entity_sha256)) == planned.inventory_sha256,
                "authority changed while binding original pin preimages")
        images = tuple(PinPreimage(lane=lane, row_id=key, etag=inventory.rows[key].etag,
                                  preimage=json.dumps(inventory.rows[key].payload, ensure_ascii=False, separators=(",", ":")),
                                  sha256=_payload_hash(inventory.rows[key].payload))
                       for lane, key in zip(LANES, m.protection_keys(self.old)))
        value = planned.model_dump(exclude={"schema_version"})
        value["valid_until"] = min(planned.valid_until, source_until)
        plan = PinRefreshPlan(**value, core_code_sha256=core_code_sha256(),
                              operator_code_sha256=self.operator_code.sha256(), old_pins=images,
                              witness_sha256=self.evidence.material().fingerprints(),
                              reviewed_source_sha256=source_sha, reviewed_source_valid_until=source_until)
        validate_images(self.old, plan)
        return plan

    def authorize(self, principal, plan, *, decision_id, expires_at):
        require(type(plan) is PinRefreshPlan, "explicit pin-refresh plan required")
        approval = super().authorize(principal, plan, decision_id=decision_id, expires_at=expires_at)
        return PinRefreshApproval(**approval.model_dump(exclude={"schema_version", "action"}),
                                  core_code_sha256=plan.core_code_sha256,
                                  operator_code_sha256=plan.operator_code_sha256)

    def authorized(self, principal, plan, approval, *, historical=False):
        if self.scope_check is not None:
            self.scope_check(historical)
        require(type(plan) is PinRefreshPlan and type(approval) is PinRefreshApproval,
                "pin refresh cannot use a v1 plan or approval")
        require(plan.core_code_sha256 == approval.core_code_sha256 == core_code_sha256()
                and plan.operator_code_sha256 == approval.operator_code_sha256 == self.operator_code.sha256()
                and plan.witness_sha256 == self.evidence.material().fingerprints(),
                "core/operator code or exact witness bytes changed")
        validate_images(self.old, plan)
        if not historical:
            current, until = current_source_permission(
                self.old, self.store, clock=self.clock, rule_until=self.new.channels.valid_until)
            require((current, until) == (plan.reviewed_source_sha256, plan.reviewed_source_valid_until),
                    "current source/review/owner differs from approved plan")
        super().authorized(principal, plan, approval, historical=historical)

    def request(self, plan, approval):
        return pin_request(self.old, self.new, plan, approval)

    def history_fields(self, plan):
        witnesses = self.evidence.material()
        require(witnesses.fingerprints() == plan.witness_sha256, "witness bytes changed after plan")
        return {"witnesses": witnesses}


def validate_pin_history(old, new, history, images):
    require(type(history) is PinRefreshHistory and tuple(images) == history.plan.old_pins,
            "explicit immutable v2 history and exact plan preimages required")
    plan, approval = history.plan, history.approval
    validate_images(old, plan)
    values = validate_witnesses(old, new, history.witnesses)
    require(history.witnesses.fingerprints() == plan.witness_sha256
            and approval.core_code_sha256 == plan.core_code_sha256
            and approval.operator_code_sha256 == plan.operator_code_sha256,
            "stored historical witness/code bindings differ")
    local = SimpleNamespace(old=old, new=new, evidence=SimpleNamespace(read=lambda: values),
                            clock=lambda: history.applied_at)
    _, observed, until = ChannelMigration.fresh(local, _protection_configuration=new.predecessor())
    require(plan.evidence_sha256 == digest({
        "current": observed, "raw_witnesses": plan.witness_sha256.model_dump(),
    }) and plan.valid_until == min(until, plan.reviewed_source_valid_until),
            "historical fresh witness/expiry differs from approved plan")


def verify_pin_predecessor(inventory, old, new, history, store):
    key = control_key("runtime-rotation-current", old.table.scope.authority_domain)
    if key not in inventory.rows:
        key = m.migration_key(old.table.scope.authority_domain, "current")
    prior = inventory.rows[inventory.rows[key].payload["data"]["history_key"]].payload["data"]
    retired = history.witnesses.current()["retirement"]
    require(retired.old_code_sha256 == prior["plan"]["code_sha256"],
            "historical retirement does not identify the installed predecessor code")
    from .behavior_runtime_rotation import _snapshot_store
    view = _snapshot_store(store, inventory, old)
    current = current_source_permission(old, view, clock=lambda: history.applied_at,
                                        rule_until=new.channels.valid_until)
    require(current == (history.plan.reviewed_source_sha256, history.plan.reviewed_source_valid_until),
            "historical current source/review differs")


def installed_pin_channels_origin(store, config, inventory):
    pointer = _parse(inventory.rows[channel_key("current", config.table.scope.authority_domain)].payload["data"],
                     PinRefreshInstallation)
    history = _parse(inventory.rows[pointer.history_id].payload["data"], PinRefreshHistory)
    return _verify_channel_history(store, config, inventory, pointer, history, pin_images=history.plan.old_pins)
