"""The peer result forward: refusals, saved forward record and replay; offline native I/O."""

from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest

from shared_context.behavior_agent_message_api import _read_root
import shared_context.behavior_agent_message_runtime as message_runtime
from shared_context.behavior_agent_message_runtime import (
    SAVED_RESULT_LIMIT_NOTICE, STOPPED_PEER_NOTICE, PeerResultForward, _controller_principal, forward_result,
    reference_refusal_notice,
)
from shared_context.behavior_agent_message_wire import MAX_AGENT_MESSAGE_BYTES
from shared_context.behavior_agent_messages import (
    AgentMessageLedger, claim_key, message_key, message_state_key,
)
from shared_context.behavior_assistant import key
from shared_context.behavior_operations import control_key
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite, InlineMaterial
from shared_context.behavior_delivery import VerifiedWorkResult
from shared_context.behavior_workers import ExecutorEvidence, worker_key
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_agent_message_api import message_turn, shared_message_turn
from test_agent_message_continuation import claim_peer, save_peer, start_peer
from test_agent_runtime import peer_runtime
from test_agent_runtime_routing import routing_peer
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_m2_copilot import runtime_path


def saved(m, signing_key, **options):
    started = start_peer(m)
    peer = claim_peer(m, started, signing_key)
    save_peer(m, started, peer, **options)
    return started


def forward(m, started, **job_changes):
    receiver = started.receiver
    store = m.b.runtime.store
    reference = receiver.profile.instruction.peer_work
    root = _read_root(store, reference.root_id)
    ledger = AgentMessageLedger(store, root.directory, clock=m.b.runtime._now)
    message = ledger.read(_controller_principal(root.directory.agents[1]), reference.message_key)
    job = m.b.runtime.dep.workers.inspect(receiver.binding.job_id).model_copy(update=job_changes)
    return forward_result(m.b, receiver, root, ledger, message, job)


TEXT = "Synthetic B review: 530 items remain."


def row(m, row_id):
    return m.b.runtime.store.read_snapshot((row_id,))[row_id]


def tamper(m, row_id, **updates):
    request = ControlRequest(request_id=control_key("tamper", row_id, *updates),
                             request_digest=hashlib.sha256(row_id.encode()).hexdigest(), read_set=(row_id,))

    def change(rows):
        payload = rows[row_id].payload
        material = None if payload["material"] is None else InlineMaterial.model_validate_json(json.dumps(payload["material"]))
        return ControlChanges(writes=(ControlWrite(row_id=row_id, mode="replace", data={**payload["data"], **updates},
                                                   material=material),), result={"synthetic_only": True})

    m.b.runtime.store.transact(request, change)


def keys_of(started):
    binding, generation = started.receiver.binding, "1"
    return dict(completion=worker_key("completion", binding.job_id, generation), result=key("result", binding.work.work_id),
                reply=key("reply", binding.job_id), forward=control_key("agent-message-result-forward", binding.job_id),
                job=worker_key("job", binding.job_id))


def refused(m, started, expected="AGENT_MESSAGE_RESULT_INVALID", **job_changes):
    with pytest.raises(Problem) as error:
        forward(m, started, **job_changes)
    assert error.value.body["error"]["code"] == expected
    assert row(m, keys_of(started)["forward"]) is None
    assert not m.b.runtime.dep.workers.inspect(started.receiver.binding.job_id).result_forwarded


def response_key_of(m, started):
    binding, reference = started.receiver.binding, started.receiver.profile.instruction.peer_work
    root = _read_root(m.b.runtime.store, reference.root_id)
    return message_key(root.spec.root_request_id, reference.recipient_agent_id,
                       control_key("agent-message-result", binding.job_id))


def response_of(m, started):
    root = _read_root(m.b.runtime.store, started.receiver.profile.instruction.peer_work.root_id)
    ledger = AgentMessageLedger(m.b.runtime.store, root.directory, clock=m.b.runtime._now)
    return ledger.read(_controller_principal(root.directory.agents[1]), response_key_of(m, started))


def with_refs(m):
    source = m.a.runtime.profiles[m.turn.binding.binding_id].source
    m.command["message"]["refs"] = [{
        "resource_kind": "source", "visibility": "team", "tenant_id": m.turn.binding.tenant_id,
        "team_id": m.turn.binding.team_id, "resource_id": source.source_id,
        "version": str(source.version), "sha256": source.sha256,
    }]


