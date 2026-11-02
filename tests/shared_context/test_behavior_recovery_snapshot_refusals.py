"""ClaimedSdkRecovery._snapshot: every check refuses, and the first failing check names the error.

Rows are edited at the read boundary and go through the real parsers, so one fixture covers every case.
"""
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from history_forgery import Forgery
from shared_context.behavior_assistant import key as assistant_key
from shared_context.behavior_foundry import native_key
from shared_context.behavior_operations import control_key
from shared_context.behavior_recovery import PreSdkRecoveryScope
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from shared_context.records import Record
from shared_context.source_materials import digest
from test_behavior_assistant_acquire_conflicts import installed_assistant, native_acquire
from test_behavior_assistant_claim_boundaries import reviewed
from test_behavior_assistant_sse import stream
from test_behavior_multi_model_recovery import mixed_models, service_for as multi_service
from test_behavior_pre_assignment_recovery import service_for as pre_assignment_service
from test_m3_auth import jwk_client, signing_key


CHANGED = "RECOVERY_ORIGINAL_CHANGED"
BINDING = (CHANGED, "original claimed binding is missing")
SESSION = (CHANGED, "original SDK session/model intent is missing")
MODEL = (CHANGED, "original SDK model intent is missing")
CONSUMED = (CHANGED, "the exact consumed model intent differs")
UNACQUIRED = (CHANGED, "the original assignment is not an unacquired SDK start")
ACQUIRED = (CHANGED, "the original SDK acquire receipt must remain absent")
ATTEMPT = (CHANGED, "the exact failed SDK attempt or its reservations changed")
FRESH_CLAIM = (CHANGED, "one exact committed fresh native claim is required")
RETAINED = (CHANGED, "assignment/acquire or SDK material is already retained")
MODEL_SET = ("RECOVERY_MODEL_SET_CHANGED", "complete original model/session set differs")
MISSING, PRESENT = object(), object()


@pytest.fixture
def recovery(native_acquire, request):
    """A failed single-model or pre-SDK attempt from the real native producers, offline and without the pinned runtime."""
    c = native_acquire
    if getattr(request, "param", None) == "pre-sdk":
        service, calls = pre_assignment_service(c)
        service.scope = PreSdkRecoveryScope(**{**service.scope.model_dump(), "recovery_phase": "before_sdk_acquire"})
        return c, service, c.principal, calls
    reviewed(c)
    c.composition.assistant.provider.transport = httpx.MockTransport(lambda _: stream([{}], finish_reason="stop"))
    c.composition.assistant.provider.credential = SimpleNamespace(get_token=lambda _: SimpleNamespace(token="SYNTHETIC_PROVIDER"))
    c.hosted.acquire()
    body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "Synthetic metrics"}],
            "stream": True, "max_tokens": 1024}
    c.hosted.reserve_model(digest(body))
    c.hosted.model(httpx.Request("POST", "https://synthetic.invalid/model", json=body))
    c.hosted.stopped("sdk_failed")
    service, calls = multi_service(SimpleNamespace(c=c), digest(body))
    return c, service, c.principal, calls


def edited(record, change):
    if change is MISSING:
        return None
    if change is PRESENT:
        return record or Record({"data": {}}, "synthetic")
    if callable(change):
        return change(record)
    return Record({**record.payload, "data": {**record.payload["data"], **change}}, record.etag)


@contextmanager
def stored(monkeypatch, store, changes):
    read = store.read_snapshot

    def snapshot(keys):
        rows = dict(read(keys))
        for key in set(rows) & set(changes):
            rows[key] = edited(rows[key], changes[key])
        return rows

    with monkeypatch.context() as patch:
        patch.setattr(store, "read_snapshot", snapshot)
        yield


def refusal(service, principal):
    with pytest.raises(Problem) as error:
        service.plan(principal)
    body = error.value.body["error"]
    return body["code"], body["message"]


