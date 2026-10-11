"""A human selects new-conversation references without rewriting old work or permission."""

from types import MappingProxyType, SimpleNamespace
import hashlib

import pytest

from shared_context.behavior_agent_template import AgentTemplate, TemplateRole, TemplateSettings, TemplateSkill
from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_profile_selection import SelectConversationProfile
from shared_context.behavior_profile_selection import validate_profile_history, selection_key
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.behavior_work_policy_api import install_work_policy_routes
from test_behavior_assistant import assistant
from test_instruction_flow import check_history, instruction_inventory, instruction_models, runtime_path, sdk_io
from shared_context.errors import Problem
from shared_context.behavior_work_policy import derive_registered_work
from shared_context.behavior_work_policy import WorkPreflightQuery
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_attachments import TextAttachment
from shared_context.behavior_operations import control_key
from test_instruction_prompt import submit, submit_legacy
from test_m3_auth import jwk_client, signing_key, token
from test_work_policy_lifecycle import enabled

PATH = "/work-policy/v1/agent-template"


def configured(h):
    h.runtime.profiles = MappingProxyType({
        key: AssistantProfile.model_validate({**value.model_dump(), "prompt_profile": None})
        for key, value in h.runtime.profiles.items()
    })
    h, control, human, _ = enabled(h)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, assistant=SimpleNamespace(authority=h.assistant),
        installation=lambda: None, require_current_native_evidence=lambda: None,
    ))
    return h, control, human


def template(profile="fa-instruction-v1", **settings):
    role = assemble_prompt(profile).record
    return AgentTemplate(role=TemplateRole(profile=profile, prefix_sha256=role.prefix_sha256,
                         capability_catalog_version=role.capability_catalog_version), settings=TemplateSettings(**settings))


def select(h, *, request_id="choose-v1", previous=None, value=None, identity="human", reconcile=False):
    command = SelectConversationProfile(request_id=request_id, expected_selection_id=previous,
                                        template=template() if value is None else value)
    response = h.client.post(PATH + ("/selection-reconcile" if reconcile else "/select"),
                             content=command.model_dump_json(), headers=h.headers(identity))
    return response, command


