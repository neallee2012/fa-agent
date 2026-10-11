"""Copy-only schema/prompt boundaries and unchanged old bytes; no runtime or model."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_assistant_wire import InstructionTask, InstructionSubmit
from shared_context.behavior_instruction_prompt import assemble_instruction_prompt
from shared_context.behavior_skill_copy import TeamSopCopy, SopCopyOutcome, SopCopyReceipt
from shared_context.behavior_skill_copy_prompt import SOP_COPY_PROMPT, assemble_sop_copy_prompt, SopCopyPromptRecord
from shared_context.behavior_wire import ExactSourceRef

PREFIX_HASH = "8f6151198fefda9410c59070d311d5ddfb724d6bebc845165b8027aa66cefa13"


def scope():
    return {"schema_version": "fa-team-sop-skill-copy-v1", "authorization": "one-approved-sop-byte-copy-per-work",
        "visibility": "team", "source_visibility": "team", "team_id": "team-one",
        "source_artifact_id": "sop", "source_version_id": "a" * 32, "source_sha256": "b" * 64,
        "source_approval_sha256": "c" * 64,
        "purpose": {"schema_version": "fa-skill-purpose-v1", "description": "Approved receipt procedure copy draft."}}


def test_new_copy_prefix_is_literal_versioned_and_old_default_bytes_are_unchanged():
    raw = SOP_COPY_PROMPT.encode("utf-8")
    assert len(raw) == 2645 and hashlib.sha256(raw).hexdigest() == PREFIX_HASH
    record = assemble_sop_copy_prompt().record
    assert record.prefix_sha256 == PREFIX_HASH and record.prefix_tokens is None
    assert record.declared_capabilities == ("read_context", "execute_operation", "copy_sop_to_skill_draft")
    with pytest.raises(ValueError):
        assemble_sop_copy_prompt(capabilities=("read_context", "execute_operation", "approve"))
    source = ExactSourceRef(source_id="source", version=1, sha256="a"*64, byte_length=5)
    profile = AssistantProfile(binding_id="binding", source=source, claim_id="claim", legacy_source_work="work",
        artifact_id="artifact", artifact_version="a"*32, policy_version="assistant-own-result-v1")
    task = InstructionTask(content="  Original instruction.\r\n", conversation_id="conversation", turn_number=1)
    for value, size, expected in (
        (profile.model_dump_json().encode(), 378, "04f80826a866fd367c2052423418d09e0d137646c51b43e34474777f02077379"),
        (task.model_dump_json().encode(), 153, "eeef164803a5e550ee432e346422ad3af586f7165903484de5ba2ff851b90d36"),
        (assemble_instruction_prompt(None).system_message.encode(), 552, "f2d312f2cab688a03d035adb971ead55e4fd5057c69bdc0eede97c5cb084799f"),
    ):
        assert len(value) == size and hashlib.sha256(value).hexdigest() == expected
    assert "sop_copy" not in task.model_dump_json()


@pytest.mark.parametrize("field,value", [
    ("schema_version", "future"), ("authorization", "admin"), ("visibility", "private"),
    ("source_visibility", "private"), ("purpose", {"schema_version": "fa-skill-purpose-v1", "description": "x" * 513}),
    ("source_version_id", "latest"), ("approved", True),
])
def test_copy_scope_rejects_unknown_authority_private_visibility_or_unbounded_purpose(field, value):
    with pytest.raises(ValueError):
        TeamSopCopy.model_validate_json(json.dumps({**scope(), field: value}))


def test_copy_scope_cannot_be_enabled_by_user_fields_or_attached_to_old_profile():
    for field in ("sop_copy", "prompt_profile", "authorization"):
        with pytest.raises(ValueError):
            InstructionSubmit.model_validate_json(json.dumps({
                "request_id": "copy", "content": "Original instruction", field: scope(),
            }))
    for selected in (None, "fa-instruction-v1", "fa-instruction-skills-v1"):
        with pytest.raises(ValueError):
            InstructionTask.model_validate_json(json.dumps({
                "content": "Original", "conversation_id": "original", "turn_number": 1,
                "prompt_profile": selected, "sop_copy": scope(),
            }))
    with pytest.raises(ValueError):
        InstructionTask(content="Original", conversation_id="original", turn_number=1,
                        prompt_profile="fa-instruction-sop-copy-v1")
    document = assemble_sop_copy_prompt().record.model_dump(mode="json")
    for change in ({"prefix_sha256": "0"*64}, {"schema_version": "future"}, {"prefix_tokens": 0},
                   {"source": "model"}, {"system_message": "untrusted"}):
        with pytest.raises(ValueError):
            SopCopyPromptRecord.model_validate_json(json.dumps({**document, **change}))


def test_copy_outcome_and_receipt_cannot_claim_unknown_as_confirmed_or_current_approval():
    for stage, known, unresolved in (
        ("create_unknown", (), ("original",)), ("created", ("original",), ("original",)),
        ("upload_unknown", ("original",), ("original",)), ("saved", ("original",), ()),
    ):
        value = SopCopyOutcome(schema_version="fa-sop-copy-outcome-v1", receipt_id="original",
                               intent_sha256="f" * 64, stage=stage)
        assert value.known_effects == known and value.unresolved_operations == unresolved
    with pytest.raises(ValueError):
        SopCopyOutcome(schema_version="fa-sop-copy-outcome-v1", receipt_id="original", intent_sha256="f" * 64,
                       stage="success")
    assert SopCopyReceipt.model_fields["state_scope"].is_required()
    with pytest.raises(ValueError):
        SopCopyOutcome(schema_version="fa-sop-copy-outcome-v1", receipt_id="original", intent_sha256="f" * 64,
                       stage="saved", approved=True)


def test_copy_wire_and_strict_bridge_import_without_hosted_sdk():
    source = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"
    program = """
