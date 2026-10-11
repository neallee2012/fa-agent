"""Linux fixed SDK through the original HTTPS-origin callback routes; local transports/provider only."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from sdk_checkpoint_local import sse
from shared_context import behavior_wire as wire
from shared_context.behavior_assistant_hosted import AssistantClient, AssistantSession
from shared_context.behavior_peer_runtime import install_peer_routing
from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
from shared_context.source_materials import digest
from test_agent_runtime import peer_runtime
from test_agent_runtime_routing import agent_headers, scoped_app
from shared_context.behavior_assistant import _LocalHttp
from test_conversation_profile_selection import template
from test_conversation_transfer_startup import claimed_turn, read_startup, synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key


@pytest.fixture
def sdk_peer(request, runtime_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("The startup fixture forbids sockets; run this loop/process case in the network-none Linux image.")
    spawn = subprocess.Popen
    case = request.getfixturevalue("peer_runtime")
    def only_sdk(arguments, *args, **kwargs):
        assert Path(arguments[0]).resolve() == runtime_path.resolve()
        return spawn(arguments, *args, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", only_sdk)
    return case


def test_b_runs_the_fixed_sdk_through_credential_bound_peer_callbacks(sdk_peer, runtime_path, signing_key, tmp_path):
    c, a, b = sdk_peer.c, sdk_peer.a, sdk_peer.b
    b.assistant.artifacts.store = c.authority.artifacts.store
    select_profile(b.runtime.work_policy, b.assistant.authority, c.human,
        SelectConversationProfile(request_id="b-sdk-profile", expected_selection_id=None, template=template()))
    calls = []
    def provider(request):
        body = json.loads(request.content)
        assert body["model"] == "gpt-4.1-mini"
        calls.append(body["model"])
        return sse(request, {"content": "B completed its own SDK turn."}, len(calls))
    b.assistant.provider.transport = httpx.MockTransport(provider)
    peer = SimpleNamespace(h=SimpleNamespace(runtime=b.runtime, config=c.h.config, time=c.h.time),
        control=b.runtime.work_policy, authority=b.assistant.authority, native=b.runtime.dep.native_launches,
        human=c.human)
    opened = claimed_turn(peer, "b-real-sdk", acquire=False)
    app = scoped_app(a)
    install_peer_routing(app, a, scoped_app)
    headers = agent_headers(c, signing_key, 1)
    with TestClient(app) as http:
        client = AssistantClient("https://context.invalid", opened.assignment, opened.ticket,
            token_provider=lambda: headers["Authorization"].removeprefix("Bearer "), transport=_LocalHttp(http))
        bridge = AssistantSession(client, root=tmp_path / "b-private-sdk", runtime_path=runtime_path)
        try:
            bridge.dispatch({"action": "start"})
            assert bridge._sdk_stop is not None and bridge._sdk_stop.graceful
            assert len(calls) == 1
            assert b.runtime.dep.workers.contract.capacity == a.runtime.dep.workers.contract.capacity == 1
            assert b.runtime.store is a.runtime.store
            result = b.assistant.authority.result(opened.launch.start.binding)
            probe = peer.native.probe(opened.launch.start.binding)
            observation = peer.native.report(opened.binding.binding_id, opened.launch.launch_id, probe.probe_id,
                wire.HostedReport(**opened.claim.model_dump(), probe_id=probe.probe_id,
                    process_generation=opened.launch.process_generation,
                    binding_digest=digest(opened.launch.start.binding.model_dump(mode="json")),
                    supervisor_id=opened.claimed.assignment.process_id, sequence=1, phase="sealed",
                    child_pid=bridge._sdk_stop.pid, sdk_stop=bridge._sdk_stop, exit_code=0, child_exited=True,
                    outcome="succeeded", result=wire.AssistantExecutionResult(
                        session_id=opened.assignment.session_id, result_digest=digest(result.model_dump(mode="json")))),
                opened.agent)
            workers = b.runtime.dep.workers
            workers.observe(opened.binding.job_id, observation.evidence_id, opened.binding.owner)
            workers.forward_result(opened.binding.job_id, opened.binding.owner, b.runtime.dep.delivery)
            assert workers.inspect(opened.binding.job_id).result_forwarded
            assert opened.binding.binding_id not in a.runtime.bindings
            assert a.runtime.work_policy is c.control
            read_startup(c)
        finally:
            client.close()
