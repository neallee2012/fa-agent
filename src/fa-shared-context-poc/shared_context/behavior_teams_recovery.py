"""Exact current-window Teams pre-acquire closure; no admission or SDK replay."""
from contextvars import ContextVar
from types import SimpleNamespace
import json

from . import behavior_foundry as n, behavior_workers as w
from .behavior_assistant import _Session, _Delegation, key
from .behavior_channels_migration import channel_key
from .behavior_channels_window import WindowInstallation, WindowHistory
from .behavior_delivery import _Obligation, _Work, _parse, _update
from .behavior_migration import DEFAULT_INVENTORY_MAX_ENTITIES
from .behavior_operations import control_key, OwnerRecord, PolicyRecord, BudgetRecord
from .behavior_recovery import (
    ClaimedSdkRecovery, TeamsPreAcquireRecoveryScope, TeamsPreAcquireRecoveryPlan,
    ExpiredTeamsCleanupScope, ExpiredTeamsCleanupPlan,
    _claimed_receipt, _multi_inventory, denied, recovery_key,
)
from .behavior_recovery_history import _Inventory
from .behavior_runtime import RuntimePolicy, runtime_key
from .behavior_server_config import ServerConfigurationV2
from .behavior_store import GUARD_KEY, ControlRequest
from .source_materials import digest
from .teams_delivery import TeamsRouteAccess
from .teams_metrics import TeamsMetrics, TeamsWork
from .teams_runtime import _Source

_stop_dispatch = ContextVar("teams_original_stop_dispatch", default=None)


def registry_rules(config):
    """Use the same pure producer derivation, without mutating runtime/config."""
    template = next(b for b in config.bindings if b.binding_id == config.channels.template_binding_id)
    profile = next(p for p in config.assistant.profiles if p.binding_id == template.binding_id)
    rules = SimpleNamespace(config=config, setup=config.channels, template=template, template_profile=profile,
        current_key=channel_key("current", config.table.scope.authority_domain),
        revocation_key=channel_key("revocation", config.sha256),
        index_key=channel_key("requests", config.delivery.entry_id))
    rules.derive = lambda event: TeamsMetrics.derive(rules, event)
    rules.work_key = lambda event: channel_key("work", event)
    return rules


def configured_work(config, event_id):
    return registry_rules(config).derive(event_id)


def recovery_keys(config, launch, inventory):
    return _keys_for_registration(config, launch, _registration(config, launch.api_binding_id, inventory))


def _registration(config, binding_id, inventory=None):
    if inventory is None:
        raise denied("TEAMS_RECOVERY_REGISTRY_REQUIRED", "complete original Teams inventory is required")
    view = _Inventory(inventory, config)
    found = []
    for rid, record in inventory.rows.items():
        if record.payload["data"].get("schema_version") != "fa-teams-metrics-work-v1":
            continue
        value = view.row(rid, TeamsWork, material=True)
        if value.binding.binding_id == binding_id:
            found.append(value)
    if len(found) != 1:
        raise denied("TEAMS_RECOVERY_REGISTRY_INVALID", "one exact original registered activity is required")
    return found[0]


def _keys_for_registration(config, launch, registered):
    b, owner = registered.binding, registered.binding.owner
    rules = registry_rules(config)
    sid = key("session-id", launch.start.binding.start_request_id)
    return tuple(dict.fromkeys((
        n.native_key("launch", launch.launch_id), n.native_key("grant", launch.grant_id),
        control_key("owner", owner.owner_record_id), control_key("policy", owner.policy_id),
        control_key("budget", owner.root_budget_id), w.worker_key("job", b.job_id),
        n.native_key("target", launch.target.executor_id), w.worker_key("pool", config.workers.workspace_id),
        key("session", sid), key("artifact", b.work.work_id), key("result", b.work.work_id),
        key("archive", sid), key("restore", sid),
        runtime_key("policy", b.binding_id), rules.current_key, rules.revocation_key, rules.index_key,
        rules.work_key(b.event_id), control_key("teams-source", b.event_id),
        control_key("teams-route-access", config.channels.route_id), key("delegation", b.event_id),
        control_key("delivery-obligation", b.obligation_id), control_key("delivery-work-state", b.obligation_id),
        control_key("operation", b.operation_id),
    )))