@pytest.mark.parametrize("profile", ["fa-instruction-v1", "fa-instruction-v2"])
def test_human_selection_changes_only_new_conversations_and_survives_reconstruction(assistant, monkeypatch, profile):
    h, control, _ = configured(assistant)
    sdk_io(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Old conversation."}, {"content": "Old follow-up."}, {"content": "New v1."}])
    first, _ = submit_legacy(h, "before-selection", "Start with original settings.", monkeypatch)
    assert h.runtime.controller.step(first["binding_id"]).state == "succeeded"
    old_record = control.records[first["binding_id"]].model_dump_json()
    original_policy = control.policy.model_dump_json()
    selected, _ = select(h, value=template(profile))
    assert selected.status_code == 200, selected.text
    selection_id = selected.json()["result"]["selection_id"]
    assert selected.json()["result"]["existing_conversations_changed"] is False
    assert control.policy.model_dump_json() == original_policy
    control.records.clear()
    control.refresh()
    second, _ = submit(h, "old-followup", "Keep this original conversation.", first["conversation_id"])
    assert control.records[second["binding_id"]].profile.prompt_profile is None
    assert control.records[second["binding_id"]].profile_selection_id is None
    assert h.runtime.controller.step(second["binding_id"]).state == "succeeded"
    third, _ = submit(h, "after-selection", "Start a new v1 conversation.")
    assert control.records[third["binding_id"]].profile_selection_id == selection_id
    assert control.records[third["binding_id"]].profile.prompt_profile == profile
    assert h.runtime.controller.step(third["binding_id"]).state == "succeeded"
    control.records.clear()
    control.refresh()
    assert control.records[first["binding_id"]].model_dump_json() == old_record
    assert control.records[third["binding_id"]].profile.prompt_profile == profile
    check_history(h)


def test_responses_selection_captures_only_new_conversations_without_rewriting_old_checkpoints(assistant, monkeypatch):
    h, control, _ = configured(assistant)
    sdk_io(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Original Completions answer."}, {"content": "Original Completions follow-up."}])
    first, _ = submit(h, "completions-first", "Keep this original conversation.")
    assert h.runtime.controller.step(first["binding_id"]).state == "succeeded"
    original = control.records[first["binding_id"]].model_dump_json()
    selected, command = select(h, value=template(model_provider="openai-responses"))
    assert selected.status_code == 200, selected.text
    control.records.clear()
    control.refresh()
    follow, _ = submit(h, "completions-follow", "Continue in the original environment.", first["conversation_id"])
    assert control.records[follow["binding_id"]].profile.instruction.model_provider is None
    assert h.runtime.controller.step(follow["binding_id"]).state == "succeeded"
    newest, _ = submit(h, "responses-first", "Start a new conversation with the selected provider.")
    record = control.records[newest["binding_id"]]
    assert record.profile.model_provider == record.profile.instruction.model_provider == "openai-responses"
    assert record.profile_selection_id == selected.json()["result"]["selection_id"]
    assert control.records[first["binding_id"]].model_dump_json() == original
    assert '"model_provider"' not in original
    control.records.clear()
    control.refresh()
    assert control.records[newest["binding_id"]].profile.instruction.model_provider == "openai-responses"
    check_history(h)


def test_responses_reasoning_mode_is_captured_without_reloading_current_model_facts(assistant):
    h, control, human = configured(assistant)
    control.models.models["configured-second-model"] = control.models.models["configured-second-model"].model_copy(
        update={"supports_reasoning_effort": True, "supports_reasoning_summary": True})
    assert select(h, value=template(model_provider="openai-responses"))[0].status_code == 200
    first, _ = control.submit(human, InstructionSubmit(request_id="responses-supported-model",
        content="Use the declared reasoning model.", model="configured-second-model"))
    record = control.records[first.binding_id]
    assert record.profile.instruction.reasoning_summary == "detailed"
    original = record.model_dump_json()
    control.models.models["configured-second-model"] = control.models.models["configured-second-model"].model_copy(
        update={"supports_reasoning_effort": True, "supports_reasoning_summary": False})
    control.records.clear()
    control.refresh()
    assert control.records[first.binding_id].model_dump_json() == original
    second, _ = control.submit(human, InstructionSubmit(request_id="responses-nonreasoning-model",
        content="Use the normal model.", model="gpt-4.1-mini"))
    assert control.records[second.binding_id].profile.instruction.model_provider == "openai-responses"
    assert control.records[second.binding_id].profile.instruction.reasoning_summary is None


@pytest.mark.parametrize("identity", ["agent", "read-only"])
def test_nonapprovers_cannot_select_even_a_valid_template(assistant, identity):
    h, control, _ = configured(assistant)
    before = h.store._guard()
    result, _ = select(h, identity=identity)
    assert result.status_code == 403
    assert h.store._guard() == before and not control.records and not h.endpoint.requests


def test_approve_without_current_read_is_not_a_profile_selection_permission(assistant, signing_key):
    h, control, _ = configured(assistant)
    before = h.store._guard()
    command = SelectConversationProfile(request_id="approve-without-read", expected_selection_id=None, template=template())
    response = h.client.post(PATH + "/select", content=command.model_dump_json(), headers={
        "Authorization": "Bearer " + token(signing_key, {"scp": "Behavior.Approve"}),
    })
    assert response.status_code == 403
    assert h.store._guard() == before and not control.records


def test_selection_reconcile_and_export_use_the_original_approved_reference(assistant):
    h, control, _ = configured(assistant)
    selected, command = select(h)
    assert selected.status_code == 200, selected.text
    before = h.store._guard()
    same, _ = select(h)
    reconciled, _ = select(h, reconcile=True, identity="read-only")
    assert same.status_code == reconciled.status_code == 200
    assert same.json() == selected.json() == reconciled.json()
    exported = h.client.get(PATH, headers=h.headers("read-only"))
    assert exported.status_code == 200, exported.text
    assert exported.json() == command.template.model_dump(mode="json")
    current = h.client.get(PATH + "/selection", headers=h.headers("read-only"))
    assert current.json()["selection_id"] == selected.json()["result"]["selection_id"]
    assert h.store._guard() == before and not control.records and not h.endpoint.requests


def test_responses_state_profile_is_selected_by_the_owner_and_round_trips_without_changing_old_bytes(assistant):
    h, control, _ = configured(assistant)
    original = template()
    assert "state_profile" not in original.settings.model_dump(mode="json")
    response_template = template(state_profile="foundry-responses-v1")
    selected, command = select(h, value=response_template)
    assert selected.status_code == 200, selected.text
    exported = h.client.get(PATH, headers=h.headers("read-only"))
    assert exported.status_code == 200 and exported.json() == command.template.model_dump(mode="json"), exported.text
    stored = h.client.get(PATH + "/selection", headers=h.headers("read-only"))
    assert stored.json()["template"]["settings"]["state_profile"] == "foundry-responses-v1"
    assert selected.json()["result"]["existing_conversations_changed"] is False
    assert not control.records and not h.endpoint.requests
    assert "state_profile" not in original.settings.model_dump(mode="json")


def test_wrong_previous_selection_and_same_request_with_new_content_do_not_overwrite(assistant):
    h, control, _ = configured(assistant)
    selected, _ = select(h)
    assert selected.status_code == 200
    before = h.store._guard()
    stale, _ = select(h, request_id="new-choice", previous=None)
    assert stale.status_code == 409
    conflict, _ = select(h, value=template(text_attachment_profile="fa-source-text-attachments-v1"))
    assert conflict.status_code == 409
    assert h.store._guard() == before and not control.records
    changed, _ = select(h, request_id="new-choice", previous=selected.json()["result"]["selection_id"],
                        value=template(text_attachment_profile="fa-source-text-attachments-v1"))
    assert changed.status_code == 200, changed.text
    assert h.client.get(PATH, headers=h.headers()).json()["settings"]["text_attachment_profile"] == "fa-source-text-attachments-v1"
    config = SimpleNamespace(table=SimpleNamespace(scope=h.store._scope), sha256="f" * 64,
        workers=h.workers.contract, delivery=h.delivery.contract,
        identity=SimpleNamespace(human_oid=h.binding.human_oid))
    choices = validate_profile_history(instruction_inventory(h), config, control.policy)
    assert set(choices) == {selected.json()["result"]["selection_id"], changed.json()["result"]["selection_id"]}


def test_unknown_original_selection_is_not_replayed_as_a_new_choice(assistant):
    h, control, _ = configured(assistant)
    before = h.store._guard()
    result, _ = select(h, reconcile=True, identity="read-only")
    assert result.status_code == 503 and result.json()["error"]["code"] == "PROFILE_SELECTION_UNKNOWN"
    assert h.store._guard() == before and not control.records


def test_imported_role_digest_cannot_invent_new_approved_instructions(assistant):
    h, _, _ = configured(assistant)
    forged = template()
    forged = forged.model_copy(update={"role": forged.role.model_copy(update={"prefix_sha256": "f" * 64})})
    before = h.store._guard()
    result, _ = select(h, value=forged)
    assert result.status_code == 422 and result.json()["error"]["code"] == "PROFILE_SELECTION_UNSUPPORTED"
    assert h.store._guard() == before


def test_lost_selection_ack_reconciles_the_original_once_without_new_writes(assistant, monkeypatch):
    h, control, _ = configured(assistant)
    transact = h.store.transact
    calls = []
    def lost(request, decide, **kwargs):
        result = transact(request, decide, **kwargs)
        if request.request_id == selection_key(control.policy.authority_domain,
                control_key("conversation-profile-id", control.policy.authority_domain, "choose-v1")):
            calls.append(True)
            raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "Synthetic lost selection acknowledgment")
        return result
    monkeypatch.setattr(h.store, "transact", lost)
    response, _ = select(h)
    assert response.status_code == 503 and calls == [True]
    before = h.store._guard()
    restored, _ = select(h, reconcile=True, identity="read-only")
    assert restored.status_code == 200
    repeated, _ = select(h)
    assert repeated.status_code == 200 and repeated.json() == restored.json()
    assert h.store._guard() == before and calls == [True] and not h.endpoint.requests


def test_later_human_choice_does_not_change_original_selection_or_work(assistant, monkeypatch):
    from shared_context.behavior_profile_selection import read_selection
    h, control, _ = configured(assistant)
    sdk_io(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "First version."}, {"content": "Same version."}, {"content": "New version."}])
    selected, _ = select(h)
    first, _ = submit(h, "selected-first", "Use the selected profile.")
    assert h.runtime.controller.step(first["binding_id"]).state == "succeeded"
    sid = selected.json()["result"]["selection_id"]
    original = read_selection(control.store, control.policy, sid)
    changed, _ = select(h, request_id="choose-attachment-profile", previous=sid,
                        value=template(text_attachment_profile="fa-source-text-attachments-v1"))
    assert changed.status_code == 200
    second, _ = submit(h, "selected-followup", "Continue the original.", first["conversation_id"])
    assert control.records[second["binding_id"]].profile_selection_id == sid
    assert control.records[second["binding_id"]].profile.text_attachment_profile is None
    assert h.runtime.controller.step(second["binding_id"]).state == "succeeded"
    third, _ = submit(h, "selected-new-conversation", "Use the current profile.")
    assert control.records[third["binding_id"]].profile.text_attachment_profile == "fa-source-text-attachments-v1"
    assert h.runtime.controller.step(third["binding_id"]).state == "succeeded"
    assert read_selection(control.store, control.policy, sid) == original
    control.refresh()
    record = control.records[third["binding_id"]]
    with pytest.raises(Problem):
        derive_registered_work(control.policy, record, h.runtime.mode)
    check_history(h)


