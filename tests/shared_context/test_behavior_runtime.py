"""Runtime dependency/startup/policy tests over actual components, OFFLINE only."""

from dataclasses import replace
from types import MappingProxyType

import pytest

from conftest import A, TENANT
from shared_context.behavior_api import create_behavior_app
from shared_context.behavior_delivery import DeliveryCore
from shared_context.behavior_operations import control_key
from shared_context.behavior_runtime import BehaviorRuntime, RuntimeChildJob, runtime_key
from shared_context.errors import Problem
from shared_context.memory_projection import NativeMemoryProjection, ProjectionSetup
from shared_context.source_backup import SourceBackupService
from shared_context.source_materials import SourceMaterials, SourceScope
from test_adapters import ENDPOINT
from test_behavior_api import CTX, LocalExecutor, RuntimeHarness
from test_behavior_native_api import NativeHarness
from test_m3_auth import jwk_client, signing_key
from test_memory_projection import Transport, client
from test_source_backup import PROTECTION, SETTINGS, CountedBackupStore, protection_verifier


@pytest.fixture
def h(auth, jwk_client, signing_key):
    return RuntimeHarness(auth, jwk_client, signing_key)


CONCRETE = (503, "BEHAVIOR_CONFIGURATION", "all concrete behavior dependencies are required")
BINDING_COUNT = (422, "BEHAVIOR_CONFIGURATION", "one to sixteen unique server bindings are required")
CONTROLLER = (503, "BEHAVIOR_CONFIGURATION", "controller must be an explicit server identifier")
INVALID_BINDING = (422, "OPERATION_INVALID_DATA", "operation contract validation failed")
JOBS = (503, "BEHAVIOR_CONFIGURATION", "root/child job and work identities must be distinct and bounded")
AMBIGUOUS = (503, "BEHAVIOR_CONFIGURATION", "work, owner, job and operation bindings must be unambiguous")
WIRING = (503, "BEHAVIOR_CONFIGURATION", "explicit, identically bound behavior dependencies are required")
NATIVE_REQUIRED = (503, "BEHAVIOR_CONFIGURATION", "native mode requires the concrete permit-bound launch authority")
NATIVE_EXACT = (503, "BEHAVIOR_CONFIGURATION",
                "native callbacks require the exact configured controller and native invocation adapter")
BACKUP = (503, "BEHAVIOR_CONFIGURATION", "backup upload must use the exact live source authority")
PROJECTION = (503, "BEHAVIOR_CONFIGURATION", "native memory projection binding differs")


def build(h, bindings=None, *, mode="offline_contract", controller_id="internal-controller"):
    return BehaviorRuntime(h.dep, (h.binding,) if bindings is None else bindings, clock=h.runtime.clock,
                           mode=mode, controller_id=controller_id)


def refusal(h, bindings=None, **options):
    with pytest.raises(Problem) as error:
        build(h, bindings, **options)
    return error.value.status, error.value.body["error"]["code"], error.value.body["error"]["message"]


def other_binding(h, name, **changes):
    """A binding sharing no identity with h.binding, except what `changes` restores."""
    return h.binding.model_copy(update={
        "binding_id": "binding-" + name, "job_id": "job-" + name, "event_id": "event-" + name,
        "operation_id": "op-" + name, "grant_id": "grant-" + name,
        "owner": h.binding.owner.model_copy(update={"owner_record_id": "owner-" + name}),
        "work": h.binding.work.model_copy(update={"work_id": "work-" + name}), **changes,
    })


def children(h, prefix, count):
    return tuple(RuntimeChildJob(job_id=f"{prefix}-{index}", work=h.binding.work.model_copy(
        update={"work_id": f"{prefix}-work-{index}"})) for index in range(count))


def invalid_binding(h):
    return h.binding.model_copy(update={"agent_oid": h.binding.human_oid})


def foreign_tenant(h, name):
    return other_binding(h, name, tenant_id="other-tenant", route=h.binding.route.model_copy(
        update={"tenant_id": "other-tenant"}))


