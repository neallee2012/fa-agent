"""ClaimedSdkRecovery.execute: every gate before the one external stop refuses and sends no stop; reads keep their order.

Real native producers, offline and without the pinned runtime; each case changes one thing at the named moment.
"""
from datetime import timedelta

import pytest

from shared_context import behavior_recovery as recovery_module
from shared_context.behavior_operations import control_key
from shared_context.behavior_recovery import PlatformStopObservation, recovery_key, stop_endpoint
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from test_behavior_assistant_acquire_conflicts import installed_assistant, native_acquire
from test_behavior_finished_result_recovery import durable_result, service_for as finished_service
from test_behavior_multi_model_recovery import mixed_models, service_for as multi_service
from test_behavior_pre_assignment_recovery import service_for as pre_assignment_service
from test_behavior_recovery_snapshot_refusals import edited, recovery
from test_behavior_settlement import write
from test_m3_auth import jwk_client, signing_key


STALE = ("RECOVERY_PLAN_STALE", "original authority data changed")
FENCED = ("RECOVERY_STOP_FENCED", "original cancellation or stop reservation changed")
PREDICATES = ("RECOVERY_STOP_FENCED", "missing-assignment predicates changed before stop reservation")
MOVED = ("RECOVERY_PLAN_STALE", "fenced attempt changed before the external stop")
FINISHED = ("RECOVERY_PLAN_STALE", "finished-result authority changed before stop")
ARTIFACT = ("RECOVERY_FINISHED_ARTIFACT_INVALID", "original M3 version/ETag/SHA/bytes differ")
REVIEWED = ("RECOVERY_FINISHED_RESULT_INVALID", "reviewed original finish references changed")
EXPIRED = ("RECOVERY_PERMISSION_EXPIRED", "the independent operator permission expired")
UNKNOWN = ("RECOVERY_STOP_UNKNOWN", "original platform stop is unresolved; never resend")
UNVERIFIED = ("RECOVERY_STOP_UNVERIFIED", "a bound terminal stop response, not idle/202, is required")
INTERLEAVED = ("RECOVERY_PLAN_STALE", "complete original inventory changed outside verified recovery commits")


def approved(service, principal):
    return service.authorize(principal, service.plan(principal), "explicit-cancel")


def refusal(service, principal, approval):
    with pytest.raises(Problem) as error:
        service.execute(principal, approval)
    body = error.value.body["error"]
    return body["code"], body["message"]


def row(service, kind):
    name = recovery_key(kind, service.scope.recovery_id)
    return service.store.read_snapshot((name,))[name]


def budget_key(c):
    return control_key("budget", c.binding.owner.root_budget_id)


def spent(data):
    return {**data, "remaining_units": data["remaining_units"] - 1}


def spend_budget(c):
    name = budget_key(c)
    write(c.competitor, name, spent(c.store.read_snapshot((name,))[name].payload["data"]))


def around_reservation(monkeypatch, service, *, before=None, rows=None, after=None):
    wanted, real = recovery_key("stop-intent", service.scope.recovery_id), service.store.transact

    def transact(request, decide, **options):
        if request.request_id != wanted:
            return real(request, decide, **options)
        if before is not None:
            before()
        receipt = real(request, decide if rows is None else lambda read: decide(rows(read)), **options)
        if after is not None:
            after()
        return receipt

    monkeypatch.setattr(service.store, "transact", transact)


def test_a_changed_snapshot_refuses_before_the_fence(recovery):
    c, service, principal, calls = recovery
    approval = approved(service, principal)
    spend_budget(c)
    assert refusal(service, principal, approval) == STALE
    assert calls == [] and not service.workers.inspect(service.scope.job_id).cancel_requested


@pytest.mark.parametrize("change", [
    {"stop_credit": 0}, {"cancel_requested": False}, lambda record: edited(record, {"binding": {
        **record.payload["data"]["binding"], "generation": record.payload["data"]["binding"]["generation"] + 1}}),
], ids=["credit-spent", "not-cancelled", "another-binding"])
def test_each_reservation_rule_refuses(recovery, monkeypatch, change):
    _, service, principal, calls = recovery
    approval = approved(service, principal)
    job = worker_key("job", service.scope.job_id)
    around_reservation(monkeypatch, service, rows=lambda read: {**read, job: edited(read[job], change)})
    assert refusal(service, principal, approval) == FENCED
    assert calls == [] and row(service, "stop-intent") is None


