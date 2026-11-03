"""Every clause of the retained-settlement gates, on an in-memory copy of the saved rows.

Each case breaks one clause while the others hold, so a dropped clause fails here even when an earlier
gate would have hidden it end to end. No provider, executor or model is invoked.
"""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from shared_context.behavior_delivery import _digest, _material
from shared_context.behavior_foundry import _NativeLaunch, native_key
from shared_context.behavior_operations import control_key
from shared_context.behavior_runtime import runtime_key
from shared_context.behavior_settlement import RetainedMaterial
from shared_context.behavior_workers import ExecutorEvidence, worker_key
from shared_context.errors import Problem
from test_behavior_settlement import retained
from test_behavior_settlement_native import native_retained
from test_m3_auth import jwk_client, signing_key


DISABLED = (403, "SETTLEMENT_DISABLED", "explicit original configuration and retained-result verifier are required")
POLICY = (403, "SETTLEMENT_POLICY_DENIED", "current operation policy or original runtime scope differs")
RESULT = (403, "SETTLEMENT_RESULT_DENIED", "only the exact forwarded immutable terminal job/result is eligible")
DIFFER = (403, "SETTLEMENT_RESULT_DENIED", "worker and delivery retained results differ")
PAYLOAD = (403, "SETTLEMENT_PAYLOAD_DENIED", "only the original exact terminal payload/version may be settled")
COMPLETION = (403, "SETTLEMENT_RESULT_DENIED", "the retained completion does not prove this terminal result")
SEALED = (403, "SETTLEMENT_RESULT_DENIED", "native execution is not the sealed terminal original")
STATE_READS = [
    "context", "row:OwnerRecord", "row:PolicyRecord", "row:RuntimePolicy", "row:BudgetRecord",
    "obligation", "row:OwnerRecord", "row:_Conversation", "row:_Obligation", "work", "row:_Work", "row:JobRecord",
]


def refused(expected, call, *args):
    with pytest.raises(Problem) as denied:
        call(*args)
    error = denied.value.body["error"]
    assert (denied.value.status, error["code"], error["message"]) == expected


def snapshot(service, principal):
    plan = service.preflight(principal)
    return service.store.read_snapshot(service._keys(plan)), plan


def patched(rows, key, *, material=None, **updates):
    payload = {**rows[key].payload, "data": {**rows[key].payload["data"], **updates}}
    if material is not None:
        payload["material"] = material.model_dump(mode="json")
    return {**rows, key: SimpleNamespace(payload=payload)}


def nested(model, field, **updates):
    return model.model_copy(update={field: getattr(model, field).model_copy(update=updates)})


def order_trace(service, monkeypatch):
    events = []

    def record(owner, name, label):
        original = getattr(owner, name)

        def wrapped(*args, **kwargs):
            events.append(label)
            return original(*args, **kwargs)
        monkeypatch.setattr(owner, name, wrapped)

    def record_row(owner):
        original = owner._row

        def wrapped(rows, key, model):
            events.append("row:" + model.__name__)
            return original(rows, key, model)
        monkeypatch.setattr(owner, "_row", wrapped)

    record(service, "_context", "context")
    record(service.core, "_obligation", "obligation")
    record(service.core, "_work", "work")
    record_row(service.core)
    record_row(service.workers)
    return events


def test_the_state_reads_its_rows_in_this_order(retained, monkeypatch):
    _, service, _, principal = retained
    rows, _ = snapshot(service, principal)
    events = order_trace(service, monkeypatch)
    service._state(rows)
    assert events == STATE_READS


def test_the_plan_reads_its_rows_in_this_order(retained, monkeypatch):
    _, service, _, principal = retained
    rows, plan = snapshot(service, principal)
    events = order_trace(service, monkeypatch)
    assert service._describe(rows) == plan
    assert events == [*STATE_READS, "row:VerifiedWorkResult", "row:_Outbox"]


def test_the_native_plan_reads_its_rows_in_this_order(native_retained, monkeypatch):
    _, service, _, principal = native_retained
    rows, plan = snapshot(service, principal)
    events = order_trace(service, monkeypatch)
    assert service._describe(rows) == plan
    assert events == [*STATE_READS, "row:VerifiedWorkResult", "row:_Outbox", "row:_NativeLaunch", "row:ExecutorEvidence"]