def names(h, service):
    binding = h.native.workers.inspect(service.scope.job_id).binding
    sid = assistant_key("session-id", binding.start_request_id)
    model = service.scope.model_request_sha256
    return SimpleNamespace(
        job=worker_key("job", service.scope.job_id), launch=native_key("launch", service.scope.launch_id),
        target=native_key("target", h.config.active_targets[0].executor_id),
        pool=worker_key("pool", h.native.workers.contract.workspace_id),
        session=assistant_key("session", sid), acquire=assistant_key("acquire", sid, "sdk-start"),
        model=None if model is None else assistant_key("model-intent", sid, model),
        artifact=assistant_key("artifact", binding.work_id), result=assistant_key("result", binding.work_id),
        owner=control_key("owner", binding.owner.owner_record_id),
    )


def data(store, key):
    return store.read_snapshot((key,))[key].payload["data"]


def test_each_attempt_check_refuses_alone(recovery, monkeypatch):
    h, service, principal, calls = recovery
    n = names(h, service)
    job, launch, slot = data(h.store, n.job), data(h.store, n.launch), data(h.store, n.pool)
    session, reservation = data(h.store, n.session), data(h.store, n.target)
    other_target = {**launch["target"], "session_id": "another-session"}
    other_binding = {**job["binding"], "generation": job["binding"]["generation"] + 1}
    cases = {
        "job binding": {n.job: {"binding": other_binding}, n.session: {"execution": other_binding},
                        n.pool: {"slots": [{**slot["slots"][0], "binding": other_binding}]}},
        "job state": {n.job: {"state": "queued"}},
        "phase": {n.launch: {"phase": "post_unknown"}},
        "no invocation": {n.launch: {"native_invocation_id": None}},
        "job id": {n.job: {"spec": {**job["spec"], "job_id": "another-job"}}, n.pool: {"pending": ["another-job"]}},
        "launch id": {n.launch: {"launch_id": "another-launch"}, n.target: {"active_launch_id": "another-launch"}},
        "workload": {n.launch: {"workload_version": "checksum-count-v1"}},
        "inactive target": {n.launch: {"target": other_target}, n.target: {"target": other_target}},
        "reservation owner": {n.target: {"active_launch_id": "another-launch"}},
        "reservation generation": {n.target: {"generation": reservation["generation"] + 1}},
        "reservation target": {n.target: {"target": other_target}},
        "children": {n.job: {"children": ["child-job"]}},
        "obligation": {n.job: {"spec": {**job["spec"], "obligation_id": "another-obligation"}}},
        "definition": {n.launch: {"definition": {**launch["definition"], "criteria_version": "another"}}},
        "invocation": {n.job: {"native_execution_id": "another-invocation"}},
        "session execution": {n.session: {"execution": other_binding}},
        "artifact": {n.artifact: PRESENT},
        "result": {n.result: PRESENT},
        "slot": {n.pool: {"slots": [{**slot["slots"][0], "binding": None}]}},
        "pending": {n.pool: {"pending": []}},
        "completions": {n.pool: {"pending": [service.scope.job_id], "completions": ["another-job"]}},
        "fenced": {n.job: {"cancel_requested": True}},
        "cancel credit": {n.job: {"cancel_credit": 0}},
        "stop credit": {n.job: {"stop_credit": 0}},
        "completion credit": {n.job: {"completion_credit": 0}},
    }
    service.plan(principal)
    for name, change in cases.items():
        with stored(monkeypatch, h.store, change):
            assert refusal(service, principal) == ATTEMPT, name
    assert calls == []


def test_a_second_slot_refuses_even_when_the_first_holds_the_attempt(recovery, monkeypatch):
    h, service, principal, _ = recovery
    real = service.workers._pool
    monkeypatch.setattr(service.workers, "_pool", lambda rows: (
        lambda pool: SimpleNamespace(slots=(*pool.slots, pool.slots[0]), pending=pool.pending,
                                     completions=pool.completions))(real(rows)))
    assert refusal(service, principal) == ATTEMPT