def refuse_current_once(monkeypatch, code):
    """A retry without the refusal would succeed, so only the first answer decides."""
    real, calls = message_runtime._result_current, []

    def unavailable(*args):
        calls.append(args)
        if len(calls) == 1:
            raise Problem(409, code, "Injected refusal after the SDK result was saved")
        return real(*args)

    monkeypatch.setattr(message_runtime, "_result_current", unavailable)


def crash_before_the_forward_record(m, started):
    store = m.b.runtime.store
    forward_row, transact = keys_of(started)["forward"], store.transact

    def crash(request, decide):
        if request.request_id == forward_row:
            raise RuntimeError("synthetic crash")
        return transact(request, decide)

    store.transact = crash
    try:
        with pytest.raises(RuntimeError):
            forward(m, started)
    finally:
        store.transact = transact


def test_saved_result_forward_record_is_exact_and_replay_keeps_one_response(message_turn, signing_key):
    m = message_turn
    started = saved(m, signing_key)
    forward(m, started)
    keys, binding = keys_of(started), started.receiver.binding
    reference = started.receiver.profile.instruction.peer_work
    root = _read_root(m.b.runtime.store, reference.root_id)
    response = message_key(root.spec.root_request_id, reference.recipient_agent_id,
                           control_key("agent-message-result", binding.job_id))
    claim = row(m, claim_key(reference.message_key)).payload["data"]
    result = row(m, keys["result"]).payload["data"]
    saved_forward = PeerResultForward.model_validate_json(json.dumps(row(m, keys["forward"]).payload["data"]))
    assert (saved_forward.root_id, saved_forward.job_id, saved_forward.claim_id) == (
        root.request.request_id, binding.job_id, claim["claim_id"])
    assert saved_forward.result_message_key == response and saved_forward.result_sha256 == digest(result)
    assert saved_forward.stop_evidence_sha256 is None
    assert saved_forward.request.request_id == keys["forward"]
    assert saved_forward.request.request_digest == digest([
        root.request.request_id, binding.job_id, claim["claim_id"], response, digest(result)])
    assert saved_forward.request.read_set == (
        keys["job"], m.b.runtime.dep.workers.pool_key(binding.job_id), keys["completion"], keys["result"],
        keys["forward"], response, message_state_key(reference.message_key))
    assert m.b.runtime.dep.workers.inspect(binding.job_id).result_forwarded
    response_message = response_of(m, started)
    assert response_message.command.kind == "result" and response_message.command.content == TEXT
    assert response_message.command.in_reply_to == reference.message_key
    assert response_message.command.recipient_agent_id == root.spec.origin_agent_id
    first = row(m, keys["forward"]).payload
    forward(m, started)
    assert row(m, keys["forward"]).payload == first and response_of(m, started) == response_message


@pytest.mark.parametrize("change", ["binding", "evidence"])
def test_forward_requires_the_job_to_agree_with_its_completion(message_turn, signing_key, change):
    m = message_turn
    started = saved(m, signing_key)
    job = m.b.runtime.dep.workers.inspect(started.receiver.binding.job_id)
    updates = {"binding": job.binding.model_copy(update={"start_request_id": "f" * 64})} if change == "binding" else {
        "last_evidence_id": "another-evidence"}
    refused(m, started, **updates)


@pytest.mark.parametrize("updates", [
    {"state": "unknown", "entire_tree_idle": False, "all_inflight_starts_fenced": True},
    {"state": "unknown", "entire_tree_idle": True, "all_inflight_starts_fenced": False},
], ids=["tree-busy", "starts-unfenced"])
def test_forward_requires_a_fully_idle_completion(message_turn, signing_key, updates):
    m = message_turn
    started = stopped_peer(m, signing_key)
    tamper(m, keys_of(started)["completion"], **updates)
    refused(m, started)


def test_forward_requires_the_saved_result_to_match_its_completion(message_turn, signing_key):
    m = message_turn
    started = saved(m, signing_key)
    tamper(m, keys_of(started)["result"], proof="YW5vdGhlcg==")
    refused(m, started)