def original_teams_state(inventory, config, launch):
    if type(config) is not ServerConfigurationV2:
        raise denied("TEAMS_RECOVERY_CONFIGURATION", "explicit current v2 configuration required")
    view = _Inventory(inventory, config)
    registered = _registration(config, launch.api_binding_id, inventory)
    b, profile = configured_work(config, registered.event_id)
    rules = registry_rules(config)
    if (registered.binding != b or registered.profile != profile
            or registered.rule_sha256 != digest(config.channels.model_dump(mode="json"))
            or registered.route_id != config.channels.route_id
            or launch.start.binding.job_id != b.job_id or launch.definition != b.work
            or launch.api_binding_id != b.binding_id or launch.start.binding.owner != b.owner
            or launch.start.checkpoint is not None):
        raise denied("TEAMS_RECOVERY_REGISTRY_INVALID", "original dynamic binding/profile/launch differs")
    index = inventory.rows.get(rules.index_key)
    if index is None or index.payload["data"].get("active_event") != b.event_id or b.event_id not in index.payload["data"].get("events", ()):
        raise denied("TEAMS_RECOVERY_REGISTRY_INVALID", "original activity is not the current registry responsibility")
    window = view.row(rules.current_key, WindowInstallation)
    history = view.row(window.history_id, WindowHistory)
    if (window.configuration_sha256 != config.sha256 or not window.active
            or window.registry_sha256 != registered.rule_sha256
            or window.history_sha256 != digest(history.model_dump(mode="json"))
            or window.approval_id != channel_key("approval", history.approval.decision_id)
            or rules.revocation_key in inventory.rows):
        raise denied("TEAMS_RECOVERY_WINDOW_CHANGED", "current original window/approval differs")
    registration = TeamsMetrics.registration_request(rules, registered)
    registration_writes = (
        rules.work_key(b.event_id), control_key("owner", b.owner.owner_record_id), control_key("policy", b.owner.policy_id),
        runtime_key("policy", b.binding_id), runtime_key("plan-index", b.binding_id),
        n.native_key("grant", n.native_key("teams-grant", b.event_id)), rules.index_key,
        control_key("teams-route-access", config.channels.route_id))
    view.receipt(registration, registration_writes, exact={rules.work_key(b.event_id): view.hash(rules.work_key(b.event_id))},
                 result={"binding_id": b.binding_id, "not_yet_admitted": True})
    source_key = control_key("teams-source", b.event_id)
    source = view.row(source_key, _Source, material=True)
    source_receipt = view.receipts.get(source.request.request_id)
    if (source_receipt is None or source_receipt.request != source.request
            or source.command.work != b.work or source.command.route != b.route
            or source.command.event.event_id != b.event_id):
        raise denied("TEAMS_RECOVERY_SOURCE_INVALID", "original signed source/admission differs")
    sid = key("session-id", launch.start.binding.start_request_id)
    session_key = key("session", sid)
    session = view.row(session_key, _Session)
    if (session.binding_id != b.binding_id or session.execution != launch.start.binding
            or session.state != "accepted" or session.reason is not None or session.context_read
            or session.model_requests != 0 or session.model_intent_ids or session.assignment.resume
            or session.assignment.session_id != sid
            or session.assignment.task.work_id != b.work.work_id
            or session.assignment.task.profile_digest != digest(profile.model_dump(mode="json"))
            or key("acquire", sid, "sdk-start") in inventory.receipts):
        raise denied("TEAMS_RECOVERY_ACQUIRE_CHANGED", "original assignment is no longer an unacquired start")
    absent = (key("artifact", b.work.work_id), key("result", b.work.work_id), key("archive", sid), key("restore", sid))
    if any(rid in inventory.rows for rid in absent):
        raise denied("TEAMS_RECOVERY_EFFECT_PRESENT", "original artifact/result/archive/restore cannot be hidden")
    for rid, record in inventory.rows.items():
        request_sha = record.payload["data"].get("request_sha256")
        if isinstance(request_sha, str) and rid == key("model-intent", sid, request_sha):
            raise denied("TEAMS_RECOVERY_EFFECT_PRESENT", "orphan or original model intent prevents zero-model closure")
    allowed_session_receipts = {key("issue", sid)}
    for record in inventory.rows.values():
        data = record.payload["data"]
        if data.get("schema_version") in {"fa-teams-pre-acquire-recovery-authorization-v1",
                                       "fa-expired-teams-cleanup-authorization-v1"}:
            recovery_id = data["plan"]["recovery_id"]
            if data["plan"]["binding"]["start_request_id"] == launch.start.binding.start_request_id:
                allowed_session_receipts.update(recovery_key(stage, recovery_id) for stage in (
                    "cancel-teams-pre-acquire", "stop-intent", "sealed"))
    if any(session_key in receipt.request.read_set and rid not in allowed_session_receipts
           for rid, receipt in view.receipts.items()):
        raise denied("TEAMS_RECOVERY_EFFECT_PRESENT", "unexpected original session/effect receipt prevents pre-acquire closure")
    operation = inventory.rows.get(control_key("operation", b.operation_id))
    if operation is not None:
        raise denied("TEAMS_RECOVERY_EFFECT_PRESENT", "operation material requires another exact closure contract")
    delegation = view.row(key("delegation", b.event_id), _Delegation, material=True)
    if delegation.profile_digest != session.assignment.task.profile_digest or delegation.event_id != b.event_id:
        raise denied("TEAMS_RECOVERY_SOURCE_INVALID", "original delegation differs")
    work = view.row(control_key("delivery-work-state", b.obligation_id), _Work)
    if work.result is not None and work.result.evidence_id != n.native_key("platform-stop-result", launch.launch_id):
        raise denied("TEAMS_RECOVERY_EFFECT_PRESENT", "another result cannot be overwritten")
    acl = view.row(control_key("teams-route-access", config.channels.route_id), TeamsRouteAccess)
    if not acl.active or acl.recipient_id != b.human_oid or b.owner.owner_record_id not in acl.owner_record_ids:
        raise denied("TEAMS_RECOVERY_OWNER_CHANGED", "original route ACL is no longer bound")
    owner = view.row(control_key("owner", b.owner.owner_record_id), OwnerRecord)
    if not owner.active or any(getattr(owner, name) != value for name, value in b.owner.model_dump().items()):
        raise denied("TEAMS_RECOVERY_OWNER_CHANGED", "original owner/epoch changed")
    native_keys = tuple(_keys_for_registration(config, launch, registered)[:7])
    claim = _claimed_receipt(inventory, launch, native_keys)
    issue_id = key("issue", sid)
    issue_reads = (runtime_key("policy", b.binding_id), control_key("owner", b.owner.owner_record_id),
        control_key("policy", b.owner.policy_id), control_key("budget", b.owner.root_budget_id),
        rules.current_key, rules.revocation_key, rules.work_key(b.event_id), control_key("teams-route-access", config.channels.route_id),
        w.worker_key("job", b.job_id), w.worker_key("pool", config.workers.workspace_id),
        session_key, key("delegation", b.event_id), control_key("delivery-obligation", b.obligation_id))
    issue = view.receipt(ControlRequest(request_id=issue_id, request_digest=digest(launch.start.model_dump(mode="json")),
                                       read_set=issue_reads), (session_key,),
                         exact={session_key: view.hash(session_key)}, result={"session_id": sid})
    if not claim.payload["guard_revision"] < issue.guard_revision:
        raise denied("TEAMS_RECOVERY_ASSIGNMENT_INVALID", "original claim/assignment receipt differs")
    refs = (rules.current_key, window.history_id, window.approval_id, rules.work_key(b.event_id),
            source_key, session_key, key("delegation", b.event_id), runtime_key("policy", b.binding_id),
            control_key("policy", b.owner.policy_id))
    rows = {rid: view.hash(rid) for rid in refs}
    receipts = {rid: digest(inventory.receipts[rid].payload) for rid in (
        registration.request_id, source.request.request_id, issue_id, claim.payload["request"]["request_id"])}
    return registered, window, rows, receipts