@pytest.mark.parametrize("state", ["starting", "running"])
def test_live_job_states_and_the_invocation_itself_still_plan(recovery, monkeypatch, state):
    h, service, principal, _ = recovery
    n = names(h, service)
    invocation = data(h.store, n.launch)["native_invocation_id"]
    with stored(monkeypatch, h.store, {n.job: {"state": state, "native_execution_id": invocation}}):
        assert service.plan(principal).snapshot_sha256


def test_each_consumed_model_check_refuses(recovery, monkeypatch):
    h, service, principal, _ = recovery
    n = names(h, service)
    for name, change in {
        "session state": {n.session: {"state": "running"}},
        "session reason": {n.session: {"reason": None}},
        "session requests": {n.session: {"model_requests": 2}},
        "unconsumed": {n.model: {"consumed": False}},
        "another intent": {n.model: {"request_sha256": "e" * 64}},
    }.items():
        with stored(monkeypatch, h.store, change):
            assert refusal(service, principal) == CONSUMED, name


def test_two_faults_name_the_earlier_check(recovery, monkeypatch):
    h, service, principal, _ = recovery
    n = names(h, service)
    moved = {n.job: {"state": "queued"}}
    for name, change, expected in (
        ("job row before pool", {n.job: MISSING, n.pool: MISSING}, ("WORKER_RECORD_MISSING", "bound worker authority record is missing")),
        ("pool before target", {n.pool: MISSING, n.target: MISSING}, ("WORKER_POOL_INVALID", "one exact work or legacy pool is required")),
        ("target before binding", {n.target: MISSING, n.job: {"binding": None}},
         ("NATIVE_RECORD_MISSING", "required bound native authority record is missing")),
        ("binding before session", {n.job: {"binding": None}, n.session: MISSING}, BINDING),
        ("binding before attempt", {n.job: {"binding": None, "state": "queued"}}, BINDING),
        ("session before attempt", {n.session: MISSING, **moved}, SESSION),
        ("model row before session state", {n.model: MISSING, n.session: {"state": "running"}}, MODEL),
        ("model row before attempt", {n.model: MISSING, **moved}, MODEL),
        ("consumed before attempt", {n.session: {"state": "running"}, **moved}, CONSUMED),
        ("attempt before current authority", {n.owner: {"active": False}, **moved}, ATTEMPT),
        ("current authority alone", {n.owner: {"active": False}},
         ("NATIVE_AUTHORITY_FENCED", "current owner/grant/policy association differs")),
    ):
        with stored(monkeypatch, h.store, change):
            assert refusal(service, principal) == expected, name


def test_fence_and_reservation_flags_read_the_job_credits(recovery, monkeypatch):
    h, service, principal, _ = recovery
    n = names(h, service)
    plan = service.plan(principal)
    launch = service.native.launch(service.scope.launch_id)
    model = service.scope.model_request_sha256
    fenced = {"cancel_requested": True, "cancel_credit": 0}
    with stored(monkeypatch, h.store, {n.job: fenced}):
        assert service._snapshot(launch, model, after_fence=True)[1] == plan.snapshot_sha256
        with pytest.raises(Problem, match=ATTEMPT[1]):
            service._snapshot(launch, model, after_fence=True, after_reservation=True)
    with stored(monkeypatch, h.store, {n.job: {**fenced, "stop_credit": 0}}):
        assert service._snapshot(launch, model, after_fence=True, after_reservation=True)[1] == plan.snapshot_sha256
    with pytest.raises(Problem, match=ATTEMPT[1]):
        service._snapshot(launch, model, after_fence=True)
    with pytest.raises(Problem, match=ATTEMPT[1]):
        service._snapshot(launch, model, after_reservation=True)