import importlib.abc, sys
class NoSdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'copilot' or fullname.startswith('copilot.'):
            raise AssertionError('SDK is not an API dependency')
sys.meta_path.insert(0, NoSdk())
sys.path.insert(0, sys.argv[1])
from shared_context.behavior_skill_copy import TeamSopCopy, SopCopyReceipt
from shared_context.behavior_skill_prompt import assemble_prompt
from web_test_console.freeform import ConversationTurn
assert assemble_prompt('fa-instruction-sop-copy-v1').record.schema_version == 'fa-sop-copy-prompt-assembly-v1'
assert 'prompt_assembly' in ConversationTurn.model_fields
assert not any(name == 'copilot' or name.startswith('copilot.') for name in sys.modules)
print('COPY_API_SDK_INDEPENDENT')
"""
    result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", program, str(source)], capture_output=True,
                            text=True, encoding="utf-8", timeout=30,
                            env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "COPY_API_SDK_INDEPENDENT"


@pytest.mark.parametrize("enabled", [False, True])
def test_production_provider_forwards_only_server_selected_copy_capability(enabled):
    import asyncio
    from types import SimpleNamespace
    import httpx
    from shared_context.behavior_assistant_server import _CurrentAssistantProvider
    from shared_context.errors import Problem
    sent, gates = [], []
    body = {"model": "gpt-4.1-mini", "stream": True, "messages": [{"role": "user", "content": "Original instruction."}],
            "tools": [{"type": "function", "function": {"name": "copy_sop_to_skill_draft",
                "description": "Server-selected copy only", "parameters": {"type": "object", "properties": {}}}}]}
    raw = b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    def respond(request):
        sent.append(True)
        return httpx.Response(200, content=raw, headers={"content-type": "text/event-stream"})
    provider = _CurrentAssistantProvider(
        SimpleNamespace(config=SimpleNamespace(assistant=SimpleNamespace(
            provider_endpoint="https://provider.invalid/openai/v1/chat/completions"))),
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_COPY_CAPABILITY")),
        transport=httpx.MockTransport(respond))
    async def current():
        gates.append(True)
    async def send():
        try:
            if enabled:
                result = await provider.send(body, text_reply=True, model="gpt-4.1-mini",
                                             sop_copy_access=True, before_send=current)
                assert result.content == raw
            else:
                with pytest.raises(Problem) as denied:
                    await provider.send(body, text_reply=True, model="gpt-4.1-mini", before_send=current)
                assert denied.value.body["error"]["code"] == "ASSISTANT_TOOLS_DENIED"
        finally:
            await provider.aclose()
    asyncio.run(send())
    assert sent == gates == ([True] if enabled else [])