@pytest.mark.parametrize("missing", ["store", "sources", "memory", "delivery", "inbox", "workers", "broker", "evidence"])
def test_missing_concrete_dependency_fails_closed_at_construction(h, missing):
    h.dep = replace(h.dep, **{missing: None})
    assert refusal(h) == CONCRETE


def test_a_valid_composition_keeps_what_later_requests_rely_on(h):
    runtime = build(h)
    assert (runtime.dep, runtime.store, runtime.clock, runtime.mode) == (h.dep, h.store, h.runtime.clock, "offline_contract")
    assert runtime.work_policy is None and runtime.historical_binding_ids == frozenset()
    assert isinstance(runtime.bindings, MappingProxyType) and runtime.bindings == {"binding-one": h.binding}
    assert runtime.controller.controller_id == "internal-controller" and runtime.controller._runtime is runtime
    assert build(h, controller_id=None).controller is None


def test_sixteen_distinct_bindings_are_allowed_and_a_seventeenth_is_refused(h):
    bindings = (h.binding, *(other_binding(h, str(index)) for index in range(15)))
    assert len(build(h, bindings).bindings) == 16
    assert refusal(h, (*bindings, other_binding(h, "last"))) == BINDING_COUNT


def test_bindings_must_be_present_and_uniquely_named(h):
    assert refusal(h, ()) == BINDING_COUNT
    assert refusal(h, (h.binding, other_binding(h, "two", binding_id=h.binding.binding_id))) == BINDING_COUNT


@pytest.mark.parametrize("controller_id", ["", "not an identifier", "-leading", "a" * 65, 7, b"bytes", ["controller"]])
def test_a_controller_must_be_an_exact_server_identifier(h, controller_id):
    assert refusal(h, controller_id=controller_id) == CONTROLLER


def test_a_controller_refusal_hides_the_validation_error_behind_it(h):
    with pytest.raises(Problem) as error:
        build(h, controller_id="not an identifier")
    assert error.value.__cause__ is None and error.value.__suppress_context__


class Constructed(Exception):
    """Raised by the wrapped __init__ once every check passed, carrying the state it left."""


def test_construction_sets_exactly_the_state_later_requests_read(monkeypatch, auth, jwk_client, signing_key):
    original = BehaviorRuntime.__init__

    def stop_after_init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise Constructed(dict(vars(self)))

    monkeypatch.setattr(BehaviorRuntime, "__init__", stop_after_init)
    try:
        RuntimeHarness(auth, jwk_client, signing_key)
        state = "construction did not stop"
    except Constructed as done:
        state = done.args[0]
    except Problem as problem:  # a check refused a valid composition; report it as a failed assertion, not a crash
        state = problem.body["error"]["message"]
    assert isinstance(state, dict), state
    assert set(state) == {"dep", "store", "clock", "mode", "work_policy", "historical_binding_ids", "controller", "bindings"}
    assert (state["mode"], state["work_policy"], state["historical_binding_ids"]) == ("offline_contract", None, frozenset())
    assert state["controller"].controller_id == "internal-controller" and isinstance(state["controller"]._runtime, BehaviorRuntime)
    assert list(state["bindings"]) == ["binding-one"] and state["store"] is state["dep"].store


def test_a_binding_that_no_longer_validates_is_refused_by_its_own_contract(h):
    assert refusal(h, (invalid_binding(h),)) == INVALID_BINDING


def test_thirty_two_registered_jobs_are_allowed_and_a_thirty_third_is_refused(h):
    bindings = (h.binding.model_copy(update={"child_jobs": children(h, "first", 16)}),
                *(other_binding(h, str(index)) for index in range(14)))
    last = other_binding(h, "last")
    assert len(build(h, (*bindings, last)).bindings) == 16
    assert refusal(h, (*bindings, last.model_copy(update={"child_jobs": children(h, "extra", 1)}))) == JOBS