def test_each_plan_field_names_its_own_source(retained):
    h, service, _, principal = retained
    rows, plan = snapshot(service, principal)
    _, ob, work, job, old = service._state(rows)
    result_sha = _material(work.result.model_dump(mode="json")).sha256
    outbox_key = control_key("delivery-outbox", ob.final_delivery_id)
    outbox = rows[outbox_key].payload
    assert (plan.settlement_id, plan.binding_id, plan.job_id) == (
        service.registration.settlement_id, service.binding.binding_id, service.binding.job_id)
    assert (plan.configuration_sha256, plan.control_binding) == (service.configuration_sha256, service.store._binding)
    assert plan.runtime_binding_digest == service.runtime.binding_digest(service.binding)
    assert plan.delivery_configuration_digest == service.core.configuration_digest()
    assert plan.worker_configuration_digest == service.workers.configuration_digest()
    assert plan.job_sha256 == _digest(job.model_dump(mode="json"))
    assert (plan.work_id, plan.terminal_state) == (work.definition.work_id, work.state)
    assert plan.operation_policy_revision == old.operation_policy_revision
    assert plan.runtime_policy_sha256 == _digest(old.model_dump(mode="json"))
    assert (plan.result_evidence_id, plan.result_sha256) == (work.result.evidence_id, result_sha)
    assert plan.delivery.model_dump(mode="json") == outbox["data"]["binding"]
    assert plan.retained_materials == (
        RetainedMaterial(row_id=control_key("delivery-work-state", ob.obligation_id), sha256=result_sha),
        RetainedMaterial(row_id=worker_key("work-result", work.result.evidence_id), sha256=result_sha),
        RetainedMaterial(row_id=outbox_key, sha256=outbox["material"]["sha256"]),
    )
    assert (plan.launch_row_id, plan.launch_sha256, plan.completion_row_id, plan.completion_sha256) == (None,) * 4


def test_the_native_plan_names_the_original_launch_and_completion(native_retained):
    _, service, _, principal = native_retained
    rows, plan = snapshot(service, principal)
    job = service._state(rows)[3]
    launch_key = native_key("launch", native_key("launch-id", job.binding.start_request_id))
    completion_key = worker_key("completion", service.binding.job_id, str(job.generation))
    assert (plan.launch_row_id, plan.completion_row_id) == (launch_key, completion_key)
    assert plan.launch_sha256 == _digest(service.workers._row(rows, launch_key, _NativeLaunch).model_dump(mode="json"))
    assert plan.completion_sha256 == _digest(service.workers._row(rows, completion_key, ExecutorEvidence).model_dump(mode="json"))


def test_each_policy_clause_refuses_alone(retained):
    _, service, _, principal = retained
    rows, _ = snapshot(service, principal)
    b = service.binding
    assert service._state(rows)
    policy = control_key("policy", b.owner.policy_id)
    runtime = runtime_key("policy", b.binding_id)
    budget = control_key("budget", b.owner.root_budget_id)
    for key, updates in (
        (policy, {"active": False}), (policy, {"policy_id": "another"}), (policy, {"revision": 99}),
        (runtime, {"active": False}), (runtime, {"binding_digest": "a" * 64}),
        (runtime, {"operation_policy_revision": 99}), (budget, {"root_budget_id": "another"}),
    ):
        refused(POLICY, service._state, patched(rows, key, **updates))


