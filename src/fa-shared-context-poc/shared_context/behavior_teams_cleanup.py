"""Independent expired-original-work cleanup, never an active channel window."""
from contextvars import ContextVar
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from .behavior_recovery import (
    ClaimedSdkRecovery, ExpiredTeamsCleanupScope, ExpiredTeamsCleanupPlan,
    ExpiredTeamsCleanupObservation, ExpiredTeamsCleanupAuthorization, recovery_key, denied, code_sha256,
)
from .behavior_teams_recovery import (
    TeamsPreAcquireRecovery, configured_work, original_teams_state, verify_plan_original,
)
from .behavior_recovery_history import _Inventory
from .behavior_delivery import _parse, _Work, _material, WorkResultRequest, MessageRequest, ReceiptRequest, DeliveryConsumer
from .behavior_operations import control_key
from .behavior_channels_migration import channel_key
from .behavior_store import _execution_owner
from .behavior_migration import installation_record
from .teams_native import TeamsNativeEvidence, TeamsNativeGate, TeamsNativeRegistration, TeamsDeliveryCore
from .teams_delivery import TeamsDeliveryAdapter, TeamsSendContext
from .source_materials import digest

_delivery_call = ContextVar("expired_original_teams_delivery", default=None)


def cleanup_grant(delivery_id):
    return channel_key("cleanup-send-grant", delivery_id)


def retained_native(view, config):
    return view.row(control_key("teams-native-evidence", config.channels.native_evidence_id),
                    TeamsNativeEvidence, material=True)


def validate_cleanup_plan(view, plan, authorization=None):
    """Pure verification of independent approval and immutable historical refs."""
    config, observation = view.config, plan.observation
    from .behavior_channels_window import WindowInstallation
    window = view.row(channel_key("current", config.table.scope.authority_domain), WindowInstallation)
    original = retained_native(view, config)
    if (plan.purpose != "stop_original_and_deliver_failure"
            or not window.valid_until <= plan.not_before < plan.cleanup_expires_at <= plan.not_before+timedelta(minutes=60)
            or observation.configuration_sha256 != config.sha256
            or observation.code_sha256 != plan.code_sha256 or observation.event_id != plan.event_id
            or observation.job_id != plan.binding.job_id or observation.launch_id != plan.launch_id
            or observation.observer_oid != config.evidence.observer_oid
            or observation.inventory_sha256 != plan.inventory_sha256
            or observation.guard_revision != plan.inventory_guard_revision
            or observation.target_sha256 != digest(plan.target.model_dump(mode="json"))
            or observation.original_native_evidence_sha256 != digest(original.model_dump(mode="json"))
            or observation.retained_authority_sha256 != digest([plan.original_rows, plan.original_receipts])
            or not observation.observed_at <= plan.not_before < observation.expires_at
            or plan.not_before-observation.observed_at > timedelta(minutes=5)
            or plan.cleanup_expires_at > observation.expires_at
            or observation.expires_at > observation.observed_at+timedelta(minutes=65)):
        raise denied("TEAMS_CLEANUP_OBSERVATION_INVALID", "independent exact original cleanup observation differs")
    if authorization is not None and (
            not plan.not_before <= authorization.approved_at < authorization.expires_at <= plan.cleanup_expires_at
            or authorization.expires_at > authorization.approved_at+timedelta(minutes=60)):
        raise denied("TEAMS_CLEANUP_AUTHORIZATION_INVALID", "original cleanup approval exceeds its independent interval")