def test_forward_requires_the_result_to_belong_to_this_work(message_turn, signing_key):
    m = message_turn
    started = saved(m, signing_key)
    keys, job = keys_of(started), m.b.runtime.dep.workers.inspect(started.receiver.binding.job_id)
    binding = job.binding.model_copy(update={"work_id": "another-work"})
    result = {**row(m, keys["result"]).payload["data"], "work_id": "another-work"}
    tamper(m, keys["result"], work_id="another-work")
    tamper(m, keys["completion"], binding=binding.model_dump(mode="json"), result=result)
    refused(m, started, binding=binding)


def test_successful_result_requires_its_complete_saved_reply(message_turn, signing_key, monkeypatch):
    m = message_turn
    started = saved(m, signing_key)
    store, reply_key = m.b.runtime.store, keys_of(started)["reply"]
    read = store.read_snapshot
    monkeypatch.setattr(store, "read_snapshot", lambda keys: {
        name: None if name == reply_key else value for name, value in read(keys).items()})
    with pytest.raises(Problem) as error:
        forward(m, started)
    assert error.value.body["error"]["code"] == "AGENT_MESSAGE_RESULT_INVALID"
    assert "complete saved SDK reply" in str(error.value.body)


def stopped_peer(m, signing_key, result=None):
    """Cancel a claimed peer and observe a verified whole-tree stop, by default with no saved result."""
    started = start_peer(m)
    claim_peer(m, started, signing_key)
    workers, binding = m.b.runtime.dep.workers, started.receiver.binding
    job = workers.inspect(binding.job_id)
    original = workers.adapter
    workers.adapter = replace(original, stop=lambda command: None)
    try:
        workers.cancel(job.spec.job_id, job.owner, expected_binding=job.binding)
        current = workers.inspect(binding.job_id)
        evidence = ExecutorEvidence(
            evidence_id="synthetic-stop", binding=current.binding, sequence=current.last_sequence + 1,
            native_execution_id=current.native_execution_id or "synthetic-native", state="stopped",
            entire_tree_idle=True, all_inflight_starts_fenced=True, proof=b"synthetic stop fixture",
            **({} if result is None else {"result": result(current)}))
        workers.adapter = replace(original, verify=lambda _: evidence)
        workers.observe(binding.job_id, evidence.evidence_id, current.owner)
    finally:
        workers.adapter = original
    assert workers.inspect(binding.job_id).state == "cancelled"
    return started


def test_verified_stop_forwards_one_blocked_notice_with_its_evidence(message_turn, signing_key):
    m = message_turn
    started = stopped_peer(m, signing_key)
    keys = keys_of(started)
    completion = row(m, keys["completion"]).payload["data"]
    forward(m, started)
    saved_forward = PeerResultForward.model_validate_json(json.dumps(row(m, keys["forward"]).payload["data"]))
    assert saved_forward.result_sha256 is None and saved_forward.stop_evidence_sha256 == digest(completion)
    reference = started.receiver.profile.instruction.peer_work
    root = _read_root(m.b.runtime.store, reference.root_id)
    claim = row(m, claim_key(reference.message_key)).payload["data"]
    assert saved_forward.request.request_digest == digest([
        root.request.request_id, started.receiver.binding.job_id, claim["claim_id"],
        saved_forward.result_message_key, None, digest(completion)])
    ledger = AgentMessageLedger(m.b.runtime.store, root.directory, clock=m.b.runtime._now)
    response = ledger.read(_controller_principal(root.directory.agents[1]), saved_forward.result_message_key)
    assert response.command.kind == "blocked" and response.command.content == STOPPED_PEER_NOTICE
    assert response.command.refs == ()


def seed(m, row_id):
    m.b.runtime.store.transact(
        ControlRequest(request_id=control_key("seed", row_id), request_digest=hashlib.sha256(row_id.encode()).hexdigest()),
        lambda _: ControlChanges(writes=(ControlWrite(row_id=row_id, mode="create", data={"kind": "late"}),),
                                 result={"synthetic_only": True}))


@pytest.mark.parametrize("name", ["result", "reply"])
def test_stop_without_a_result_refuses_any_saved_result(message_turn, signing_key, name):
    m = message_turn
    started = stopped_peer(m, signing_key)
    seed(m, keys_of(started)[name])
    with pytest.raises(Problem) as error:
        forward(m, started)
    assert error.value.body["error"]["code"] == "AGENT_MESSAGE_RESULT_INVALID"
    assert "intact terminal stop" in str(error.value.body)


