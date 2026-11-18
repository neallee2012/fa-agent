"""從 test_skill_purpose_api.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

from types import MappingProxyType, SimpleNamespace
from conftest import A
from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_work_policy_api import install_work_policy_routes
from test_m3_artifacts import V1
from test_skill_purpose import BODY, PROFILE, PURPOSE, create_skill, decide, human, upload_purpose
from work_policy_lifecycle_helpers import enabled


def add_purpose_skill(h, signing_key, jwk_client, *, name="receipt-guide", description=PURPOSE,
                      body=BODY, version=V1, create=True, approve=True, resources=None):
    artifacts = h.artifacts
    before = (create_skill(artifacts, name, source_work="legacy-source") if create
              else artifacts.get(A, A.team, name))
    uploaded = upload_purpose(artifacts, before.etag, version=version, description=description, body=body,
                              name=name, resources=resources)
    selected = artifacts.version(human(artifacts), A.team, name, version).model_dump()
    if approve:
        decide(artifacts, uploaded, selected, name=name)
    return selected


def configured_purpose(assistant, selected=("receipt-guide",), *, profile_id=PROFILE):
    assistant.runtime.profiles = MappingProxyType({
        name: AssistantProfile.model_validate({
            **profile.model_dump(), "prompt_profile": profile_id, "skill_artifacts": selected,
        }) for name, profile in assistant.runtime.profiles.items()
    })
    h, control, human, _ = enabled(assistant)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
        require_current_native_evidence=lambda: None,
    ))
    return h, control, human