class ExpiredTeamsCleanup(TeamsPreAcquireRecovery):
    def __init__(self, composition, scope, stop, *, observation, current_observation):
        if type(scope) is not ExpiredTeamsCleanupScope or type(observation) is not ExpiredTeamsCleanupObservation:
            raise denied("TEAMS_CLEANUP_SCOPE_INVALID", "explicit expired-original scope and trusted observation required")
        if not callable(current_observation):
            raise denied("TEAMS_CLEANUP_OBSERVATION_INVALID", "current trusted cleanup observer read is required")
        if digest(observation.model_dump(mode="json")) != scope.observation_sha256:
            raise denied("TEAMS_CLEANUP_OBSERVATION_INVALID", "server observation does not match original approved scope")
        self.observation = observation
        self.current_observation = current_observation
        super().__init__(composition, scope, stop)

    def _operator(self, principal, *, historical=False):
        ClaimedSdkRecovery._operator(self, principal, historical=historical)
        self.teams.refresh()
        launch = self.native.launch(self.scope.launch_id)
        inventory = self.store.read_inventory()
        registered, window, _, _ = original_teams_state(inventory, self.config, launch)
        if (registered.event_id != self.scope.event_id or registered.binding.job_id != self.scope.job_id
                or self.scope.not_before < window.valid_until
                or self.scope.expires_at > self.observation.expires_at):
            raise denied("TEAMS_CLEANUP_SCOPE_INVALID", "cleanup is not the original expired registered activity")
        if not historical:
            if self.current_observation() != self.observation:
                raise denied("TEAMS_CLEANUP_OBSERVATION_INVALID", "current cleanup observation revoked or changed")
            guard, etag = self.store._guard()
            if inventory.guard.payload != guard.model_dump(mode="json") or inventory.guard.etag != etag:
                raise denied("TEAMS_CLEANUP_AUTHORITY_CHANGED", "original authority changed during cleanup validation")
            self._time()

    def _time(self, authorization=None):
        now = self.teams.runtime._now()
        lower = self.scope.not_before if authorization is None else max(self.scope.not_before, authorization.approved_at)
        upper = min(self.scope.expires_at, self.observation.expires_at,
                    self.scope.expires_at if authorization is None else authorization.expires_at)
        if not lower <= now < upper:
            raise denied("TEAMS_CLEANUP_EXPIRED", "independent original-work cleanup permission is not current")

    def _build_plan(self, **values):
        plan = ExpiredTeamsCleanupPlan(**values, purpose=self.scope.purpose,
            not_before=self.scope.not_before, cleanup_expires_at=self.scope.expires_at, observation=self.observation)
        validate_cleanup_plan(_Inventory(self.store.read_inventory(), self.config), plan)
        if self.teams.runtime._now()-self.observation.observed_at > timedelta(minutes=5):
            raise denied("TEAMS_CLEANUP_OBSERVATION_STALE", "initial cleanup plan requires a fresh independent observation")
        return plan

    def _authorization(self, principal, authorization, *, historical=False):
        super()._authorization(principal, authorization, historical=historical)
        if (type(authorization) is not ExpiredTeamsCleanupAuthorization or
                authorization.plan.observation != self.observation or
                authorization.plan.not_before != self.scope.not_before or
                authorization.plan.cleanup_expires_at != self.scope.expires_at):
            raise denied("TEAMS_CLEANUP_AUTHORIZATION_INVALID", "exact independent cleanup approval is required")
        if not historical:
            self._time(authorization)

    def _last_stop_time(self, authorization, window, policy, now):
        self._time(authorization)

    def _deliver(self, principal, authorization):
        self._authorization(principal, authorization)
        authority = CleanupDelivery(self, principal, authorization)
        return authority.run()


