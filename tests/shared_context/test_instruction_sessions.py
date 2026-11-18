"""Per-work Hosted session SDK calls, never real cloud I/O."""
from types import SimpleNamespace
from unittest.mock import MagicMock
import httpx
from azure.ai.projects.models import AgentSessionResource,VersionRefIndicator
from shared_context.behavior_foundry import FoundryTarget,FoundryHttpClient,instruction_target
import pytest


def test_official_session_create_is_once_per_work_and_never_mutates_shared_target(monkeypatch):
    import azure.ai.projects
    import shared_context.behavior_foundry as module
    calls=[]
    def create(name,*,version_indicator,agent_session_id):
        calls.append((name,version_indicator.agent_version,agent_session_id))
        return AgentSessionResource(agent_session_id=agent_session_id,version_indicator=version_indicator,status="creating")
    agents=SimpleNamespace(create_session=create,get_session=lambda name,sid:AgentSessionResource(
        agent_session_id=sid,version_indicator=VersionRefIndicator(agent_version="31"),status="idle"))
    factory=MagicMock()
    factory.return_value.__enter__.return_value=SimpleNamespace(agents=agents)
    monkeypatch.setattr(azure.ai.projects,"AIProjectClient",factory)
    monkeypatch.setattr(module.time,"sleep",lambda _:None)
    target=FoundryTarget(executor_id="executor",project_endpoint="https://offline.services.ai.azure.com/api/projects/p",
        agent_id="11111111-1111-1111-1111-111111111111",agent_name="same-agent",agent_version="31",
        session_id="original-must-not-change",agent_oid="22222222-2222-2222-2222-222222222222")
    def denied(request):raise AssertionError("no real HTTP")
    client=FoundryHttpClient(target,credential=object(),transport=httpx.MockTransport(denied))
    try:
        one,two=instruction_target(target,"work-one"),instruction_target(target,"work-two")
        client.for_target(one).create_session(before_send=lambda:None)
        client.for_target(two).create_session(before_send=lambda:None)
        assert client.target==target and one.session_id!=two.session_id
        assert calls==[("same-agent","31",one.session_id),("same-agent","31",two.session_id)]
        for call in factory.call_args_list:
            assert call.kwargs["endpoint"]==target.project_endpoint
            assert all(call.kwargs[k]==0 for k in ("retry_total","retry_connect","retry_read","retry_status"))
    finally:client.close()


@pytest.mark.parametrize("status,code,expected", [
    (412, "BEHAVIOR_CONFLICT", 2),
    (503, "BEHAVIOR_COMMIT_UNKNOWN", 1),
    (403, "WORK_POLICY_REVOKED", 1),
    (412, "OTHER_CONFLICT", 1),
])
def test_instruction_route_classification_retries_only_rejected_pure_reads(status, code, expected):
    from shared_context.behavior_server import ServerComposition
    from shared_context.errors import Problem
    calls = []
    # Uncached work: only then does classification refresh (cached instruction work is classified directly).
    records = {}
    def refresh(_binding_id=None):
        calls.append(True)
        if len(calls) == 1:
            raise Problem(status, code, "OFFLINE concurrent writer")
        records["registered-work"] = SimpleNamespace(profile=SimpleNamespace(instruction=object()))
    composition = SimpleNamespace(runtime=SimpleNamespace(
        refresh_bindings=refresh, bindings={}, work_policy=SimpleNamespace(records=records)))
    request = SimpleNamespace(path_params={"binding_id": "registered-work"})
    if expected == 2:
        assert ServerComposition.instruction_request(composition, request) is True
    else:
        with pytest.raises(Problem) as failed:
            ServerComposition.instruction_request(composition, request)
        assert failed.value.body["error"]["code"] == code
    assert len(calls) == expected