def verify_plan_original(inventory, config, launch, plan):
    registered, window, rows, receipts = original_teams_state(inventory, config, launch)
    if (registered.event_id != plan.event_id or rows != plan.original_rows
            or receipts != plan.original_receipts or plan.configuration_sha256 != config.sha256):
        raise denied("TEAMS_RECOVERY_ORIGINAL_CHANGED", "approved immutable original authority differs")
    return registered, window


def validate_original_catalog(inventory, config, launch):
    """Do not anchor untyped/orphan current records into a new approval."""
    from . import behavior_migration as m
    from .behavior_runtime import CommandPlan, _PlanIndex
    from .behavior_delivery import VerifiedIngress
    from .behavior_wire import HostedReportBody
    from .teams_native import TeamsNativeEvidence
    from .teams_identity import TeamsRouteRecord
    view = _Inventory(inventory, config)
    registered, window, _, _ = original_teams_state(inventory, config, launch)
    history = view.row(window.history_id, WindowHistory)
    retained = {m.installation_record(config)[0], channel_key("current", config.table.scope.authority_domain),
                window.history_id, window.approval_id, *(rid for rid, _ in history.catalog)}
    for rid, _ in history.catalog:
        # Channel catalogs use their own explicit schema, not rotation chunks.
        from .behavior_channels_migration import CatalogChunk as ChannelCatalog
        chunk = view.row(rid, ChannelCatalog)
        retained.update(entry.row_id for entry in chunk.entries if entry.kind == "row")
    rules = registry_rules(config)
    primitive = {
        rules.index_key: {"events", "active_event"},
        control_key("teams-source-capacity", config.delivery.entry_id): {"count"},
        control_key("teams-native-current", config.channels.personal.configuration_digest):
            {"active", "evidence_digest", "evidence_id", "observed_at"},
    }
    models = (*m._kind_models().values(), CommandPlan, RuntimePolicy, _PlanIndex, _Delegation, _Session,
              _Source, TeamsWork, TeamsRouteRecord, TeamsNativeEvidence, VerifiedIngress, w.ExecutorEvidence,
              HostedReportBody, m._WorkerAdmission, m._NativePostIntent)
    for rid, record in inventory.rows.items():
        if rid in retained:
            continue
        data = record.payload["data"]
        if rid in primitive:
            if set(data) != primitive[rid]:
                raise denied("TEAMS_RECOVERY_UNCLASSIFIED", "current primitive record shape differs")
            continue
        matching = []
        for model in models:
            try:
                matching.append(model.model_validate_json(json.dumps(data)))
            except (ValueError, TypeError):
                continue
        if not matching:
            raise denied("TEAMS_RECOVERY_UNCLASSIFIED", "unknown current record cannot enter recovery approval")
        for value in matching:
            if isinstance(value, (n._NativeLaunch, n._NativeEvidence, n._NativeProbe)):
                if value.launch_id != launch.launch_id:
                    raise denied("TEAMS_RECOVERY_UNCLASSIFIED", "another current native launch requires its own closure")
            if isinstance(value, w.JobRecord) and value.spec.job_id != registered.binding.job_id:
                raise denied("TEAMS_RECOVERY_UNCLASSIFIED", "another current job cannot enter this closure")


