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
from shared_context.behavior_turn_ticket import TurnTicketIndex, restore_turn_tickets
from shared_context.behavior_work_policy import WorkRegistration, WorkRegistry, work_policy_key
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


def test_startup_rebuilds_ticket_index_from_existing_validated_rows_without_reads(assistant, monkeypatch):
    turn = _opened(assistant)
    record = turn.control.records[turn.binding.binding_id]
    keys = (work_policy_key("registration", record.admission_id), key("session", turn.sid))
    inventory = SimpleNamespace(rows=turn.h.store.read_snapshot(keys))
    index = TurnTicketIndex()
    monkeypatch.setattr(turn.h.store, "_turn_ticket_index", index)
    monkeypatch.setattr(turn.h.store, "_read_physical",
                        lambda *_: pytest.fail("the startup inventory already contains these rows"))
    restore_turn_tickets(turn.h.store, inventory)
    captured = index.authenticate(turn.binding.binding_id, turn.sid, turn.ticket, turn.principal)
    assert captured.authorization_digest == digest(record.model_dump(mode="json"))
    assert captured.ticket_digest != turn.ticket.get_secret_value()


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
