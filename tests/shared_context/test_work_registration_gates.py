"""The gates of `WorkPolicyControl.register`: replay, capture, and the admission transaction.

Rows come from real synthetic turns. Only what a case names is replaced, and the transaction cases
change what `changes` sees, as a concurrent writer would. No gate here is reached through a private
helper, so the same file pins the behavior before and after `register` is split.
"""
import hashlib
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from shared_context import behavior_work_registry as registry
from shared_context import behavior_instruction, behavior_work_policy
from shared_context.behavior_agent_worker import AgentVmBinding
from shared_context.behavior_assistant_wire import AssistantSubmit, InstructionSubmit
from shared_context.behavior_capabilities import available_grants
from shared_context.behavior_foundry import native_key
from shared_context.behavior_instruction import conversation_key
from shared_context.behavior_operations import control_key
from shared_context.behavior_profile_selection import (
    ConversationProfileHead, SelectConversationProfile, head_key, select_profile,
)
from shared_context.behavior_routine_wire import RoutineOccurrence
from shared_context.behavior_runtime import runtime_key
from shared_context.behavior_work_policy import WorkRegistration, derive_work, work_policy_key
from shared_context.errors import Problem
from test_agent_message_api import message_turn
from test_agent_runtime import peer_runtime
from test_agent_runtime_routing import routing_peer
from test_behavior_assistant import assistant
from test_behavior_store import store
from test_conversation_profile_selection import template
from test_conversation_transfer_startup import SyntheticSessionClient, saved_turn, synthetic_standing_v1
from test_instruction_flow import instruction_models, runtime_path, sdk_io
from test_m3_auth import jwk_client, signing_key
from test_routine_producer import routine_owner
from test_work_result_access import DIGEST, Reads

ADMISSION_CONFLICT = (412, "BEHAVIOR_CONFLICT")


def sha(command):
    return hashlib.sha256(command.model_dump_json().encode()).hexdigest()


def code(call, *args, **kwargs):
    with pytest.raises(Problem) as denied:
        call(*args, **kwargs)
    return denied.value.status, denied.value.body["error"]["code"], str(denied.value)


def instruction(request_id, **values):
    return InstructionSubmit(request_id=request_id, content="Synthetic registration gate.", **values)


def register(c, command, **kwargs):
    return c.control.register(c.human, command.request_id, sha(command), instruction=command, **kwargs)


def bypass_peer_check(monkeypatch, root=None):
    from shared_context import behavior_agent_peer_work as peer
    monkeypatch.setattr(peer, "authorize_registration", lambda *args: root)


@pytest.fixture
def structured(synthetic_standing_v1, monkeypatch):
    # The reviewed source fact belongs to `source_ready`; these cases are about what follows it.
    c = synthetic_standing_v1
    monkeypatch.setattr(c.control, "source_ready", lambda principal, policy: None)
    return c


@pytest.mark.parametrize("command", [instruction("routine-source"), AssistantSubmit(request_id="routine-source")])
def test_a_routine_needs_a_typed_occurrence(synthetic_standing_v1, command):
    c = synthetic_standing_v1
    assert code(c.control.register, c.human, command.request_id, sha(command),
                instruction=command if type(command) is InstructionSubmit else None, routine="an-occurrence")[:2] == (
        403, "ROUTINE_SOURCE_CHANGED")


def test_a_routine_needs_an_instruction(synthetic_standing_v1):
    c = synthetic_standing_v1
    occurrence = RoutineOccurrence.model_construct()
    assert code(c.control.register, c.human, "routine-source", DIGEST, routine=occurrence)[:2] == (
        403, "ROUTINE_SOURCE_CHANGED")


