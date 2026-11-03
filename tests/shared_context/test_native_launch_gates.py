"""Which saved state may start or fence a launch, in what order, and exactly what each one reads.

Local authority over offline Table doubles. The start adapter only captures the command, so nothing is
posted and no process runs. The trace tests pin reads and checks, because moving one across the
transaction boundary or doing it twice changes what a concurrent writer can get past.
"""

from dataclasses import replace
from datetime import timedelta
import hashlib
from types import SimpleNamespace

from pydantic import SecretStr
import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_foundry import (
    SESSION_VERSION_CHANGED, _TargetReservation, _unused_assignment_keys, native_key,
)
from shared_context.behavior_operations import control_key
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from test_behavior_delivery import CONTEXT, NOW
from test_behavior_foundry import NativeHarness
from test_behavior_settlement import write


def data(h, key):
    return dict(h.store.read_snapshot((key,))[key].payload["data"])


def change(h, key, **updates):
    write(h.store, key, {**data(h, key), **updates})


def refuses(status, error_code, call, *args, **kwargs):
    with pytest.raises(Problem) as refused:
        call(*args, **kwargs)
    assert (refused.value.status, refused.value.body["error"]["code"]) == (status, error_code)


def trace(h, monkeypatch):
    """Record, in order, each store read, transaction and parsed row. Pure in-memory lookups are left out."""
    events = []

    def record(owner, name, label=None):
        original = getattr(owner, name)

        def wrapped(*args, **kwargs):
            events.append(label or name)
            return original(*args, **kwargs)
        monkeypatch.setattr(owner, name, wrapped)

    def record_row(owner):
        original = owner._row

        def wrapped(rows, key, model):
            events.append("row:" + model.__name__)
            return original(rows, key, model)
        monkeypatch.setattr(owner, "_row", wrapped)

    record(h.store, "read_snapshot")
    record(h.store, "transact")
    record(h.native, "_current")
    record(h.native, "targets_for")
    record_row(h.native)
    record_row(h.workers)
    return events


@pytest.fixture
def starting(tmp_path):
    """A job in `starting` with its saved attempt and slot, and no launch yet."""
    h = NativeHarness(tmp_path)
    started = []
    adapter = h.workers.adapter
    h.workers.adapter = replace(adapter, start=started.append, verify=h.native.verify)
    job_id = h.specs[0].job_id
    h.workers.start(job_id, CONTEXT)
    h.workers.adapter = adapter
    try:
        yield h, job_id, started[0]
    finally:
        h.close()


def issued(launch, permit):
    """The one-use permit a caller gets is the secret whose digest the saved launch keeps."""
    return launch.permit_digest == hashlib.sha256(permit.get_secret_value().encode("ascii")).hexdigest()


def test_a_fresh_prepare_writes_one_launch_and_a_replay_gets_no_permit(starting):
    h, job_id, start = starting
    grant_key = native_key("grant", "native-grant-0")
    launch, permit = h.native.prepare(start)
    assert issued(launch, permit) and launch.phase == "post_unknown"
    assert launch.process_generation == 1 and launch.api_binding_id == "binding-0"
    assert data(h, grant_key)["remaining_uses"] == 3
    reservation = data(h, h.native.target_key(launch.target, job_id))
    assert reservation["active_launch_id"] == launch.launch_id and reservation["generation"] == 1
    intent = data(h, native_key("post-intent", launch.launch_id))
    assert (intent["post_digest"], intent["launch_id"]) == (launch.post_digest, launch.launch_id)

    replay, no_permit = h.native.prepare(start)
    assert replay == launch and no_permit is None
    assert data(h, grant_key)["remaining_uses"] == 3


def test_a_prepare_reads_and_checks_in_this_order(starting, monkeypatch):
    h, _, start = starting
    events = trace(h, monkeypatch)
    h.native.prepare(start)
    assert events == [
        "targets_for", "transact", "row:OwnerRecord", "row:PolicyRecord", "row:JobRecord", "row:_Pool",
        "row:NativeExecutionGrant", "_current", "row:OwnerRecord", "row:NativeExecutionGrant",
        "row:PolicyRecord", "row:BudgetRecord", "read_snapshot", "row:_NativeLaunch",
    ]


def test_a_job_without_a_route_is_refused_after_the_worker_is_ready(starting):
    h, job_id, start = starting
    h.native.jobs = {}
    refuses(403, "NATIVE_JOB_UNASSIGNED", h.native.prepare, start)
    h.workers.adapter = None
    refuses(403, "WORKER_DISABLED", h.native.prepare, start)


