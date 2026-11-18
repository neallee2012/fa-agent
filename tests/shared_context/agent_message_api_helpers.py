"""從 test_agent_message_api.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

import pytest


@pytest.fixture
def shared_message_turn(request, monkeypatch):
    import test_agent_runtime
    from shared_context.behavior_work_policy import SteadyWorkPolicy
    from shared_context.source_materials import digest
    original = test_agent_runtime.peer_policy
    def shared_policy(config, policy):
        peer = original(config, policy)
        binding = peer.template.model_copy(update={"source_ids": policy.template.source_ids})
        profile = peer.profile.model_copy(update={"source": policy.profile.source})
        runtime_policy = peer.legacy_template_policy.model_copy(update={
            "binding_digest": digest(["fa-behavior-api-v1", peer.evidence_mode, binding.model_dump(mode="json")])})
        return SteadyWorkPolicy.model_validate_json(peer.model_copy(update={
            "template": binding, "profile": profile, "legacy_template_policy": runtime_policy}).model_dump_json())
    monkeypatch.setattr(test_agent_runtime, "peer_policy", shared_policy)
    return request.getfixturevalue("message_turn")
