"""Server prompt defaults affect new conversations, not existing work or rights."""

import asyncio
import pytest

from shared_context.behavior_assistant_wire import Task
from shared_context.behavior_skill_prompt import SKILL_PROFILE
from shared_context.behavior_work_policy import derive_registered_work
from shared_context.behavior_work_policy_api import work_response
from shared_context.errors import Problem
from assistant_api_helpers import assistant_api
from instruction_flow_helpers import instruction_inventory, instruction_models, runtime_path
from direct_submit_helpers import submit_direct
from instruction_prompt_helpers import configured
from test_m3_auth import jwk_client, signing_key


ENV = "FA_INSTRUCTION_DEFAULT_PROMPT_PROFILE"


@pytest.fixture(autouse=True)
def isolated_default(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


def profile_for(h, accepted):
    binding = h.runtime.bindings[accepted["binding_id"]]
    task = Task.model_validate_json(h.runtime.job_input(binding))
    profile = h.runtime.profiles[binding.binding_id]
    assert task.instruction.prompt_profile == profile.prompt_profile
    return profile.prompt_profile


def test_v2_human_selection_keeps_original_policy_and_write_profile_boundaries(assistant_api):
    from conversation_profile_selection_helpers import configured as selectable, select, template
    h, control, _ = selectable(assistant_api)
    original_policy = control.policy.model_dump_json()
    selected, command = select(h, request_id="select-v2", value=template("fa-instruction-v2"))
    assert selected.status_code == 200, selected.text
    result = selected.json()["result"]
    assert result["permissions_changed"] is False and result["existing_conversations_changed"] is False
    exported = h.client.get("/work-policy/v1/agent-template", headers=h.headers())
    assert exported.status_code == 200 and exported.json() == command.template.model_dump(mode="json")
    before = instruction_inventory(h).entity_sha256
    for profile in ("fa-instruction-sop-copy-v1", "fa-instruction-skill-draft-v1"):
        denied, _ = select(h, request_id="deny-" + profile, previous=result["selection_id"], value=template(profile))
        assert denied.status_code == 422
        assert denied.json()["error"]["code"] == "PROFILE_SELECTION_UNSUPPORTED"
    assert instruction_inventory(h).entity_sha256 == before
    assert control.policy.model_dump_json() == original_policy
    assert control.records == {}


def test_post_admission_prompt_failure_is_not_reported_as_unaccepted_work():
    def fail():
        raise Problem(503, "INSTRUCTION_PROMPT_CONFIGURATION", "PRIVATE_POST_ADMISSION_DETAIL")

    with pytest.raises(Problem) as refused:
        asyncio.run(work_response(fail))
    assert refused.value.body["error"]["code"] == "INSTRUCTION_PROMPT_CONFIGURATION"


@pytest.mark.parametrize("profile", ["fa-instruction-v1", "fa-instruction-v2", "fa-instruction-v3", "fa-instruction-v4",
                                     SKILL_PROFILE])
@pytest.mark.parametrize("default", ["fa-instruction-v1", "fa-instruction-v2", "unknown"])
def test_default_does_not_replace_an_explicit_server_policy_profile(assistant_api, monkeypatch, profile, default):
    h, control, _ = configured(assistant_api, profile)
    original = control.policy.model_dump_json()
    monkeypatch.setenv(ENV, default)
    accepted, _ = submit_direct(h, "explicit-policy", "Retain the configured policy.")
    assert profile_for(h, accepted) == profile
    assert control.policy.model_dump_json() == original


@pytest.mark.parametrize("mode", ["offline_contract", "native"])
@pytest.mark.parametrize("profile", ["fa-instruction-v1", "fa-instruction-v2"])
def test_runtime_and_history_share_original_profile_reconstruction(assistant_api, monkeypatch, mode, profile):
    h, control, _ = configured(assistant_api, None)
    monkeypatch.setenv(ENV, profile)
    accepted, _ = submit_direct(h, "reconstruct-default", "Capture the original server choice.")
    record = control.records[accepted["binding_id"]]
    monkeypatch.setenv(ENV, "unknown")
    assert derive_registered_work(control.policy, record, mode) == (
        record.admission_id, record.binding, record.profile,
    )
    invalid = record.model_copy(update={"profile": record.profile.model_copy(update={
        "prompt_profile": SKILL_PROFILE,
        "instruction": record.profile.instruction.model_copy(update={"prompt_profile": SKILL_PROFILE}),
    })})
    with pytest.raises(Problem) as refused:
        derive_registered_work(control.policy, invalid, mode)
    assert refused.value.body["error"]["code"] == "WORK_POLICY_REGISTRY_INVALID"