def edited_budget(c):
    name = budget_key(c)
    return lambda read: {**read, name: edited(read[name], spent(read[name].payload["data"]))}


@pytest.fixture
def multi(mixed_models):
    service, calls = multi_service(mixed_models)
    return mixed_models.c, service, mixed_models.c.principal, calls


def test_the_unfenced_image_is_rechecked_at_reservation(multi, monkeypatch):
    c, service, principal, calls = multi
    approval = approved(service, principal)
    around_reservation(monkeypatch, service, rows=edited_budget(c))
    assert refusal(service, principal, approval) == PREDICATES
    assert calls == [] and row(service, "stop-intent") is None


@pytest.mark.parametrize("native_acquire", ["before-assignment"], indirect=True)
def test_a_pre_assignment_image_is_rechecked_at_reservation(native_acquire, monkeypatch):
    c = native_acquire
    service, calls = pre_assignment_service(c)
    approval = approved(service, c.principal)
    around_reservation(monkeypatch, service, rows=edited_budget(c))
    assert refusal(service, c.principal, approval) == PREDICATES
    assert calls == [] and row(service, "stop-intent") is None


@pytest.mark.parametrize("recovery", [None, "pre-sdk"], indirect=True)
def test_a_plan_outside_the_inventory_recheck_has_no_unfenced_image_to_compare(recovery, monkeypatch):
    c, service, principal, calls = recovery
    approval = approved(service, principal)
    around_reservation(monkeypatch, service, rows=edited_budget(c))
    assert service.execute(principal, approval)["state"] == "cancelled"
    assert len(calls) == 1


def test_an_approval_expiring_at_the_reservation_refuses(recovery, monkeypatch):
    c, service, principal, calls = recovery
    approval = approved(service, principal)
    around_reservation(monkeypatch, service, before=lambda: c.clock.__setitem__(0, approval.expires_at))
    assert refusal(service, principal, approval) == EXPIRED
    assert calls == [] and row(service, "stop-intent") is None


def test_a_reservation_another_caller_made_is_reconciled_without_a_stop(recovery, monkeypatch):
    _, service, principal, calls = recovery
    approval = approved(service, principal)
    wanted, real = recovery_key("stop-intent", service.scope.recovery_id), service.store.transact
    monkeypatch.setattr(service.store, "transact", lambda request, decide, **options: (
        None if request.request_id == wanted else real(request, decide, **options)))
    result = service.execute(principal, approval)
    assert result["stop_resent"] is False and result["stop-observation"] is False and calls == []


def test_a_saved_reservation_is_reconciled_and_the_stop_is_never_resent(recovery):
    _, service, principal, calls = recovery
    approval = approved(service, principal)
    real = service.stop

    def unknown(target, deadline):
        real(target, deadline)
        raise TimeoutError("untrusted provider text")

    service.stop = unknown
    assert refusal(service, principal, approval) == UNKNOWN
    result = service.execute(principal, approval)
    assert result["stop_resent"] is False and result["stop-intent"] is True and result["stop-observation"] is False
    assert len(calls) == 1


def test_a_run_stopped_after_the_fence_compares_the_unfenced_image_on_its_retry(recovery, monkeypatch):
    _, service, principal, calls = recovery
    approval = approved(service, principal)

    def interrupted():
        raise Problem(503, "SYNTHETIC_UNAVAILABLE", "synthetic")

    with monkeypatch.context() as patch:
        around_reservation(patch, service, before=interrupted)
        assert refusal(service, principal, approval) == ("SYNTHETIC_UNAVAILABLE", "synthetic")
    assert service.workers.inspect(service.scope.job_id).cancel_requested and calls == []
    assert service.execute(principal, approval)["state"] == "cancelled"
    assert len(calls) == 1


def test_a_change_after_the_reservation_refuses_before_the_stop(recovery, monkeypatch):
    c, service, principal, calls = recovery
    approval = approved(service, principal)
    around_reservation(monkeypatch, service, after=lambda: spend_budget(c))
    assert refusal(service, principal, approval) == MOVED
    assert calls == [] and row(service, "stop-intent") is not None