@pytest.mark.parametrize("updates", [{"state": "queued"}, {"cancel_requested": True}])
def test_a_start_that_is_no_longer_current_is_refused(starting, updates):
    h, job_id, start = starting
    change(h, worker_key("job", job_id), **updates)
    refuses(409, "NATIVE_START_FENCED", h.native.prepare, start)


def test_a_start_for_another_binding_is_refused(starting):
    h, job_id, start = starting
    key = worker_key("job", job_id)
    change(h, key, binding={**data(h, key)["binding"], "start_request_id": "another-start"})
    refuses(409, "NATIVE_START_FENCED", h.native.prepare, start)


def test_only_the_saved_attempt_may_launch(starting):
    h, job_id, start = starting
    key = worker_key("attempt", job_id, "1")
    saved = data(h, key)
    write(h.store, key, {**saved, "kind": "another"})
    refuses(403, "NATIVE_INPUT_UNVERIFIED", h.native.prepare, start)
    write(h.store, key, {**saved, "start": {**saved["start"], "input": "ZGlmZmVyZW50"}})
    refuses(403, "NATIVE_INPUT_UNVERIFIED", h.native.prepare, start)
    # Material cannot be rewritten, so a matching start is what makes only the material differ.
    other = start.model_copy(update={"input": b"different"})
    write(h.store, key, {**saved, "start": other.model_dump(mode="json")})
    refuses(403, "NATIVE_INPUT_UNVERIFIED", h.native.prepare, other)


def test_a_missing_attempt_is_refused(starting, monkeypatch):
    h, job_id, start = starting
    missing = worker_key("attempt", job_id, "9")
    monkeypatch.setattr("shared_context.behavior_foundry.worker_key", lambda kind, *ids: (
        missing if kind == "attempt" else worker_key(kind, *ids)))
    refuses(403, "NATIVE_INPUT_UNVERIFIED", h.native.prepare, start)


def test_a_job_without_its_one_capacity_slot_is_refused(starting):
    h, job_id, start = starting
    key = h.workers.pool_key(job_id)
    pool = data(h, key)
    write(h.store, key, {**pool, "slots": [{**slot, "binding": None} for slot in pool["slots"]]})
    refuses(409, "NATIVE_SLOT_FENCED", h.native.prepare, start)


@pytest.mark.parametrize("active,other_target", [("another-launch", False), (None, True)])
def test_a_target_held_or_owned_by_another_execution_is_refused(starting, active, other_target):
    h, job_id, start = starting
    target = h.targets[0]
    saved = target.model_copy(update={"session_id": "another-session"}) if other_target else target
    write(h.store, h.native.target_key(target, job_id), _TargetReservation(
        target=saved, configuration_digest=h.workers.configuration_digest(), active_launch_id=active,
    ).model_dump(mode="json"))
    refuses(409, "NATIVE_TARGET_BUSY", h.native.prepare, start)


def test_a_fresh_launch_takes_the_grant_checkpoint_limit(starting):
    h, _, start = starting
    change(h, native_key("grant", "native-grant-0"), checkpoint_after_bytes=1)
    assert h.native.prepare(start)[0].checkpoint_after_bytes == 1


def test_a_resumed_launch_does_not_park_again(starting, monkeypatch):
    h, _, start = starting
    change(h, native_key("grant", "native-grant-0"), checkpoint_after_bytes=1)
    monkeypatch.setattr("shared_context.behavior_foundry._require_saved_attempt", lambda *args: None)
    assert h.native.prepare(start.model_copy(update={"checkpoint": b"saved"}))[0].checkpoint_after_bytes is None


def test_input_other_than_the_granted_bytes_is_refused(starting, monkeypatch):
    h, _, start = starting
    # The saved attempt would refuse this first; the grant keeps its own digest check behind it.
    monkeypatch.setattr("shared_context.behavior_foundry._require_saved_attempt", lambda *args: None)
    refuses(403, "NATIVE_GRANT_DENIED", h.native.prepare, start.model_copy(update={"input": b"different"}))


def test_a_settled_target_keeps_counting_its_generations(starting):
    h, job_id, start = starting
    target = h.targets[0]
    write(h.store, h.native.target_key(target, job_id), _TargetReservation(
        target=target, configuration_digest=h.workers.configuration_digest(), generation=4,
    ).model_dump(mode="json"))
    assert h.native.prepare(start)[0].process_generation == 5


@pytest.mark.parametrize("updates", [
    {"remaining_uses": 0}, {"input_digest": "a" * 64}, {"checkpoint_after_bytes": 8192},
])
def test_a_grant_that_cannot_cover_this_input_is_refused(starting, updates):
    h, _, start = starting
    grant_key = native_key("grant", "native-grant-0")
    change(h, grant_key, **updates)
    # A changed digest differs from the binding first; both refuse before any launch exists.
    expected = "NATIVE_AUTHORITY_FENCED" if "input_digest" in updates else "NATIVE_GRANT_DENIED"
    refuses(403, expected, h.native.prepare, start)
    assert data(h, grant_key)["remaining_uses"] == updates.get("remaining_uses", 4)