def test_each_terminal_clause_refuses_alone(retained, monkeypatch):
    _, service, _, principal = retained
    rows, _ = snapshot(service, principal)
    _, ob, work, job, _ = service._state(rows)
    other_definition = work.definition.model_copy(update={"goal": "another goal"})
    cases = (
        ("work", work.model_copy(update={"result": None})),
        ("work", work.model_copy(update={"state": "queued"})),
        ("work", work.model_copy(update={"definition": other_definition})),
        ("work", nested(work, "result", work_revision=2)),
        ("work", nested(work, "result", verifier_version="another")),
        ("job", job.model_copy(update={"state": "failed"})),
        ("job", job.model_copy(update={"result_forwarded": False})),
        ("job", job.model_copy(update={"completion_credit": 1})),
        ("job", job.model_copy(update={"children": ("child-job",)})),
        ("job", nested(job, "spec", parent_job_id="parent")),
        ("job", nested(job, "spec", job_id="another")),
        ("job", nested(job, "spec", work_id="another")),
        ("job", nested(job, "spec", obligation_id="another")),
        ("job", nested(job, "spec", input=b"another input")),
        ("job", nested(job, "owner", conversation_id="another")),
        ("job", nested(job, "owner", policy_id="another")),
        ("ob", ob.model_copy(update={"event_id": "another"})),
        ("ob", ob.model_copy(update={"root_request_id": "another"})),
        ("ob", nested(ob, "route", thread_id="another")),
        ("ob", ob.model_copy(update={"required_confirmation_level": "emitted"})),
    )
    for part, value in cases:
        with monkeypatch.context() as local:
            if part == "work":
                local.setattr(service.core, "_work", lambda *args: value)
            elif part == "ob":
                local.setattr(service.core, "_obligation", lambda *args: value)
                local.setattr(service.core, "_work", lambda *args: work)
            else:
                local.setattr(service.workers, "_row", lambda *args: value)
            refused(RESULT, service._state, rows)
    with monkeypatch.context() as local:
        local.setattr(service.core, "_work", lambda *args: work.model_copy(update={"state": "queued"}))
        local.setattr(service.workers, "_row", lambda *args: job.model_copy(update={"state": "queued"}))
        refused(RESULT, service._state, rows)
    # The epoch alone may advance: a later owner epoch keeps the same terminal job.
    with monkeypatch.context() as local:
        local.setattr(service.workers, "_row", lambda *args: nested(job, "owner", epoch=job.owner.epoch + 1))
        assert service._state(rows)


def test_policy_is_refused_before_the_terminal_job(retained, monkeypatch):
    _, service, _, principal = retained
    rows, _ = snapshot(service, principal)
    b = service.binding
    job = service._state(rows)[3]
    local_rows = patched(rows, control_key("policy", b.owner.policy_id), active=False)
    monkeypatch.setattr(service.workers, "_row", lambda *args: job.model_copy(update={"result_forwarded": False}))
    refused(POLICY, service._state, local_rows)


def with_state(service, monkeypatch, rows, **parts):
    state = dict(zip(("context", "ob", "work", "job", "old"), service._state(rows)))
    state.update(parts)
    monkeypatch.setattr(service, "_state", lambda _: tuple(state.values()))


def test_a_changed_result_reference_is_refused(retained):
    _, service, _, principal = retained
    rows, plan = snapshot(service, principal)
    key = worker_key("work-result", plan.result_evidence_id)
    refused((403, "SETTLEMENT_RESULT_DENIED", "the original result reference changed"), service._describe,
            {name: row for name, row in rows.items() if name != key})


def test_a_retained_result_that_differs_is_refused(retained):
    _, service, _, principal = retained
    rows, plan = snapshot(service, principal)
    key = worker_key("work-result", plan.result_evidence_id)
    refused(DIFFER, service._describe, patched(rows, key, proof="b3RoZXI="))
    refused(DIFFER, service._describe, patched(rows, key, material=_material({"another": True})))


def test_a_plan_without_a_prepared_outbox_is_refused(retained, monkeypatch):
    _, service, _, principal = retained
    rows, _ = snapshot(service, principal)
    ob = service._state(rows)[1]
    with_state(service, monkeypatch, rows, ob=ob.model_copy(update={"final_delivery_id": None}))
    refused((409, "SETTLEMENT_NOT_PREPARED", "no immutable terminal outbox is prepared"), service._describe, rows)


def test_a_changed_outbox_reference_is_refused(retained):
    _, service, _, principal = retained
    rows, plan = snapshot(service, principal)
    key = control_key("delivery-outbox", plan.delivery.delivery_id)
    refused((403, "SETTLEMENT_PAYLOAD_DENIED", "the original outbox reference changed"), service._describe,
            {name: row for name, row in rows.items() if name != key})