def test_an_approval_expiring_after_the_fenced_snapshot_refuses_before_the_stop(recovery, monkeypatch):
    c, service, principal, calls = recovery
    approval = approved(service, principal)
    real = service._snapshot

    def snapshot(*args, **options):
        result = real(*args, **options)
        if options.get("after_reservation"):
            c.clock[0] = approval.expires_at
        return result

    monkeypatch.setattr(service, "_snapshot", snapshot)
    assert refusal(service, principal, approval) == EXPIRED
    assert calls == [] and row(service, "stop-intent") is not None


def test_an_unrelated_commit_after_the_reservation_refuses_before_the_stop(multi, monkeypatch):
    c, service, principal, calls = multi
    approval = approved(service, principal)
    around_reservation(monkeypatch, service, after=lambda: write(c.competitor, control_key("synthetic", "row"), {"a": 1}))
    assert refusal(service, principal, approval) == INTERLEAVED
    assert calls == []


@pytest.fixture
def finished(durable_result):
    service, stops = finished_service(durable_result)
    return durable_result.c, service, durable_result.c.principal, stops


def test_finished_artifact_is_read_again_before_the_stop(finished, monkeypatch):
    c, service, principal, stops = finished
    approval = approved(service, principal)
    artifacts, real = c.authority.artifacts, c.authority.artifacts.download
    armed = []

    def download(*args, **options):
        record, content = real(*args, **options)
        return record, b"another content" if armed else content

    monkeypatch.setattr(artifacts, "download", download)
    around_reservation(monkeypatch, service, after=lambda: armed.append(True))
    assert refusal(service, principal, approval) == ARTIFACT
    assert stops == [] and row(service, "stop-intent") is not None


def test_finished_references_are_compared_with_the_review_before_the_stop(finished, monkeypatch):
    c, service, principal, stops = finished
    approval = approved(service, principal)
    artifacts, read_version, read_bytes = c.authority.artifacts, c.authority.artifacts.version, c.authority.artifacts.download
    armed, saved = [], []

    def version(*args, **options):
        record = read_version(*args, **options)
        return record.model_copy(update={"blob": record.blob.model_copy(update={"etag": "another-etag"})}) if armed else record

    def download(*args):
        # The real download checks the ETag through ``version``, so the changed one is only seen by the proof.
        if not armed:
            saved[:] = [read_bytes(*args)]
        return saved[0]

    monkeypatch.setattr(artifacts, "version", version)
    monkeypatch.setattr(artifacts, "download", download)
    around_reservation(monkeypatch, service, after=lambda: armed.append(True))
    assert refusal(service, principal, approval) == REVIEWED
    assert stops == []


def test_finished_authority_changing_during_that_read_refuses_before_the_stop(finished, monkeypatch):
    c, service, principal, stops = finished
    approval = approved(service, principal)
    artifacts, real = c.authority.artifacts, c.authority.artifacts.download
    armed = []

    def download(*args, **options):
        result = real(*args, **options)
        if armed.count(True) == 1:
            armed.append(False)
            write(c.competitor, control_key("synthetic", "row"), {"a": 1})
        return result

    monkeypatch.setattr(artifacts, "download", download)
    around_reservation(monkeypatch, service, after=lambda: armed.append(True))
    assert refusal(service, principal, approval) == FINISHED
    assert stops == []


def unavailable(error):
    def stop(_target, _deadline):
        raise error
    return stop


def test_an_unknown_stop_is_never_called_a_result(recovery):
    _, service, principal, _ = recovery
    approval = approved(service, principal)
    service.stop = unavailable(TimeoutError("untrusted provider text"))
    with pytest.raises(Problem) as error:
        service.execute(principal, approval)
    assert error.value.status == 503 and error.value.__suppress_context__ is True
    assert (error.value.body["error"]["code"], error.value.body["error"]["message"]) == UNKNOWN
    assert row(service, "stop-intent") is not None and row(service, "stop-observation") is None


def test_a_problem_from_the_stop_keeps_its_own_answer(recovery):
    _, service, principal, _ = recovery
    approval = approved(service, principal)
    refused = Problem(409, "SYNTHETIC_STOP_REFUSED", "synthetic")
    service.stop = unavailable(refused)
    with pytest.raises(Problem) as error:
        service.execute(principal, approval)
    assert error.value is refused and row(service, "stop-observation") is None