def test_stop_without_a_result_refuses_changed_evidence_bytes(message_turn, signing_key):
    m = message_turn
    started = stopped_peer(m, signing_key)
    tamper(m, keys_of(started)["completion"], proof="YW5vdGhlcg==")
    refused(m, started)


def test_stop_without_a_result_requires_a_cancel_request(message_turn, signing_key):
    m = message_turn
    started = stopped_peer(m, signing_key)
    refused(m, started, cancel_requested=False)


def test_stop_with_a_worker_result_reads_it_from_its_own_row(message_turn, signing_key):
    m = message_turn
    version = m.b.runtime.dep.delivery.contract.verifier_version
    started = stopped_peer(m, signing_key, lambda current: VerifiedWorkResult(
        evidence_id="stop-result", verifier_version=version, work_id=current.binding.work_id,
        definition_digest=digest(started_work(m)), state="cancelled", passed_checks=(),
        content=b"saved before the stop", proof=b"offline result proof"))
    keys = keys_of(started)
    forward(m, started)
    saved_row = worker_key("work-result", "stop-result")
    saved_forward = PeerResultForward.model_validate_json(json.dumps(row(m, keys["forward"]).payload["data"]))
    assert saved_row in saved_forward.request.read_set and keys["result"] not in saved_forward.request.read_set
    assert saved_forward.result_sha256 == digest(row(m, saved_row).payload["data"])
    assert saved_forward.stop_evidence_sha256 is None
    message = response_of(m, started).command
    assert message.kind == "blocked" and message.refs == ()
    assert message.content == "The peer work ended as cancelled; no successful result was produced."


def started_work(m):
    binding = next(iter(m.b.runtime.work_policy.records.values())).binding
    return binding.work.model_dump(mode="json")


def test_saved_result_keeps_the_original_references(shared_message_turn, signing_key):
    m = shared_message_turn
    with_refs(m)
    started = saved(m, signing_key)
    forward(m, started)
    command = response_of(m, started).command
    assert command.kind == "result" and [ref.resource_kind for ref in command.refs] == ["source"]


def test_result_over_the_message_limit_is_replaced_by_the_saved_notice(shared_message_turn, signing_key):
    m = shared_message_turn
    with_refs(m)
    started = saved(m, signing_key, text="x" * MAX_AGENT_MESSAGE_BYTES)
    forward(m, started)
    command = response_of(m, started).command
    assert (command.kind, command.content, command.refs) == ("blocked", SAVED_RESULT_LIMIT_NOTICE, ())


def test_failed_result_with_a_saved_reply_is_blocked_with_that_reply(message_turn, signing_key):
    m = message_turn
    started = saved(m, signing_key, failure="sdk_failed")
    forward(m, started)
    command = response_of(m, started).command
    assert (command.kind, command.content) == ("blocked", TEXT)


def test_reference_refusal_sends_only_a_blocked_notice(shared_message_turn, signing_key, monkeypatch):
    m = shared_message_turn
    with_refs(m)
    started = saved(m, signing_key)
    real = message_runtime._result_current

    def refuse_refs(root, peer, receiver, refs):
        if refs:
            raise Problem(409, "SOURCE_EXPIRED", "Injected expiry")
        real(root, peer, receiver, refs)

    monkeypatch.setattr(message_runtime, "_result_current", refuse_refs)
    forward(m, started)
    command = response_of(m, started).command
    assert (command.kind, command.content, command.refs) == ("blocked", reference_refusal_notice("SOURCE_EXPIRED"), ())
    assert row(m, keys_of(started)["forward"]) is not None


def test_a_response_that_won_the_race_is_kept_after_a_reference_refusal(shared_message_turn, signing_key, monkeypatch):
    m = shared_message_turn
    with_refs(m)
    started = saved(m, signing_key)
    monkeypatch.setattr(message_runtime, "_result_current", lambda *args: None)
    send = AgentMessageLedger.send

    def race(self, principal, assignment, command):
        send(self, principal, assignment, command.model_copy(update={"kind": "blocked", "content": "won", "refs": ()}))
        raise Problem(409, "SOURCE_EXPIRED", "Injected expiry after a concurrent send")

    monkeypatch.setattr(AgentMessageLedger, "send", race)
    forward(m, started)
    assert response_of(m, started).command.content == "won"
    assert row(m, keys_of(started)["forward"]) is not None