@pytest.mark.parametrize("field,value", [
    ("work_id", "another-work"), ("required_checks", ["unsupported_check"]), ("result_kind", "artifact"),
])
def test_a_grant_definition_outside_the_native_contract_is_refused(starting, monkeypatch, field, value):
    h, _, start = starting
    grant_key = native_key("grant", "native-grant-0")
    grant = data(h, grant_key)
    change(h, grant_key, definition={**grant["definition"], field: value})
    # The definition is copied into the launch, so the association check cannot see this one change.
    monkeypatch.setattr(h.native, "_current", lambda *args, **kwargs: None)
    refuses(403, "NATIVE_GRANT_DENIED", h.native.prepare, start)


def test_a_grant_definition_with_a_human_check_is_refused(starting, monkeypatch):
    h, _, start = starting
    grant_key = native_key("grant", "native-grant-0")
    definition = data(h, grant_key)["definition"]
    change(h, grant_key, definition={**definition, "human_checks": definition["required_checks"][:1]})
    monkeypatch.setattr(h.native, "_current", lambda *args, **kwargs: None)
    refuses(403, "NATIVE_GRANT_DENIED", h.native.prepare, start)


def test_an_assistant_grant_needs_the_registered_authority_and_skips_only_the_check_list(starting, monkeypatch):
    h, _, start = starting
    grant_key = native_key("grant", "native-grant-0")
    grant = data(h, grant_key)
    change(h, grant_key, workload_version="assistant-v1.2",
           definition={**grant["definition"], "required_checks": ["assistant_only_check"]})
    monkeypatch.setattr(h.native, "_current", lambda *args, **kwargs: None)
    refuses(403, "NATIVE_ASSISTANT_DISABLED", h.native.prepare, start)

    seen = []
    h.native.assistant = SimpleNamespace(validate_native_start=lambda *args: seen.append(args))
    change(h, grant_key, remaining_uses=0)
    refuses(403, "NATIVE_GRANT_DENIED", h.native.prepare, start)
    assert [(item[0], item[2]) for item in seen] == [(start, "binding-0")]

    change(h, grant_key, remaining_uses=4)
    launch, permit = h.native.prepare(start)
    assert issued(launch, permit) and data(h, grant_key)["remaining_uses"] == 3
    assert (launch.workload_version, launch.phase, launch.api_binding_id) == ("assistant-v1.2", "post_unknown", "binding-0")


def test_each_refusal_waits_for_the_one_before_it(starting):
    """Everything broken at once: fixing each one uncovers exactly the next, then the launch succeeds."""
    h, job_id, start = starting
    job_key = worker_key("job", job_id)
    owner_key = control_key("owner", CONTEXT.owner_record_id)
    attempt_key = worker_key("attempt", job_id, "1")
    pool_key = h.workers.pool_key(job_id)
    grant_key = native_key("grant", "native-grant-0")
    target_key = h.native.target_key(h.targets[0], job_id)
    saved = {key: data(h, key) for key in (job_key, owner_key, attempt_key, pool_key, grant_key)}
    target_row = _TargetReservation(target=h.targets[0], configuration_digest=h.workers.configuration_digest())

    change(h, owner_key, active=False)
    change(h, job_key, cancel_requested=True)
    change(h, attempt_key, kind="another")
    change(h, pool_key, slots=[{**slot, "binding": None} for slot in saved[pool_key]["slots"]])
    write(h.store, target_key, target_row.model_copy(update={"active_launch_id": "another"}).model_dump(mode="json"))
    change(h, grant_key, active=False, workload_version="assistant-v1.2", remaining_uses=0)

    refuses(403, "WORKER_OWNER_FENCED", h.native.prepare, start)
    write(h.store, owner_key, saved[owner_key])
    refuses(409, "NATIVE_START_FENCED", h.native.prepare, start)
    write(h.store, job_key, saved[job_key])
    refuses(403, "NATIVE_INPUT_UNVERIFIED", h.native.prepare, start)
    write(h.store, attempt_key, saved[attempt_key])
    refuses(409, "NATIVE_SLOT_FENCED", h.native.prepare, start)
    write(h.store, pool_key, saved[pool_key])
    refuses(409, "NATIVE_TARGET_BUSY", h.native.prepare, start)
    write(h.store, target_key, target_row.model_dump(mode="json"))
    refuses(403, "NATIVE_AUTHORITY_FENCED", h.native.prepare, start)
    change(h, grant_key, active=True)
    refuses(403, "NATIVE_ASSISTANT_DISABLED", h.native.prepare, start)
    change(h, grant_key, workload_version="checksum-count-v1")
    refuses(403, "NATIVE_GRANT_DENIED", h.native.prepare, start)
    change(h, grant_key, remaining_uses=4)
    launch, permit = h.native.prepare(start)
    assert issued(launch, permit) and data(h, grant_key)["remaining_uses"] == 3
    assert (launch.workload_version, launch.phase, launch.api_binding_id) == (
        "checksum-count-v1", "post_unknown", "binding-0")


