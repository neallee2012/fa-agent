"""從 test_conversation_profile_selection.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

from types import MappingProxyType, SimpleNamespace
from shared_context.behavior_agent_template import AgentTemplate, TemplateRole, TemplateSettings
from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_profile_selection import SelectConversationProfile
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.behavior_work_policy_api import install_work_policy_routes
from work_policy_lifecycle_helpers import enabled


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
