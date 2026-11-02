"""verify_platform_stop: each proof rule refuses, and the first failing rule names the error.

A forged proof also rewrites everything sealed from it, so only the named rule can refuse.
"""
import copy
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from history_forgery import Forgery, _material as inline_material
from shared_context.behavior_assistant import key
from shared_context.behavior_foundry import _NativeEvidence, native_key
from shared_context.behavior_recovery import _PlatformProof, recovery_key, verify_platform_stop
from shared_context.errors import Problem
from shared_context.records import Record
from shared_context.source_materials import digest
from test_behavior_assistant_acquire_conflicts import installed_assistant, native_acquire
from test_behavior_finished_result_recovery import durable_result, service_for as finished_service
from test_behavior_multi_model_recovery import mixed_models, service_for as multi_service
from test_behavior_pre_assignment_recovery import service_for as pre_assignment_service
from test_behavior_recovery_snapshot_refusals import MISSING, recovery, stored
from test_m3_auth import jwk_client, signing_key


INVALID = "RECOVERY_PLATFORM_PROOF_INVALID"
PROOF = (INVALID, "permanent original cancellation/platform-stop proof differs")
PROVENANCE = (INVALID, "exact immutable cancellation/stop provenance is missing")
RECEIPTS = (INVALID, "original cancellation and stop receipts differ")
CLAIM = (INVALID, "original native claim receipt differs")
MODEL_SET = (INVALID, "original model set changed after stop")
IMAGE = (INVALID, "original model intent image changed after stop")
LATE = (INVALID, "late model consume receipt")
MODEL_RECEIPT = (INVALID, "original model receipt changed after stop")
FINISHED_ROW = (INVALID, "original finished row changed after stop")
FINISHED_CLAIM = (INVALID, "original finished claim changed")
FINISHED_RECEIPT = (INVALID, "original finish/acquire/assignment receipt changed")


def sealed(native, recovery_id, launch_id):
    launch = native.launch(launch_id)
    proof_key = native_key("platform-stop", launch_id)
    evidence_key = native_key("evidence", launch.sealed_evidence_id)
    rows = native.store.read_snapshot((proof_key, evidence_key))
    saved = native._row(rows, evidence_key, _NativeEvidence)
    return SimpleNamespace(native=native, store=native.store, launch=launch, saved=saved, row=rows[proof_key],
                           recovery_id=recovery_id)


def refusal(w, *, launch=None, saved=None, row=None):
    with pytest.raises(Problem) as error:
        verify_platform_stop(w.native, launch or w.launch, saved or w.saved, row or w.row)
    body = error.value.body["error"]
    return body["code"], body["message"]


def forged_proof(w, change, *, result=None, **evidence_updates):
    """The stored proof after change(body), resealed: its row, material, evidence proof and report digest agree."""
    body = copy.deepcopy(w.row.payload["data"])
    change(body)
    body = _PlatformProof.model_validate_json(json.dumps(body)).model_dump(mode="json")
    material = inline_material(body)
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    evidence = w.saved.evidence
    finished = "finished_result" in body["intent"]["authorization"]["plan"]
    sealed_result = evidence.result if finished else evidence.result.model_copy(update={"proof": raw, **(result or {})})
    saved = w.saved.model_copy(update={"report_digest": digest(body), "evidence": evidence.model_copy(update={
        "proof": raw, "result": sealed_result, **evidence_updates})})
    return {"row": SimpleNamespace(payload={"data": body, "material": material}), "saved": saved}


def in_plan(**updates):
    return lambda body: body["intent"]["authorization"]["plan"].update(updates)


def in_observation(**updates):
    return lambda body: body["observation"].update(updates)


