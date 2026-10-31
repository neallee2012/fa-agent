"""Delegation receipts through the real #141 standing/current-runtime readers.

The inherited fixture excludes the historical Window2/WebHandoff/ACK outer chain.
Capability admission and SDK delegation are not enabled by these synthetic facts.
"""

import pytest

from shared_context import behavior_assistant_wire as aw
from shared_context.behavior_delegation import DelegationLedger, DelegationState, capture_parent, state_key
from shared_context.behavior_instruction_cost import record_usage
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_behavior_delegation import TASK
from test_conversation_transfer_startup import read_startup, saved_turn, synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key


class CancelFactsSaved(Exception):
    pass


@pytest.mark.parametrize("outcome", ["completed", "cancelled"])
def test_delegation_intent_result_usage_and_stop_facts_survive_startup(
        synthetic_standing_v1, monkeypatch, outcome):
    c = synthetic_standing_v1
    original = c.authority.save_text_reply
    stages = []

    def before_parent_reply(session, command, principal):
        parent = capture_parent(c.authority, session, capability_sha256="e" * 64)
        ledger = DelegationLedger(c.h.runtime.store)
        prepared = ledger.prepare(parent, "startup-task-call", TASK)
        stages.append(read_startup(c))
        assert prepared.view.status == "unknown"
        ledger.record_started(parent, "startup-task-call", "startup-child", event_id="startup-child-start")
        stages.append(read_startup(c))

        body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "Synthetic child request."}],
                "stream": True}
        request_sha256 = digest(body)
        c.authority.reserve_model(session, aw.Reservation(request_id=request_sha256,
                                                         request_sha256=request_sha256), principal)
        c.authority.consume_model(session, aw.ModelRequest(request_id=request_sha256, body=body), principal)
        linked = ledger.link_model_request(parent, "startup-task-call", "startup-child", request_sha256)
        stages.append(read_startup(c))
        assert linked.root_budget_id == parent.execution.owner.root_budget_id
        record_usage(c.authority, session, request_sha256, (37, 11))
        stages.append(read_startup(c))

        if outcome == "cancelled":
            ledger.request_cancel(parent, request_id="startup-cancel")
            pending = ledger.record_result(parent, "startup-task-call", "startup-child",
                event_id="startup-child-end", outcome="cancelled", text="Original partial public result.")
            assert pending.status == "unknown" and not pending.stop_confirmed
            stages.append(read_startup(c))
            final = ledger.record_parent_idle(parent, "startup-task-call", "startup-child", event_id="startup-idle")
            assert final.status == "cancelled" and final.stop_confirmed
        else:
            final = ledger.record_result(parent, "startup-task-call", "startup-child",
                event_id="startup-child-end", outcome="completed", text="Original complete public result.")
            assert final.status == "completed"
        stages.append(read_startup(c))
        assert DelegationLedger(c.h.runtime.store).read(parent) == final
        if outcome == "cancelled":
            raise CancelFactsSaved()
        return original(session, command, principal)

    monkeypatch.setattr(c.authority, "save_text_reply", before_parent_reply)
    if outcome == "cancelled":
        with pytest.raises(CancelFactsSaved):
            saved_turn(c, "delegation-startup-parent")
    else:
        saved_turn(c, "delegation-startup-parent")
    after = read_startup(c)
    kinds = {row.payload["data"].get("kind") for row in after.rows.values()}
    assert {"sdk_delegation_intent", "sdk_delegation_state",
            "sdk_delegation_result", "sdk_delegation_model_usage"} <= kinds
    assert all(after.receipts[key] == row for inventory in stages for key, row in inventory.receipts.items())


def test_captured_delegation_api_and_child_model_link_remain_readable_at_startup(synthetic_standing_v1, monkeypatch):
    from shared_context import behavior_delegation_api as api
    from shared_context.behavior_capabilities import available_grants
    from shared_context.behavior_delegation_wire import DelegationModelRef, DelegationObservation, DelegationPrepare
    from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
    from test_conversation_profile_selection import template
    c = synthetic_standing_v1
    grants = available_grants(None, ("fa-delegate-v1",))
    c.h.runtime.capability_grants = grants
    select_profile(c.control, c.authority, c.human, SelectConversationProfile(request_id="startup-delegate-choice",
        expected_selection_id=None, template=template("fa-instruction-history-v1",
            user_text_file_profile="fa-user-text-files-v1", capabilities=(grants["fa-delegate-v1"].ref,))))
    original = c.authority.save_text_reply

    def before_reply(session, command, principal):
        first = api.prepare(c.authority, session, DelegationPrepare(tool_call_id="startup-task", arguments=TASK), principal)
        assert first.fresh
        api.observe(c.authority, session, DelegationObservation(kind="started", event_id="startup-start",
            tool_call_id="startup-task", child_agent_id="startup-child"), principal)
        read_startup(c)
        reference = DelegationModelRef(tool_call_id="startup-task", child_agent_id="startup-child")
        body = {"model": "gpt-4.1-mini", "stream": True,
                "messages": [{"role": "user", "content": "Original child request."}]}
        identity = digest(body)
        c.authority.reserve_model(session, aw.Reservation(request_id=identity, request_sha256=identity), principal)
        api.validate_model(c.authority, session, reference)
        c.authority.consume_model(session, aw.ModelRequest(request_id=identity, body=body, delegation=reference), principal)
        api.link_model(c.authority, session, reference, identity)
        read_startup(c)
        record_usage(c.authority, session, identity, (37, 11))
        api.observe(c.authority, session, DelegationObservation(kind="result", event_id="startup-result",
            tool_call_id="startup-task", child_agent_id="startup-child", outcome="completed", text="530 remain."), principal)
        read_startup(c)
        return original(session, command, principal)

    monkeypatch.setattr(c.authority, "save_text_reply", before_reply)
    saved_turn(c, "startup-captured-delegation")
    read_startup(c)
