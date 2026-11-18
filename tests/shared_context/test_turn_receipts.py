"""The SDK checkpoint check a VM turn end relies on, over a saved turn; no live calls."""

import pytest

from shared_context.behavior_agent_worker import AgentTurnAddress, AgentVmBinding
from shared_context.behavior_turn_receipts import validate_sdk_checkpoint
from shared_context.behavior_turn_wire import TurnTicket
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_conversation_transfer_startup import saved_turn, synthetic_standing_v1
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client, signing_key  # fixtures that synthetic_standing_v1 needs

def test_checkpoint_adapter_uses_original_published_parts_and_exact_stop(synthetic_standing_v1):
    from shared_context.behavior_sdk_checkpoint import published_record

    c = synthetic_standing_v1
    original = saved_turn(c, "original-checkpoint-for-ticket")
    binding, assignment = original.binding, original.assignment
    vm = AgentVmBinding(
        team_id=binding.team_id, agent_id=c.h.config.active_targets[0].agent_id,
        hosted_session_id="logical-session", hosted_agent_version=assignment.task.instruction.hosted_agent_version,
        vm_generation="logical-generation",
    )
    ticket = TurnTicket(
        tenant_id=binding.tenant_id, vm=vm, address=AgentTurnAddress(
            conversation_id=assignment.task.instruction.conversation_id,
            sdk_session_id=assignment.sdk_session_id, request_id="original-checkpoint-for-ticket"),
        assignment=assignment, owner_oid=binding.human_oid, agent_oid=binding.agent_oid,
        issuer_oid=binding.human_oid, source="human", request_sha256=digest("original admitted input"),
    )
    published = published_record(c.authority, original.archive, job_id=binding.job_id)
    validate_sdk_checkpoint(c.authority, ticket, original.archive, published.sdk_stop)
    with pytest.raises(Problem) as stop_changed:
        validate_sdk_checkpoint(c.authority, ticket, original.archive,
                                published.sdk_stop.model_copy(update={"pid": published.sdk_stop.pid + 1}))
    assert stop_changed.value.body["error"]["code"] == "SDK_STATE_INCOMPLETE"
    with pytest.raises(Problem):
        validate_sdk_checkpoint(c.authority, ticket, original.archive.model_copy(update={
            "artifact_id": "td-checkpoint-not-the-original"}), published.sdk_stop)

