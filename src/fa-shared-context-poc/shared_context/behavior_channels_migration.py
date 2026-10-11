"""One explicit drained v1 -> v2 channel extension, preserving every business row.

The original delivery/worker contracts, counters and root remain byte-identical.
An immutable catalog reconstructs the real predecessor for its existing typed
history verifier; it is not a new namespace or an exemption for unknown capacity.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import field_validator

from . import behavior_migration as m
from .behavior_auth import BehaviorPrincipal
from .behavior_delivery import PendingIndexRecord, QuotaRecord, _Model, _material, _parse, _utc
from .behavior_operations import BudgetRecord, control_key
from .behavior_workers import _Pool, worker_key
from .behavior_runtime_rotation import _snapshot_store
from .behavior_runtime_rotation_history import receipt_catalog, shared_keys
from .behavior_runtime_transition import RuntimeRetirement, _counter_hash, _payload_hash, _without_pin_migration
from .behavior_server_config import ProtectionObservation, ServerConfiguration, ServerConfigurationV2, read_document
from .behavior_store import GUARD_KEY, ControlChanges, ControlRequest, ControlWrite, Digest, _Row
from .errors import Problem
from .models import Identifier
from .records import Record
from .source_materials import InventoryProtectionVerifier, digest


def channel_key(kind, identity):
    return control_key("channels-" + kind, identity)


def require(condition, detail):
    if not condition:
        raise Problem(409, "CHANNELS_MIGRATION_DENIED", detail)


def code_sha256():
    names = ("behavior_channels.py", "behavior_channels_api.py", "behavior_channels_migration.py", "behavior_server_config.py",
             "behavior_server.py", "behavior_runtime.py", "behavior_delivery.py", "teams_metrics.py",
             "teams_native.py", "teams_runtime.py", "teams_delivery.py", "teams_identity.py", "teams_personal.py",
             "teams_capture_server.py", "behavior_ingress.py", "behavior_api.py", "behavior_workers.py",
             "behavior_foundry.py", "behavior_assistant.py", "behavior_assistant_wire.py")
    return digest({name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in names})


class ChannelRetirement(RuntimeRetirement):
    schema_version: Literal["fa-channel-retirement-v1"] = "fa-channel-retirement-v1"
    observer_oid: Identifier


@dataclass(frozen=True)
class ChannelEvidenceFiles:
    sources: Path
    backups: Path
    retirement: Path

    def read(self):
        return {
            "sources": read_document(self.sources, ProtectionObservation),
            "backups": read_document(self.backups, ProtectionObservation),
            "retirement": read_document(self.retirement, ChannelRetirement),
        }


class ChannelPlan(_Model):
    schema_version: Literal["fa-channel-plan-v1"] = "fa-channel-plan-v1"
    transition_id: Identifier
    old_configuration_sha256: Digest
    new_configuration_sha256: Digest
    control_binding: Digest
    code_sha256: Digest
    inventory_sha256: Digest
    preserved_sha256: Digest
    counter_sha256: Digest
    evidence_sha256: Digest
    guard: m.GuardAnchor
    valid_until: datetime
    _time = field_validator("valid_until")(_utc)

    @property
    def sha256(self):
        return digest(self.model_dump(mode="json"))


class ChannelApproval(_Model):
    schema_version: Literal["fa-channel-approval-v1"] = "fa-channel-approval-v1"
    decision_id: Identifier
    operator_oid: Identifier
    plan_sha256: Digest
    action: Literal["enable-bounded-teams-metrics"] = "enable-bounded-teams-metrics"
    not_before: datetime
    expires_at: datetime
    _times = field_validator("not_before", "expires_at")(_utc)


class CatalogEntry(_Model):
    kind: Literal["row", "receipt"]
    row_id: Identifier
    sha256: Digest
    preimage: str | None = None
    etag: str


class CatalogChunk(_Model):
    schema_version: Literal["fa-channel-catalog-v1"] = "fa-channel-catalog-v1"
    transition_id: Identifier
    index: int
    entries: tuple[CatalogEntry, ...]


class ChannelHistory(_Model):
    schema_version: Literal["fa-channel-history-v1"] = "fa-channel-history-v1"
    plan: ChannelPlan
    approval: ChannelApproval
    previous_configuration: dict
    before_guard: dict
    physical_guard_sha256: Digest
    catalog: tuple[tuple[Identifier, Digest], ...]
    request: ControlRequest
    initial_acl: dict | None = None
    applied_at: datetime
    _time = field_validator("applied_at")(_utc)


class ChannelInstallation(_Model):
    schema_version: Literal["fa-channel-installation-v1"] = "fa-channel-installation-v1"
    configuration_sha256: Digest
    predecessor_sha256: Digest
    registry_sha256: Digest
    history_id: Identifier
    history_sha256: Digest
    approval_id: Identifier
    active: bool = True


def parse_channel_installation(data):
    """Read either explicit installed format, never infer a missing version."""
    from .behavior_channels_pin_refresh import PinRefreshInstallation
    from .behavior_channels_window import WindowInstallation

    model = {
        "fa-channel-installation-v1": ChannelInstallation,
        "fa-channel-pin-installation-v2": PinRefreshInstallation,
        "fa-channel-window-installation-v1": WindowInstallation,
    }.get(data.get("schema_version") if isinstance(data, dict) else None)
    if model is None:
        raise Problem(502, "CHANNELS_RECORD_VERSION_DENIED", "unknown channel installation version")
    return _parse(data, model)


def parse_channel_approval(data, installation, *, operator_oid):
    """Keep the original approval identity and pair it with its installed format."""
    from .behavior_channels_pin_refresh import PinRefreshApproval, PinRefreshInstallation
    from .behavior_channels_window import WindowApproval, WindowInstallation

    models = {ChannelInstallation: ChannelApproval, PinRefreshInstallation: PinRefreshApproval,
              WindowInstallation: WindowApproval}
    model = models.get(type(installation))
    if model is None:
        raise Problem(502, "CHANNELS_RECORD_VERSION_DENIED", "unknown channel installation type")
    if not isinstance(data, dict) or data.get("schema_version") != model.model_fields["schema_version"].default:
        raise Problem(502, "CHANNELS_RECORD_VERSION_DENIED", "approval version does not match the installed channel")
    approval = _parse(data, model)
    if (channel_key("approval", approval.decision_id) != installation.approval_id
            or approval.operator_oid != operator_oid):
        raise Problem(403, "CHANNELS_APPROVAL_DENIED", "approval is not the installed designated human decision")
    if type(installation) is WindowInstallation and (
            approval.window.window_id != installation.window_id or approval.window.revision != installation.revision
            or approval.window.not_before != installation.not_before or approval.window.valid_until != installation.valid_until
            or approval.window.predecessor_configuration_sha256 != installation.predecessor_sha256):
        raise Problem(403, "CHANNELS_APPROVAL_DENIED", "approval window differs from its exact installed successor")
    return approval


def check_window_interval(installation, *, now):
    from .behavior_channels_window import WindowInstallation
    if type(installation) is WindowInstallation and not installation.not_before <= now < installation.valid_until:
        raise Problem(403, "CHANNELS_RULE_DENIED", "independent channel window is not currently effective")


def _verified_predecessor(store, inventory, config):
    """Reuse typed history replay; capture is never an unclassified exemption."""
    receipt_catalog(inventory)
    view = _snapshot_store(store, inventory, config)
    rotation = control_key("runtime-rotation-current", config.table.scope.authority_domain)
    transition = control_key("runtime-transition-current", config.table.scope.authority_domain)
    if rotation in inventory.rows:
        # This verifier recursively checks the anchored capture pair and rejects
        # unknown additions, including late captures outside the old catalog.
        return m.installed_origin(view, config)
    require(transition not in inventory.rows,
            "尚未完成原 SDK 退役／rotation 歷史核對，不能直接擴充通道")
    # The existing helper validates the exact capture EGT before projecting its
    # two immutable rows + receipt out of the pin-only quiescence view.
    original, _ = _without_pin_migration(view, inventory, config)
    return original


def _drained(inventory):
    for record in inventory.rows.values():
        data = record.payload["data"]
        kind = data.get("kind")
        if kind == "delivery_quota":
            require(data.get("pending") == 0, "原交付 quota 尚有 pending，不能換配置")
        elif kind == "delivery_pending_index":
            require(not data.get("entries"), "原 root pending index 尚未清空")
        elif kind == "delivery_obligation":
            require(data.get("fulfillment") == "fulfilled", "原交付責任尚未履行，不能用新工作代替")
        elif kind == "worker_pool":
            require(not data.get("pending") and not data.get("completions")
                    and all(slot.get("binding") is None and slot.get("lease_until") is None for slot in data["slots"]),
                    "原 C1 仍 occupied／pending／capacity_uncertain")
        elif kind == "worker_job":
            require(data["state"] in {"succeeded", "failed", "cancelled"}
                    and (data["spec"].get("obligation_id") is None or data["result_forwarded"]),
                    "原工作尚未真正終止及交付核對")
        elif kind == "native_target_reservation":
            require(data.get("active_launch_id") is None, "原 native invocation 尚未完成退役核對")


def _required_authority(inventory, config):
    def row(key, model):
        require(key in inventory.rows, "原 root/pool 不完整，不能用 channel 遷移補建")
        return _parse(inventory.rows[key].payload["data"], model)
    worker_digest = digest({"contract": config.workers.model_dump(mode="json"), "mode": "native"})
    delivery_digest = digest({"contract": config.delivery.model_dump(mode="json"), "mode": "native"})
    pool = row(worker_key("pool", config.workers.workspace_id), _Pool)
    require(pool.workspace_id == config.workers.workspace_id and pool.configuration_digest == worker_digest
            and len(pool.slots) == config.workers.capacity
            and {slot.number for slot in pool.slots} == set(range(config.workers.capacity)),
            "原 C1 pool contract 不吻合")
    for root in config.roots:
        budget = row(control_key("budget", root.root_budget_id), BudgetRecord)
        index = row(control_key("delivery-index", root.root_budget_id), PendingIndexRecord)
        quota = row(control_key("delivery-quota", "root", root.root_budget_id), QuotaRecord)
        require(budget.root_budget_id == index.root_budget_id == quota.scope_id == root.root_budget_id
                and budget.active and quota.scope_kind == "root" and quota.root_request_id == root.root_request_id
                and index.configuration_digest == quota.configuration_digest == delivery_digest
                and index.evidence_mode == quota.evidence_mode == "native",
                "原 root budget/request/index 綁定不吻合或已撤權")
    for kind, name in (("team", config.identity.team_id), ("entry", config.delivery.entry_id)):
        quota = row(control_key("delivery-quota", kind, name), QuotaRecord)
        require(quota.scope_kind == kind and quota.scope_id == name
                and quota.configuration_digest == delivery_digest and quota.evidence_mode == "native",
                "共用 team/entry quota 不吻合")


class ChannelMigration:
    plan_type = ChannelPlan
    approval_type = ChannelApproval
    history_type = ChannelHistory
    installation_type = ChannelInstallation
    replace_current_pointer = False

    def __init__(self, store, old, new, evidence: ChannelEvidenceFiles, *, clock, allow_apply=False):
        require(type(old) is ServerConfiguration and type(new) is ServerConfigurationV2
                and new.predecessor() == old, "只允許明確 v1 -> v2 channel 擴充；原配置其他欄位不得改")
        require(isinstance(store, m.MigrationStore) and store._installation_fence is None
                and store._scope == old.table.scope and store._binding == old.delivery.control_binding,
                "必須使用原 authority 的獨立 operator store")
        require(type(evidence) is ChannelEvidenceFiles, "觀察只可來自受信任 server 檔案")
        self.store, self.old, self.new, self.evidence = store, old, new, evidence
        self.clock, self.allow_apply = clock, allow_apply

    def operator(self, principal):
        if (not isinstance(principal, BehaviorPrincipal) or principal.kind != "human"
                or principal.tenant_id != self.old.identity.tenant_id or principal.oid != self.old.identity.human_oid):
            raise Problem(403, "CHANNELS_OPERATOR_REQUIRED", "需要原租戶指定管理者的 delegated 身分")
        principal.require(self.old.identity.team_id, "read")
        principal.require(self.old.identity.team_id, "approve")

    def fresh(self, *, _protection_configuration=None):
        values, now = self.evidence.read(), _utc(self.clock())
        retired = values["retirement"]
        require(retired.operator_oid == self.old.identity.human_oid
                and retired.observer_oid == self.old.evidence.observer_oid
                and retired.authority_domain == self.old.table.scope.authority_domain
                and retired.old_configuration_sha256 == self.old.sha256
                and retired.new_configuration_sha256 == self.new.sha256
                and retired.api_resource_id == self.old.api_resource_id
                and retired.controller_resource_id == self.old.controller_resource_id
                and tuple(item.target for item in retired.targets) == self.old.active_targets
                and retired.observed_at <= now < retired.valid_until
                and retired.valid_until - retired.observed_at <= timedelta(minutes=20),
                "需要目前原 API/controller/完整 native target 退役觀察")
        deadlines = [retired.valid_until, self.new.channels.valid_until]
        require(now < self.new.channels.valid_until <= now + timedelta(hours=4),
                "新的有限 metrics 授權必須另行批准，不能延長舊 policy")
        for lane in ("sources", "backups"):
            observed, expected = values[lane], getattr(_protection_configuration or self.old, lane)
            require(observed.configuration == expected.protection
                    and set(observed.inventory_coverage or ()) == set(self.old.evidence.inventory_scopes)
                    and observed.blob_service.account_resource_id == expected.account_resource_id
                    and observed.management_policy.account_resource_id == expected.account_resource_id
                    and observed.inventory_observed_at is not None
                    and observed.inventory_valid_until is not None
                    and observed.inventory_observed_at <= now < observed.inventory_valid_until,
                    "原 source/backup 保護與完整 inventory 必須仍有效")
            InventoryProtectionVerifier(observed.state, clock=self.clock).verify(expected.protection)
            for start, end in ((observed.observed_at, observed.valid_until),
                               (observed.inventory_observed_at, observed.inventory_valid_until),
                               (observed.blob_service.observed_at, observed.blob_service.valid_until),
                               (observed.management_policy.observed_at, observed.management_policy.valid_until)):
                require(start <= now < end and now - start <= timedelta(seconds=self.old.evidence.max_age_seconds),
                        "原保護觀察已過期")
                deadlines.append(end)
        return values, digest({key: value.model_dump(mode="json") for key, value in values.items()}), min(deadlines)

    def replacement_data(self):
        key, data = m.installation_record(self.new)
        return {key: data}

    def history_fields(self, plan):
        return {}

    def installation_fields(self, plan):
        return {}

    def plan(self, principal, *, transition_id):
        self.operator(principal)
        values, observation_sha, until = self.fresh()
        inventory = self.store.read_inventory()
        require(channel_key("current", self.old.table.scope.authority_domain) not in inventory.rows,
                "不能重做既有 channel 遷移")
        # Existing typed migration/rotation history must validate before extension.
        _verified_predecessor(self.store, inventory, self.old)
        _required_authority(inventory, self.old)
        _drained(inventory)
        from .teams_personal import PersonalRouteEnrollment
        from .teams_delivery import TeamsRouteAccess
        enrolled = PersonalRouteEnrollment(self.store, self.new.channels.personal, clock=self.clock).settings()
        require(enrolled.routes[0].route_id == self.new.channels.route_id, "新配置只能引用原已驗 signed personal route")
        access = inventory.rows.get(control_key("teams-route-access", self.new.channels.route_id))
        if access is not None:
            acl = _parse(access.payload["data"], TeamsRouteAccess)
            require(acl.active and acl.route_id == self.new.channels.route_id
                    and acl.recipient_id == self.old.identity.human_oid
                    and self.old.bindings[0].owner.owner_record_id in acl.owner_record_ids,
                    "既有 personal ACL 未授權模板，不能覆寫撤權")
        prior_key = control_key("runtime-rotation-current", self.old.table.scope.authority_domain)
        if prior_key not in inventory.rows:
            prior_key = m.migration_key(self.old.table.scope.authority_domain, "current")
        prior = inventory.rows[inventory.rows[prior_key].payload["data"]["history_key"]].payload["data"]
        require(values["retirement"].old_code_sha256 == prior["plan"]["code_sha256"],
                "退役觀察不屬於原 installed code")
        replaced = {GUARD_KEY, *("behavior:v1:row:" + key for key in self.replacement_data())}
        preserved = {key: value for key, value in inventory.entity_sha256.items()
                     if key not in replaced}
        return ChannelPlan(
            transition_id=transition_id, old_configuration_sha256=self.old.sha256,
            new_configuration_sha256=self.new.sha256, control_binding=self.store._binding,
            code_sha256=code_sha256(), inventory_sha256=digest(dict(inventory.entity_sha256)),
            preserved_sha256=digest(preserved), counter_sha256=_counter_hash(inventory.rows),
            evidence_sha256=observation_sha,
            guard=m.GuardAnchor(binding=self.store._binding, revision=inventory.guard.payload["revision"],
                                etag=inventory.guard.etag), valid_until=until,
        )

    def authorize(self, principal, plan, *, decision_id, expires_at):
        self.operator(principal)
        require(plan == self.plan(principal, transition_id=plan.transition_id), "批准前原 inventory 已改變")
        now = _utc(self.clock())
        require(now < _utc(expires_at) <= min(plan.valid_until, now + timedelta(minutes=15)), "批准必須有限且目前有效")
        return ChannelApproval(decision_id=decision_id, operator_oid=principal.oid, plan_sha256=plan.sha256,
                               not_before=now, expires_at=expires_at)

    def authorized(self, principal, plan, approval, *, historical=False):
        self.operator(principal)
        require(approval.operator_oid == principal.oid and approval.plan_sha256 == plan.sha256
                and plan.old_configuration_sha256 == self.old.sha256
                and plan.new_configuration_sha256 == self.new.sha256
                and plan.control_binding == self.store._binding
                and approval.not_before < approval.expires_at <= min(plan.valid_until, approval.not_before + timedelta(minutes=15)),
                "批准、配置或 authority 不吻合")
        if not historical:
            _, evidence_sha, until = self.fresh()
            require(plan.code_sha256 == code_sha256() and plan.evidence_sha256 == evidence_sha
                    and approval.not_before <= _utc(self.clock()) < min(approval.expires_at, until),
                    "程式、觀察或批准期限已改變")

    def request(self, plan, approval):
        return ControlRequest(request_id=channel_key("apply", plan.transition_id),
                              request_digest=digest([plan.sha256, approval.model_dump(mode="json")]),
                              read_set=(m.installation_record(self.old)[0],
                                        control_key("teams-route-access", self.new.channels.route_id)))

    def apply(self, principal, plan, approval):
        if not self.allow_apply:
            raise Problem(403, "CHANNELS_APPLY_DISABLED", "此 operator 僅允許審核")
        self.authorized(principal, plan, approval)
        require(plan == self.plan(principal, transition_id=plan.transition_id), "原 pending、capacity 或 inventory 已改變")
        inventory = self.store.read_inventory()
        require(digest(dict(inventory.entity_sha256)) == plan.inventory_sha256, "原 inventory 已改變")
        access_key = control_key("teams-route-access", self.new.channels.route_id)
        replacements = self.replacement_data()
        ckey = channel_key("current", self.old.table.scope.authority_domain)
        mutable = shared_keys(self.old) | {access_key} | set(replacements)
        if self.replace_current_pointer:
            mutable.add(ckey)
        entries = tuple(CatalogEntry(
            kind=kind, row_id=key, sha256=_payload_hash(row.payload), etag=row.etag,
            preimage=json.dumps(row.payload, ensure_ascii=False, separators=(",", ":"))
            if kind == "row" and key in mutable else None,
        ) for kind, values in (("row", inventory.rows), ("receipt", inventory.receipts))
            for key, row in sorted(values.items()))
        chunks = tuple(CatalogChunk(transition_id=plan.transition_id, index=index // 64,
                                    entries=entries[index:index + 64]) for index in range(0, len(entries), 64))
        require(len(chunks) + len(replacements) + 6 <= self.store._scope.max_operations,
                "保留完整歷史超過單一 EGT 上限")
        catalog = tuple((channel_key("catalog", plan.transition_id + "-" + str(item.index)), item) for item in chunks)
        request = self.request(plan, approval)
        from .teams_delivery import TeamsRouteAccess
        initial_acl = None if access_key in inventory.rows else TeamsRouteAccess(
            route_id=self.new.channels.route_id, recipient_id=self.old.identity.human_oid,
            active=True, owner_record_ids=(self.old.bindings[0].owner.owner_record_id,), artifacts=(),
        ).model_dump(mode="json")
        history = self.history_type(
            plan=plan, approval=approval, previous_configuration=self.old.model_dump(mode="json"),
            before_guard={**inventory.guard.payload, "etag": inventory.guard.etag},
            physical_guard_sha256=inventory.entity_sha256[GUARD_KEY],
            catalog=tuple((key, digest(chunk.model_dump(mode="json"))) for key, chunk in catalog),
            request=request, initial_acl=initial_acl, applied_at=_utc(self.clock()),
            **self.history_fields(plan),
        )
        hkey, akey = channel_key("history", plan.transition_id), channel_key("approval", approval.decision_id)
        pointer = self.installation_type(
            configuration_sha256=self.new.sha256, predecessor_sha256=self.old.sha256,
            registry_sha256=digest(self.new.channels.model_dump(mode="json")),
            history_id=hkey, history_sha256=digest(history.model_dump(mode="json")), approval_id=akey,
            **self.installation_fields(plan),
        )

        def decide(rows):
            self.authorized(principal, plan, approval)
            for key in replacements:
                require(rows[key] is not None and rows[key].payload == inventory.rows[key].payload
                        and rows[key].etag == inventory.rows[key].etag, "原 installation/protection 已改變")
            writes = [ControlWrite(row_id=key, mode="replace", data=data) for key, data in replacements.items()]
            if initial_acl is not None:
                require(rows[access_key] is None, "原 personal ACL 已改變")
                writes.append(ControlWrite(row_id=access_key, mode="create", data=initial_acl))
            writes.extend(ControlWrite(row_id=key,
                          mode="replace" if key == ckey and self.replace_current_pointer else "create",
                          data=value.model_dump(mode="json"))
                          for key, value in ((hkey, history), (akey, approval),
                                             (channel_key("current", self.old.table.scope.authority_domain), pointer), *catalog))
            return ControlChanges(writes=tuple(writes), result={
                "configuration_sha256": self.new.sha256, "history_id": hkey,
                "counter_sha256": plan.counter_sha256, "activation_performed": False,
            })

        return self.store.transact(request, decide, expected_guard=inventory.guard,
                                   before_send=lambda: self.authorized(principal, plan, approval),
                                   install_configuration_sha256=self.new.sha256)

    def reconcile(self, principal, plan, approval):
        self.authorized(principal, plan, approval, historical=True)
        return self.store.reconcile(self.request(plan, approval))

    def verify(self, principal, plan, approval):
        receipt = self.reconcile(principal, plan, approval)
        inventory = self.store.read_inventory()
        writes = dict(receipt.payload["writes"])
        require(all(inventory.entity_sha256.get("behavior:v1:row:" + key) == value for key, value in writes.items()),
                "遷移後影像已改變；不能宣稱即時 cutover readback 成功")
        excluded = {GUARD_KEY, "behavior:v1:receipt:" + receipt.payload["request"]["request_id"],
                    *("behavior:v1:row:" + key for key in writes)}
        require(digest({key: value for key, value in inventory.entity_sha256.items() if key not in excluded})
                == plan.preserved_sha256 and _counter_hash(inventory.rows) == plan.counter_sha256,
                "原 counters、業務 rows 或 receipts 被改動")
        installed_channels_origin(self.store, self.new)
        return {**receipt.payload["result"], "preserved_sha256": plan.preserved_sha256}


def installed_channels_origin(store, config):
    require(type(config) is ServerConfigurationV2, "channel history 需要明確 v2 配置")
    inventory = store.read_inventory()
    receipt_catalog(inventory)
    ckey = channel_key("current", config.table.scope.authority_domain)
    pointer = parse_channel_installation(inventory.rows[ckey].payload["data"])
    from .behavior_channels_window import WindowInstallation, installed_window_origin
    if type(pointer) is WindowInstallation:
        return installed_window_origin(store, config, inventory, pointer)
    if type(pointer) is not ChannelInstallation:
        from .behavior_channels_pin_refresh import installed_pin_channels_origin
        return installed_pin_channels_origin(store, config, inventory)
    history = _parse(inventory.rows[pointer.history_id].payload["data"], ChannelHistory)
    return _verify_channel_history(store, config, inventory, pointer, history)


def _verify_channel_history(store, config, inventory, pointer, history, *, pin_images=(), window=False):
    from .behavior_server_config import ServerConfigurationDocument
    old = (ServerConfigurationDocument if window else ServerConfiguration).model_validate_json(
        json.dumps(history.previous_configuration))
    approval, plan = history.approval, history.plan
    ckey = channel_key("current", config.table.scope.authority_domain)
    if window:
        from .behavior_channels_window import validate_window_history
        validate_window_history(old, config, history, pointer)
    elif pin_images:
        from .behavior_channels_pin_refresh import validate_pin_history
        validate_pin_history(old, config, history, pin_images)
    else:
        require(config.predecessor() == old, "原 v1 channel 配置不得改")
    require(pointer.active
            and pointer.configuration_sha256 == config.sha256 == inventory.installation_configuration_sha256
            and pointer.predecessor_sha256 == old.sha256 == plan.old_configuration_sha256
            and plan.new_configuration_sha256 == config.sha256
            and pointer.registry_sha256 == digest(config.channels.model_dump(mode="json"))
            and pointer.history_sha256 == digest(history.model_dump(mode="json"))
            and pointer.history_id == channel_key("history", plan.transition_id)
            and pointer.approval_id == channel_key("approval", approval.decision_id)
            and inventory.rows[pointer.approval_id].payload["data"] == approval.model_dump(mode="json")
            and approval.plan_sha256 == plan.sha256 and approval.operator_oid == config.identity.human_oid
            and approval.not_before <= history.applied_at < min(approval.expires_at, plan.valid_until)
            and plan.control_binding == store._binding,
            "channel installation 不是原 authority 批准的完整歷史")
    key, installed = m.installation_record(config)
    require(inventory.rows[key].payload["data"] == installed, "目前 installation 不吻合")
    expected_request = ControlRequest(request_id=channel_key("apply", plan.transition_id),
                                     request_digest=digest([plan.sha256, approval.model_dump(mode="json")]),
                                     read_set=(key, control_key("teams-route-access", config.channels.route_id),
                                               *((ckey,) if window else ()),
                                               *(image.row_id for image in pin_images)))
    require(history.request == expected_request, "原遷移請求不吻合")
    receipt = store.reconcile(history.request)
    expected = {key: _payload_hash(inventory.rows[key].payload),
                **{name: _payload_hash(inventory.rows[name].payload)
                   for name in (ckey, pointer.history_id, pointer.approval_id)}}
    for lane, image in zip(("sources", "backups"), pin_images):
        current_pin = inventory.rows[image.row_id]
        require(current_pin.payload["data"] == getattr(config, lane).protection.model_dump(mode="json")
                and current_pin.payload.get("material") is None, "目前 protection pin 不吻合")
        expected[image.row_id] = _payload_hash(current_pin.payload)
    rows, receipts, hashes = {}, {}, {GUARD_KEY: history.physical_guard_sha256}
    access_key = control_key("teams-route-access", config.channels.route_id)
    pin_by_key = {image.row_id: image for image in pin_images}
    mutable = shared_keys(old) | {access_key} | set(pin_by_key)
    if window:
        mutable.add(ckey)
    if history.initial_acl is not None:
        from .teams_delivery import TeamsRouteAccess
        initial_acl = _parse(history.initial_acl, TeamsRouteAccess).model_dump(mode="json")
        expected[access_key] = _payload_hash(_Row(binding=store._binding, row_id=access_key,
                                                 data=initial_acl).model_dump(mode="json"))
    for index, (chunk_key, chunk_sha) in enumerate(history.catalog):
        chunk = _parse(inventory.rows[chunk_key].payload["data"], CatalogChunk)
        require(chunk.transition_id == plan.transition_id and chunk.index == index
                and chunk_key == channel_key("catalog", plan.transition_id + "-" + str(index))
                and digest(chunk.model_dump(mode="json")) == chunk_sha, "保留 catalog 已改變")
        expected[chunk_key] = _payload_hash(inventory.rows[chunk_key].payload)
        for entry in chunk.entries:
            target = rows if entry.kind == "row" else receipts
            current = (inventory.rows if entry.kind == "row" else inventory.receipts).get(entry.row_id)
            require(current is not None and entry.row_id not in target, "原保留 row/receipt 遺失或重複")
            if entry.kind == "row" and entry.row_id in mutable:
                require(entry.preimage is not None, "共用 counter 缺少原影像")
                if entry.row_id in pin_by_key:
                    image = pin_by_key[entry.row_id]
                    require((entry.preimage, entry.etag, entry.sha256) == (image.preimage, image.etag, image.sha256),
                            "pin preimage 與原 plan/catalog 不吻合")
                value = Record(json.loads(entry.preimage), entry.etag)
            else:
                require(entry.preimage is None, "不可用替代影像掩蓋原 immutable 記錄")
                value = current
            require(_payload_hash(value.payload) == entry.sha256, "原 row/receipt 已被改寫")
            target[entry.row_id] = value
            hashes["behavior:v1:" + ("row:" if entry.kind == "row" else "receipt:") + entry.row_id] = entry.sha256
    require(len(rows) + len(receipts) <= m.DEFAULT_INVENTORY_MAX_ENTITIES and digest(hashes) == plan.inventory_sha256,
            "完整原 inventory 不吻合")
    require(dict(receipt.payload["writes"]) == expected
            and receipt.payload["guard_revision"] == plan.guard.revision + 1, "單一 EGT 寫入回條不吻合")
    guard = {key: value for key, value in history.before_guard.items() if key != "etag"}
    before = m.AuthorityInventory(store._scope, Record(guard, history.before_guard["etag"]),
                                  MappingProxyType(rows), MappingProxyType(receipts), MappingProxyType(hashes), old.sha256)
    require(plan.guard.binding == guard["binding"] == store._binding
            and plan.guard.revision == guard["revision"] and plan.guard.etag == before.guard.etag
            and _counter_hash(before.rows) == plan.counter_sha256
            and digest({name: value for name, value in hashes.items()
                        if name not in {GUARD_KEY, "behavior:v1:row:" + key,
                                        *("behavior:v1:row:" + name for name in ((ckey,) if window else ())),
                                        *("behavior:v1:row:" + name for name in pin_by_key)}}) == plan.preserved_sha256,
            "原 guard、保留集合或已耗用 counter 與批准不吻合")
    receipt_catalog(before)
    _required_authority(before, old)
    _drained(before)
    if window:
        from .behavior_channels_window import verify_window_predecessor
        original = verify_window_predecessor(store, before, old, config, history)
    else:
        original = _verified_predecessor(store, before, old)
    if pin_images:
        from .behavior_channels_pin_refresh import verify_pin_predecessor
        verify_pin_predecessor(before, old, config, history, store)
    from .behavior_teams_recovery import validate_teams_recovery_history
    validate_teams_recovery_history(inventory, config)
    store.bind_installation(key, config.sha256, protected_rows=(
        ckey, pointer.history_id, pointer.approval_id, *(item[0] for item in history.catalog),
        *pin_by_key,
        *(name for name in rows if name not in mutable),
    ))
    store._verified_migration_epoch = config.sha256
    return original