def test_a_routine_cannot_be_peer_work(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    bypass_peer_check(monkeypatch)
    command = instruction("routine-peer")
    assert code(register, c, command, peer_work=object(), routine=RoutineOccurrence.model_construct())[:2] == (
        403, "ROUTINE_SOURCE_CHANGED")


@pytest.fixture
def admitted(synthetic_standing_v1):
    c = synthetic_standing_v1
    command = instruction("registered-once")
    c.record = register(c, command)
    c.command = command
    c.registration = work_policy_key("registration", c.record.admission_id)
    return c


def test_the_same_request_returns_its_saved_work_and_recaches_it(admitted):
    c = admitted
    c.control.records.pop(c.record.binding.binding_id)
    assert register(c, c.command) == c.record
    assert c.control.records[c.record.binding.binding_id] == c.record


@pytest.mark.parametrize("field", ["active", "read_enabled"])
def test_a_replay_needs_current_rights(admitted, monkeypatch, field):
    c = admitted
    installed = c.control.installed
    monkeypatch.setattr(c.control, "installed", lambda rows=None: installed(rows).model_copy(update={field: False}))
    assert code(register, c, c.command)[:2] == (403, "WORK_POLICY_REVOKED")


def test_a_replay_with_other_bytes_is_a_conflict(admitted):
    c = admitted
    assert code(c.control.register, c.human, c.command.request_id, DIGEST, instruction=c.command) == (
        409, "WORK_POLICY_REQUEST_CONFLICT", "same work ID has different original input")


def test_a_replay_of_a_row_saved_for_another_request_is_a_conflict(admitted, monkeypatch):
    c = admitted
    Reads(c.h.runtime.store, monkeypatch).fields(c.registration, request_id="another-request")
    assert code(register, c, c.command) == (
        409, "WORK_POLICY_REQUEST_CONFLICT", "same work ID has different original input")


def test_a_replay_saved_for_another_principal_is_a_conflict(admitted, monkeypatch):
    c = admitted
    Reads(c.h.runtime.store, monkeypatch).fields(c.registration, principal_oid="another-person")
    assert code(register, c, c.command) == (
        409, "WORK_POLICY_REQUEST_CONFLICT", "same work ID has different original input")


def test_a_replay_from_another_occurrence_is_a_conflict(admitted):
    c = admitted
    assert code(register, c, c.command, routine=RoutineOccurrence.model_construct()) == (
        409, "WORK_POLICY_REQUEST_CONFLICT", "the original work has a different occurrence source")


def test_a_replay_cannot_pick_up_peer_work(admitted, monkeypatch):
    c = admitted
    bypass_peer_check(monkeypatch)
    assert code(register, c, c.command, peer_work=object())[:2] == (409, "AGENT_MESSAGE_ASSIGNMENT_DENIED")


def test_a_replay_cannot_move_to_another_vm(admitted):
    c = admitted
    assert code(register, c, c.command, agent_vm=SimpleNamespace()) == (
        409, "WORK_POLICY_REQUEST_CONFLICT", "the original request cannot be rebound to another VM")


def vm_of(c, hosted_session_id="server-agent-session", hosted_agent_version="41"):
    return AgentVmBinding(team_id=c.human.team, agent_id=c.native.targets[0].agent_id, vm_generation="generation-one",
                          hosted_session_id=hosted_session_id, hosted_agent_version=hosted_agent_version)


def test_a_replay_without_a_vm_still_returns_the_turn_saved_on_one(synthetic_standing_v1):
    c = synthetic_standing_v1
    command = instruction("a-turn-on-a-vm")
    saved = register(c, command, agent_vm=vm_of(c))
    assert register(c, command) == saved


def test_a_structured_replay_has_no_peer_source(structured, monkeypatch):
    c = structured
    command = AssistantSubmit(request_id="structured-replay")
    c.control.register(c.human, command.request_id, sha(command))
    bypass_peer_check(monkeypatch)
    assert code(c.control.register, c.human, command.request_id, sha(command), peer_work=object(),
                instruction=None)[:2] == (409, "AGENT_MESSAGE_ASSIGNMENT_DENIED")


def test_an_instruction_must_be_the_typed_original(synthetic_standing_v1):
    c = synthetic_standing_v1
    command = instruction("the-original")
    assert code(c.control.register, c.human, "another-request", sha(command), instruction=command)[:2] == (
        422, "WORK_POLICY_COMMAND")
    assert code(c.control.register, c.human, "not-typed", DIGEST, instruction=AssistantSubmit(request_id="not-typed"))[:2] == (
        422, "WORK_POLICY_COMMAND")


def test_a_mismatched_instruction_stops_before_any_conversation_read(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    monkeypatch.setattr(behavior_instruction, "instruction_state", lambda *a, **k: pytest.fail("read a conversation"))
    command = instruction("the-original")
    assert code(c.control.register, c.human, "another-request", sha(command), instruction=command)[:2] == (
        422, "WORK_POLICY_COMMAND")


def test_images_are_refused_here_and_never_dropped(synthetic_standing_v1):
    c = synthetic_standing_v1
    command = instruction("with-images").model_copy(update={"images": ("an-image",)})
    assert code(register, c, command) == (422, "WORK_POLICY_COMMAND", "attached images and files need a native Agent turn")


def test_a_new_conversation_refuses_a_profile_head_that_moved(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    policy = c.control.policy
    head = SimpleNamespace(payload={"data": ConversationProfileHead(selection_id="a-new-selection").model_dump(mode="json")})
    Reads(c.h.runtime.store, monkeypatch).row(head_key(policy.authority_domain, policy.agent_id), head)
    from shared_context import behavior_profile_selection as selection
    monkeypatch.setattr(selection, "choice_for_conversation", lambda control, conversation: None)
    assert code(register, c, instruction("head-moved")) == (
        409, "PROFILE_SELECTION_CONFLICT", "the new-conversation profile changed during admission")


def test_capabilities_only_come_from_a_human_selection(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    installed = c.control.installed
    grants = (available_grants(None, ("fa-agent-collaboration-v1",))["fa-agent-collaboration-v1"].ref,)

    def with_capabilities(rows=None):
        policy = installed(rows)
        return policy.model_copy(update={"profile": policy.profile.model_copy(update={
            "capabilities": grants, "prompt_profile": "fa-instruction-history-v1",
            "user_text_file_profile": "fa-user-text-files-v1"})})
    monkeypatch.setattr(c.control, "installed", with_capabilities)
    assert code(register, c, instruction("capability-without-selection"))[:2] == (403, "CAPABILITY_SELECTION_REQUIRED")


@pytest.fixture
def continuing(synthetic_standing_v1):
    c = synthetic_standing_v1
    first = saved_turn(c, "first-turn")
    c.first = c.control.records[first.binding.binding_id]
    c.cid = first.assignment.task.instruction.conversation_id
    c.next = instruction("next-turn", conversation_id=c.cid)
    return c


def own_read(control, monkeypatch, read):
    """Replace the previous-turn read of `register` only: memory and transfer read the same record."""
    real = control._job_record

    def job_record(job_id):
        mine = sys._getframe(1).f_code.co_filename.endswith("behavior_work_policy.py")
        return read(job_id) if mine else real(job_id)
    monkeypatch.setattr(control, "_job_record", job_record)
    return real


def test_a_continuation_refreshes_once_to_find_its_previous_turn(continuing, monkeypatch):
    c = continuing
    calls, refresh = [], c.control.refresh

    def refreshed(binding_id=None):
        if binding_id is None:
            calls.append("refresh")
        return refresh(binding_id)

    def read(job_id):
        calls.append(job_id)
        return None if calls.count(job_id) == 1 else real(job_id)
    real = own_read(c.control, monkeypatch, read)
    monkeypatch.setattr(c.control, "refresh", refreshed)
    assert register(c, c.next).profile.instruction.turn_number == 2
    job = c.first.binding.job_id
    assert calls[calls.index(job):] [:3] == [job, "refresh", job]


def test_a_continuation_needs_its_previous_turn(continuing, monkeypatch):
    c = continuing
    own_read(c.control, monkeypatch, lambda job_id: None)
    assert code(register, c, c.next) == (409, "CONVERSATION_CONTEXT_CHANGED", "original conversation profile is unavailable")


def test_a_continuation_needs_a_session_that_is_still_active(continuing, monkeypatch):
    c = continuing
    monkeypatch.setattr(SyntheticSessionClient, "verify_session", lambda self, *, before_send: SimpleNamespace(status="idle"))
    assert code(register, c, c.next) == (
        409, "CONVERSATION_SESSION_UNAVAILABLE",
        "the original session is not active and is not woken; reset or new session must be explicit")


def test_a_continuation_on_a_vm_does_not_read_the_session(continuing, monkeypatch):
    c = continuing
    real = behavior_instruction.instruction_state
    vms = []

    def on_a_vm(control, command, **kwargs):
        conversation, row = real(control, command, **kwargs)
        vms.append(vm_of(c, conversation.hosted_session_id, conversation.hosted_agent_version))
        return conversation.model_copy(update={"agent_vm": vms[-1]}), row
    monkeypatch.setattr(behavior_instruction, "instruction_state", on_a_vm)
    monkeypatch.setattr(SyntheticSessionClient, "verify_session", lambda *a, **k: pytest.fail("read the VM session"))
    vm = register(c, c.next).agent_vm
    assert vm == vms[-1] and vm.vm_generation == "generation-one"


def test_a_continuation_refuses_a_current_selection_that_moved(synthetic_standing_v1, monkeypatch):
    from shared_context import behavior_profile_selection as selection
    c = synthetic_standing_v1
    grants = available_grants(None, ("fa-agent-collaboration-v1",))
    c.h.runtime.capability_grants = grants
    select_profile(c.control, c.authority, c.human, SelectConversationProfile(
        request_id="select-capabilities", expected_selection_id=None,
        template=template("fa-instruction-history-v1", user_text_file_profile="fa-user-text-files-v1",
                          capabilities=(grants["fa-agent-collaboration-v1"].ref,))))
    first = saved_turn(c, "first-turn")
    real = selection.current_selection

    def moved(store, policy):
        return real(store, policy).model_copy(update={"selection_id": "a-moved-selection"})
    monkeypatch.setattr(selection, "current_selection", moved)
    following = instruction("next-turn", conversation_id=first.assignment.task.instruction.conversation_id)
    assert code(register, c, following) == (
        409, "PROFILE_SELECTION_CONFLICT", "the current profile selection changed during admission")


def previous_with(record, **changes):
    task = record.profile.instruction
    profile = record.profile.model_copy(update={"instruction": None if not changes else task.model_copy(update=changes)})
    return record.model_copy(update={"profile": profile})


@pytest.mark.parametrize("changes", [{}, {"conversation_id": "another-conversation"}, {"turn_number": 7}])
def test_a_continuation_refuses_a_previous_turn_of_another_conversation(continuing, monkeypatch, changes):
    c = continuing
    own_read(c.control, monkeypatch, lambda job_id: previous_with(c.first, **changes))
    assert code(register, c, c.next) == (409, "CONVERSATION_CONTEXT_CHANGED", "original conversation profile is unavailable")


def test_a_native_runtime_without_its_launch_authority_is_refused(structured):
    c = structured
    runtime = c.h.runtime
    runtime.dep = replace(runtime.dep, native_launches=None)
    command = AssistantSubmit(request_id="no-native-authority")
    assert code(c.control.register, c.human, command.request_id, sha(command))[:2] == (403, "WORK_POLICY_NATIVE_DISABLED")


def test_new_paid_work_needs_the_cost_decision_to_allow_it(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    real = behavior_work_policy.cumulative_decision

    def denied(policy, ledger, observation, now, *, read_id, **kwargs):
        decision = real(policy, ledger, observation, now, read_id=read_id, **kwargs)
        return SimpleNamespace(decision="deny", reason="cumulative_budget_exhausted") if read_id == "paid" else decision
    monkeypatch.setattr(behavior_work_policy, "cumulative_decision", denied)
    assert code(register, c, instruction("paid")) == (
        403, "WORK_POLICY_CUMULATIVE_BUDGET_EXHAUSTED", "new paid work is not authorized")


def test_a_structured_turn_needs_its_reviewed_source(synthetic_standing_v1):
    c = synthetic_standing_v1
    command = AssistantSubmit(request_id="no-reviewed-source")
    assert code(c.control.register, c.human, command.request_id, sha(command))[:2] == (409, "MEMORY_REQUIRED_FACT_MISSING")


def raced(monkeypatch, edit):
    """Run the real transaction, with `changes` seeing what `edit` makes of the current rows."""
    seen = SimpleNamespace(requests=[], results=[])
    real = registry.transact

    def transact(store, policy, request, decide, *, append=False):
        def changed(current):
            rows = dict(current)
            edit(rows)
            result = decide(rows)
            seen.results.append(result)
            return result
        seen.requests.append(request)
        return real(store, policy, request, changed, append=append)
    monkeypatch.setattr(registry, "transact", transact)
    return seen


def test_a_first_turn_writes_and_reads_in_its_original_order(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    seen = raced(monkeypatch, lambda rows: None)
    record = register(c, instruction("write-order"))
    b, control = record.binding, c.control
    registration = work_policy_key("registration", record.admission_id)
    new_rows = [registration, control_key("owner", b.owner.owner_record_id), control_key("policy", b.owner.policy_id),
                runtime_key("policy", b.binding_id), runtime_key("plan-index", b.binding_id)]
    writes = [write.row_id for write in seen.results[0].writes]
    assert writes[:5] == new_rows
    assert writes[-2:] == [native_key("grant", native_key("bootstrap-grant", b.job_id)),
                           conversation_key(record.profile.instruction.conversation_id)]
    assert seen.requests[0].request_id == registration
    assert seen.requests[0].read_set[:9] == (control.policy_key, control.revocation_key, control.cost_key,
                                            control.registry_key, *new_rows)
    policy = control.policy
    assert seen.requests[0].read_set[9:11] == (head_key(policy.authority_domain, policy.agent_id),
                                              conversation_key(record.profile.instruction.conversation_id))
    assert seen.results[0].result == {"admission_id": record.admission_id, "binding_id": b.binding_id,
                                      "not_yet_admitted": True}
    assert isinstance(record, WorkRegistration) and record.hosted_session_id is not None
    assert record.turn_authorization is not None and record.runtime_policy.instruction


def test_a_structured_turn_has_no_conversation_write_and_keeps_its_deadline(structured, monkeypatch):
    c = structured
    seen = raced(monkeypatch, lambda rows: None)
    command = AssistantSubmit(request_id="structured-order")
    record = c.control.register(c.human, command.request_id, sha(command))
    assert record.hosted_session_id is None and record.turn_authorization is None
    assert record.runtime_policy.queue_deadline is not None
    assert len(seen.requests[0].read_set) == 9
    assert seen.results[0].writes[-1].row_id == native_key("grant", native_key("bootstrap-grant", record.binding.job_id))


def test_a_conversation_that_changed_is_a_conflict(continuing, monkeypatch):
    c = continuing
    key = conversation_key(c.cid)

    def edit(rows):
        rows[key] = None
    raced(monkeypatch, edit)
    assert code(register, c, c.next) == (*ADMISSION_CONFLICT, "original conversation changed before admission")


def test_a_continuation_conflicts_with_a_registry_that_moved(continuing, monkeypatch):
    c = continuing
    key = c.control.registry_key

    def edit(rows):
        rows[key] = None
    raced(monkeypatch, edit)
    assert code(register, c, c.next) == (*ADMISSION_CONFLICT, "the continuation registry changed before admission")


def test_a_new_conversation_ignores_a_registry_that_moved(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    store, control = c.h.runtime.store, c.control
    head = registry.head_key(control.registry_key)
    real = store.read_snapshot
    first = [True]

    def older(keys):
        rows = dict(real(keys))
        if head in rows and first:
            rows[head] = SimpleNamespace(payload={})
            first.clear()
        return rows
    monkeypatch.setattr(store, "read_snapshot", older)
    assert register(c, instruction("unrelated-registry-move")).profile.instruction.turn_number == 1


def test_a_profile_head_that_moved_is_a_conflict(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    policy = c.control.policy
    key = head_key(policy.authority_domain, policy.agent_id)

    def edit(rows):
        rows[key] = SimpleNamespace(payload={})
    raced(monkeypatch, edit)
    assert code(register, c, instruction("profile-head-race")) == (
        *ADMISSION_CONFLICT, "approved profile selection changed before admission")


def test_a_memory_row_that_moved_is_a_conflict(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1

    def edit(rows):
        rows[seen.requests[0].read_set[-1]] = SimpleNamespace()
    seen = raced(monkeypatch, edit)
    assert code(register, c, instruction("memory-race")) == (
        *ADMISSION_CONFLICT, "memory setting or selected note changed before admission")


def existing_row(rows):
    return rows[next(key for key, row in rows.items() if row is not None)]


@pytest.mark.parametrize("index", range(5))
def test_a_row_this_registration_creates_must_not_exist(synthetic_standing_v1, monkeypatch, index):
    c = synthetic_standing_v1
    command = instruction("exact-new-rows")
    policy = c.control.policy
    admission, binding, _ = derive_work(policy, command.request_id, "native")
    b = binding
    key = (work_policy_key("registration", admission), control_key("owner", b.owner.owner_record_id),
           control_key("policy", b.owner.policy_id), runtime_key("policy", b.binding_id),
           runtime_key("plan-index", b.binding_id))[index]

    def edit(rows):
        rows[key] = existing_row(rows)
    raced(monkeypatch, edit)
    assert code(register, c, command) == (409, "WORK_POLICY_SCOPE_CHANGED", "exact new registration changed")


def test_a_policy_that_changed_during_admission_is_refused(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    installed = c.control.installed
    calls = []

    def changing(rows=None):
        policy = installed(rows)
        if type(rows) is dict and "changed" in calls:
            return policy.model_copy(update={"revision": policy.revision + 1})
        return policy

    def edit(rows):
        calls.append("changed")
    monkeypatch.setattr(c.control, "installed", changing)
    raced(monkeypatch, edit)
    assert code(register, c, instruction("policy-race")) == (409, "WORK_POLICY_SCOPE_CHANGED", "exact new registration changed")


@pytest.mark.parametrize("count,refused", [(31, False), (32, True)])
def test_structured_work_has_a_bounded_registry(structured, monkeypatch, count, refused):
    c = structured
    monkeypatch.setattr(registry, "count_registry", lambda store, policy, rows=None: count)
    command = AssistantSubmit(request_id="bounded-registry")
    if refused:
        assert code(c.control.register, c.human, command.request_id, sha(command))[:2] == (429, "WORK_POLICY_CATALOG_CAPACITY")
    else:
        assert c.control.register(c.human, command.request_id, sha(command)).request_id == command.request_id


def test_instructions_are_not_bound_by_the_structured_registry_limit(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    monkeypatch.setattr(registry, "count_registry", lambda store, policy, rows=None: 32)
    assert register(c, instruction("unbounded-instruction")).request_id == "unbounded-instruction"


@pytest.fixture
def peer_work(message_turn, monkeypatch):
    from shared_context.behavior_agent_message_api import _read_root
    m = message_turn
    sent = m.client.post(m.path, json=m.command, headers=m.headers)
    assert sent.status_code == 200, sent.text
    control = m.b.runtime.work_policy
    record = next(r for r in control.records.values() if r.profile.instruction.peer_work is not None)
    reference = record.profile.instruction.peer_work
    bypass_peer_check(monkeypatch, _read_root(control.store, reference.root_id))
    return SimpleNamespace(control=control, record=record, reference=reference, root=_read_root(control.store, reference.root_id),
                           sender=SimpleNamespace(oid=record.principal_oid))


def replay_peer(p, *, sender=None, reference=None):
    saved = p.record
    command = InstructionSubmit(request_id=saved.request_id, content="Replayed peer work.")
    return p.control.register(sender or p.sender, saved.request_id, saved.submit_sha256, instruction=command,
                              peer_work=reference or p.reference)


def test_the_same_peer_work_replays(peer_work):
    assert replay_peer(peer_work) == peer_work.record


def test_peer_work_from_another_sender_is_a_conflict(peer_work):
    assert code(replay_peer, peer_work, sender=SimpleNamespace(oid="another-sender")) == (
        409, "WORK_POLICY_REQUEST_CONFLICT", "same work ID has different original input")


def test_peer_work_with_another_source_is_denied(peer_work):
    other = peer_work.reference.model_copy(update={"content_sha256": DIGEST})
    assert code(replay_peer, peer_work, reference=other) == (
        409, "AGENT_MESSAGE_ASSIGNMENT_DENIED", "the original receiver work has a different peer source")


def test_a_root_keeps_one_receiver_conversation(peer_work):
    p = peer_work
    command = InstructionSubmit(request_id="another-peer-request", content="A second receiver.")
    assert code(p.control.register, p.sender, command.request_id, sha(command), instruction=command,
                peer_work=p.reference) == (
        409, "AGENT_MESSAGE_HANDOFF_EXISTS", "this root already has its original receiver conversation")


def hide_receiver(p, monkeypatch):
    """Let a second receiver through the pre-read: both reads happen before the transaction."""
    key = conversation_key(p.reference.conversation_id)
    real = p.control.store.read_snapshot

    def hidden(keys):
        rows = dict(real(keys))
        if tuple(keys) == (key,):
            rows[key] = None
        return rows
    monkeypatch.setattr(p.control.store, "read_snapshot", hidden)
    return key


def second_receiver(p):
    command = InstructionSubmit(request_id="peer-race", content="A second receiver.")
    return code(p.control.register, p.sender, command.request_id, sha(command), instruction=command, peer_work=p.reference)


def test_a_peer_conversation_created_meanwhile_is_a_conflict(peer_work, monkeypatch):
    hide_receiver(peer_work, monkeypatch)
    assert second_receiver(peer_work) == (*ADMISSION_CONFLICT, "original conversation changed before admission")


def test_a_peer_assignment_that_moved_is_a_conflict(peer_work, monkeypatch):
    from shared_context.behavior_agent_peer_work import peer_reads
    p = peer_work
    key = hide_receiver(p, monkeypatch)

    def edit(rows):
        rows[key] = None
        rows[peer_reads(p.root)[0]] = SimpleNamespace()
    raced(monkeypatch, edit)
    assert second_receiver(p) == (*ADMISSION_CONFLICT, "the original peer assignment changed before registration")


def test_a_peer_registration_rechecks_its_parent_inside_the_transaction(peer_work, monkeypatch):
    from shared_context import behavior_delegation
    p = peer_work
    key = hide_receiver(p, monkeypatch)
    origins = []

    def parent_gone(current, origin):
        origins.append(origin)
        raise Problem(409, "A_PARENT_GONE", "the parent moved")

    def edit(rows):
        rows[key] = None
    raced(monkeypatch, edit)
    monkeypatch.setattr(behavior_delegation, "_current_parent", parent_gone)
    assert second_receiver(p)[:2] == (409, "A_PARENT_GONE")
    assert origins == [p.root.origin]


def test_a_routine_is_checked_again_inside_the_transaction(routine_owner, monkeypatch):
    from shared_context import behavior_routine
    c = routine_owner
    state = SimpleNamespace(inside=False, checks=[])
    real = behavior_routine.validate_submission

    def checked(*args, **kwargs):
        state.checks.append(state.inside)
        return real(*args, **kwargs)

    def edit(rows):
        state.inside = True
    monkeypatch.setattr(behavior_routine, "validate_submission", checked)
    raced(monkeypatch, edit)
    response = c.h.client.post("/work-policy/v1/routines/daily-check/trigger", json={"scheduled_for": c.scheduled_for},
                               headers=c.h.headers())
    assert response.status_code == 200 and state.checks[-1] is True