def test_the_result_is_checked_before_the_outbox_is_looked_at(retained, monkeypatch):
    _, service, _, principal = retained
    rows, plan = snapshot(service, principal)
    ob = service._state(rows)[1]
    with_state(service, monkeypatch, rows, ob=ob.model_copy(update={"final_delivery_id": None}))
    broken = patched(rows, worker_key("work-result", plan.result_evidence_id), proof="b3RoZXI=")
    refused(DIFFER, service._describe, broken)


def test_each_outbox_clause_refuses_alone(retained, monkeypatch):
    _, service, _, principal = retained
    rows, plan = snapshot(service, principal)
    key = control_key("delivery-outbox", plan.delivery.delivery_id)
    binding = rows[key].payload["data"]["binding"]
    assert service._describe(rows) == plan
    for broken in (
        patched(rows, key, message_kind="ack"),
        patched(rows, key, binding={**binding, "obligation_id": "another"}),
        patched(rows, key, binding={**binding, "delivery_id": "another"}),
        patched(rows, key, binding={**binding, "route": {**binding["route"], "thread_id": "another"}}),
        patched(rows, key, binding={**binding, "version": binding["version"] + 1}),
        patched(rows, key, binding={**binding, "channel_version": "another"}),
        patched(rows, key, binding={**binding, "payload_digest": "a" * 64}),
        patched(rows, key, material=_material({"another": True})),
    ):
        refused(PAYLOAD, service._describe, broken)


def test_each_obligation_or_result_clause_of_the_outbox_refuses_alone(retained, monkeypatch):
    _, service, _, principal = retained
    rows, _ = snapshot(service, principal)
    _, ob, work, _, _ = service._state(rows)
    for parts in (
        {"ob": ob.model_copy(update={"last_version": ob.final_version + 1})},
        {"ob": ob.model_copy(update={"channel_version": "another"})},
        {"ob": ob.model_copy(update={"final_version": ob.final_version + 1, "last_version": ob.final_version + 1})},
        {"ob": ob.model_copy(update={"event_id": "another"})},
    ):
        with monkeypatch.context() as local:
            with_state(service, local, rows, **parts)
            refused(PAYLOAD, service._describe, rows)


def test_outbox_artifacts_must_be_the_result_artifacts(retained):
    _, service, _, principal = retained
    rows, plan = snapshot(service, principal)
    key = control_key("delivery-outbox", plan.delivery.delivery_id)
    binding = rows[key].payload["data"]["binding"]
    other = [{"artifact_id": "another", "version": "1", "sha256": "a" * 64}]
    refused(PAYLOAD, service._describe, patched(rows, key, binding={**binding, "artifacts": other}))


def native_rows(service, principal):
    rows, plan = snapshot(service, principal)
    return rows, plan, plan.launch_row_id, plan.completion_row_id


def test_a_native_plan_needs_its_original_rows(native_retained, monkeypatch):
    _, service, _, principal = native_retained
    rows, _, launch_key, completion_key = native_rows(service, principal)
    missing = lambda key: {name: row for name, row in rows.items() if name != key}
    refused((403, "SETTLEMENT_RESULT_DENIED", "the original native execution changed"), service._describe, missing(launch_key))
    refused((403, "SETTLEMENT_RESULT_DENIED", "the original completion reference changed"), service._describe, missing(completion_key))
    job = service._state(rows)[3]
    with_state(service, monkeypatch, rows, job=job.model_copy(update={"binding": None}))
    refused((403, "SETTLEMENT_RESULT_DENIED", "a sealed original native execution is required"), service._describe, rows)


