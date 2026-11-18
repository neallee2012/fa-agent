"""Real API/task/Hosted/save path with pinned SDK I/O doubles; not model quality."""
import hashlib
import json
from types import MappingProxyType, SimpleNamespace

import pytest

from shared_context.behavior_assistant import AssistantProfile, key
from shared_context.behavior_assistant_wire import InstructionTask, SdkStop
from shared_context.behavior_instruction import TurnArchive, turn_material
from shared_context.behavior_instruction_prompt import (
    INSTRUCTION_CAPABILITIES, PromptAssemblyRecord, assemble_instruction_prompt,
)
from shared_context.behavior_wire import ExactSourceRef
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from shared_context.source_materials import digest
from assistant_api_helpers import assistant_api
from instruction_flow_helpers import instruction_inventory, instruction_models, sdk_io
from test_m3_auth import signing_key, jwk_client
from work_policy_lifecycle_helpers import enabled
from instruction_prompt_helpers import configured, saved_turn


def test_default_serialization_and_system_prefix_match_fixed_parent_bytes():
    source = ExactSourceRef(source_id="source", version=1, sha256="a"*64, byte_length=5)
    profile = AssistantProfile(binding_id="binding", source=source, claim_id="claim", legacy_source_work="work",
        artifact_id="artifact", artifact_version="a"*32, policy_version="assistant-own-result-v1")
    task = InstructionTask(content="  Original instruction.\r\n", conversation_id="conversation", turn_number=1)
    assembly = assemble_instruction_prompt(None)
    for raw, length, sha in (
        (profile.model_dump_json().encode(), 378, "04f80826a866fd367c2052423418d09e0d137646c51b43e34474777f02077379"),
        (task.model_dump_json().encode(), 153, "eeef164803a5e550ee432e346422ad3af586f7165903484de5ba2ff851b90d36"),
        (assembly.system_message.encode(), 552, "f2d312f2cab688a03d035adb971ead55e4fd5057c69bdc0eede97c5cb084799f"),
    ):
        assert len(raw) == length and hashlib.sha256(raw).hexdigest() == sha
    assert assembly.record is None
    original = json.dumps({
        "kind": "assistant_sdk_turn_archive", "conversation_id": "conversation", "job_id": "job", "turn_number": 1,
        "archive": {"error": "SDK_ARCHIVE_UNAVAILABLE"},
        "sdk_stop": SdkStop(pid=7, exit_code=0, graceful=True).model_dump(mode="json"),
        "context_read": False, "failure": None,
    }, separators=(",", ":"))
    assert TurnArchive.model_validate_json(original).model_dump_json() == original


def test_assembly_has_canonical_sections_tools_and_versioned_hashes():
    full = assemble_instruction_prompt("fa-instruction-v1")
    reversed_tools = assemble_instruction_prompt("fa-instruction-v1", capabilities=tuple(reversed(INSTRUCTION_CAPABILITIES)))
    read_only = assemble_instruction_prompt("fa-instruction-v1", capabilities=("read_context",))
    no_tools = assemble_instruction_prompt("fa-instruction-v1", capabilities=())
    assert full == reversed_tools
    assert len({hashlib.sha256(value.system_message.encode()).hexdigest()
                for value in (assemble_instruction_prompt(None), full, read_only, no_tools)}) == 4
    assert full.record.declared_capabilities == INSTRUCTION_CAPABILITIES
    assert full.record.prefix_utf8_bytes == len(full.system_message.encode("utf-8"))
    assert full.record.prefix_sha256 == hashlib.sha256(full.system_message.encode("utf-8")).hexdigest()
    assert full.record.prefix_utf8_bytes == 2094
    assert full.record.prefix_sha256 == "8e24b5291f5a46d3795e9c9b56c8e4dcde3af705deb23eaa08899ce0613963cb"
    assert full.record.prefix_tokens is None
    assert "execute_operation:" not in read_only.system_message and "No tools configured." in no_tools.system_message
    headings = ("## Role", "## Authority and receipts", "## Configured capabilities", "## Instruction scope",
                "## Privacy", "## Conversation and output")
    assert [full.system_message.index(h) for h in headings] == sorted(full.system_message.index(h) for h in headings)


@pytest.mark.parametrize("profile,capabilities", [
    ("unknown", INSTRUCTION_CAPABILITIES), ("", INSTRUCTION_CAPABILITIES),
    ("fa-instruction-v1", ("read_context", "shell")),
    ("fa-instruction-v1", ("read_context", "read_context")), (None, ("read_context",)),
])
def test_unknown_profiles_and_false_capabilities_fail_explicitly(profile, capabilities):
    with pytest.raises(ValueError):
        assemble_instruction_prompt(profile, capabilities=capabilities)


@pytest.mark.parametrize("field,value", [
    ("profile", "future"), ("source", "client-attested"), ("prefix_sha256", "f"*64),
    ("prefix_utf8_bytes", 1), ("prefix_tokens", 0), ("sdk_version", "future"),
    ("runtime_version", "future"), ("omissions", ["PRIVATE_REASONING_CANARY"]),
    ("declared_capabilities", ["browser"]), ("system_message", "PRIVATE_SYSTEM_CANARY"),
])
@pytest.mark.parametrize("profile", ["fa-instruction-v1", "fa-instruction-v2", "fa-instruction-v3", "fa-instruction-v4"])
def test_safe_record_rejects_invented_source_counts_capabilities_or_private_fields(field, value, profile):
    record = assemble_instruction_prompt(profile).record.model_dump(mode="json")
    record[field] = value
    with pytest.raises(ValueError):
        PromptAssemblyRecord.model_validate_json(json.dumps(record))


def test_server_selector_changes_profile_digest_and_unknown_configuration_is_rejected(assistant_api):
    from shared_context.behavior_server_config import ServerConfiguration
    from test_behavior_assistant_server import assistant_configuration
    original = next(iter(assistant_api.runtime.profiles.values()))
    selected = AssistantProfile.model_validate(original.model_copy(update={"prompt_profile": "fa-instruction-v1"}).model_dump())
    assert digest(original.model_dump(mode="json")) != digest(selected.model_dump(mode="json"))
    with pytest.raises(ValueError):
        AssistantProfile.model_validate(original.model_copy(update={"prompt_profile": "future"}).model_dump())
    with pytest.raises(ValueError):
        InstructionTask(content="Test", conversation_id="chat", turn_number=1, prompt_profile="future")
    raw = assistant_configuration()
    before = ServerConfiguration.model_validate_json(json.dumps(raw))
    raw["assistant"]["profiles"][0]["prompt_profile"] = "fa-instruction-v1"
    after = ServerConfiguration.model_validate_json(json.dumps(raw))
    assert before.sha256 != after.sha256
    assert before.resource_bindings()["assistant_sha256"] != after.resource_bindings()["assistant_sha256"]

