"""Each saved wait stays in its Team, under the key of its original call, and an Agent reads only its own."""

import pytest

from shared_context.errors import Problem
from test_connector_consent import create_card
from test_m3_auth import jwk_client, signing_key
from test_native_hitl import create_pending, hitl, inventory
from test_native_hitl_questions import create_question


def _pending(c):
    return c.service.read, create_pending(c)[1]["pending_id"]


def _question(c):
    return c.service.read_question, create_question(c)[1]["question_id"]


def _consent(c):
    return c.service.read_consent, create_card(c, call_id="card-one").json()["result"]["consent_id"]


def _other_call(value):
    """The same record with its original call changed, so its key no longer identifies that call."""
    wait_field = next(name for name in ("action", "question", "consent") if name in type(value.command).model_fields)
    wait = getattr(value.command, wait_field)
    changed = wait.model_copy(update={"call": wait.call.model_copy(update={"tool_call_id": "another-call"})})
    return value.model_copy(update={"command": value.command.model_copy(update={wait_field: changed})})


WAITS = {
    "pending": (_pending, "pending_id", "the pending call belongs to another Team",
                "the pending key does not identify its original call",
                "only the original Agent may read or close this pending call"),
    "question": (_question, "question_id", "the question belongs to another Team",
                 "the question key does not identify its original wait",
                 "only the original Agent may read this question"),
    "consent": (_consent, "consent_id", "the Connect card belongs to another Team",
                "the Connect card key does not identify its original wait",
                "only the original Agent may read this Connect card"),
}
TAMPERED = {
    "tenant": (lambda value, _: value.model_copy(update={"tenant_id": "another-tenant"}), 2),
    "team": (lambda value, _: value.model_copy(update={"team_id": "another-team"}), 2),
    "own_id": (lambda value, own_id: value.model_copy(update={own_id: "f" * 64}), 2),
    "call": (lambda value, _: _other_call(value), 3),
    "agent": (lambda value, _: value.model_copy(update={"agent_oid": "another-agent"}), 4),
}


def _refusal(read, principal, record_id):
    # The service's own words: the HTTP layer answers every refusal with one general text.
    with pytest.raises(Problem) as refused:
        read(principal, record_id)
    return refused.value.status, refused.value.body["error"]["code"], refused.value.body["error"]["message"]


def _serve_tampered(monkeypatch, c, tamper, own_id):
    load = c.service._load

    def tampered(record, key, model):
        value = load(record, key, model)
        return tamper(value, own_id) if model.__name__.endswith("Pending") else value
    monkeypatch.setattr(c.service, "_load", tampered)


@pytest.mark.parametrize("wait", WAITS)
@pytest.mark.parametrize("case", TAMPERED)
def test_a_record_outside_its_scope_is_refused_in_its_own_words(monkeypatch, hitl, wait, case):
    c = hitl
    create, own_id, *messages = WAITS[wait]
    read, record_id = create(c)
    tamper, which = TAMPERED[case]
    _serve_tampered(monkeypatch, c, tamper, own_id)
    expected = (409, "NATIVE_HITL_RECORD_INVALID") if which == 3 else (403, "NATIVE_HITL_SCOPE_DENIED")
    assert _refusal(read, c.agent, record_id) == (*expected, messages[which - 2])


@pytest.mark.parametrize("wait", WAITS)
def test_only_an_agent_is_held_to_the_saved_agent_id(monkeypatch, hitl, wait):
    c = hitl
    create, own_id, *_ = WAITS[wait]
    read, record_id = create(c)
    _serve_tampered(monkeypatch, c, TAMPERED["agent"][0], own_id)
    assert read(c.owner, record_id).model_dump()


@pytest.mark.parametrize("wait", WAITS)
def test_the_live_agent_check_comes_before_the_saved_agent_id(monkeypatch, hitl, wait):
    c = hitl
    create, own_id, *_ = WAITS[wait]
    read, record_id = create(c)
    _serve_tampered(monkeypatch, c, TAMPERED["agent"][0], own_id)
    assert _refusal(read, c.other_agent, record_id) == (
        403, "NATIVE_HITL_SCOPE_DENIED", "the call belongs to another Agent or Team")


@pytest.mark.parametrize("wait", WAITS)
@pytest.mark.parametrize("case", ["team", "call"])
def test_team_and_key_checks_come_before_the_saved_agent_id(monkeypatch, hitl, wait, case):
    c = hitl
    create, own_id, *messages = WAITS[wait]
    read, record_id = create(c)
    tamper, which = TAMPERED[case]
    _serve_tampered(monkeypatch, c, lambda value, name: TAMPERED["agent"][0](tamper(value, name), name), own_id)
    expected = (409, "NATIVE_HITL_RECORD_INVALID") if which == 3 else (403, "NATIVE_HITL_SCOPE_DENIED")
    assert _refusal(read, c.agent, record_id) == (*expected, messages[which - 2])