@pytest.fixture
def posted(tmp_path):
    """A posted launch nothing has claimed yet."""
    h = NativeHarness(tmp_path)
    h.hold_start = True
    job_id = h.specs[0].job_id
    h.workers.start(job_id, CONTEXT)
    binding = h.workers.inspect(job_id).binding
    try:
        yield h, job_id, binding, h.native.launch(native_key("launch-id", binding.start_request_id))
    finally:
        h.close()


def cancel_requested(h, job_id):
    change(h, worker_key("job", job_id), cancel_requested=True)


@pytest.mark.parametrize("observed", ["7", "0", "01", "1" * 17, "latest", "9\n", 999])
def test_a_refusal_needs_a_different_concrete_version_before_reading_anything_else(posted, monkeypatch, observed):
    h, _, binding, launch = posted
    assert launch.target.agent_version == "7"
    events = trace(h, monkeypatch)
    refuses(409, "NATIVE_VERSION_REFUSAL_INVALID", h.native.fence_unclaimed, binding, observed_agent_version=observed)
    assert events == ["read_snapshot", "row:_NativeLaunch"]


def test_a_cancel_fence_never_reads_the_owner_outside_its_transaction(posted, monkeypatch):
    h, job_id, binding, _ = posted
    cancel_requested(h, job_id)
    events = trace(h, monkeypatch)
    h.native.fence_unclaimed(binding)
    assert events == [
        "read_snapshot", "row:_NativeLaunch", "transact", "row:_NativeLaunch", "_current", "row:OwnerRecord",
        "row:NativeExecutionGrant", "row:PolicyRecord", "row:BudgetRecord", "row:JobRecord",
        "row:_TargetReservation",
    ]


def test_a_version_fence_reads_and_checks_in_this_order(posted, monkeypatch):
    h, _, binding, _ = posted
    events = trace(h, monkeypatch)
    h.native.fence_unclaimed(binding, observed_agent_version="999")
    assert events == [
        "read_snapshot", "row:_NativeLaunch", "read_snapshot", "row:OwnerRecord", "transact",
        "row:_NativeLaunch", "_current", "row:OwnerRecord", "row:NativeExecutionGrant", "row:PolicyRecord",
        "row:BudgetRecord", "row:JobRecord", "row:_TargetReservation", "row:OwnerRecord",
    ]


def test_each_fence_writes_its_own_proof(posted):
    h, job_id, binding, launch = posted
    evidence_id = h.native.fence_unclaimed(binding, observed_agent_version="999")
    fence = data(h, native_key("unclaimed-fence", launch.launch_id))
    assert fence == {
        "kind": "terminal_unclaimed_launch_fence", "launch_id": launch.launch_id,
        "original_start_request_id": binding.start_request_id, "cancel_requested": False,
        "claim_never_issued": True, "no_provider_stop_claim": True,
        "failure_code": SESSION_VERSION_CHANGED, "observed_agent_version": "999",
    }
    saved = data(h, native_key("evidence", evidence_id))
    assert saved["probe_id"] == native_key("unclaimed-fence", launch.launch_id)
    assert saved["evidence"]["state"] == "failed" and saved["evidence"]["result"]["state"] == "failed"
    assert saved["evidence"]["native_execution_id"] == binding.start_request_id
    sealed = h.native.launch(launch.launch_id)
    assert (sealed.phase, sealed.sealed_evidence_id, sealed.evidence_sequence) == ("sealed", evidence_id, 1)
    target = data(h, h.native.target_key(launch.target, job_id))
    assert target["active_launch_id"] is None


def test_a_cancel_fence_writes_a_stopped_proof_without_a_result(posted):
    h, job_id, binding, launch = posted
    cancel_requested(h, job_id)
    evidence_id = h.native.fence_unclaimed(binding)
    fence = data(h, native_key("unclaimed-fence", launch.launch_id))
    assert fence["cancel_requested"] is True and "failure_code" not in fence and "observed_agent_version" not in fence
    saved = data(h, native_key("evidence", evidence_id))
    assert saved["evidence"]["state"] == "stopped" and saved["evidence"]["result"] is None


