"""Admitted ticket identity is local; mutable state and effect CAS are not cached."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError

from shared_context.behavior_assistant import _Session, key
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_instruction import instruction_state
from shared_context.behavior_operations import _row, _write
from shared_context.behavior_store import ControlChanges, ControlRequest
from shared_context.behavior_work_policy import WorkRegistration, WorkRegistry
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models
from test_light_route_authority import _agent, _session_id
from test_m3_auth import jwk_client, signing_key
from test_session_authority_read import _fresh_turn_beside_uncertain_work, runtime_path
from test_conversation_transfer_startup import saved_turn, synthetic_standing_v1


def _opened(assistant):
    h, _, binding, root, headers, control, human = _fresh_turn_beside_uncertain_work(assistant)
    return SimpleNamespace(h=h, binding=binding, sid=_session_id(root), control=control, human=human,
                           principal=_agent(h, binding), ticket=SecretStr(headers["x-behavior-assistant-ticket"]))


def test_ticket_authentication_reads_no_table_rows(assistant, monkeypatch):
    turn = _opened(assistant)
    monkeypatch.setattr(turn.h.store, "_read_physical",
                        lambda *_: pytest.fail("turn credential verification must not read Table"))
    captured = turn.h.assistant.turn_tickets.authenticate(
        turn.binding.binding_id, turn.sid, turn.ticket, turn.principal)
    assert captured.authorization_digest == digest(
        turn.control.records[turn.binding.binding_id].model_dump(mode="json"))
    assert captured.session_id == turn.sid


@pytest.mark.parametrize("mismatch", ["ticket", "session", "binding", "agent", "tenant", "team", "human"])
def test_wrong_ticket_or_identity_fails_without_reading_storage(assistant, monkeypatch, mismatch):
    turn = _opened(assistant)
    sid, binding, ticket, principal = turn.sid, turn.binding.binding_id, turn.ticket, turn.principal
    if mismatch == "ticket":
        ticket = SecretStr("0" * 64)
    elif mismatch == "session":
        sid = "0" * 64
    elif mismatch == "binding":
        binding = "another-work"
    else:
        changes = {"agent": {"oid": "another-agent"}, "tenant": {"tenant_id": "another-tenant"},
                   "team": {"team": "another-team"}, "human": {"kind": "human"}}
        principal = replace(principal, **changes[mismatch])
    monkeypatch.setattr(turn.h.store, "_read_physical",
                        lambda *_: pytest.fail("wrong credential must not reach Table"))
    with pytest.raises(Problem) as failure:
        turn.h.assistant.turn_tickets.authenticate(binding, sid, ticket, principal)
    assert failure.value.status == 403


def test_ticket_entry_reads_current_session_once_without_guard_or_registry(assistant, monkeypatch):
    turn = _opened(assistant)
    reads = []
    original = turn.h.store._read_physical

    def read(row_key, model):
        reads.append(row_key)
        return original(row_key, model)

    monkeypatch.setattr(turn.h.store, "_read_physical", read)
    session = turn.h.assistant.model_session(
        turn.binding.binding_id, turn.sid, turn.ticket, turn.principal)
    assert session.state == "running"
    assert reads == ["behavior:v1:row:" + key("session", turn.sid)]
    turn.h.mutate(key("session", turn.sid), context_read=True)
    reads.clear()
    current = turn.h.assistant.model_session(
        turn.binding.binding_id, turn.sid, turn.ticket, turn.principal)
    assert current.context_read is True and session.context_read is False
    assert reads == ["behavior:v1:row:" + key("session", turn.sid)]


@pytest.mark.parametrize("field", ["ticket_digest", "turn_authorization_digest", "binding_id"])
def test_a_changed_stored_ticket_is_not_hidden_by_the_index(assistant, field):
    turn = _opened(assistant)
    turn.h.mutate(key("session", turn.sid), **{field: "f" * 64})
    with pytest.raises(Problem) as failure:
        turn.h.assistant.model_session(turn.binding.binding_id, turn.sid, turn.ticket, turn.principal)
    assert failure.value.body["error"]["code"] == "ASSISTANT_TICKET_DENIED"


def test_legacy_registration_and_session_bytes_do_not_gain_ticket_fields(assistant):
    turn = _opened(assistant)
    record = turn.control.records[turn.binding.binding_id].model_copy(update={"turn_authorization": None})
    session = turn.h.assistant.model_session(
        turn.binding.binding_id, turn.sid, turn.ticket, turn.principal).model_copy(
            update={"turn_authorization_digest": None})
    assert "turn_authorization" not in record.model_dump(mode="json")
    assert "turn_authorization_digest" not in session.model_dump(mode="json")
    assert WorkRegistration.model_validate_json(record.model_dump_json()).model_dump_json() == record.model_dump_json()
    assert _Session.model_validate_json(session.model_dump_json()).model_dump_json() == session.model_dump_json()
    with pytest.raises(ValidationError):
        InstructionSubmit.model_validate({"request_id": "untrusted-ticket", "content": "OFFLINE",
                                          "turn_authorization": {"schema_version": "fa-admitted-turn-v1"}})


def test_completed_turn_cannot_use_a_withdrawn_registry_for_a_new_admission(synthetic_standing_v1):
    c = synthetic_standing_v1
    prior = saved_turn(c, "ticket-completed-turn")
    record = c.control.records[prior.binding.binding_id]
    command = InstructionSubmit(request_id="ticket-next-turn", content="OFFLINE follow-up",
                                conversation_id=prior.assignment.task.instruction.conversation_id)
    current, _ = instruction_state(c.control, command)
    assert current.active_job_id is None
    registry_key = c.control.registry_key
    request = ControlRequest(request_id="offline-withdraw-turn-registration", request_digest=digest("OFFLINE"),
                             read_set=(registry_key,))

    def withdraw(rows):
        registry = _row(rows, registry_key, WorkRegistry)
        return ControlChanges(writes=(_write(registry_key, registry.model_copy(update={
            "entries": tuple(value for value in registry.entries if value != record.admission_id)}), "replace"),),
            result={"withdrawn": True})

    c.control.store.transact(request, withdraw)
    c.control.refresh()
    guard = c.control.store._guard()
    with pytest.raises(Problem) as failure:
        c.control.submit(c.human, command)
    assert failure.value.body["error"]["code"] == "BEHAVIOR_BINDING_DENIED"
    assert c.control.store._guard() == guard


# --- Work admitted before turn tickets: no turn_authorization and no cached ticket, so model_session keeps the light
# principal/registry/ticket check (the docstring's "legacy instructions").

def _legacy(assistant, monkeypatch):
    turn = _opened(assistant)
    record = turn.control.records[turn.binding.binding_id]
    monkeypatch.setitem(turn.control.records, turn.binding.binding_id,
                        record.model_copy(update={"turn_authorization": None}))
    monkeypatch.delitem(turn.h.assistant.turn_tickets._tickets, turn.sid)
    monkeypatch.setattr(turn.h.assistant.turn_tickets, "authenticate",
                        lambda *args, **kwargs: pytest.fail("legacy work has no turn ticket to authenticate"))
    return turn


def _legacy_session(turn, *, binding_id=None, ticket=None, principal=None):
    return turn.h.assistant.model_session(binding_id or turn.binding.binding_id, turn.sid,
                                          turn.ticket if ticket is None else ticket, principal or turn.principal)


def _denied(call):
    with pytest.raises(Problem) as failure:
        call()
    return failure.value.status, failure.value.body["error"]["code"], failure.value.body["error"]["message"]


UNKNOWN = (403, "BEHAVIOR_BINDING_DENIED", "unknown authorized binding")
UNASSIGNED = (403, "BEHAVIOR_BINDING_DENIED", "principal is not assigned to this binding")
NO_TICKET = (403, "ASSISTANT_TICKET_DENIED", "server-issued binding ticket is required")


def test_legacy_work_reads_its_session_with_the_light_registry_check(assistant, monkeypatch):
    turn = _legacy(assistant, monkeypatch)
    monkeypatch.setattr(turn.h.assistant, "current_session", lambda *args, **kwargs: pytest.fail("light check only"))
    reads = []
    original = turn.h.store._read_physical

    def read(row_key, model):
        reads.append(row_key)
        return original(row_key, model)
    monkeypatch.setattr(turn.h.store, "_read_physical", read)
    session = _legacy_session(turn)
    assert session.binding_id == turn.binding.binding_id and session.state == "running"
    assert "behavior:v1:row:" + key("session", turn.sid) in reads
    assert "behavior:v1:row:" + turn.control.registry_key in reads


@pytest.mark.parametrize("change", ["no-record", "not-instruction", "other-binding", "historical", "no-work-policy"])
def test_work_outside_the_light_check_keeps_the_full_current_session_check(assistant, monkeypatch, change):
    turn = _legacy(assistant, monkeypatch)
    binding_id, records = turn.binding.binding_id, turn.control.records
    if change == "no-record":
        monkeypatch.delitem(records, binding_id)
    elif change == "not-instruction":
        record = records[binding_id]
        monkeypatch.setitem(records, binding_id, record.model_copy(update={
            "profile": record.profile.model_copy(update={"instruction": None})}))
    elif change == "other-binding":
        changed = turn.h.runtime.bindings[binding_id].model_copy(update={"event_id": "another"})
        monkeypatch.setattr(turn.h.runtime, "bindings", {**turn.h.runtime.bindings, binding_id: changed})
    elif change == "historical":
        monkeypatch.setattr(turn.h.runtime, "historical_binding_ids", frozenset({binding_id}))
    else:
        monkeypatch.setattr(turn.h.runtime, "work_policy", None)
    full = []
    monkeypatch.setattr(turn.h.assistant, "current_session", lambda *args: full.append(args) or "full check")
    assert _legacy_session(turn) == "full check"
    assert full == [(binding_id, turn.sid, turn.ticket, turn.principal)]


def test_legacy_work_without_its_binding_keeps_the_full_current_session_check(assistant, monkeypatch):
    turn = _legacy(assistant, monkeypatch)
    monkeypatch.setattr(turn.h.runtime, "bindings", {
        binding_id: binding for binding_id, binding in turn.h.runtime.bindings.items()
        if binding_id != turn.binding.binding_id})
    monkeypatch.setattr(turn.h.assistant, "current_session", lambda *args: "full check")
    assert _legacy_session(turn) == "full check"


@pytest.mark.parametrize("change, refusal", [
    ("not-a-principal", UNKNOWN),
    ("no-execute", (403, "BEHAVIOR_ACCESS_DENIED", "principal cannot perform this action in the requested team")),
    ("other-team", (403, "BEHAVIOR_ACCESS_DENIED", "principal cannot perform this action in the requested team")),
    ("other-tenant", UNASSIGNED), ("other-agent", UNASSIGNED), ("human-with-agent-oid", UNASSIGNED),
    ("human", NO_TICKET),
])
def test_legacy_work_refuses_a_principal_not_assigned_to_it_before_any_read(assistant, monkeypatch, change, refusal):
    turn = _legacy(assistant, monkeypatch)
    principal, binding = turn.principal, turn.binding
    changes = {"no-execute": {"actions": frozenset({"read", "submit"})}, "other-team": {"team": "another-team"},
               "other-tenant": {"tenant_id": "another-tenant"}, "other-agent": {"oid": "another-agent"},
               "human-with-agent-oid": {"kind": "human", "actor_id": None},
               "human": {"kind": "human", "actor_id": None, "oid": binding.human_oid}}
    principal = (SimpleNamespace(**vars(principal)) if change == "not-a-principal"
                 else replace(principal, **changes[change]))
    if change != "human":
        monkeypatch.setattr(turn.h.store, "_read_physical", lambda *_: pytest.fail("refused before Table"))
    assert _denied(lambda: _legacy_session(turn, principal=principal)) == refusal


def test_legacy_work_removed_from_the_registry_is_refused(assistant, monkeypatch):
    turn = _legacy(assistant, monkeypatch)
    admission = turn.control.records[turn.binding.binding_id].admission_id
    listed = turn.h.store.read_snapshot((turn.control.registry_key,))[turn.control.registry_key]
    turn.h.mutate(turn.control.registry_key,
                  entries=[entry for entry in listed.payload["data"]["entries"] if entry != admission])
    assert _denied(lambda: _legacy_session(turn)) == UNKNOWN


@pytest.mark.parametrize("change", ["wrong-ticket", "plain-string"])
def test_legacy_work_needs_the_server_issued_ticket(assistant, monkeypatch, change):
    turn = _legacy(assistant, monkeypatch)
    ticket = SecretStr("0" * 64) if change == "wrong-ticket" else turn.ticket.get_secret_value()
    assert _denied(lambda: _legacy_session(turn, ticket=ticket)) == NO_TICKET


def test_legacy_ticket_bound_to_another_assignment_is_refused(assistant, monkeypatch):
    turn = _legacy(assistant, monkeypatch)
    turn.h.mutate(key("session", turn.sid), binding_id="another-assignment")
    assert _denied(lambda: _legacy_session(turn)) == (
        403, "ASSISTANT_BINDING_DENIED", "ticket is bound to another assignment")