@pytest.mark.parametrize("changes", [
    lambda h: {"job_id": h.binding.job_id},
    lambda h: {"work": h.binding.work},
    lambda h: {"child_jobs": (RuntimeChildJob(job_id=h.binding.job_id, work=h.binding.work.model_copy(
        update={"work_id": "work-child"})),)},
    lambda h: {"child_jobs": (RuntimeChildJob(job_id="job-child", work=h.binding.work),)},
], ids=["root-job", "root-work", "child-reuses-root-job", "child-reuses-root-work"])
def test_job_and_work_identity_cannot_repeat_across_bindings(h, changes):
    assert refusal(h, (h.binding, other_binding(h, "two", **changes(h)))) == JOBS


@pytest.mark.parametrize("changes", [
    lambda h: {"owner": h.binding.owner},
    lambda h: {"event_id": h.binding.event_id},
    lambda h: {"operation_id": h.binding.operation_id},
    lambda h: {"grant_id": h.binding.grant_id},
], ids=["owner", "event", "operation", "grant"])
def test_ownership_and_operation_identity_cannot_repeat_across_bindings(h, changes):
    assert refusal(h, (h.binding, other_binding(h, "two", **changes(h)))) == AMBIGUOUS


def set_registration(h, **changes):
    h.delivery.registration = replace(h.delivery.registration, **changes)


def set_inbox_registration(h, **changes):
    h.inbox.registration = replace(h.inbox.registration, **changes)


def set_channel(h, **changes):
    h.delivery.contract = h.delivery.contract.model_copy(update={
        "channel": h.delivery.contract.channel.model_copy(update=changes)})


def unregistered_delivery(h):
    delivery = DeliveryCore(h.store, clock=lambda: h.time)
    h.inbox.core, h.dep = delivery, replace(h.dep, delivery=delivery)


@pytest.mark.parametrize("break_wiring", [
    lambda h: setattr(h.sources, "store", object()),
    lambda h: setattr(h.memory, "store", object()),
    lambda h: setattr(h.broker, "store", object()),
    lambda h: setattr(h.delivery, "store", object()),
    lambda h: setattr(h.inbox, "store", object()),
    lambda h: setattr(h.workers, "store", object()),
    lambda h: setattr(h.evidence, "store", object()),
    lambda h: setattr(h.memory, "sources", SourceMaterials(h.store)),
    lambda h: setattr(h.inbox, "core", object()),
    lambda h: setattr(h.delivery, "registration", None),
    unregistered_delivery,
    lambda h: set_registration(h, mode="native"),
    lambda h: setattr(h.workers, "mode", "native"),
    lambda h: set_registration(h, channel_adapter=object()),
    lambda h: set_registration(h, verify_ingress=lambda _: None),
    lambda h: set_registration(h, verify_work_result=lambda _: None),
    lambda h: set_registration(h, verify_receipt=lambda _: None),
    lambda h: set_inbox_registration(h, verify_access=lambda _: None),
    lambda h: set_inbox_registration(h, authentication_version="other-version"),
    lambda h: set_channel(h, channel_id="other-channel"),
    lambda h: set_channel(h, version="other-version"),
    lambda h: setattr(h.workers, "contract", h.workers.contract.model_copy(update={"control_binding": "f" * 64})),
    lambda h: setattr(h.delivery, "contract", h.delivery.contract.model_copy(update={"control_binding": "f" * 64})),
    lambda h: setattr(h, "dep", replace(h.dep, claim_reviewer=object())),
    lambda h: setattr(h.reviewer, "sources", SourceMaterials(h.store)),
    lambda h: setattr(h.memory, "_review", None),
], ids=[
    "sources-store", "memory-store", "broker-store", "delivery-store", "inbox-store", "workers-store", "evidence-store",
    "memory-sources", "inbox-core", "no-registration", "unregistered-delivery", "registration-mode", "workers-mode",
    "channel-adapter", "verify-ingress", "verify-work-result", "verify-receipt", "verify-access", "auth-version",
    "channel-id", "channel-version", "workers-control-binding", "delivery-control-binding", "reviewer-type",
    "reviewer-sources", "memory-reviewer",
])
def test_every_dependency_must_be_bound_to_the_same_authority(h, break_wiring):
    break_wiring(h)
    assert refusal(h) == WIRING