def test_approved_destination_skill_can_be_selected_without_moving_policy_or_grants(
    assistant, monkeypatch, signing_key, jwk_client,
):
    from test_skill_purpose_api import add_purpose_skill
    version = add_purpose_skill(assistant, signing_key, jwk_client, name="receipt-guide")
    h, control, _ = configured(assistant)
    original_policy = control.policy.model_dump_json()
    value = template("fa-instruction-skill-purpose-v1").model_copy(update={"skills": (TemplateSkill(
        name="receipt-guide", version_id=version["version_id"], sha256=version["sha256"],
        metadata_sha256=version["metadata_sha256"],
    ),)})
    result, _ = select(h, value=value)
    assert result.status_code == 200, result.text
    sdk_io(monkeypatch, expected_tools=("read_context", "execute_operation", "read_skill"))
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Approved destination skill selected."}])
    accepted, _ = submit(h, "selected-skill-conversation", "Use the approved guide if needed.")
    profile = control.records[accepted["binding_id"]].profile
    assert profile.skill_artifacts == ("receipt-guide",)
    assert profile.prompt_profile == "fa-instruction-skill-purpose-v1"
    assert h.runtime.controller.step(accepted["binding_id"]).state == "succeeded"
    assert control.policy.model_dump_json() == original_policy
    check_history(h)


