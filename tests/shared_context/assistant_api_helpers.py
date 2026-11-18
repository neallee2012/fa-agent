"""The assistant composition without the legacy lane: no /assistant/* routes and no LocalAssistantExecutor.

The setup the retired legacy `assistant` fixture had, for tests that only need the app or the runtime.
"""

import asyncio
import json
import subprocess
from types import MappingProxyType, SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import A
from shared_context import behavior_wire as wire
from shared_context.behavior_api import create_behavior_app
from shared_context.behavior_assistant import AssistantAuthority, AssistantProfile, AssistantRuntime
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.m3_artifacts import ArtifactService
from shared_context.m3_auth import ApprovalSettings
from shared_context.m3_store import InMemoryArtifactStore
from shared_context.models import CreateWork
from instruction_attachments_helpers import configured_attachments
from local_http_helpers import _LocalHttp
from skill_purpose_api_helpers import add_purpose_skill
from test_behavior_api import RuntimeHarness
from test_m2_copilot import ScriptedEndpoint
from test_m3_auth import HUMAN_OID, CLIENT_ID
from test_skill_purpose import PROFILE
from web_test_console.adapter import parse_config
from web_test_console.instructions import InstructionConnection
from web_test_console_frozen_interface_helpers import open_ui
from web_test_console_standing_helpers import configuration
from work_policy_lifecycle_helpers import enabled


@pytest.fixture
def assistant_api(auth, jwk_client, signing_key, system):
    h = RuntimeHarness(auth, jwk_client, signing_key, work_checks=(
        "answer_verified", "artifact_verified", "finish_verified",
    ))
    ref = h.source()
    candidate = wire.ClaimDraft(
        request_id="review", version=1, text="The accepted metrics source has been reviewed.",
        supporting=(ref,), effective_from=h.time,
    )
    preview = h.post("/claims/claim-one/preview", candidate).json()
    reviewed = h.post("/claims/claim-one/review", wire.ClaimReview(
        **candidate.model_dump(), decision_id="decision",
        candidate_sha256=preview["candidate_sha256"], evidence_sha256=preview["evidence_sha256"],
        policy_version=preview["policy_version"],
        reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"),
    ))
    assert reviewed.status_code == 200, reviewed.text
    assert h.post("/claims/claim-one/commit", wire.Command(request_id="review")).status_code == 200
    context = system[0]
    context.create(A, A.team, "legacy-source", CreateWork(goal="Assistant result source"))
    h.artifacts = ArtifactService(context, InMemoryArtifactStore(auth.tenant_id),
                                 ApprovalSettings(HUMAN_OID, CLIENT_ID, A.team))
    profile = AssistantProfile(
        binding_id="binding-one", source=ref, claim_id="claim-one",
        legacy_source_work="legacy-source", artifact_id="assistant-report",
        artifact_version="a" * 32, policy_version="assistant-own-result-v1",
    )
    h.runtime = AssistantRuntime(h.dep, (h.binding,), profiles=(profile,),
                                 clock=lambda: h.time, mode="offline_contract",
                                 controller_id="internal-controller")
    h.assistant = AssistantAuthority(h.runtime, h.artifacts, (A,))
    h.app = create_behavior_app(h.runtime, h.verifier)
    h.client = TestClient(h.app)
    # Never called: tests assert it saw no model request, and shared helpers (role_agent, capability_capture
    # enabled) still configure it.
    h.endpoint = ScriptedEndpoint([])
    return h


@pytest.fixture
def picker_api(assistant_api, monkeypatch, tmp_path):
    """`test_web_test_console_attachments.picker` on `assistant_api`."""
    def forbidden(*args, **kwargs):
        pytest.fail("Attachment picker must not start runtime, model or real network")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    h, control, human, attachment = configured_attachments(assistant_api)
    h.client.close()
    h.app = create_behavior_app(h.runtime, h.verifier)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, assistant=SimpleNamespace(authority=h.assistant),
        installation=lambda: control.installed(), require_current_native_evidence=lambda: None,
    ))
    h.client = TestClient(h.app)
    monkeypatch.setattr(type(h.runtime.controller), "step", forbidden)
    local = _LocalHttp(h.client)
    c = SimpleNamespace(h=h, control=control, human=human, attachment=attachment, calls=[],
                        mutate=lambda request, response: response, before=lambda request: None,
                        journal=tmp_path / "picker.sqlite3")

    def send(request):
        c.calls.append((request.method, request.url.path, request.content))
        c.before(request)
        return c.mutate(request, local.handle_request(request))

    c.config = parse_config(json.dumps(configuration(h, control)).encode())
    c.connection = InstructionConnection(schema_version="fa-console-instructions-connection-v1",
        configuration_sha256=c.config.sha256, backend_core_sha256="c" * 64)
    c.backend = httpx.Client(transport=httpx.MockTransport(send))
    c.browser = open_ui(c)
    c.calls.clear()
    try:
        yield c
    finally:
        c.browser.ui.close()
        c.backend.close()


@pytest.fixture
def template_case_api(assistant_api, signing_key, jwk_client):
    """`test_agent_template.template_case` on `assistant_api`."""
    h = assistant_api
    source = add_purpose_skill(h, signing_key, jwk_client)
    h.runtime.profiles = MappingProxyType({
        key: AssistantProfile.model_validate({
            **value.model_dump(), "prompt_profile": PROFILE, "skill_artifacts": ("receipt-guide",),
        }) for key, value in h.runtime.profiles.items()
    })
    h, control, human, _ = enabled(h)
    h.app = create_behavior_app(h.runtime, h.verifier)
    composition = SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier,
        assistant=SimpleNamespace(authority=h.assistant),
    )
    install_work_policy_routes(h.app, composition)
    with TestClient(h.app) as http:
        h.client = http
        yield h, control, human, source, composition