def test_a_cancel_and_a_refusal_are_different_requests(posted):
    h, job_id, binding, _ = posted
    first = h.native._request("fence-unclaimed", ("launch",), [binding.model_dump(mode="json"), "999"], ())
    second = h.native._request("fence-unclaimed", ("launch",), binding.model_dump(mode="json"), ())
    assert first.request_digest != second.request_digest
    seen = []
    transact = h.store.transact
    h.store.transact = lambda request, decide: seen.append(request) or transact(request, decide)
    h.native.fence_unclaimed(binding, observed_agent_version="999")
    assert seen[0].request_digest == first.request_digest


@pytest.mark.parametrize("failed", [False, True])
def test_a_claim_already_issued_is_never_fenced(posted, failed):
    h, job_id, binding, launch = posted
    observed = "999" if failed else None
    if not failed:
        cancel_requested(h, job_id)
    key = native_key("launch", launch.launch_id)
    saved = data(h, key)
    for field, value in (("native_invocation_id", "claimed"), ("claim_id", "claimed"), ("process_id", "claimed"),
                         ("report_token_digest", "a" * 64), ("child_pid", 7), ("phase", "claimed")):
        write(h.store, key, {**saved, field: value})
        refuses(409, "NATIVE_CLAIM_ALREADY_OWNED", h.native.fence_unclaimed, binding, observed_agent_version=observed)
    write(h.store, key, saved)


def test_a_binding_other_than_the_saved_start_is_refused(posted):
    h, job_id, binding, launch = posted
    key = worker_key("job", job_id)
    other = binding.model_copy(update={"generation": 2})
    refuses(409, "NATIVE_CLAIM_ALREADY_OWNED", h.native.fence_unclaimed, other, observed_agent_version="999")
    change(h, key, binding={**data(h, key)["binding"], "generation": 2})
    refuses(409, "NATIVE_CLAIM_ALREADY_OWNED", h.native.fence_unclaimed, binding, observed_agent_version="999")


def test_cancel_and_refusal_each_need_their_own_cancel_state(posted):
    h, job_id, binding, _ = posted
    refuses(409, "NATIVE_CLAIM_ALREADY_OWNED", h.native.fence_unclaimed, binding)
    cancel_requested(h, job_id)
    refuses(409, "NATIVE_CLAIM_ALREADY_OWNED", h.native.fence_unclaimed, binding, observed_agent_version="999")


def test_a_launch_saved_for_another_start_is_refused_even_when_the_job_matches(posted):
    h, _, binding, launch = posted
    key = native_key("launch", launch.launch_id)
    start = data(h, key)["start"]
    change(h, key, start={**start, "binding": {**start["binding"], "generation": 2}})
    refuses(409, "NATIVE_CLAIM_ALREADY_OWNED", h.native.fence_unclaimed, binding, observed_agent_version="999")


def test_a_target_moved_on_is_refused(posted):
    h, job_id, binding, launch = posted
    change(h, h.native.target_key(launch.target, job_id), active_launch_id="another-launch")
    refuses(409, "NATIVE_CLAIM_ALREADY_OWNED", h.native.fence_unclaimed, binding, observed_agent_version="999")


@pytest.mark.parametrize("updates", [
    {"state": "running"}, {"generation": 2}, {"children": ["child-job"]}, {"checkpoint": "Y2hlY2twb2ludA=="},
])
def test_job_activity_prevents_a_refusal_but_not_a_cancel(posted, updates):
    h, job_id, binding, _ = posted
    key = worker_key("job", job_id)
    saved = data(h, key)
    change(h, key, **updates)
    refuses(409, "NATIVE_VERSION_REFUSAL_UNRESOLVED", h.native.fence_unclaimed, binding, observed_agent_version="999")
    write(h.store, key, {**saved, **updates, "cancel_requested": True})
    assert h.native.fence_unclaimed(binding)


def test_capacity_uncertain_may_still_be_refused(posted):
    h, job_id, binding, _ = posted
    change(h, worker_key("job", job_id), state="capacity_uncertain")
    assert h.native.fence_unclaimed(binding, observed_agent_version="999")


def test_the_claim_check_runs_before_the_activity_check(posted):
    h, job_id, binding, launch = posted
    change(h, worker_key("job", job_id), generation=2)
    change(h, native_key("launch", launch.launch_id), claim_id="claimed")
    refuses(409, "NATIVE_CLAIM_ALREADY_OWNED", h.native.fence_unclaimed, binding, observed_agent_version="999")