def test_each_native_completion_clause_refuses_alone(native_retained):
    _, service, _, principal = native_retained
    rows, plan, launch_key, key = native_rows(service, principal)
    assert service._describe(rows) == plan
    completion = service.workers._row(rows, key, ExecutorEvidence)
    launch = rows[launch_key].payload["data"]
    # The material always matches the change, so only the clause under test can refuse it.
    for updates in (
        {"binding": completion.binding.model_copy(update={"start_request_id": "another"})},
        {"evidence_id": "another"}, {"native_execution_id": "another"},
        {"state": "failed"}, {"result": nested(completion, "result", proof=b"another").result},
    ):
        value = completion.model_copy(update=updates).model_dump(mode="json")
        refused(COMPLETION, service._describe, patched(rows, key, material=_material(value), **value))
    refused(COMPLETION, service._describe, patched(rows, key, material=_material({"another": True})))
    # A later launch count leaves only the job's own sequence to refuse this one.
    later = completion.model_copy(update={"sequence": completion.sequence + 1}).model_dump(mode="json")
    raised = patched(rows, launch_key, evidence_sequence=launch["evidence_sequence"] + 1)
    refused(COMPLETION, service._describe, patched(raised, key, material=_material(later), **later))
    # Sequences past the launch's own count stay refused even when the job agrees with the completion.
    refused(COMPLETION, service._describe, patched(rows, launch_key, evidence_sequence=launch["evidence_sequence"] - 1))


def test_the_idle_flags_of_a_completion_are_required(native_retained, monkeypatch):
    _, service, _, principal = native_retained
    rows, _, _, key = native_rows(service, principal)
    parsed = service.workers._row(rows, key, ExecutorEvidence)
    original = service.workers._row
    for field in ("entire_tree_idle", "all_inflight_starts_fenced"):
        broken = parsed.model_copy(update={field: False})
        matching = patched(rows, key, material=_material(broken.model_dump(mode="json")))
        with monkeypatch.context() as local:
            # The saved model refuses this shape when parsed, so only a stub reaches the clause.
            local.setattr(service.workers, "_row", lambda r, k, m: broken if k == key else original(r, k, m))
            refused(COMPLETION, service._describe, matching)


def test_a_cancelled_job_needs_stopped_completion_state(native_retained, monkeypatch):
    _, service, _, principal = native_retained
    rows, plan, _, _ = native_rows(service, principal)
    job = service._state(rows)[3]
    assert job.state == "cancelled" and service._describe(rows) == plan
    with_state(service, monkeypatch, rows, job=job.model_copy(update={"state": "failed"}))
    refused(COMPLETION, service._describe, rows)


def test_each_sealed_launch_clause_refuses_alone(native_retained, monkeypatch):
    _, service, _, principal = native_retained
    rows, _, key, _ = native_rows(service, principal)
    launch = rows[key].payload["data"]
    for broken in (
        patched(rows, key, phase="claimed"),
        patched(rows, key, sealed_evidence_id=None),
        patched(rows, key, sealed_evidence_id="another"),
        patched(rows, key, api_binding_id="another"),
        patched(rows, key, start={**launch["start"], "binding": {**launch["start"]["binding"], "start_request_id": "x"}}),
        patched(rows, key, definition={**launch["definition"], "goal": "another goal"}),
    ):
        refused(SEALED, service._describe, broken)
    parsed = service.workers._row(rows, key, _NativeLaunch)
    original = service.workers._row
    with monkeypatch.context() as local:
        local.setattr(service.workers, "_row", lambda r, k, m: (
            parsed.model_copy(update={"configuration_digest": "a" * 64}) if k == key else original(r, k, m)))
        refused(SEALED, service._describe, rows)


def test_the_completion_is_checked_before_the_launch(native_retained):
    _, service, _, principal = native_retained
    rows, _, launch_key, key = native_rows(service, principal)
    broken = patched(patched(rows, launch_key, phase="claimed"), key, evidence_id="another")
    refused(COMPLETION, service._describe, broken)


@pytest.mark.parametrize("native_retained", [True], indirect=True)
def test_a_reverification_id_must_name_the_completion_sequence(native_retained, monkeypatch):
    _, service, _, principal = native_retained
    rows, plan, launch_key, key = native_rows(service, principal)
    assert service._describe(rows) == plan
    refused(SEALED, service._describe, patched(rows, launch_key, sealed_evidence_id=None))
    job = service._state(rows)[3]
    launch_id = rows[launch_key].payload["data"]["launch_id"]
    other = native_key("terminal-verification-id", launch_id, "3")
    with_state(service, monkeypatch, rows, job=job.model_copy(update={"last_evidence_id": other}))
    renamed = service.workers._row(rows, key, ExecutorEvidence).model_copy(update={"evidence_id": other})
    refused(SEALED, service._describe, patched(rows, key, evidence_id=other, material=_material(renamed.model_dump(mode="json"))))


