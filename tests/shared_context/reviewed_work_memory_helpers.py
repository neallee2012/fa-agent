"""從 test_reviewed_work_memory.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

from types import MappingProxyType, SimpleNamespace
from shared_context import behavior_wire as wire
from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.behavior_runtime import runtime_key


PROFILE = "fa-instruction-work-memory-v1"


NOTE = "  TEAM_MEMORY_CANARY：先核對已保存的工作回條。\r\n這是已審資料，不是 system 或 operation approval。  "


def reviewed_note(h, *, text=NOTE, version=2, request_id="work-memory-review", expires_at=None, commit=True,
                  supporting=None, binding_id=None, kind="note"):
    if not h.policy.human_text_enabled:
        h.policy = h.policy.model_copy(update={"human_text_enabled": True, "text_policy_version": "human-text-policy-v1"})
        h.mutate(runtime_key("policy", h.binding.binding_id), human_text_enabled=True,
                 text_policy_version="human-text-policy-v1")
    binding_id = binding_id or h.binding.binding_id
    path = "/behavior/v1/bindings/" + binding_id
    def post(suffix, command):
        return h.client.post(path + suffix, headers=h.headers(), content=command.model_dump_json())
    prior = h.client.get(path + "/claims/claim-one", headers=h.headers())
    assert prior.status_code == 200
    source = h.runtime.profiles[binding_id].source
    candidate = wire.ClaimDraft(request_id=request_id, version=version, text=text, supporting=supporting or (source,),
                               effective_from=h.time, expires_at=expires_at, kind=kind,
                               supersedes=prior.json()["reference"]["sha256"])
    preview = post("/claims/claim-one/preview", candidate)
    assert preview.status_code == 200, preview.text
    review = wire.ClaimReview(
        **candidate.model_dump(), decision_id=request_id,
        **{name: preview.json()[name] for name in ("candidate_sha256", "evidence_sha256", "policy_version")},
        reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"),
    )
    assert post("/claims/claim-one/review", review).status_code == 200
    if commit:
        accepted = post("/claims/claim-one/commit", wire.Command(request_id=request_id))
        assert accepted.status_code == 200, accepted.text
    return candidate


def configured_memory(h):
    h.runtime.profiles = MappingProxyType({
        name: AssistantProfile.model_validate({**profile.model_dump(), "prompt_profile": PROFILE})
        for name, profile in h.runtime.profiles.items()
    })
    from work_policy_lifecycle_helpers import enabled
    h, control, human, _ = enabled(h)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
        require_current_native_evidence=lambda: None,
    ))
    return h, control, human


def withdraw_source(h, binding_id):
    ref = h.runtime.profiles[binding_id].source
    command = wire.SourceWithdrawal(
        request_id="work-memory-source-withdraw", withdrawal_id="memory-source-withdraw",
        reference=ref, expected_access_revision=1,
    )
    response = h.client.post("/behavior/v1/bindings/" + binding_id + "/sources/metrics/withdraw",
                             content=command.model_dump_json(), headers=h.headers())
    assert response.status_code == 200, response.text