def test_every_binding_must_belong_to_the_delivery_team(h):
    assert refusal(h, (h.binding.model_copy(update={"team_id": "other-team"}),)) == WIRING
    assert refusal(h, (h.binding, other_binding(h, "two", team_id="other-team"))) == WIRING


def test_no_claim_reviewer_is_a_valid_closed_composition(h):
    h.dep, h.memory._review = replace(h.dep, claim_reviewer=None), object()
    assert build(h).dep.claim_reviewer is None


def native_mode(h):
    set_registration(h, mode="native")
    h.workers.mode = "native"


def test_native_mode_requires_a_launch_authority(h):
    native_mode(h)
    assert refusal(h, mode="native") == NATIVE_REQUIRED


@pytest.fixture
def native(auth, jwk_client, signing_key):
    value = NativeHarness(auth, jwk_client, signing_key)
    yield value
    value.native_http.close()


def test_a_matching_launch_authority_is_accepted_in_native_mode(native):
    native_mode(native)
    assert build(native, mode="native").mode == "native"


def set_adapter(h, **changes):
    h.workers.adapter = replace(h.workers.adapter, **changes)


def set_route(h, **changes):
    h.native.jobs = {"job-one": h.native.jobs["job-one"].model_copy(update=changes)}


@pytest.mark.parametrize("break_native", [
    lambda h: setattr(h, "dep", replace(h.dep, native_launches=object())),
    lambda h: setattr(h.native, "workers", object()),
    lambda h: setattr(h.native, "team_id", "other-team"),
    lambda h: setattr(h.workers, "adapter", None),
    lambda h: set_adapter(h, start=lambda _: None),
    lambda h: set_adapter(h, start=LocalExecutor().start),
    lambda h: setattr(h.native_executor, "authority", object()),
    lambda h: set_adapter(h, query=lambda _: None),
    lambda h: set_adapter(h, stop=lambda _: None),
    lambda h: set_adapter(h, verify=lambda _: None),
    lambda h: setattr(h.native, "jobs", {}),
    lambda h: setattr(h.native, "jobs", {**h.native.jobs, "job-extra": h.native.jobs["job-one"]}),
    lambda h: set_route(h, api_binding_id="other-binding"),
], ids=[
    "authority-type", "workers", "team", "no-adapter", "plain-start", "other-executor", "executor-authority",
    "query", "stop", "verify", "missing-job", "extra-job", "other-binding-route",
])
def test_a_launch_authority_must_match_the_runtime_exactly(native, break_native):
    break_native(native)
    assert refusal(native) == NATIVE_EXACT


def test_a_launch_authority_requires_the_same_controller(native):
    assert refusal(native, controller_id=None) == NATIVE_EXACT
    assert refusal(native, controller_id="other-controller") == NATIVE_EXACT


def test_every_registered_job_must_route_to_its_own_binding(native):
    child = children(native, "child", 1)[0]
    second = other_binding(native, "two")
    bindings = (native.binding.model_copy(update={"child_jobs": (child,)}), second)
    route = native.native.jobs["job-one"]
    routes = {"job-one": route, child.job_id: route.model_copy(update={"job_id": child.job_id}),
              second.job_id: route.model_copy(update={"job_id": second.job_id, "api_binding_id": second.binding_id})}
    native.native.jobs = routes
    assert len(build(native, bindings).bindings) == 2
    native.native.jobs = {**routes, child.job_id: routes[child.job_id].model_copy(update={"api_binding_id": second.binding_id})}
    assert refusal(native, bindings) == NATIVE_EXACT
    native.native.jobs = {**routes, second.job_id: routes[second.job_id].model_copy(update={"api_binding_id": "binding-one"})}
    assert refusal(native, bindings) == NATIVE_EXACT


def backup_service(h):
    return SourceBackupService(CountedBackupStore(), SETTINGS, sources=h.sources, protection=PROTECTION,
                               protection_verifier=protection_verifier(), clock=lambda: h.time)