def test_other_refusals_leave_the_saved_result_unforwarded(shared_message_turn, signing_key, monkeypatch):
    m = shared_message_turn
    with_refs(m)
    started = saved(m, signing_key)
    refuse_current_once(monkeypatch, "AGENT_MESSAGE_DISABLED")
    refused(m, started, "AGENT_MESSAGE_DISABLED")
    assert row(m, response_key_of(m, started)) is None


def test_reference_refusal_without_references_is_not_replaced(message_turn, signing_key, monkeypatch):
    m = message_turn
    started = saved(m, signing_key)
    refuse_current_once(monkeypatch, "SOURCE_EXPIRED")
    refused(m, started, "SOURCE_EXPIRED")
    assert row(m, response_key_of(m, started)) is None


def test_crash_before_the_forward_record_reuses_the_retained_response(message_turn, signing_key):
    m = message_turn
    started = saved(m, signing_key)
    crash_before_the_forward_record(m, started)
    first = response_of(m, started)
    forward(m, started)
    assert response_of(m, started) == first
    assert m.b.runtime.dep.workers.inspect(started.receiver.binding.job_id).result_forwarded


@pytest.mark.parametrize("change", ["from_work", "to_work", "in_reply_to"])
def test_retained_response_must_belong_to_this_work(message_turn, signing_key, monkeypatch, change):
    m = message_turn
    started = saved(m, signing_key)
    crash_before_the_forward_record(m, started)
    target, read, acknowledged = response_key_of(m, started), AgentMessageLedger.read, []

    def changed(self, principal, identifier):
        value = read(self, principal, identifier)
        if identifier != target:
            return value
        if change == "in_reply_to":
            return SimpleNamespace(message=value.message, command=value.command.model_copy(update={"in_reply_to": "x"}))
        name = "from_work_id" if change == "from_work" else "to_work_id"
        return SimpleNamespace(message=value.message.model_copy(update={name: "x"}), command=value.command)

    monkeypatch.setattr(AgentMessageLedger, "read", changed)
    # The crash already acknowledged once, and repeating it changes no row, so only a spy can see a second call.
    monkeypatch.setattr(AgentMessageLedger, "acknowledge", lambda self, *args, **kwargs: acknowledged.append(args))
    refused(m, started, "AGENT_MESSAGE_RESULT_CHANGED")
    assert acknowledged == []


def test_forward_refuses_a_job_that_changed_before_the_record(message_turn, signing_key):
    m = message_turn
    started = saved(m, signing_key)
    refused(m, started, "AGENT_MESSAGE_RESULT_CHANGED", cancel_requested=True)


CHANGES = {
    "completion": lambda m, keys, r: tamper(m, keys["completion"], proof="YW5vdGhlcg=="),
    "result": lambda m, keys, r: tamper(m, keys["result"], proof="YW5vdGhlcg=="),
    "accepted": lambda m, keys, r: tamper(m, r, payload_sha256="f" * 64),
    "consumed-status": lambda m, keys, r: tamper(m, keys["state"], status="claimed"),
    "consumed-claim": lambda m, keys, r: tamper(m, keys["state"], claim_id="another-claim"),
    "pending": lambda m, keys, r: tamper(m, keys["pool"], pending=[keys["job_id"]]),
    "completions": lambda m, keys, r: tamper(m, keys["pool"], completions=[keys["job_id"]]),
    "slots": lambda m, keys, r: tamper(m, keys["pool"], slots=[
        {**slot, "binding": keys["binding"]} for slot in row(m, keys["pool"]).payload["data"]["slots"]]),
}