class TeamsPreAcquireRecovery(ClaimedSdkRecovery):
    """Independent v2 constructor; legacy static recovery gate is unchanged."""
    def __init__(self, composition, scope, stop):
        config, runtime = composition.config, composition.runtime
        native = runtime.dep.native_launches
        if (type(scope) not in {TeamsPreAcquireRecoveryScope, ExpiredTeamsCleanupScope} or type(config) is not ServerConfigurationV2
                or native is None or runtime.teams_metrics is None
                or native.recovery_binding != n.NativeRecoveryBinding(
                    configuration_sha256=config.sha256, operator_oid=config.identity.human_oid,
                    credential_client_id=config.identity.managed_identity_client_id)
                or config.sha256 != scope.configuration_sha256 or config.identity.human_oid != scope.operator_oid):
            raise denied("TEAMS_RECOVERY_SCOPE_INVALID", "exact v2 original recovery identity is required")
        self.composition, self.teams = composition, runtime.teams_metrics
        self.config, self.native, self.scope, self.clock = config, native, scope, runtime.clock
        self._platform_stop, self.stop = stop, self._dispatch_stop
        self.workers, self.store = native.workers, native.store
        self.delivery = runtime.delivery_for(configured_work(config, scope.event_id)[0])

    def _operator(self, principal, *, historical=False):
        super()._operator(principal, historical=historical)
        self.teams.refresh()
        pointer = self.teams.pointer(current=not historical)
        if (type(pointer) is not WindowInstallation or not historical and self.clock() >= pointer.valid_until
                or self.scope.expires_at > min(pointer.valid_until, self.config.channels.valid_until)):
            raise denied("TEAMS_RECOVERY_WINDOW_EXPIRED", "original finite window is unavailable")
        if not historical:
            binding, _ = configured_work(self.config, self.scope.event_id)
            _, policy, _ = self.teams.runtime.authorize(binding.binding_id, principal, "read")
            now = self.teams.runtime._now()
            if not pointer.not_before <= now < min(self.scope.expires_at, pointer.valid_until, policy.valid_until):
                raise denied("TEAMS_RECOVERY_WINDOW_EXPIRED", "original window expired after final authority read")

    def _keys(self, launch, _):
        registered = _registration(self.config, launch.api_binding_id, self.store.read_inventory())
        return _keys_for_registration(self.config, launch, registered)

    def _snapshot(self, launch, _, *, after_fence=False, after_reservation=False):
        inventory = self.store.read_inventory()
        registered, window, _, _ = original_teams_state(inventory, self.config, launch)
        if (registered.event_id != self.scope.event_id or registered.binding.job_id != self.scope.job_id
                or launch.launch_id != self.scope.launch_id):
            raise denied("TEAMS_RECOVERY_SCOPE_INVALID", "scope is not the exact registered original work")
        view = _Inventory(inventory, self.config)
        job_key = w.worker_key("job", self.scope.job_id)
        job = view.row(job_key, w.JobRecord)
        pool = view.row(w.worker_key("pool", self.config.workers.workspace_id), w._Pool)
        target = view.row(n.native_key("target", launch.target.executor_id), n._TargetReservation)
        if (launch.phase != "claimed" or launch.native_invocation_id is None or launch.target not in self.config.active_targets
                or job.binding != launch.start.binding or job.state not in {"starting", "running", "capacity_uncertain"}
                or job.children or job.cancel_requested != after_fence or job.cancel_credit != int(not after_fence)
                or job.stop_credit != int(not after_reservation) or job.completion_credit != 1 or job.result_forwarded
                or pool.pending != (job.spec.job_id,) or pool.completions or len(pool.slots) != 1
                or pool.slots[0].binding != job.binding or target.active_launch_id != launch.launch_id
                or target.target != launch.target or target.generation != launch.process_generation):
            raise denied("TEAMS_RECOVERY_ORIGINAL_CHANGED", "original job/capacity/credits/launch changed")
        self.native._current(inventory.rows, launch)
        keys = _keys_for_registration(self.config, launch, registered)
        rows = {rid: inventory.rows.get(rid) for rid in keys}
        normalized = {rid: None if row is None else row.payload for rid, row in rows.items()}
        normalized[job_key] = {**rows[job_key].payload, "data": _update(
            job, cancel_requested=False, cancel_credit=1, stop_credit=1).model_dump(mode="json")}
        return rows, digest(normalized)

    def plan(self, principal):
        from .behavior_recovery import code_sha256
        self._operator(principal)
        self.composition.installation()
        self._original_ingress()
        launch = self.native.launch(self.scope.launch_id)
        _, snapshot = self._snapshot(launch, ())
        inventory = self.store.read_inventory()
        validate_original_catalog(inventory, self.config, launch)
        registered, _, rows, receipts = original_teams_state(inventory, self.config, launch)
        if digest({rid: None if inventory.rows.get(rid) is None else inventory.rows[rid].payload
                   for rid in _keys_for_registration(self.config, launch, registered)}) != snapshot:
            raise denied("TEAMS_RECOVERY_ORIGINAL_CHANGED", "original plan changed during complete read")
        return self._build_plan(
            recovery_id=self.scope.recovery_id, configuration_sha256=self.config.sha256,
            code_sha256=code_sha256(), operator_oid=self.scope.operator_oid, binding=launch.start.binding,
            launch_id=launch.launch_id, target=launch.target, native_invocation_id=launch.native_invocation_id,
            snapshot_sha256=snapshot, recovery_phase=self.scope.recovery_phase, event_id=self.scope.event_id,
            original_rows=rows, original_receipts=receipts, inventory_sha256=digest(dict(inventory.entity_sha256)),
            inventory_guard_revision=inventory.guard.payload["revision"], inventory_guard_sha256=inventory.entity_sha256[GUARD_KEY],
            inventory_guard_etag=inventory.guard.etag,
            original_job_sha256=inventory.entity_sha256["behavior:v1:row:"+w.worker_key("job", self.scope.job_id)],
            **{"original_"+name: getattr(_parse(inventory.rows[w.worker_key("job", self.scope.job_id)].payload["data"], w.JobRecord), name)
               for name in ("last_sequence", "last_evidence_id", "native_execution_id")},
            original_job_state=inventory.rows[w.worker_key("job", self.scope.job_id)].payload["data"]["state"],
            original_scans_remaining=inventory.rows[control_key("delivery-quota", "root", registered.binding.owner.root_budget_id)].payload["data"]["scans_remaining"])

    def _original_ingress(self):
        self.teams.channel.runtime.verify_ingress(self.scope.event_id)

    def _build_plan(self, **values):
        return TeamsPreAcquireRecoveryPlan(**values)

    def _authorization(self, principal, authorization, *, historical=False):
        super()._authorization(principal, authorization, historical=historical)
        inventory = self.store.read_inventory()
        verify_plan_original(inventory, self.config, self.native.launch(self.scope.launch_id), authorization.plan)
        validate_teams_recovery_history(inventory, self.config)

    def _write_gate(self, principal, authorization):
        self._authorization(principal, authorization)
        self.composition.installation()
        self._operator(principal)

    def _approval_gate(self, principal, plan):
        self._operator(principal)
        self.composition.installation()
        inventory = self.store.read_inventory()
        launch = self.native.launch(plan.launch_id)
        verify_plan_original(inventory, self.config, launch, plan)
        _multi_inventory(inventory, self.config, plan, recovery_keys(self.config, launch, inventory))
        self._operator(principal)

    def execute(self, principal, authorization):
        token = _stop_dispatch.set((self, principal, authorization))
        try:
            return super().execute(principal, authorization)
        finally:
            _stop_dispatch.reset(token)

    def _dispatch_stop(self, target, deadline):
        current = _stop_dispatch.get()
        if current is None or current[0] is not self:
            raise denied("TEAMS_RECOVERY_DISPATCH_DENIED", "original authorized stop call is required")
        _, principal, authorization = current
        def final_gate():
            self._authorization(principal, authorization)
            self.composition.installation()
            launch = self.native.launch(authorization.plan.launch_id)
            self._snapshot(launch, (), after_fence=True, after_reservation=True)
            inventory = self.store.read_inventory()
            verify_plan_original(inventory, self.config, launch, authorization.plan)
            _multi_inventory(inventory, self.config, authorization.plan,
                             recovery_keys(self.config, launch, inventory), authorization)
            self._operator(principal)
            if self.store.read_inventory().entity_sha256 != inventory.entity_sha256:
                raise denied("TEAMS_RECOVERY_DISPATCH_DENIED", "authority changed before original provider stop")
            registered, window = verify_plan_original(inventory, self.config, launch, authorization.plan)
            policy = _parse(inventory.rows[runtime_key("policy", registered.binding.binding_id)].payload["data"], RuntimePolicy)
            now = self.teams.runtime._now()
            self._last_stop_time(authorization, window, policy, now)
        return self._platform_stop(target, deadline, before_send=final_gate)

    def _last_stop_time(self, authorization, window, policy, now):
        if not max(window.not_before, authorization.approved_at) <= now < min(
                window.valid_until, policy.valid_until, authorization.expires_at, self.scope.expires_at):
            raise denied("TEAMS_RECOVERY_WINDOW_EXPIRED", "original permission expired after last stop read")

    def deliver(self, principal, authorization):
        return self.store.validated_writes(
            lambda: self._deliver(principal, authorization),
            lambda: self._write_gate(principal, authorization))

    def _deliver(self, principal, authorization):
        self._authorization(principal, authorization)
        registered, _ = verify_plan_original(self.store.read_inventory(), self.config,
            self.native.launch(self.scope.launch_id), authorization.plan)
        b = registered.binding
        from .behavior_delivery import DeliveryConsumer
        prepared = DeliveryConsumer(self.teams.channel.core).consume(b.obligation_id, b.owner, "teams-original-recovery")
        return self.teams.deliver(b, prepared)

    def finish(self, principal, authorization):
        inherited = super().finish
        result = self.store.validated_writes(
            lambda: inherited(principal, authorization), lambda: self._write_gate(principal, authorization))
        return {**result,
                "execution_closure": "platform_stop_before_sdk_acquire", "sdk_graceful_exit_claim": False}