def test_template_cannot_select_unavailable_destination_skill_versions(assistant):
    h, control, _ = configured(assistant)
    imported = template("fa-instruction-skill-purpose-v1").model_copy(update={"skills": (TemplateSkill(
        name="not-granted-by-template", version_id="a" * 32, sha256="a" * 64,
    ),)})
    before = h.store._guard()
    response, _ = select(h, value=imported)
    assert response.status_code == 409 and response.json()["error"]["code"] == "PROFILE_SELECTION_SOURCES_CHANGED"
    assert h.store._guard() == before and not control.records


def test_revoked_standing_scope_cannot_select_or_reconcile_profile(assistant):
    h, control, human = configured(assistant)
    selected, _ = select(h)
    assert selected.status_code == 200
    control.revoke(human, "revoke-profile-scope")
    before = h.store._guard()
    for reconcile in (False, True):
        response, _ = select(h, reconcile=reconcile)
        assert response.status_code == 403
    assert h.store._guard() == before


def test_selected_attachment_work_reconciles_original_submission(assistant):
    h, control, _ = configured(assistant)
    selected, _ = select(h, value=template(text_attachment_profile="fa-source-text-attachments-v1"))
    assert selected.status_code == 200
    attachment = TextAttachment(name="metrics.json", source=control.policy.profile.source)
    command = InstructionSubmit(request_id="selected-attachment", content="Read this permitted attachment.",
                                attachments=(attachment,))
    first = h.client.post("/work-policy/v1/submit", content=command.model_dump_json(), headers=h.headers())
    assert first.status_code == 200, first.text
    before = h.store._guard()
    result = h.client.post("/work-policy/v1/submit-reconcile", content=command.model_dump_json(), headers=h.headers())
    assert result.status_code == 200, result.text
    assert result.json()["binding_id"] == first.json()["binding_id"]
    assert h.store._guard() == before and not h.endpoint.requests
    from web_test_console.freeform import InstructionControl
    client = object.__new__(InstructionControl)
    client.config = SimpleNamespace(standing_policy=control.policy)
    client.command = lambda request_id: command
    client.store = SimpleNamespace(get=lambda request_id: {"thread_id": "selected-thread"}, lane=lambda thread_id: None)
    task = client.task(command.request_id)
    assert task.binding_id == first.json()["binding_id"]
    assert task.job_id == control.records[first.json()["binding_id"]].binding.job_id
    from web_test_console.adapter import ConsoleError
    client.store = SimpleNamespace(get=lambda request_id: {"thread_id": "selected-thread"},
                                   lane=lambda thread_id: "foundry-native-v1")
    with pytest.raises(ConsoleError) as moved:
        client.task(command.request_id)
    assert moved.value.code == "INSTRUCTION_ROUTE_CHANGED"