def with_backup(h, service):
    h.dep = replace(h.dep, source_backups=service)


def test_a_backup_service_on_the_live_source_authority_is_accepted(h):
    service = backup_service(h)
    with_backup(h, service)
    assert build(h).dep.source_backups is service


@pytest.mark.parametrize("break_backup", [
    lambda h, service: setattr(service, "sources", SourceMaterials(h.store)),
    lambda h, service: setattr(service, "settings", service.settings.model_copy(update={"authority_domain": "other-domain"})),
    lambda h, service: setattr(service, "settings", service.settings.model_copy(update={"tenant_id": "other-tenant"})),
], ids=["sources", "authority-domain", "tenant"])
def test_a_backup_service_must_use_the_live_source_authority(h, break_backup):
    service = backup_service(h)
    break_backup(h, service)
    with_backup(h, service)
    assert refusal(h) == BACKUP


def test_a_backup_service_must_be_the_concrete_type(h):
    with_backup(h, object())
    assert refusal(h) == BACKUP


def test_a_backup_service_must_cover_every_binding_tenant(h):
    with_backup(h, backup_service(h))
    assert refusal(h, (h.binding, foreign_tenant(h, "two"))) == BACKUP


@pytest.fixture
def projection(h):
    memory_client = client(Transport([]))
    yield NativeMemoryProjection(h.memory, memory_client, ProjectionSetup(
        control_binding=h.store._binding, binding_id=h.binding.binding_id, endpoint=ENDPOINT, store="existing-memory",
        projection_namespace="fa-reviewed-v1-api", evidence_sha256="e" * 64,
        scope=SourceScope(tenant_id=TENANT, team_id=A.team, visibility="team"), subject=h.binding.subject,
        claim_ids=("claim-one",), versions=h.binding.context_versions, max_polls=3, max_searches=2,
        max_input_tokens=4096, max_query_tokens=512, max_input_bytes=16384, max_response_bytes=32768,
    ))
    memory_client.close()


def with_projections(h, projections):
    h.dep = replace(h.dep, memory_projections=projections)


def test_a_projection_matching_its_binding_is_accepted(h, projection):
    with_projections(h, {"binding-one": projection})
    assert build(h).dep.memory_projections == {"binding-one": projection}
    wider = h.binding.model_copy(update={"claim_ids": ("claim-one", "claim-two")})
    assert build(h, (wider,)).dep.memory_projections == {"binding-one": projection}
    with_projections(h, {})
    assert build(h).dep.memory_projections == {}


@pytest.mark.parametrize("changes", [
    lambda setup: {"binding_id": "other-binding"},
    lambda setup: {"subject": "other-subject"},
    lambda setup: {"scope": setup.scope.model_copy(update={"tenant_id": "other-tenant"})},
    lambda setup: {"scope": setup.scope.model_copy(update={"team_id": "other-team"})},
    lambda setup: {"claim_ids": ("claim-one", "claim-two")},
], ids=["binding", "subject", "tenant", "team", "claims"])
def test_a_projection_must_match_its_binding(h, projection, changes):
    projection.setup = projection.setup.model_copy(update=changes(projection.setup))
    with_projections(h, {"binding-one": projection})
    assert refusal(h) == PROJECTION


def test_a_projection_must_use_the_same_memory_governance(h, projection):
    projection.governance = object()
    with_projections(h, {"binding-one": projection})
    assert refusal(h) == PROJECTION


def test_a_projection_must_be_the_concrete_type(h):
    with_projections(h, {"binding-one": object()})
    assert refusal(h) == PROJECTION


def test_a_projection_cannot_name_an_unconfigured_binding(h, projection):
    projection.setup = projection.setup.model_copy(update={"binding_id": "unknown-binding"})
    with_projections(h, {"unknown-binding": projection})
    assert refusal(h) == PROJECTION


def test_every_projection_is_checked_not_only_the_first(h, projection):
    with_projections(h, {"binding-one": projection, "unknown-binding": object()})
    assert refusal(h) == PROJECTION