def test_an_operation_added_after_the_owner_read_prevents_a_refusal(posted):
    """The ids read before the transaction stay the comparison; a re-read would lose this race."""
    h, _, binding, _ = posted
    owner_key = control_key("owner", CONTEXT.owner_record_id)
    transact = h.store.transact

    def after_the_read(request, decide):
        h.store.transact = transact
        change(h, owner_key, operation_ids=[*data(h, owner_key)["operation_ids"], "late-operation"])
        return transact(request, decide)

    h.store.transact = after_the_read
    refuses(409, "NATIVE_VERSION_REFUSAL_UNRESOLVED", h.native.fence_unclaimed, binding, observed_agent_version="999")


def test_a_row_saved_after_the_owner_read_prevents_a_refusal(posted):
    h, _, binding, _ = posted
    operations = data(h, control_key("owner", CONTEXT.owner_record_id))["operation_ids"]
    write(h.store, _unused_assignment_keys(binding, operations)[-1], {"kind": "retained-opaque"})
    refuses(409, "NATIVE_VERSION_REFUSAL_UNRESOLVED", h.native.fence_unclaimed, binding, observed_agent_version="999")


def refuses_because(status, error_code, message, call, *args, **kwargs):
    """Both halves of the authority check share one code, so the message is what tells them apart."""
    with pytest.raises(Problem) as refused:
        call(*args, **kwargs)
    error = refused.value.body["error"]
    assert (refused.value.status, error["code"], error["message"]) == (status, error_code, message)


ASSOCIATION = "current owner/grant/policy association differs"
DISPATCH = "native dispatch grant is revoked, stale, or expired"
OUTSIDE = "native invocation is outside the assigned job/attempt"
UNAVAILABLE = "one-use native launch permit is unavailable"
ANOTHER_PROCESS = "claim replay belongs to another process/permit"


def claim_command(h, launch, **updates):
    target = launch.target
    return wire.HostedClaimRequest(**{
        "request_id": "hosted-claim", "invocation_id": "original-native-invocation",
        "identity": wire.HostedIdentity(**{field: getattr(target, field) for field in (
            "agent_id", "agent_name", "agent_version", "session_id")}),
        "process_id": "original-supervisor",
        "launch_permit": SecretStr(h.native_calls[0].headers[wire.NATIVE_LAUNCH_PERMIT_HEADER]),
        **updates,
    })


def agent(launch):
    return BehaviorPrincipal("tenant", launch.target.agent_oid, "team", "agent", launch.target.agent_name,
                             frozenset({"consume"}))


def claim(h, launch, command=None, binding_id=None, principal=None, **kwargs):
    return h.native.claim(binding_id or launch.api_binding_id, launch.launch_id,
                          command or claim_command(h, launch), principal or agent(launch), **kwargs)


def test_a_fresh_claim_issues_the_saved_assignment_and_a_replay_gets_no_token(posted):
    h, job_id, binding, launch = posted
    fresh = claim(h, launch)
    saved = h.native.launch(launch.launch_id)
    assert (saved.phase, saved.process_id, saved.native_invocation_id) == (
        "claimed", "original-supervisor", "original-native-invocation")
    assert fresh.claim_state == "fresh" and fresh.report_token is not None
    assignment = fresh.assignment
    assert (assignment.launch_id, assignment.claim_id, assignment.invocation_id, assignment.process_id) == (
        launch.launch_id, saved.claim_id, "original-native-invocation", "original-supervisor")
    assert (assignment.process_generation, assignment.input, assignment.input_digest) == (
        1, launch.start.input, binding.input_digest)
    assert (assignment.previous_launch_id, assignment.checkpoint, assignment.checkpoint_after_bytes) == (
        None, None, None)
    assert assignment.identity == claim_command(h, launch).identity

    replay = claim(h, launch)
    assert replay.claim_state == "replay" and replay.report_token is None
    assert replay.assignment == assignment


def test_a_resumed_launch_names_the_launch_before_it(posted):
    h, job_id, binding, launch = posted
    key, job_key = native_key("launch", launch.launch_id), worker_key("job", job_id)
    resumed = {**data(h, job_key)["binding"], "checkpoint_digest": hashlib.sha256(b"checkpoint").hexdigest()}
    change(h, job_key, binding=resumed)
    change(h, key, start={**data(h, key)["start"], "checkpoint": "Y2hlY2twb2ludA==", "binding": resumed})
    assignment = claim(h, launch).assignment
    assert (assignment.checkpoint, assignment.previous_launch_id) == (
        b"checkpoint", native_key("launch-id", worker_key("start", job_id, "0")))