def receipt_present(monkeypatch, store, key):
    read = store._read
    monkeypatch.setattr(store, "_read", lambda wanted, model, *fetched: (
        ("synthetic", "etag") if wanted == "behavior:v1:receipt:" + key else read(wanted, model, *fetched)))


@pytest.mark.parametrize("recovery", ["pre-sdk"], indirect=True)
def test_each_unacquired_check_refuses(recovery, monkeypatch):
    h, service, principal, _ = recovery
    n = names(h, service)
    assignment = data(h.store, n.session)["assignment"]
    start = data(h.store, n.launch)["start"]
    for name, change in {
        "state": {n.session: {"state": "running"}},
        "reason": {n.session: {"reason": "sdk_failed"}},
        "context": {n.session: {"context_read": True}},
        "requests": {n.session: {"model_requests": 1}},
        "resume": {n.session: {"assignment": {**assignment, "resume": True}}},
        "checkpoint": {n.launch: {"start": {**start, "checkpoint": "Y2hlY2twb2ludA=="}}},
    }.items():
        with stored(monkeypatch, h.store, change):
            assert refusal(service, principal) == UNACQUIRED, name


@pytest.mark.parametrize("recovery", ["pre-sdk"], indirect=True)
def test_pre_sdk_two_faults_name_the_earlier_check(recovery, monkeypatch):
    h, service, principal, _ = recovery
    n = names(h, service)
    with stored(monkeypatch, h.store, {n.session: MISSING}):
        assert refusal(service, principal) == SESSION
    receipt_present(monkeypatch, h.store, n.acquire)
    with stored(monkeypatch, h.store, {n.session: {"state": "running"}}):
        assert refusal(service, principal) == UNACQUIRED
    with stored(monkeypatch, h.store, {n.job: {"state": "queued"}}):
        assert refusal(service, principal) == ACQUIRED


@pytest.mark.parametrize("native_acquire", ["before-assignment"], indirect=True)
def test_pre_assignment_inventory_checks_speak_before_the_job(native_acquire, monkeypatch):
    c = native_acquire
    service, stops = pre_assignment_service(c)
    sid = assistant_key("session-id", c.starts[0].binding.start_request_id)
    job_key = worker_key("job", c.binding.job_id)
    clean = c.store.read_inventory()
    assert service.plan(c.principal).snapshot_sha256
    moved = Forgery(clean).data(job_key, lambda job: {**job, "state": "queued"})
    monkeypatch.setattr(c.store, "read_inventory", moved.inventory)
    assert refusal(service, c.principal) == ATTEMPT
    moved.copy(job_key, assistant_key("archive", sid))
    assert refusal(service, c.principal) == RETAINED
    assert stops == []


@pytest.mark.parametrize("native_acquire", ["before-assignment"], indirect=True)
def test_resumed_start_without_assignment_refuses_as_no_fresh_claim(native_acquire):
    c = native_acquire
    service, _ = pre_assignment_service(c)
    launch = c.native.launch(c.launch_id)
    resumed = launch.model_copy(update={"start": launch.start.model_copy(update={"checkpoint": b"checkpoint"})})
    with pytest.raises(Problem, match=FRESH_CLAIM[1]):
        service._snapshot(resumed, None)


def test_multi_model_set_speaks_before_the_job(mixed_models, monkeypatch):
    h, c = mixed_models, mixed_models.c
    service, calls = multi_service(h)
    job_key = worker_key("job", c.binding.job_id)
    sid = c.assignment.session_id
    extra = assistant_key("model-intent", sid, "e" * 64)
    moved = Forgery(c.store.read_inventory()).data(job_key, lambda job: {**job, "state": "queued"})
    monkeypatch.setattr(c.store, "read_inventory", moved.inventory)
    assert refusal(service, c.principal) == ATTEMPT
    moved.copy(assistant_key("model-intent", sid, h.first), extra).data(
        extra, lambda intent: {**intent, "request_sha256": "e" * 64}, later=True)
    assert refusal(service, c.principal) == MODEL_SET
    assert calls == []