@pytest.mark.parametrize("change", list(CHANGES))
def test_forward_rechecks_what_it_read_inside_the_record_transaction(message_turn, signing_key, monkeypatch, change):
    m = message_turn
    started = saved(m, signing_key)
    binding, reference = started.receiver.binding, started.receiver.profile.instruction.peer_work
    keys = {**keys_of(started), "state": message_state_key(reference.message_key), "job_id": binding.job_id,
            "pool": m.b.runtime.dep.workers.pool_key(binding.job_id),
            "binding": m.b.runtime.dep.workers.inspect(binding.job_id).binding.model_dump(mode="json")}
    acknowledge = AgentMessageLedger.acknowledge

    def then_change(self, *args, **kwargs):
        value = acknowledge(self, *args, **kwargs)
        CHANGES[change](m, keys, response_key_of(m, started))
        return value

    monkeypatch.setattr(AgentMessageLedger, "acknowledge", then_change)
    refused(m, started, "AGENT_MESSAGE_RESULT_CHANGED")


def cancelled_before_sdk(m, monkeypatch):
    response = m.client.post(m.path, json=m.command, headers=m.headers)
    assert response.status_code == 200, response.text
    receiver = next(iter(m.b.runtime.work_policy.records.values()))
    m.c.authority.record_instruction_activity(m.turn.session)
    workers = m.b.runtime.dep.workers
    with monkeypatch.context() as patch:
        patch.setattr(workers, "start", lambda *args, **kwargs: None)
        m.a.runtime.controller.step(m.turn.binding.binding_id)
    job = workers.inspect(receiver.binding.job_id)
    workers.cancel(job.spec.job_id, job.owner)
    job = workers.inspect(receiver.binding.job_id)
    assert (job.state, job.generation, job.binding, job.cancel_requested) == ("cancelled", 0, None, True)
    return SimpleNamespace(receiver=receiver)


def test_cancelled_before_sdk_forwards_one_blocked_notice(message_turn, monkeypatch):
    m = message_turn
    started = cancelled_before_sdk(m, monkeypatch)
    binding, reference = started.receiver.binding, started.receiver.profile.instruction.peer_work
    keys = keys_of(started)
    forward(m, started)
    root = _read_root(m.b.runtime.store, reference.root_id)
    claim = row(m, claim_key(reference.message_key)).payload["data"]
    saved_forward = PeerResultForward.model_validate_json(json.dumps(row(m, keys["forward"]).payload["data"]))
    assert saved_forward.result_sha256 is None and saved_forward.stop_evidence_sha256 is None
    assert saved_forward.request.request_digest == digest([
        root.request.request_id, binding.job_id, claim["claim_id"], saved_forward.result_message_key, None])
    assert worker_key("completion", binding.job_id, "0") in saved_forward.request.read_set
    command = response_of(m, started).command
    assert (command.kind, command.content, command.refs) == (
        "blocked", "The peer work was cancelled before SDK execution.", ())


def test_cancelled_before_sdk_drops_the_original_references(shared_message_turn, monkeypatch):
    m = shared_message_turn
    with_refs(m)
    started = cancelled_before_sdk(m, monkeypatch)
    forward(m, started)
    reference = started.receiver.profile.instruction.peer_work
    root = _read_root(m.b.runtime.store, reference.root_id)
    ledger = AgentMessageLedger(m.b.runtime.store, root.directory, clock=m.b.runtime._now)
    assert ledger.read(_controller_principal(root.directory.agents[1]), reference.message_key).command.refs
    command = response_of(m, started).command
    assert (command.kind, command.content, command.refs) == (
        "blocked", "The peer work was cancelled before SDK execution.", ())


def test_cancelled_before_sdk_requires_the_stopped_receipt(message_turn, monkeypatch):
    m = message_turn
    started = cancelled_before_sdk(m, monkeypatch)
    workers, asked = m.b.runtime.dep.workers, []

    def receipt(*args):
        asked.append(args)
        return SimpleNamespace(payload={"result": {"executor_stopped": False}})

    monkeypatch.setattr(workers, "read_cancellation_receipt", receipt)
    refused(m, started)
    job = workers.inspect(started.receiver.binding.job_id)
    assert asked == [(job.spec.job_id, job.owner, 0)]


@pytest.mark.parametrize("name", ["completion", "result", "reply"])
def test_cancelled_before_sdk_refuses_any_saved_row(message_turn, monkeypatch, name):
    m = message_turn
    started = cancelled_before_sdk(m, monkeypatch)
    binding = started.receiver.binding
    seed(m, {**keys_of(started), "completion": worker_key("completion", binding.job_id, "0")}[name])
    refused(m, started)