def test_each_installed_clause_refuses_alone(retained, monkeypatch):
    h, service, _, _ = retained
    service._installed()
    r, config, core, workers, inbox = service.registration, service.config, service.core, service.workers, service.inbox
    other_store = SimpleNamespace(_scope=service.store._scope, _binding=service.store._binding)
    delivery = core.contract.model_copy(update={"control_binding": "a" * 64})
    worker_contract = workers.contract.model_copy(update={"control_binding": "a" * 64})
    cases = (
        lambda m: m.setattr(service, "registration", None),
        lambda m: m.setattr(service, "binding", None),
        lambda m: m.setattr(service, "registration", replace(r, protection_verifier=None)),
        lambda m: m.setattr(service, "registration", replace(r, binding_id="another")),
        lambda m: m.setattr(config, "sha256", "a" * 64),
        lambda m: m.setattr(service, "registration", replace(r, configuration_sha256="a" * 64)),
        lambda m: m.setattr(config, "delivery", delivery),
        lambda m: m.setattr(config, "workers", worker_contract),
        lambda m: m.setattr(config, "table", SimpleNamespace(scope="another")),
        lambda m: (m.setattr(config, "delivery", delivery), m.setattr(core, "contract", delivery)),
        lambda m: (m.setattr(config, "workers", worker_contract), m.setattr(workers, "contract", worker_contract)),
        lambda m: m.setattr(config, "bindings", ()),
        lambda m: m.setattr(config.identity, "human_oid", "another"),
        lambda m: m.setattr(config.identity, "tenant_id", "another"),
        lambda m: m.setattr(config.identity, "team_id", "another"),
        lambda m: m.setattr(config, "authentication_version", "another"),
        lambda m: m.setattr(inbox, "core", object()),
        lambda m: m.setattr(inbox, "store", object()),
        lambda m: m.setattr(core, "registration", replace(core.registration, channel_adapter=object())),
        lambda m: m.setattr(workers, "store", other_store),
        lambda m: m.setattr(core, "registration", replace(core.registration, mode="native")),
        lambda m: m.setattr(workers, "mode", "native"),
    )
    for case in cases:
        with monkeypatch.context() as local:
            case(local)
            refused(DISABLED, service._installed)
    service._installed()


@pytest.mark.parametrize("field,value,error", [
    ("settlement_id", "bad id!", ValidationError), ("protection_evidence_id", "bad id!", ValidationError),
    ("protection_verifier_version", "bad id!", ValidationError),
])
def test_a_malformed_registration_id_never_selects_a_namespace(retained, field, value, error):
    _, service, _, _ = retained
    service.registration = replace(service.registration, **{field: value})
    with pytest.raises(error):
        service._installed()
    service.binding = None
    refused(DISABLED, service._installed)


def test_malformed_binding_digest_and_expiry_are_refused(retained, monkeypatch):
    _, service, _, _ = retained
    r = service.registration
    with monkeypatch.context() as local:
        local.setattr(service, "registration", replace(r, binding_id="bad id!"))
        local.setattr(service, "binding", service.binding.model_copy(update={"binding_id": "bad id!"}))
        with pytest.raises(ValidationError):
            service._installed()
    with monkeypatch.context() as local:
        local.setattr(service, "registration", replace(r, configuration_sha256="short"))
        local.setattr(service, "configuration_sha256", "short")
        local.setattr(service.config, "sha256", "short")
        with pytest.raises(ValidationError):
            service._installed()
    with monkeypatch.context() as local:
        local.setattr(service, "registration", replace(r, expires_at=r.expires_at.replace(tzinfo=None)))
        with pytest.raises(ValueError):
            service._installed()