def later(value, seconds):
    return (datetime.fromisoformat(value.replace("Z", "+00:00")) + timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def single(recovery):
    _, service, principal, _ = recovery
    service.execute(principal, service.authorize(principal, service.plan(principal), "explicit-cancel"))
    return sealed(service.native, service.scope.recovery_id, service.scope.launch_id)


def test_each_binding_rule_refuses_alone(single):
    w = single
    plan = w.row.payload["data"]["intent"]["authorization"]["plan"]
    observation = w.row.payload["data"]["observation"]
    verify_platform_stop(w.native, w.launch, w.saved, w.row)
    other_target = {**observation["target"], "session_id": "another-session"}

    def approved_late(body):
        body["intent"]["authorization"]["approved_at"] = later(body["intent"]["fenced_at"], 1)

    def observed_early(body):
        body["observation"]["observed_at"] = later(body["intent"]["fenced_at"], -1)

    def observed_at_expiry(body):
        body["observation"]["observed_at"] = body["intent"]["authorization"]["expires_at"]

    for name, change in {
        "configuration": in_plan(configuration_sha256="e" * 64),
        "operator": in_plan(operator_oid="another-operator"),
        "credential": in_observation(credential_client_id="another-client"),
        "target": in_observation(target=other_target),
        "endpoint": in_observation(endpoint="https://untrusted.invalid"),
        "success with an error": in_observation(status_code=204, error_code="session_already_stopped"),
        "conflict without already-stopped": in_observation(status_code=409, error_code=None),
        "approved after the fence": approved_late,
        "observed before the fence": observed_early,
        "observed at expiry": observed_at_expiry,
    }.items():
        assert refusal(w, **forged_proof(w, change)) == PROOF, name
    assert refusal(w, row=SimpleNamespace(payload={**w.row.payload, "material": inline_material({"another": True})})) == PROOF


def test_plan_identity_rules_refuse_with_the_seal_rewritten_to_match(single, monkeypatch):
    w = single
    plan = w.row.payload["data"]["intent"]["authorization"]["plan"]
    moved_launch = forged_proof(w, in_plan(launch_id="another-launch"),
                                result={"evidence_id": native_key("platform-stop-result", "another-launch")})
    assert refusal(w, **moved_launch) == PROOF
    moved_invocation = forged_proof(w, in_plan(native_invocation_id="another-invocation"),
                                    native_execution_id="another-invocation")
    assert refusal(w, **moved_invocation) == PROOF
    moved_binding = forged_proof(w, in_plan(binding={**plan["binding"], "generation": plan["binding"]["generation"] + 1}))
    job = w.native.workers.inspect(w.launch.start.binding.job_id)
    other = job.binding.model_copy(update={"generation": job.binding.generation + 1})
    monkeypatch.setattr(w.native.workers, "inspect", lambda _: job.model_copy(update={"binding": other}))
    assert refusal(w, **moved_binding) == PROOF


def test_each_job_launch_and_evidence_rule_refuses(single, monkeypatch):
    w = single
    job = w.native.workers.inspect(w.launch.start.binding.job_id)
    other_binding = job.binding.model_copy(update={"generation": job.binding.generation + 1})
    for name, value in {"not fenced": job.model_copy(update={"cancel_requested": False}),
                        "another binding": job.model_copy(update={"binding": other_binding})}.items():
        with monkeypatch.context() as patch:
            patch.setattr(w.native.workers, "inspect", lambda _, value=value: value)
            assert refusal(w) == PROOF, name
    assert refusal(w, launch=w.launch.model_copy(update={"phase": "claimed"})) == PROOF
    evidence = w.saved.evidence
    for name, change in {
        "state": {"state": "failed"},
        "parked": {"state": "parked", "checkpoint": b"checkpoint"},
        "result": {"result": evidence.result.model_copy(update={"content": b"False success"})},
        "invocation": {"native_execution_id": "another-invocation"},
        "proof": {"proof": b"another proof"},
    }.items():
        assert refusal(w, saved=w.saved.model_copy(update={"evidence": evidence.model_copy(update=change)})) == PROOF, name
    assert refusal(w, saved=w.saved.model_copy(update={"report_digest": "e" * 64})) == PROOF


def read_evidence(w, **changes):
    """The stored evidence row with these fields changed, read the way both production callers read it."""
    evidence_key = native_key("evidence", w.launch.sealed_evidence_id)
    record = w.store.read_snapshot((evidence_key,))[evidence_key]
    data = {**record.payload["data"], "evidence": {**record.payload["data"]["evidence"], **changes}}
    return w.native._row({evidence_key: Record({**record.payload, "data": data}, record.etag)}, evidence_key, _NativeEvidence)


def assert_unfenced_evidence_cannot_be_read(w):
    assert read_evidence(w) == w.saved
    for change in ({"entire_tree_idle": False}, {"all_inflight_starts_fenced": False}, {"checkpoint": "Y2hlY2twb2ludA=="}):
        with pytest.raises(Problem) as error:
            read_evidence(w, **change)
        assert error.value.body["error"]["code"] == "DELIVERY_RECORD_INVALID", change


def test_stored_terminal_evidence_always_has_its_fences_and_no_checkpoint(single):
    assert_unfenced_evidence_cannot_be_read(single)


@pytest.mark.parametrize("schema_version", [
    "fa-teams-pre-acquire-recovery-plan-v1", "fa-expired-teams-cleanup-plan-v1"])
def test_a_stored_teams_stop_proof_refuses_at_parse(single, schema_version):
    verify_platform_stop(single.native, single.launch, single.saved, single.row)
    old = copy.deepcopy(single.row.payload)
    old["data"]["intent"]["authorization"]["plan"]["schema_version"] = schema_version
    assert refusal(single, row=SimpleNamespace(payload=old))[0] == "DELIVERY_RECORD_INVALID"


def test_each_provenance_row_refuses_and_reads_stop_at_the_first_fault(single, monkeypatch):
    w = single
    keys = [recovery_key(name, w.recovery_id) for name in ("authorization", "stop-intent", "stop-observation")]

    def another_data(record):
        return Record({**record.payload, "data": {**record.payload["data"], "another": True}}, record.etag)

    def another_material(record):
        return Record({**record.payload, "material": inline_material({"another": True})}, record.etag)

    for position, wanted in enumerate(keys):
        for fault in (MISSING, another_data, another_material):
            reads = []
            with stored(monkeypatch, w.store, {wanted: fault}):
                read = w.store.read_snapshot
                with monkeypatch.context() as patch:
                    patch.setattr(w.store, "read_snapshot", lambda wanted_keys: reads.append(wanted_keys) or read(wanted_keys))
                    assert refusal(w) == PROVENANCE, wanted
            assert [item for wanted_keys in reads for item in wanted_keys if item in keys] == keys[:position + 1]


def receipts_with(monkeypatch, store, fault_at, change):
    real, calls = store.reconcile, []

    def reconcile(request):
        record = real(request)
        calls.append(request.request_id)
        return change(record) if len(calls) - 1 == fault_at else record

    monkeypatch.setattr(store, "reconcile", reconcile)
    return calls


@pytest.mark.parametrize("fault_at", [0, 1])
def test_a_changed_cancel_or_stop_receipt_refuses_and_stops_reading(single, monkeypatch, fault_at):
    w = single
    calls = receipts_with(monkeypatch, w.store, fault_at, lambda record: Record({**record.payload, "another": True}, record.etag))
    assert refusal(w) == RECEIPTS
    assert len(calls) == fault_at + 1


def test_the_reservation_receipt_is_read_for_its_result(single, monkeypatch):
    w = single
    def another_result(record):
        return Record({**record.payload, "result": {"stop_reserved": False}}, record.etag)
    with monkeypatch.context() as patch:
        calls = receipts_with(patch, w.store, 2, another_result)
        assert refusal(w) == RECEIPTS
    assert calls[1:] == [recovery_key("stop-observation", w.recovery_id), recovery_key("stop-intent", w.recovery_id)]
    with monkeypatch.context() as patch:
        receipts_with(patch, w.store, 2, lambda record: Record({**record.payload, "another": True}, record.etag))
        verify_platform_stop(w.native, w.launch, w.saved, w.row)


def test_two_faults_name_the_earlier_rule(single, monkeypatch):
    w = single
    authorization = recovery_key("authorization", w.recovery_id)
    forged = forged_proof(w, in_observation(endpoint="https://untrusted.invalid"))
    with stored(monkeypatch, w.store, {authorization: MISSING}):
        assert refusal(w, **forged) == PROOF
        with monkeypatch.context() as patch:
            calls = receipts_with(patch, w.store, 0, lambda record: Record({**record.payload, "another": True}, record.etag))
            assert refusal(w) == PROVENANCE
        assert calls == []

    def unavailable(_):
        raise Problem(503, "SYNTHETIC_JOB_UNAVAILABLE", "synthetic job read")

    monkeypatch.setattr(w.native.workers, "inspect", unavailable)
    assert refusal(w, **forged) == ("SYNTHETIC_JOB_UNAVAILABLE", "synthetic job read")


@pytest.mark.parametrize("native_acquire", ["before-assignment"], indirect=True)
def test_pre_assignment_claim_rules_speak_first(native_acquire, monkeypatch):
    c = native_acquire
    service, _ = pre_assignment_service(c)
    service.execute(c.principal, service.authorize(c.principal, service.plan(c.principal), "explicit-cancel"))
    w = sealed(c.native, service.scope.recovery_id, c.launch_id)
    verify_platform_stop(w.native, w.launch, w.saved, w.row)
    wrong_hash = forged_proof(w, in_plan(native_claim_receipt_sha256="e" * 64))
    assert refusal(w, **wrong_hash) == CLAIM
    wrong_request = forged_proof(w, lambda body: body["intent"]["authorization"]["plan"]["native_claim_request"].update(
        request_digest="e" * 64))
    assert refusal(w, **wrong_request) == ("RECOVERY_CLAIM_UNVERIFIED", "one exact original native claim receipt is required")
    bad_job = forged_proof(w, lambda body: (in_plan(native_claim_receipt_sha256="e" * 64)(body),
                                            in_observation(endpoint="https://untrusted.invalid")(body)))
    assert refusal(w, **bad_job) == CLAIM


@pytest.fixture
def models(mixed_models):
    h, c = mixed_models, mixed_models.c
    service, _ = multi_service(h)
    service.execute(c.principal, service.authorize(c.principal, service.plan(c.principal), "explicit-cancel"))
    w = sealed(c.native, service.scope.recovery_id, c.launch_id)
    sid = c.assignment.session_id
    w.sid, w.inventory = sid, c.store.read_inventory()
    w.consumed, w.reserved = key("model-intent", sid, h.first), key("model-intent", sid, h.second)
    w.first_reserve, w.first_consume = key("model", sid, h.first), key("model-send", sid, h.first)
    w.late_consume, w.first = key("model-send", sid, h.second), h.first
    return w


def inventory_is(monkeypatch, store, forgery):
    monkeypatch.setattr(store, "read_inventory", forgery.inventory)


def extra_intent(w):
    extra = key("model-intent", w.sid, "e" * 64)
    return Forgery(w.inventory).copy(w.consumed, extra).data(extra, lambda intent: {**intent, "request_sha256": "e" * 64}, later=True)


def late_consume(w):
    forgery = Forgery(w.inventory)
    forgery.receipts[w.late_consume] = forgery.receipts[w.first_consume]
    return forgery


def denied_result(payload):
    return {**payload, "result": {"state": "denied"}}


def in_intent(w, **updates):
    def change(body):
        intents = body["intent"]["authorization"]["plan"]["model_intents"]
        next(item for item in intents if item["request_sha256"] == w.first).update(updates)
    return change


def test_each_model_set_rule_refuses(models, monkeypatch):
    w = models
    verify_platform_stop(w.native, w.launch, w.saved, w.row)
    for name, forgery, expected in (
        ("extra intent", extra_intent(w), MODEL_SET),
        ("missing intent", Forgery(w.inventory).drop(w.reserved), MODEL_SET),
        ("changed image", Forgery(w.inventory).data(w.reserved, lambda intent: {**intent, "consumed": True}), IMAGE),
        ("late consume", late_consume(w), LATE),
        ("missing reserve", Forgery(w.inventory).drop_receipt(w.first_reserve), MODEL_RECEIPT),
        ("changed reserve", Forgery(w.inventory).receipt(w.first_reserve, denied_result), MODEL_RECEIPT),
        ("missing consume", Forgery(w.inventory).drop_receipt(w.first_consume), MODEL_RECEIPT),
        ("changed consume", Forgery(w.inventory).receipt(w.first_consume, denied_result), MODEL_RECEIPT),
    ):
        with monkeypatch.context() as patch:
            inventory_is(patch, w.store, forgery)
            assert refusal(w) == expected, name
    for field in ("reserve_request_sha256", "consume_request_sha256"):
        assert refusal(w, **forged_proof(w, in_intent(w, **{field: "e" * 64}))) == MODEL_RECEIPT, field


def test_model_faults_speak_in_order_and_before_the_job(models, monkeypatch):
    w = models
    for name, forgery, expected in (
        ("set before image", extra_intent(w).data(w.reserved, lambda intent: {**intent, "consumed": True}), MODEL_SET),
        ("image before late consume", late_consume(w).data(w.reserved, lambda intent: {**intent, "consumed": True}), IMAGE),
        ("image before receipts", Forgery(w.inventory).data(w.consumed, lambda intent: {**intent, "consumed": False})
         .receipt(w.first_reserve, denied_result), IMAGE),
        ("reserve before consume", Forgery(w.inventory).drop_receipt(w.first_reserve), MODEL_RECEIPT),
    ):
        with monkeypatch.context() as patch:
            inventory_is(patch, w.store, forgery)
            assert refusal(w, **forged_proof(w, in_observation(endpoint="https://untrusted.invalid"))) == expected, name


@pytest.fixture
def finished(durable_result):
    h, c = durable_result, durable_result.c
    service, _ = finished_service(h)
    service.execute(c.principal, service.authorize(c.principal, service.plan(c.principal), "explicit-closure"))
    w = sealed(c.native, service.scope.recovery_id, c.launch_id)
    sid = c.assignment.session_id
    work = c.binding.work.work_id
    w.sid, w.inventory, w.result = sid, c.store.read_inventory(), key("result", work)
    w.rows = {"session": key("session", sid), "artifact": key("artifact", work), "result": w.result}
    w.intent = key("model-intent", sid, service.scope.model_intents[0].request_sha256)
    w.receipts = {"assignment": key("issue", sid), "acquire": key("acquire", sid, "sdk-start"),
                  "finish": key("finish", sid, "finish-task")}
    return w


def in_refs(**updates):
    return lambda body: body["intent"]["authorization"]["plan"]["finished_result"].update(updates)


def test_each_finished_rule_refuses(finished, monkeypatch):
    w = finished
    verify_platform_stop(w.native, w.launch, w.saved, w.row)
    cases = []
    for row in w.rows.values():
        cases.append((Forgery(w.inventory).drop(row), FINISHED_ROW))
        cases.append((Forgery(w.inventory).data(row, lambda value: {**value, "another": True}), FINISHED_ROW))
    for receipt in w.receipts.values():
        cases.append((Forgery(w.inventory).drop_receipt(receipt), FINISHED_RECEIPT))
        cases.append((Forgery(w.inventory).receipt(receipt, denied_result), FINISHED_RECEIPT))
    for forgery, expected in cases:
        with monkeypatch.context() as patch:
            inventory_is(patch, w.store, forgery)
            assert refusal(w) == expected
    for field in ("native_claim_receipt_sha256", "native_claim_request_sha256"):
        assert refusal(w, **forged_proof(w, in_refs(**{field: "e" * 64}))) == FINISHED_CLAIM, field
    for name in w.receipts:
        field = name + "_request_sha256"
        assert refusal(w, **forged_proof(w, in_refs(**{field: "e" * 64}))) == FINISHED_RECEIPT, field
    for name in ("session", "artifact", "result"):
        field = name + "_image_sha256"
        assert refusal(w, **forged_proof(w, in_refs(**{field: "e" * 64}))) == FINISHED_ROW, field


def test_stored_succeeded_evidence_always_has_its_fences_and_no_checkpoint(finished):
    assert_unfenced_evidence_cannot_be_read(finished)


def test_finished_faults_speak_in_order(finished, monkeypatch):
    w = finished
    row_and_claim = forged_proof(w, in_refs(session_image_sha256="e" * 64, native_claim_receipt_sha256="e" * 64))
    assert refusal(w, **row_and_claim) == FINISHED_ROW
    claim_and_receipt = forged_proof(w, in_refs(native_claim_receipt_sha256="e" * 64, finish_request_sha256="e" * 64))
    assert refusal(w, **claim_and_receipt) == FINISHED_CLAIM
    receipt_and_job = forged_proof(w, lambda body: (in_refs(finish_request_sha256="e" * 64)(body),
                                                    in_observation(endpoint="https://untrusted.invalid")(body)))
    assert refusal(w, **receipt_and_job) == FINISHED_RECEIPT


def test_model_intents_speak_before_finished_rows(finished, monkeypatch):
    w = finished
    forgery = Forgery(w.inventory).data(w.intent, lambda intent: {**intent, "consumed": not intent["consumed"]})
    inventory_is(monkeypatch, w.store, forgery.drop(w.rows["session"]))
    assert refusal(w) == IMAGE


def test_the_finished_result_is_read_once_and_only_after_the_earlier_rules(finished, monkeypatch):
    w = finished
    read, reads = w.store.read_snapshot, []

    def snapshot(keys):
        reads.append(keys)
        return read(keys)

    monkeypatch.setattr(w.store, "read_snapshot", snapshot)
    verify_platform_stop(w.native, w.launch, w.saved, w.row)
    assert reads.count((w.result,)) == 1
    reads.clear()
    assert refusal(w, launch=w.launch.model_copy(update={"phase": "claimed"})) == PROOF
    assert (w.result,) not in reads
