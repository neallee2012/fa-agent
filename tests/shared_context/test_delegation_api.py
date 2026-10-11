"""Signed original-session delegation routes with real authority/store producers, no SDK process."""

import pytest

from shared_context.behavior_capabilities import available_grants
from shared_context.behavior_delegation_wire import DelegationPrepare, DelegationObservation, DelegationReply
from shared_context.behavior_wire import Command
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_conversation_profile_selection import configured, select, template
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import open_turn


@pytest.fixture
def delegate_turn(assistant):
    h, control, human = configured(assistant)
    grants = available_grants(None, ("fa-delegate-v1",))
    h.runtime.capability_grants = grants
    selected, _ = select(h, value=template("fa-instruction-history-v1",
        user_text_file_profile="fa-user-text-files-v1", capabilities=(grants["fa-delegate-v1"].ref,)))
    assert selected.status_code == 200, selected.text
    turn = open_turn(h, control, human, "delegation-wired-parent")
    try:
        yield turn
    finally:
        turn.client.close()


def preparation():
    return DelegationPrepare(tool_call_id="original-task-call", arguments={
        "agent_type": "fa-delegate-v1", "mode": "sync", "name": "compare", "description": "Compare Team fragments",
        "prompt": "Read only these synthetic fragments: total 850; assigned 320.",
    })


def test_captured_capability_saves_original_child_then_reads_it_without_new_dispatch(delegate_turn):
    c = delegate_turn
    first = c.client._post("delegation-prepare", preparation(), DelegationReply)
    assert first.fresh and first.status == "unknown"
    assert first.parent_job_id == c.binding.job_id and first.sdk_session_id == c.assignment.sdk_session_id
    again = c.client._post("delegation-prepare", preparation(), DelegationReply)
    assert not again.fresh and again.delegation_id == first.delegation_id
    started = c.client._post("delegation-observe", DelegationObservation(event_id="child-start",
        tool_call_id=first.tool_call_id, kind="started", child_agent_id="original-child"), DelegationReply)
    assert started.status == "running"
    result = c.client._post("delegation-observe", DelegationObservation(event_id="child-completed",
        tool_call_id=first.tool_call_id, kind="result", child_agent_id="original-child",
        outcome="completed", text="530 remain."), DelegationReply)
    saved = c.client._post("delegation-read", Command(request_id="delegation-read"), DelegationReply)
    assert saved == result and saved.result == "530 remain."
    assert c.h.workers.inspect(c.binding.job_id).state not in {"succeeded", "failed", "cancelled"}


def test_normal_comparison_material_larger_than_legacy_route_limit_can_reserve(delegate_turn):
    c = delegate_turn
    command = preparation()
    command = command.model_copy(update={"arguments": command.arguments.model_copy(
        update={"prompt": "Synthetic approved source fragment.\n" * 1200})})
    assert 24 * 1024 < len(command.model_dump_json().encode("utf-8")) < 192 * 1024
    value = c.client.delegation("prepare", command)
    assert value.fresh and value.status == "unknown"
    original = c.client.delegation("read", Command(request_id="delegation-read"))
    assert original.input_sha256 == value.input_sha256 and not original.fresh


def test_server_size_refusal_is_explicitly_before_reservation(delegate_turn):
    c = delegate_turn
    command = preparation()
    command = command.model_copy(update={"arguments": command.arguments.model_copy(
        update={"prompt": "x" * (192 * 1024)})})
    before = c.h.store._guard()
    with pytest.raises(Problem) as refused:
        c.client.delegation("prepare", command)
    assert refused.value.status == 413
    assert refused.value.body["error"]["code"] == "DELEGATION_PREPARE_TOO_LARGE"
    assert c.h.store._guard() == before


def test_old_profile_cannot_call_the_delegation_entrypoint(assistant):
    h, control, human = configured(assistant)
    turn = open_turn(h, control, human, "no-delegation")
    try:
        before = h.store._guard()
        with pytest.raises(Problem):
            turn.client._post("delegation-prepare", preparation(), DelegationReply)
        assert h.store._guard() == before
    finally:
        turn.client.close()


@pytest.mark.parametrize(("suffix", "status"), [("operation", 409), ("save-skill-draft", 422), ("copy-sop-draft", 403)])
def test_parent_effect_routes_keep_their_original_authority_and_validation(delegate_turn, suffix, status, caplog):
    c = delegate_turn
    path = (f"/behavior/v1/bindings/{c.binding.binding_id}/assistant"
            f"/sessions/{c.assignment.session_id}/{suffix}")
    result = c.h.client.post(path, json={"request_id": suffix},
        headers={**c.h.headers("agent"), "x-behavior-assistant-ticket": c.client.ticket.get_secret_value()})
    assert result.status_code == status
    assert "DELEGATION_SESSION_READ_ONLY" not in caplog.text