class CleanupDelivery:
    """One synchronous original-result-only capability on a private channel view."""
    def __init__(self, service, principal, authorization):
        from .behavior_teams_continuation import (
            OriginalFailureDeliveryContinuation, DeliveryContinuationAuthorization, continuation_key, continuation_grant)
        continuation = type(service) is OriginalFailureDeliveryContinuation
        if not ((type(service) is ExpiredTeamsCleanup and type(authorization) is ExpiredTeamsCleanupAuthorization)
                or (continuation and type(authorization) is DeliveryContinuationAuthorization)):
            raise denied("TEAMS_CLEANUP_DELIVERY_DENIED", "explicit original cleanup service is required")
        self.service, self.principal, self.authorization = service, principal, authorization
        self.binding, _ = configured_work(service.config, service.scope.event_id)
        self.delivery_id = control_key("delivery-final-id", self.binding.obligation_id)
        self.original_authorization = service.original() if continuation else authorization
        self.authorization_key = (continuation_key(self.delivery_id) if continuation else
                                  recovery_key("authorization", service.scope.recovery_id))
        self.grant_id = continuation_grant(self.delivery_id) if continuation else cleanup_grant(self.delivery_id)
        self.owner, self.active = _execution_owner(), False
        self.consumed = None
        original = service.teams.channel.adapter
        self.adapter = TeamsDeliveryAdapter(service.store, original.settings, clock=service.clock,
            resolve_send=lambda did: self.send_context(did), bot_token=original.bot_token,
            transport=original.transport, artifact_links=None, artifact_origins=(),
            native_registration=original.native_registration, cleanup_authority=self)
        registration = replace(service.teams.channel.core.registration, channel_adapter=self.adapter)
        self.core = TeamsDeliveryCore(service.store, clock=service.clock, registration=registration,
                                      shared_channels=service.teams.channel.core.shared_channels)
        self.adapter.attach(self.core)

    @property
    def expires_at(self):
        return min(self.authorization.expires_at, self.service.observation.expires_at)

    def check_owner(self):
        if not self.active or self.owner != _execution_owner() or _delivery_call.get() is not self:
            raise denied("TEAMS_CLEANUP_DELIVERY_DENIED", "cleanup delivery cannot escape its synchronous owner")

    def send_context(self, delivery_id):
        self.check_owner()
        if delivery_id != self.delivery_id:
            raise denied("TEAMS_CLEANUP_DELIVERY_DENIED", "another delivery cannot use this original cleanup")
        return TeamsSendContext(context=self.binding.owner, grant_id=self.grant_id)

    def validate_command(self, command, context):
        from .behavior_foundry import native_key
        self.check_owner()
        if (type(command) not in {WorkResultRequest, MessageRequest, ReceiptRequest} or context != self.binding.owner
                or command.obligation_id != self.binding.obligation_id
                or isinstance(command, MessageRequest) and (command.delivery_id != self.delivery_id or command.message_kind != "failure")
                or isinstance(command, ReceiptRequest) and (
                    command.delivery_id != self.delivery_id or command.evidence_id != self.delivery_id)
                or isinstance(command, WorkResultRequest) and command.evidence_id != native_key(
                    "platform-stop-result", self.original_authorization.plan.launch_id)):
            raise denied("TEAMS_CLEANUP_DELIVERY_DENIED", "cleanup authorizes only the original failure result")

    def check(self, rows=None):
        self.check_owner()
        service = self.service
        service._authorization(self.principal, self.authorization)
        service.composition.installation()
        inventory = service.store.read_inventory()
        view = _Inventory(inventory, service.config)
        launch = service.native.launch(service.scope.launch_id)
        from .behavior_recovery import _proof_model, _cancelled_result
        from .behavior_foundry import native_key
        proof = view.row(native_key("platform-stop", launch.launch_id), _proof_model(self.original_authorization.plan), material=True)
        result = _cancelled_result(service.native, launch, proof)
        workkey = control_key("delivery-work-state", self.binding.obligation_id)
        work = view.row(workkey, _Work, material=result.model_dump(mode="json"))
        if (work.result != result or result.state != "cancelled" or result.artifacts or result.known_effects
                or result.unresolved_operations or not service.workers.inspect(self.binding.job_id).result_forwarded):
            raise denied("TEAMS_CLEANUP_RESULT_INVALID", "positive stop and original forwarded zero-model failure required")
        if rows is not None and any(inventory.rows.get(k) != row for k, row in rows.items()):
            raise denied("TEAMS_CLEANUP_DELIVERY_CHANGED", "original delivery snapshot changed before its effect")
        evidence = retained_native(view, service.config)
        # Retention/authentication is evaluated at the original evidence time.
        # This does not refresh its expiry or make it current business evidence.
        adapter = self.service.teams.channel.adapter
        reader = SimpleNamespace(core=adapter.core, settings=adapter.settings, clock=lambda: evidence.observed_at)
        TeamsNativeGate(reader, adapter.native_registration)._validate(evidence, inventory.rows)
        if digest(evidence.model_dump(mode="json")) != self.service.observation.original_native_evidence_sha256:
            raise denied("TEAMS_CLEANUP_OBSERVATION_INVALID", "retained original native evidence changed")
        if service.current_observation() != service.observation:
            raise denied("TEAMS_CLEANUP_OBSERVATION_INVALID", "independent observer changed before original send")
        guard, etag = service.store._guard()
        if inventory.guard.payload != guard.model_dump(mode="json") or inventory.guard.etag != etag:
            raise denied("TEAMS_CLEANUP_DELIVERY_CHANGED", "original authority changed during last send verification")
        service._time(self.authorization)
        return evidence

    def state(self, binding, rows):
        if binding != self.binding:
            raise denied("TEAMS_CLEANUP_DELIVERY_DENIED", "replacement binding denied")
        return self.check(rows)

    def record_consumed(self, receipt, send):
        """Bind the just-committed one-send gate, not a reusable validation flag."""
        self.check_owner()
        if (self.consumed is not None or send.delivery_id != self.delivery_id
                or receipt.payload["request"] != send.gate_request.model_dump(mode="json")
                or receipt.payload["result"] != {"status": "delivery_unknown", "send_permission": False,
                                                "send_digest": digest(send.model_dump(mode="json"))}):
            raise denied("TEAMS_CLEANUP_DELIVERY_DENIED", "original fresh consumed receipt differs")
        self.consumed = receipt, send

    def check_consumption_deadline(self, send):
        self.check_owner()
        self.service._time(self.authorization)
        now = self.service.teams.runtime._now()
        if (send is None or send.delivery_id != self.delivery_id or send.gated_at is None
                or send.not_after is None or not send.gated_at <= now < send.not_after):
            raise denied("TEAMS_CLEANUP_DELIVERY_EXPIRED", "original timed send expired before its single consumption commit")

    def check_wire(self, send):
        self.check_owner()
        if self.consumed is None or send != self.consumed[1]:
            raise denied("TEAMS_CLEANUP_DELIVERY_DENIED", "only this original fresh consumed gate may send")
        service = self.service
        if service.current_observation() != service.observation:
            raise denied("TEAMS_CLEANUP_OBSERVATION_INVALID", "independent observer changed before original send")
        # All original predicates were verified before this single EGT. A
        # physical guard advance now invalidates that exact consumed snapshot.
        guard, _ = service.store._guard()
        if (guard.binding != self.consumed[0].payload["binding"]
                or guard.revision != self.consumed[0].payload["guard_revision"]):
            raise denied("TEAMS_CLEANUP_DELIVERY_CHANGED", "authority changed after original send consumption")
        service._time(self.authorization)

    def run(self):
        if _delivery_call.get() is not None:
            raise denied("TEAMS_CLEANUP_DELIVERY_DENIED", "cleanup capabilities cannot nest")
        self.active = True
        token = _delivery_call.set(self)
        try:
            self.check()
            prepared = DeliveryConsumer(self.core).consume(
                self.binding.obligation_id, self.binding.owner, "teams-original-recovery")
            return self.service.teams.deliver(self.binding, prepared, cleanup_authority=self)
        finally:
            self.active = False
            self.consumed = None
            _delivery_call.reset(token)
