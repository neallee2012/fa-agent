"""E-12 on the pinned SDK 1.0.13 / runtime 1.0.83: a demonstrated procedure becomes an approved skill used elsewhere.

Conversation A saves the user's procedure as an unapproved Team draft; a human approves it; the template then pins
that skill with fa-skills-v1; a separate conversation B reads the approved version. Scripted provider; no cloud,
network or real model.
"""

import importlib.metadata
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from conftest import A
from sdk_checkpoint_local import sse
from shared_context.behavior_agent_template import template_options
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_history_skill_draft_prompt import HISTORY_SKILL_DRAFT_CAPABILITIES
from shared_context.behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE, SkillDraftReceipt
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.m3_auth import HumanApprover
from shared_context.m3_models import ApprovalInput
from test_behavior_assistant import assistant
from test_conversation_profile_selection import configured, select, template
from test_instruction_flow import instruction_models
from test_instruction_prompt import submit
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_skill_draft import BODY, PURPOSE
from test_skills_capability import FILES, GRANTS, SKILLS_REF

DEMO = "When I hand over an operator shift, confirm the original request and save only non-secret guidance."
USE = "Please hand over this shift using my saved procedure."


def test_demonstrated_procedure_becomes_an_approved_skill_used_in_a_separate_conversation(assistant, monkeypatch):
    h, control, human = configured(assistant)
    h.runtime.capability_grants = GRANTS
    drafting, _ = select(h, request_id="choose-drafting", value=template(
        HISTORY_SKILL_DRAFT_PROFILE, user_text_file_profile=FILES, capabilities=(SKILLS_REF,)))
    assert drafting.status_code == 200, drafting.text
    requests, receipts, reads = [], [], []

    def provider(request):
        body = json.loads(request.content)
        tools = {item["function"]["name"] for item in body["tools"]}
        users = [m["content"] for m in body["messages"] if m["role"] == "user"]
        results = [m for m in body["messages"] if m["role"] == "tool"]
        text = "" if not results else (results[-1]["content"] if isinstance(results[-1]["content"], str)
                                       else "".join(part["text"] for part in results[-1]["content"]))
        requests.append({"tools": tools, "users": json.dumps(users)})
        if DEMO in json.dumps(users):
            if not results:
                delta = {"tool_calls": [{"index": 0, "id": "save-demo", "type": "function", "function": {
                    "name": "save_skill_draft", "arguments": json.dumps({"purpose": PURPOSE, "body": BODY})}}]}
            else:
                receipts.append(SkillDraftReceipt.model_validate_json(text))
                delta = {"content": "Saved an unapproved skill draft for human review."}
        elif not results:
            delta = {"tool_calls": [{"index": 0, "id": "read-approved", "type": "function", "function": {
                "name": "read_skill", "arguments": json.dumps({"name": receipts[0].artifact_id})}}]}
        else:
            reads.append(json.loads(text))
            delta = {"content": "Handed over using your approved procedure."}
        return sse(request, delta, len(requests))

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_E12_PROVIDER")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        assistant=SimpleNamespace(authority=h.assistant),
        installation=lambda: None, require_current_native_evidence=lambda: None))
    parent = Path(__file__).resolve().parents[2] / ".azure"
    parent.mkdir(exist_ok=True)
    with TestClient(h.app) as client, TemporaryDirectory(prefix="e12-", dir=parent) as directory:
        h.client = client
        h.executor.http_client, h.executor.model_transport = client, None
        h.executor.root = Path(directory) / "a"
        demo, _ = submit(h, "e12-demonstration", DEMO)
        assert h.runtime.controller.step(demo["binding_id"]).state == "succeeded"
        assert len(receipts) == 1 and receipts[0].approved is False
        assert receipts[0].artifact_id not in {item.name for item in template_options(control, h.assistant, human).skills}

        receipt = receipts[0]
        record = h.artifacts.get(A, A.team, receipt.artifact_id)
        h.artifacts.decide(HumanApprover(h.artifacts.approval.approver_oid, A.team), A.team, receipt.artifact_id,
            record.etag, ApprovalInput(decision_id=receipt.version_id, version_id=receipt.version_id,
                                       sha256=receipt.sha256, metadata_sha256=receipt.metadata_sha256,
                                       decision="approve"))
        options = template_options(control, h.assistant, human)
        offered = {item.name: item for item in options.skills}
        assert offered[receipt.artifact_id].version_id == receipt.version_id
        assert [(item.entry_id, item.entry_sha256) for item in options.capabilities] == [
            (SKILLS_REF.entry_id, SKILLS_REF.entry_sha256)]
        pinned = template(HISTORY_SKILL_DRAFT_PROFILE, user_text_file_profile=FILES,
                          capabilities=(SKILLS_REF,)).model_copy(update={"skills": (offered[receipt.artifact_id],)})
        chosen, _ = select(h, request_id="choose-approved-skill", previous=drafting.json()["result"]["selection_id"],
                           value=pinned)
        assert chosen.status_code == 200, chosen.text

        h.executor.root = Path(directory) / "b"
        used, _ = submit(h, "e12-separate-use", USE)
        assert used["conversation_id"] != demo["conversation_id"]
        assert h.runtime.controller.step(used["binding_id"]).state == "succeeded"
        assert len(reads) == 1 and reads[0]["skill"]["name"] == receipt.artifact_id
        assert reads[0]["skill"]["version_id"] == receipt.version_id and reads[0]["text"] == BODY
        later = [item for item in requests if USE in item["users"]]
        assert later and all(DEMO not in item["users"] for item in later)
        assert all("read_skill" in item["tools"] and "save_skill_draft" in item["tools"] for item in later)
    assert importlib.metadata.version("github-copilot-sdk") == "1.0.13"