def test_a_foreign_stop_is_not_saved(recovery):
    c, service, principal, _ = recovery
    approval = approved(service, principal)
    real = service.stop
    service.stop = lambda target, deadline: real(target, deadline).model_copy(update={"credential_client_id": "another"})
    assert refusal(service, principal, approval) == UNVERIFIED
    assert row(service, "stop-observation") is None and row(service, "stop-intent") is not None


def test_the_one_stop_binds_the_plan_and_is_saved_as_observed(recovery, monkeypatch):
    c, service, principal, _ = recovery
    approval = approved(service, principal)
    seen = []

    def stop(target, deadline):
        seen.append((target, deadline))
        return PlatformStopObservation(
            target=target, endpoint=stop_endpoint(target), status_code=204, observed_at=c.clock[0] + timedelta(seconds=1),
            credential_client_id=c.config.identity.managed_identity_client_id)

    service.stop = stop
    saved = []
    finish = service.finish
    monkeypatch.setattr(service, "finish", lambda *args: saved.append(args) or finish(*args))
    assert service.execute(principal, approval)["state"] == "cancelled"
    assert seen == [(approval.plan.target, approval.expires_at)] and saved == [(principal, approval)]
    data = row(service, "stop-observation").payload["data"]
    assert data == stop(*seen[0]).model_dump(mode="json")
    intent = row(service, "stop-intent").payload["data"]
    assert intent["authorization"] == approval.model_dump(mode="json") and intent["fenced_at"]


def traced(monkeypatch, service):
    """Each read and write execute makes itself, in order; what a step does inside is that step's own test."""
    events, inside = [], []
    names = {recovery_key(kind, service.scope.recovery_id): kind for kind in ("stop-intent", "stop-observation")}

    def step(owner, name, label):
        real = getattr(owner, name)

        def call(*args, **options):
            if not inside:
                events.append(label(*args, **options) if callable(label) else label)
            inside.append(True)
            try:
                return real(*args, **options)
            finally:
                inside.pop()

        monkeypatch.setattr(owner, name, call)

    for owner, name, label in (
        (service.native, "launch", "launch"), (service.store, "read_inventory", "inventory"),
        (service.workers, "inspect", "inspect"), (service, "clock", "clock"), (service, "_cancel", "cancel"),
        (service, "_authorization", "authorization"), (service, "_finished_live", "live"), (service, "stop", "stop"),
        (recovery_module, "_multi_models", "models"), (recovery_module, "_multi_inventory", "multi"),
        (service, "_snapshot", lambda *_, **options: "snapshot:" + ",".join(sorted(k for k, v in options.items() if v))),
        (service.store, "transact", lambda request, _decide, **options: "transact:" + names[request.request_id] + (
            "+guard" if "expected_guard" in options else "")),
    ):
        step(owner, name, label)
    monkeypatch.setattr(service, "finish", lambda *_: events.append("finish"))
    return events


AFTER_RESERVATION = "snapshot:after_fence,after_reservation"
BEFORE_FENCE = ["authorization", "launch", "inspect", "snapshot:", "cancel", "clock"]
OBSERVED = ["stop", "transact:stop-observation", "finish"]


def traced_run(service, principal, monkeypatch):
    approval = approved(service, principal)
    events = traced(monkeypatch, service)
    service.execute(principal, approval)
    return events


@pytest.mark.parametrize("recovery", [None, "pre-sdk"], indirect=True)
def test_a_plain_plan_reads_in_one_fixed_order(recovery, monkeypatch):
    _, service, principal, _ = recovery
    assert traced_run(service, principal, monkeypatch) == [
        *BEFORE_FENCE, "transact:stop-intent", "launch", AFTER_RESERVATION, "authorization", *OBSERVED]


def test_a_multi_model_plan_reads_in_one_fixed_order(multi, monkeypatch):
    _, service, principal, _ = multi
    assert traced_run(service, principal, monkeypatch) == [
        *BEFORE_FENCE, "inventory", "models", "multi", "transact:stop-intent+guard", "launch", AFTER_RESERVATION,
        "authorization", "inventory", "multi", *OBSERVED]


def test_a_finished_plan_reads_in_one_fixed_order(finished, monkeypatch):
    _, service, principal, _ = finished
    assert traced_run(service, principal, monkeypatch) == [
        *BEFORE_FENCE, "inventory", "models", "multi", "transact:stop-intent+guard", "launch", AFTER_RESERVATION,
        "authorization", "inventory", "launch", "live", "multi", "inventory", *OBSERVED]