def test_a_fresh_claim_reads_and_checks_in_this_order(posted, monkeypatch):
    h, _, _, launch = posted
    events = trace(h, monkeypatch)
    claim(h, launch)
    assert events == [
        "read_snapshot", "row:_NativeLaunch", "transact", "row:_NativeLaunch", "_current", "row:OwnerRecord",
        "row:NativeExecutionGrant", "row:PolicyRecord", "row:BudgetRecord", "row:JobRecord",
        "row:_TargetReservation", "read_snapshot", "row:_NativeLaunch", "read_snapshot", "_current",
        "row:OwnerRecord", "row:NativeExecutionGrant", "row:PolicyRecord", "row:BudgetRecord",
    ]


def test_a_replayed_claim_reads_and_checks_in_this_order(posted, monkeypatch):
    """A replay never re-enters the decision, yet still re-checks the committed launch once."""
    h, _, _, launch = posted
    claim(h, launch)
    events = trace(h, monkeypatch)
    claim(h, launch)
    assert events == [
        "read_snapshot", "row:_NativeLaunch", "transact", "read_snapshot", "row:_NativeLaunch", "read_snapshot",
        "_current", "row:OwnerRecord", "row:NativeExecutionGrant", "row:PolicyRecord", "row:BudgetRecord",
    ]


@pytest.mark.parametrize("workload_version,registered", [("checksum-count-v1", True), ("assistant-v1.2", False)])
def test_a_bounded_claim_needs_the_registered_assistant_profile_before_anything_else(
    posted, workload_version, registered,
):
    h, _, _, launch = posted
    change(h, native_key("launch", launch.launch_id), workload_version=workload_version)
    h.native.assistant = SimpleNamespace() if registered else None
    other = BehaviorPrincipal("tenant", "another-oid", "team", "agent", "another", frozenset({"consume"}))
    refuses_because(403, "NATIVE_ASSISTANT_DISABLED", "bounded claim requires the registered assistant profile",
                    claim, h, launch, binding_id="another-binding", principal=other, assistant_conflicts=True)
    assert h.native.launch(launch.launch_id).phase == "post_unknown"


def test_the_agent_is_checked_before_the_assignment(posted):
    h, _, _, launch = posted
    other = BehaviorPrincipal("tenant", "another-oid", "team", "agent", "another", frozenset({"consume"}))
    refuses(403, "NATIVE_EXECUTOR_IDENTITY_DENIED", claim, h, launch, binding_id="another-binding", principal=other)


def test_a_claim_for_another_binding_is_refused_before_the_transaction(posted, monkeypatch):
    h, _, _, launch = posted
    events = trace(h, monkeypatch)
    refuses_because(403, "NATIVE_CLAIM_DENIED", OUTSIDE, claim, h, launch, binding_id="another-binding")
    assert "transact" not in events


def test_a_claim_with_another_invocation_than_the_saved_one_is_refused_before_the_transaction(posted):
    h, _, _, launch = posted
    change(h, native_key("launch", launch.launch_id), native_invocation_id="saved-invocation")
    refuses_because(403, "NATIVE_CLAIM_DENIED", OUTSIDE, claim, h, launch)
    # The same invocation passes the early check, and the saved one still makes the permit used.
    refuses_because(403, "NATIVE_CLAIM_DENIED", UNAVAILABLE, claim, h, launch,
                    claim_command(h, launch, invocation_id="saved-invocation"))


def test_a_claim_with_another_permit_is_refused(posted):
    h, _, _, launch = posted
    refuses_because(403, "NATIVE_CLAIM_DENIED", UNAVAILABLE, claim, h, launch,
                    claim_command(h, launch, launch_permit=SecretStr("0" * 64)))
    assert h.native.launch(launch.launch_id).phase == "post_unknown"


@pytest.mark.parametrize("row,updates", [
    ("launch", {"phase": "claimed"}),
    ("job", {"cancel_requested": True}),
    ("job_binding", {"generation": 2}),
    ("target", {"active_launch_id": "another-launch"}),
    ("target", {"generation": 2}),
])
def test_a_claim_once_its_permit_is_no_longer_usable_is_refused(posted, row, updates):
    h, job_id, _, launch = posted
    key = {"launch": native_key("launch", launch.launch_id), "job": worker_key("job", job_id),
           "job_binding": worker_key("job", job_id), "target": h.native.target_key(launch.target, job_id)}[row]
    if row == "job_binding":
        updates = {"binding": {**data(h, key)["binding"], **updates}}
    change(h, key, **updates)
    refuses_because(403, "NATIVE_CLAIM_DENIED", UNAVAILABLE, claim, h, launch)