def validate_teams_recovery_history(inventory, config):
    """Typed current-v2 recovery recognition, never a new dispatch permission."""
    from .behavior_recovery import TeamsPreAcquireRecoveryAuthorization, ExpiredTeamsCleanupAuthorization
    from .behavior_recovery_history import _validate
    candidates = {rid: record for rid, record in inventory.rows.items()
                  if record.payload["data"].get("schema_version") in {
                      "fa-teams-pre-acquire-recovery-authorization-v1", "fa-expired-teams-cleanup-authorization-v1"}}
    if not candidates:
        if any(row.payload["data"].get("schema_version") in {
                   "fa-original-failure-delivery-authorization-v1", "fa-original-web-handoff-authorization-v1"}
               for row in inventory.rows.values()):
            raise denied("DELIVERY_CONTINUATION_HISTORY_INVALID", "continuation has no original cleanup history")
        return set()
    if len(candidates) != 1 or len(inventory.rows)+len(inventory.receipts) > DEFAULT_INVENTORY_MAX_ENTITIES:
        raise denied("TEAMS_RECOVERY_HISTORY_INVALID", "one bounded original activity closure is supported")
    view = _Inventory(inventory, config)
    classified, seen = {}, set()
    for rid in candidates:
        model = (ExpiredTeamsCleanupAuthorization if candidates[rid].payload["data"]["schema_version"] ==
                 "fa-expired-teams-cleanup-authorization-v1" else TeamsPreAcquireRecoveryAuthorization)
        authorization = view.row(rid, model, material=True)
        if isinstance(authorization.plan, ExpiredTeamsCleanupPlan):
            from .behavior_teams_cleanup import validate_cleanup_plan
            validate_cleanup_plan(view, authorization.plan, authorization)
        _validate(view, rid, authorization, classified, seen)
        if n.native_key("platform-stop", authorization.plan.launch_id) in inventory.rows:
            from .behavior_recovery import _proof_model, _cancelled_result
            from .behavior_teams_recovery_history import delivery_history
            plan = authorization.plan
            launch = view.row(n.native_key("launch", plan.launch_id), n._NativeLaunch)
            jobkey = w.worker_key("job", plan.binding.job_id)
            job = view.row(jobkey, w.JobRecord)
            original_job = job.model_copy(update={
                "state": plan.original_job_state, "cancel_requested": False, "cancel_credit": 1,
                "stop_credit": 1, "completion_credit": 1, "result_forwarded": False,
                "last_sequence": plan.original_last_sequence, "last_evidence_id": plan.original_last_evidence_id,
                "native_execution_id": plan.original_native_execution_id,
            })
            if view.image_hash(jobkey, original_job) != plan.original_job_sha256:
                raise denied("TEAMS_RECOVERY_HISTORY_INVALID", "original job/query/control history changed")
            proof = view.row(n.native_key("platform-stop", plan.launch_id), _proof_model(plan), material=True)
            result = _cancelled_result(SimpleNamespace(verifier_version=config.delivery.verifier_version), launch, proof)
            binding, _ = configured_work(config, plan.event_id)
            from .behavior_teams_continuation import validate_continuation
            original_checked = set(view.checked_receipts)
            continuation = validate_continuation(view, authorization)
            from .behavior_web_handoff import validate_web_handoff
            handoff = validate_web_handoff(view, config)
            if handoff is None:
                delivery_history(view, config, binding, original_result=result, authorization=authorization,
                                 continuation=continuation)
            elif continuation is not None:
                raise denied("TEAMS_RECOVERY_HISTORY_INVALID", "web handoff cannot replace a Teams continuation")
            if continuation is not None and any(
                    receipt.guard_revision > continuation.plan.inventory_guard_revision
                    and rid not in view.checked_receipts-original_checked
                    for rid, receipt in view.receipts.items()):
                raise denied("DELIVERY_CONTINUATION_HISTORY_INVALID", "only the original continuation and delivery may advance this snapshot")
            added = {rid: receipt for rid, receipt in view.receipts.items()
                     if receipt.guard_revision > plan.inventory_guard_revision}
            if set(added) - view.checked_receipts:
                raise denied("TEAMS_RECOVERY_HISTORY_INVALID", "unknown or unverified receipt after original recovery plan")
            baseline = {}
            for receipt in sorted(view.receipts.values(), key=lambda value: value.guard_revision):
                if receipt.guard_revision <= plan.inventory_guard_revision:
                    baseline.update(receipt.writes)
            changed = {key for receipt in added.values() for key, _ in receipt.writes}
            if (set(inventory.rows) != set(baseline) | changed
                    or any(view.hash(key) != fingerprint for key, fingerprint in baseline.items() if key not in changed)):
                raise denied("TEAMS_RECOVERY_HISTORY_INVALID", "unknown row or unrelated historical mutation after recovery")
            from .teams_delivery import TeamsDeliveryAdapter
            did = control_key("delivery-final-id", binding.obligation_id)
            reserved = control_key("authorize", TeamsDeliveryAdapter.operation_id(did),
                                   control_key("teams-attempt", did)) in added
            budgetkey = control_key("budget", binding.owner.root_budget_id)
            budget = view.row(budgetkey, BudgetRecord)
            restored = budget.model_copy(update={
                "remaining_attempts": budget.remaining_attempts+int(reserved),
                "remaining_units": budget.remaining_units+int(reserved)})
            if view.image_hash(budgetkey, restored) != baseline[budgetkey]:
                raise denied("TEAMS_RECOVERY_HISTORY_INVALID", "original root budget changed outside its once send reservation")
            quota = inventory.rows[control_key("delivery-quota", "root", binding.owner.root_budget_id)].payload["data"]
            if quota["scans_remaining"] != plan.original_scans_remaining:
                raise denied("TEAMS_RECOVERY_HISTORY_INVALID", "closure cannot replenish or consume another scan")
    return set(classified)
