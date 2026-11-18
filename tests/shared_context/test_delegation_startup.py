"""Delegation receipts through the real #141 standing/current-runtime readers.

The inherited fixture excludes the historical Window2/WebHandoff/ACK outer chain.
Capability admission and SDK delegation are not enabled by these synthetic facts.
The parent turns ran on the retired job lane; each stage is the state main 80ec0865 stored at that point
(legacy_replay_helpers), and the startup readers here read it.
"""

import pytest

from legacy_replay_helpers import legacy_tokens, pinned_configuration, replayed
from test_conversation_transfer_startup import synthetic_standing_v1
from legacy_turn_rows_helpers import read_startup
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client, signing_key


def stage(c):
    """The next stored stage of the parent turn, read by the startup readers."""
    replayed(c.h, c.authority.artifacts)
    return read_startup(c)


@pytest.mark.parametrize("outcome", ["completed", "cancelled"])
def test_delegation_intent_result_usage_and_stop_facts_survive_startup(
        legacy_tokens, pinned_configuration, synthetic_standing_v1, outcome):
    c = synthetic_standing_v1
    # Inside the parent's save: prepared, started, child model linked, usage recorded, [cancel pending,] final.
    stages = [stage(c) for _ in range(6 if outcome == "cancelled" else 5)]
    after = stage(c)  # the parent turn after its save (or after the cancel stopped it)
    kinds = {row.payload["data"].get("kind") for row in after.rows.values()}
    assert {"sdk_delegation_intent", "sdk_delegation_state",
            "sdk_delegation_result", "sdk_delegation_model_usage"} <= kinds
    assert all(after.receipts[key] == row for inventory in stages for key, row in inventory.receipts.items())


def test_captured_delegation_api_and_child_model_link_remain_readable_at_startup(
        legacy_tokens, pinned_configuration, synthetic_standing_v1):
    from shared_context.behavior_capabilities import available_grants
    from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
    from conversation_profile_selection_helpers import template
    c = synthetic_standing_v1
    grants = available_grants(None, ("fa-delegate-v1",))
    c.h.runtime.capability_grants = grants
    select_profile(c.control, c.authority, c.human, SelectConversationProfile(request_id="startup-delegate-choice",
        expected_selection_id=None, template=template("fa-instruction-history-v1",
            user_text_file_profile="fa-user-text-files-v1", capabilities=(grants["fa-delegate-v1"].ref,))))
    # Inside the parent's save, through the delegation API: started, child model linked, result; then the save.
    for _ in range(4):
        stage(c)