def test_each_configuration_refusal_wins_over_the_ones_after_it(h):
    dup_owner = other_binding(h, "two", owner=h.binding.owner)

    assert refusal(h, (invalid_binding(h),), controller_id="not an identifier") == CONTROLLER
    assert refusal(h, (invalid_binding(h), other_binding(h, "two", job_id=h.binding.job_id))) == INVALID_BINDING
    assert refusal(h, (h.binding, dup_owner.model_copy(update={"job_id": h.binding.job_id}))) == JOBS
    assert refusal(h, (), controller_id="not an identifier") == BINDING_COUNT

    with_projections(h, {"binding-one": object()})
    with_backup(h, object())
    assert refusal(h) == BACKUP
    native_mode(h)
    assert refusal(h, mode="native") == NATIVE_REQUIRED
    assert refusal(h) == WIRING
    h.sources.store = object()
    assert refusal(h, (h.binding, dup_owner)) == AMBIGUOUS
    h.dep = replace(h.dep, inbox=None)
    assert refusal(h, ()) == CONCRETE


def test_a_wrong_launch_authority_wins_over_backup_and_loses_to_wiring(native):
    native.native.team_id = "other-team"
    with_backup(native, object())
    assert refusal(native) == NATIVE_EXACT
    native.workers.mode = "native"
    assert refusal(native) == WIRING


def test_policy_revision_mismatch_cannot_enable_routes_or_supervision(h):
    h.mutate(control_key("policy", "policy"), revision=2)
    with pytest.raises(Problem):
        create_behavior_app(h.runtime, h.verifier)
    with pytest.raises(Problem):
        h.runtime.resolve_owner(CTX.owner_record_id)


def test_binding_configuration_is_immutable_and_owner_epoch_is_read_from_authority(h):
    with pytest.raises(TypeError):
        h.runtime.bindings["arbitrary"] = h.binding
    assert h.runtime.resolve_owner(CTX.owner_record_id) == CTX
    h.mutate(control_key("owner", "owner"), epoch=2)
    assert h.runtime.resolve_owner(CTX.owner_record_id).epoch == 2
    with pytest.raises(Problem):
        h.runtime.resolve_owner("other-owner")


def test_bootstrap_is_explicit_and_cannot_relabel_the_same_binding(h):
    assert h.store.read_snapshot((control_key("grant", "grant"),))[control_key("grant", "grant")] is None
    with pytest.raises(Problem):
        h.runtime.bootstrap({})
    with pytest.raises(Problem):
        h.runtime.bootstrap({"binding-one": h.policy.model_copy(update={"binding_digest": "f" * 64})})
    with pytest.raises(Problem):
        h.runtime.bootstrap({"binding-one": h.policy.model_copy(update={"max_commands": 63})})


def test_bounded_command_inventory_keeps_existing_reconciliation_available(h):
    from shared_context import behavior_wire as wire
    from test_behavior_api import METRICS

    h.mutate(runtime_key("policy", "binding-one"), max_commands=1)
    command = wire.SourcePrepare(request_id="one", version=1, original=METRICS)
    assert h.post("/sources/metrics/prepare", command).status_code == 200
    assert h.post("/sources/metrics/commit", wire.Command(request_id="one")).status_code == 200
    assert h.post("/sources/metrics/prepare", command.model_copy(update={"request_id": "two", "version": 2})).status_code == 429
    assert h.post("/sources/metrics/reconcile", wire.Command(request_id="one")).status_code == 200


def test_saved_source_plan_cannot_bypass_a_current_visibility_change(h):
    from shared_context import behavior_wire as wire
    from test_behavior_api import METRICS

    prepared = wire.SourcePrepare(request_id="old-scope", version=1, original=METRICS)
    assert h.post("/sources/metrics/prepare", prepared).status_code == 200
    h.mutate(control_key("policy", "policy"), revision=2)
    h.mutate(runtime_key("policy", "binding-one"), operation_policy_revision=2, source_visibility="private")
    assert h.post("/sources/metrics/commit", wire.Command(request_id="old-scope")).status_code == 403