@pytest.mark.parametrize("field,value", [("process_id", "another-process"), ("permit_digest", "a" * 64)])
def test_a_replay_of_a_claim_another_process_holds_is_refused(posted, field, value):
    h, _, _, launch = posted
    claim(h, launch)
    change(h, native_key("launch", launch.launch_id), **{field: value})
    refuses_because(403, "NATIVE_CLAIM_DENIED", ANOTHER_PROCESS, claim, h, launch)


def test_a_replay_with_another_invocation_than_the_claimed_one_is_refused_early(posted):
    h, _, _, launch = posted
    claim(h, launch)
    change(h, native_key("launch", launch.launch_id), native_invocation_id="another-invocation")
    refuses_because(403, "NATIVE_CLAIM_DENIED", OUTSIDE, claim, h, launch)


def authority_change(h, launch, updates):
    grant = data(h, native_key("grant", launch.grant_id))
    if updates == "another_target":
        return {"targets": [{**target, "session_id": "another-session"} for target in grant["targets"]]}
    if updates == "another_definition":
        return {"definition": {**grant["definition"], "work_id": "another-work"}}
    if updates == "another_owner":
        return {"owner": {**CONTEXT.model_dump(mode="json"), "epoch": 2}}
    return updates


@pytest.mark.parametrize("row,updates,message", [
    ("grant", {"controller_id": "another-controller"}, ASSOCIATION),
    ("grant", {"job_id": "another-job"}, ASSOCIATION),
    ("grant", {"grant_id": "another-grant"}, ASSOCIATION),
    ("grant", {"input_digest": "a" * 64}, ASSOCIATION),
    ("grant", "another_definition", ASSOCIATION),
    ("grant", "another_target", ASSOCIATION),
    ("grant", {"workload_version": "assistant-v1.2"}, ASSOCIATION),
    ("owner", {"active": False}, ASSOCIATION),
    ("owner", {"principal_id": "another-person"}, ASSOCIATION),
    ("owner", {"conversation_id": "another-conversation"}, ASSOCIATION),
    ("policy", {"policy_id": "another-policy"}, ASSOCIATION),
    ("budget", {"root_budget_id": "another-root"}, ASSOCIATION),
    ("grant", {"active": False}, DISPATCH),
    ("policy", {"active": False}, DISPATCH),
    ("budget", {"active": False}, DISPATCH),
    ("policy", {"revision": 2}, DISPATCH),
    ("owner", {"epoch": 2}, DISPATCH),
    ("grant", "another_owner", DISPATCH),
    ("grant", {"expires_at": None}, DISPATCH),
])
def test_a_claim_outside_the_current_authority_is_refused_with_its_reason(posted, row, updates, message):
    h, _, _, launch = posted
    key = {"grant": native_key("grant", launch.grant_id), "owner": control_key("owner", CONTEXT.owner_record_id),
           "policy": control_key("policy", CONTEXT.policy_id), "budget": control_key("budget", CONTEXT.root_budget_id)}[row]
    change(h, key, **authority_change(h, launch, updates))
    refuses_because(403, "NATIVE_AUTHORITY_FENCED", message, claim, h, launch)
    assert h.native.launch(launch.launch_id).phase == "post_unknown"


def test_an_association_problem_is_reported_before_a_dispatch_problem(posted):
    h, _, _, launch = posted
    change(h, native_key("grant", launch.grant_id), active=False, controller_id="another-controller")
    refuses_because(403, "NATIVE_AUTHORITY_FENCED", ASSOCIATION, claim, h, launch)


@pytest.mark.parametrize("offset,allowed", [
    (timedelta(0), True), (-timedelta(microseconds=1), False),
    (timedelta(hours=1) - timedelta(microseconds=1), True), (timedelta(hours=1), False),
])
def test_a_claim_needs_the_grant_window(posted, offset, allowed):
    h, _, _, launch = posted
    h.time = NOW + offset
    if allowed:
        assert claim(h, launch).claim_state == "fresh"
    else:
        refuses_because(403, "NATIVE_AUTHORITY_FENCED", DISPATCH, claim, h, launch)


def test_a_probe_checks_only_the_association(posted):
    """Observing a launch never needs a dispatchable grant; it does need the same owner and grant."""
    h, _, binding, launch = posted
    change(h, native_key("grant", launch.grant_id), active=False)
    change(h, control_key("policy", CONTEXT.policy_id), active=False, revision=2)
    change(h, control_key("owner", CONTEXT.owner_record_id), epoch=2)
    h.time = NOW + timedelta(hours=2)
    assert h.native.probe(binding).launch_id == launch.launch_id
    change(h, native_key("grant", launch.grant_id), controller_id="another-controller")
    refuses_because(403, "NATIVE_AUTHORITY_FENCED", ASSOCIATION, h.native.probe, binding)
