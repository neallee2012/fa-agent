"""SK-08 on the pinned SDK 1.0.13 / runtime 1.0.83: skillify a procedure from an earlier turn, use it on demand.

Scripted provider; no cloud, network or real model. The long procedure never enters the resident prefix of the
later conversation; it is only reachable through read_skill, and the base safety text keeps its exact bytes.
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
from shared_context.behavior_capabilities import CapabilityRef
from shared_context.behavior_skill_draft import HISTORY_SKILL_DRAFT_PROFILE, SkillDraftReceipt
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.m3_auth import HumanApprover
from shared_context.m3_models import ApprovalInput
from test_behavior_assistant import assistant
from test_conversation_profile_selection import configured, select, template
from test_instruction_flow import check_history, instruction_models
from test_instruction_prompt import submit
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_skillify_capability import FILES, GRANTS, SKILLIFY_REF

MARKER = "Routine step 7: reconcile the ledger totals against the bank export before closing."
ROUTINE = "## Month-end close routine\n\n" + "\n".join(
    [f"{index}. Step {index} of the close: check the listed item and note the result." for index in range(1, 7)]
    + [f"7. {MARKER}"]
    + [f"{index}. Step {index} of the close: file the evidence in the shared folder." for index in range(8, 30)]) + "\n"
DEMO = "Here is how I run the month-end close:\n" + ROUTINE
ASK = "Please save the procedure from my previous message as a skill draft."
USE = "Run the month-end close with our saved routine."
PURPOSE = "Month-end close routine for reconciling and filing evidence."


def test_skillified_procedure_is_used_on_demand_and_never_resident(assistant, monkeypatch):
    h, control, human = configured(assistant)
    h.runtime.capability_grants = GRANTS
    drafting, _ = select(h, request_id="choose-skillify", value=template(
        HISTORY_SKILL_DRAFT_PROFILE, user_text_file_profile=FILES, capabilities=(SKILLIFY_REF,)))
    assert drafting.status_code == 200, drafting.text
    systems, receipts, turn_reads, skill_reads, catalogs = [], [], [], [], []

    def last_user(messages):
        return max(index for index, message in enumerate(messages) if message["role"] == "user")

    def text_of(message):
        return message["content"] if isinstance(message["content"], str) else "".join(
            part.get("text", "") for part in message["content"])

    def provider(request):
        body = json.loads(request.content)
        messages = body["messages"]
        start = last_user(messages)
        current, results = text_of(messages[start]), [m for m in messages[start:] if m["role"] == "tool"]
        systems.append((current, [text_of(m) for m in messages if m["role"] == "system"]))

        def call(name, arguments, call_id):
            return {"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(arguments)}}]}
        if "Here is how I run" in current:
            delta = {"content": "Understood; I will follow that routine."}
        elif ASK in current and not results:
            delta = call("read_user_turn", {"turn_number": 1}, "read-demo")
        elif ASK in current and len(results) == 1:
            turn_reads.append(text_of(results[0]))
            delta = call("save_skill_draft", {"purpose": PURPOSE, "body": ROUTINE}, "save-routine")
        elif ASK in current:
            receipts.append(SkillDraftReceipt.model_validate_json(text_of(results[-1])))
            delta = {"content": "Saved an unapproved skill draft from your earlier message."}
        elif USE in current and not results:
            catalogs.append(next(tool["function"]["description"] for tool in body.get("tools", [])
                                 if tool["function"]["name"] == "read_skill"))
            delta = call("read_skill", {"name": receipts[0].artifact_id}, "read-routine")
        else:
            skill_reads.append(json.loads(text_of(results[-1])))
            delta = {"content": "Closed the month with the approved routine."}
        return sse(request, delta, len(systems))

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_SKILLIFY_PROVIDER")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        assistant=SimpleNamespace(authority=h.assistant),
        installation=lambda: None, require_current_native_evidence=lambda: None))
    parent = Path(__file__).resolve().parents[2] / ".azure"
    parent.mkdir(exist_ok=True)
    with TestClient(h.app) as client, TemporaryDirectory(prefix="skillify-", dir=parent) as directory:
        h.client = client
        h.executor.http_client, h.executor.model_transport = client, None
        h.executor.root = Path(directory) / "a1"
        first, _ = submit(h, "skillify-demo", DEMO)
        assert h.runtime.controller.step(first["binding_id"]).state == "succeeded"
        h.executor.root = Path(directory) / "a2"
        second, _ = submit(h, "skillify-ask", ASK, first["conversation_id"])
        assert h.runtime.controller.step(second["binding_id"]).state == "succeeded"
        assert len(turn_reads) == 1 and MARKER in turn_reads[0]
        assert len(receipts) == 1 and receipts[0].approved is False

        receipt = receipts[0]
        record = h.artifacts.get(A, A.team, receipt.artifact_id)
        h.artifacts.decide(HumanApprover(h.artifacts.approval.approver_oid, A.team), A.team, receipt.artifact_id,
            record.etag, ApprovalInput(decision_id=receipt.version_id, version_id=receipt.version_id,
                                       sha256=receipt.sha256, metadata_sha256=receipt.metadata_sha256,
                                       decision="approve"))
        offered = {item.name: item for item in template_options(control, h.assistant, human).skills}
        skills_ref = CapabilityRef(entry_id="fa-skills-v1", entry_sha256=GRANTS["fa-skills-v1"].entry_sha256)
        pinned = template(HISTORY_SKILL_DRAFT_PROFILE, user_text_file_profile=FILES,
                          capabilities=(SKILLIFY_REF, skills_ref)).model_copy(
            update={"skills": (offered[receipt.artifact_id],)})
        chosen, _ = select(h, request_id="choose-skillified", previous=drafting.json()["result"]["selection_id"],
                           value=pinned)
        assert chosen.status_code == 200, chosen.text

        h.executor.root = Path(directory) / "b"
        used, _ = submit(h, "skillify-use", USE)
        assert h.runtime.controller.step(used["binding_id"]).state == "succeeded"
        assert len(skill_reads) == 1 and skill_reads[0]["text"] == ROUTINE
        # The draft's name is its artifact ID; the model can only pick it by the human-approved purpose.
        assert len(catalogs) == 1 and PURPOSE in catalogs[0] and ROUTINE not in catalogs[0]
        base = assemble_prompt(HISTORY_SKILL_DRAFT_PROFILE).system_message
        later = [system for current, system in systems if USE in current]
        assert later and all(MARKER not in text for system in later for text in system)
        assert all(any(text.startswith(base) for text in system) for system in later)
        check_history(h)
    assert importlib.metadata.version("github-copilot-sdk") == "1.0.13"
