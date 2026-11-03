"""Pins derive_work's admission and profile guards, their order, and the derived output; attachment, text-file,
capability-base and role-prompt guards are pinned in their own suites. No runtime, SDK, model or cloud writes."""
import json

import pytest

from shared_context.behavior_assistant_wire import InstructionSubmit, PeerWorkReference
from shared_context.behavior_server_config import ServerConfiguration
from shared_context.behavior_work_policy import derive_work
from shared_context.errors import Problem
from test_agent_scope_registry import legacy_policy
from test_behavior_assistant_server import assistant_configuration

SENTINEL = object()  # every guard below fires on "is not None" before it reads the value


@pytest.fixture(scope="module")
def policy():
    return legacy_policy(ServerConfiguration.model_validate_json(json.dumps(assistant_configuration())))


def command(request_id="pinned-request", **values):
    return InstructionSubmit(request_id=request_id, content="Pinned instruction.", **values)


def peer():
    return PeerWorkReference(root_id="root", message_id="message", message_key="message-key", message_sha256="a" * 64,
        content_sha256="b" * 64, sender_agent_id="agent-a", recipient_agent_id="agent-b",
        recipient_work_id="work-b", conversation_id="conversation")


def problem(policy, **kwargs):
    with pytest.raises(Problem) as denied:
        derive_work(policy, "pinned-request", "native", **kwargs)
    return denied.value.status, denied.value.body["error"]["code"]


@pytest.mark.parametrize("kwargs,expected", [
    ({"agent_origin": SENTINEL}, (403, "AGENT_ADMISSION_INVALID")),
    ({"agent_origin": SENTINEL, "instruction": "command", "peer_work": SENTINEL}, (403, "AGENT_ADMISSION_INVALID")),
    ({"instruction": "other-request"}, (422, "WORK_POLICY_COMMAND")),
    ({"peer_work": SENTINEL}, (403, "AGENT_MESSAGE_ASSIGNMENT_DENIED")),
    ({"peer_work": SENTINEL, "instruction": "command", "profile_template": SENTINEL}, (403, "AGENT_MESSAGE_ASSIGNMENT_DENIED")),
    ({"peer_work": SENTINEL, "instruction": "command", "memory_capture": SENTINEL}, (403, "AGENT_MESSAGE_ASSIGNMENT_DENIED")),
    ({"peer_work": SENTINEL, "instruction": "command", "capability_capture": SENTINEL}, (403, "AGENT_MESSAGE_ASSIGNMENT_DENIED")),
    ({"peer_work": SENTINEL, "instruction": "command", "prompt_override": "fa-instruction-v1"}, (403, "AGENT_MESSAGE_ASSIGNMENT_DENIED")),
    ({"peer_work": SENTINEL, "instruction": "command", "previous_archive_id": "archive"}, (403, "AGENT_MESSAGE_ASSIGNMENT_DENIED")),
    ({"peer_work": SENTINEL, "instruction": "command", "turn_number": 2}, (403, "AGENT_MESSAGE_ASSIGNMENT_DENIED")),
    ({"profile_template": SENTINEL}, (422, "PROFILE_SELECTION_UNSUPPORTED")),
    ({"profile_template": SENTINEL, "instruction": "command", "prompt_override": "fa-instruction-v3"},
     (422, "PROFILE_SELECTION_UNSUPPORTED")),
    ({"prompt_override": "fa-instruction-v1"}, (422, "WORK_POLICY_PROMPT_PROFILE")),
    ({"prompt_override": "fa-instruction-v3", "instruction": "command"}, (422, "WORK_POLICY_PROMPT_PROFILE")),
    ({"prompt_override": "fa-instruction-v3", "instruction": "command", "memory_capture": SENTINEL},
     (422, "WORK_POLICY_PROMPT_PROFILE")),
    ({"memory_capture": SENTINEL}, (403, "MEMORY_RECALL_SCOPE_DENIED")),
    ({"instruction": "command", "role_prompt": SENTINEL}, (422, "AGENT_ROLE_PROMPT_UNSUPPORTED")),
])
def test_each_guard_keeps_its_status_code_and_order(policy, kwargs, expected):
    kwargs = dict(kwargs)
    if kwargs.get("instruction") == "command":
        kwargs["instruction"] = command()
    elif kwargs.get("instruction") == "other-request":
        kwargs["instruction"] = command("other-request")
    assert problem(policy, **kwargs) == expected


def test_a_configured_prompt_profile_refuses_any_override(policy):
    configured = policy.model_copy(update={"profile": policy.profile.model_copy(update={"prompt_profile": "fa-instruction-v1"})})
    assert problem(configured, instruction=command(), prompt_override="fa-instruction-v2") == (
        422, "WORK_POLICY_PROMPT_PROFILE")


def test_plain_work_output_is_unchanged(policy):
    _, binding, profile = derive_work(policy, "pinned-request", "native")
    assert profile.instruction is None and binding.claim_ids == (policy.profile.claim_id,)
    assert profile.binding_id == binding.binding_id and len(profile.artifact_version) == 32
    assert profile.max_model_requests == policy.max_model_requests == 1


def test_instruction_output_maps_every_turn_field(policy):
    _, binding, profile = derive_work(policy, "pinned-request", "native", instruction=command(model="asked"),
        turn_number=3, previous_archive_id="archive-2", hosted_agent_version="7")
    task = profile.instruction
    assert (task.content, task.turn_number, task.previous_archive_id, task.hosted_agent_version) == (
        "Pinned instruction.", 3, "archive-2", "7")
    assert (task.model, task.requested_model, task.peer_work, task.capabilities, task.role_prompt) == (
        "asked", "asked", None, None, None)
    assert binding.work.result_kind == "answer" and binding.work.goal.startswith("Follow the original human instruction: ")
    _, _, chosen = derive_work(policy, "pinned-request", "native", instruction=command(model="asked"), model="chosen")
    assert (chosen.instruction.model, chosen.instruction.requested_model) == ("chosen", "asked")


def test_prompt_override_reaches_profile_and_task(policy):
    _, _, profile = derive_work(policy, "pinned-request", "native", instruction=command(),
                                prompt_override="fa-instruction-v1")
    assert profile.prompt_profile == profile.instruction.prompt_profile == "fa-instruction-v1"


def test_agent_origin_changes_only_the_goal_wording(policy):
    _, human, _ = derive_work(policy, "pinned-request", "native", instruction=command())
    _, agent, _ = derive_work(policy, "pinned-request", "native", instruction=command(), agent_origin=SENTINEL)
    assert agent.work.goal.startswith("Follow the original agent message: ")
    assert agent.work.goal.split(": ")[1] == human.work.goal.split(": ")[1]
    assert agent.model_copy(update={"work": human.work}) == human


def test_peer_work_narrows_profile_and_rebinds_the_work(policy):
    _, binding, profile = derive_work(policy, "pinned-request", "native", instruction=command(), peer_work=peer())
    assert binding.work.work_id == "work-b"
    assert binding.work.goal == "Follow the original agent handoff: " + "b" * 64
    assert profile.prompt_profile == "fa-instruction-v1" and profile.capabilities == ()
    assert profile.instruction.conversation_id == "conversation" and profile.instruction.peer_work == peer()