@pytest.mark.parametrize("existing", [True, False], ids=["admitted", "denied-conversation"])
def test_preflight_always_returns_the_instruction_binding_not_the_metrics_template(assistant, existing):
    h, control, _ = configured(assistant)
    command = InstructionSubmit(request_id="preflight-same-original", content="Answer the exact original instruction.",
        conversation_id=None if existing else "unavailable-conversation")
    if existing:
        accepted = h.client.post("/work-policy/v1/submit", content=command.model_dump_json(), headers=h.headers())
        assert accepted.status_code == 200
        expected = control.records[accepted.json()["binding_id"]].binding
    else:
        from shared_context.behavior_work_policy import derive_work
        _, expected, _ = derive_work(control.policy, command.request_id, h.runtime.mode, instruction=command)
    query = WorkPreflightQuery(read_id="preflight-same-read", request_id=command.request_id,
        submit_body_sha256=hashlib.sha256(command.model_dump_json().encode()).hexdigest(),
        content=command.content, conversation_id=command.conversation_id)
    response = h.client.post("/work-policy/v1/preflight", content=query.model_dump_json(), headers=h.headers())
    assert response.status_code == 200, response.text
    assert response.json()["binding"] == expected.model_dump(mode="json")
    assert response.json()["already_admitted"] is existing
    if not existing:
        assert response.json()["decision"] == "deny"


@pytest.mark.parametrize("max_prompt_tokens,expected", [
    (None, "SKILL_CATALOG_CAPACITY_UNKNOWN"), (50, "SKILL_CATALOG_BUDGET_TOO_SMALL"),
])
def test_impossible_default_model_catalog_budget_is_refused_before_selection(assistant, max_prompt_tokens, expected):
    h, control, _ = configured(assistant)
    current = control.models.select(None)
    control.models.models[current.id] = current.model_copy(update={"max_prompt_tokens": max_prompt_tokens})
    before = h.store._guard()
    response, _ = select(h, value=template("fa-instruction-skills-v1",
        model_capacity_profile="fa-model-capacity-v1", skill_catalog_budget_profile="fa-skill-catalog-budget-v1"))
    assert response.status_code in {413, 503}
    assert response.json()["error"]["code"] == expected
    assert h.store._guard() == before and not control.records
