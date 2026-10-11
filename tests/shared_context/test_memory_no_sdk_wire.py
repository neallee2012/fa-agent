"""Version selection and legacy bytes; DTO validation never substitutes for gate observation."""

import copy
import json

import pytest

from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_assistant_wire import InstructionTask
from shared_context.behavior_wire import SdkNoStart
from shared_context.behavior_server_config import ServerConfiguration
from test_behavior_assistant_server import assistant_configuration


def literal_disposition(version):
    return {
        "schema_version": "fa-sdk-no-start-" + version,
        "gate": {
            "schema_version": "fa-sdk-start-gate-record-" + version,
            "binding": {
                "schema_version": "fa-sdk-start-binding-" + version, "profile": "fa-sdk-start-gate-" + version,
                "session_id": "1" * 64, "sdk_session_id": "original-sdk", "job_id": "original-job",
                "task_digest": "2" * 64, "profile_digest": "3" * 64, "execution_digest": "4" * 64,
                "worker_configuration_digest": "5" * 64, "code_sha256": "6" * 64,
            },
            "owner": {"kind": "local-offline", "supervisor_id": "original-owner",
                      "launch_id": None, "claim_id": None, "process_generation": None},
            "state": "closed_before_entry", "refusal_code": "SKILL_SOURCE_CHANGED" if version == "v1" else "SOURCE_WITHDRAWN",
        },
        "owned_thread_completed": True,
    }


def test_explicit_gate_versions_roundtrip_without_changing_v1_or_none_bytes():
    for version in ("v1", "v2"):
        raw = json.dumps(literal_disposition(version), separators=(",", ":"))
        assert SdkNoStart.model_validate_json(raw).model_dump_json() == raw
    old = InstructionTask(content="  Original task.\r\n", conversation_id="original", turn_number=1).model_dump_json()
    assert InstructionTask.model_validate_json(old).model_dump_json() == old
    assert "sdk_start_profile" not in old


@pytest.mark.parametrize("part", ["binding", "record", "disposition", "profile", "code", "future", "missing", "extra"])
def test_v1_disposition_cannot_be_upgraded_by_mixing_v2_material(part):
    value = literal_disposition("v1")
    if part == "binding":
        value["gate"]["binding"]["schema_version"] = "fa-sdk-start-binding-v2"
    elif part == "record":
        value["gate"]["schema_version"] = "fa-sdk-start-gate-record-v2"
    elif part == "disposition":
        value["schema_version"] = "fa-sdk-no-start-v2"
    elif part == "profile":
        value["gate"]["binding"]["profile"] = "fa-sdk-start-gate-v2"
    elif part == "code":
        value["gate"]["refusal_code"] = "SOURCE_WITHDRAWN"
    elif part == "future":
        value["schema_version"] = "fa-sdk-no-start-v3"
    elif part == "missing":
        value["gate"].pop("schema_version")
    else:
        value["memory_authorized"] = True
    with pytest.raises(ValueError):
        SdkNoStart.model_validate_json(json.dumps(value))


def test_v2_is_server_owned_memory_only_and_changes_original_configuration_digest():
    raw = assistant_configuration()
    before = ServerConfiguration.model_validate_json(json.dumps(raw))
    raw["assistant"]["profiles"][0].update(
        prompt_profile="fa-instruction-work-memory-v1", sdk_start_profile="fa-sdk-start-gate-v2",
    )
    current = ServerConfiguration.model_validate_json(json.dumps(raw))
    assert current.sha256 != before.sha256
    assert current.resource_bindings()["assistant_sha256"] != before.resource_bindings()["assistant_sha256"]
    assert current.identity == before.identity and current.table == before.table
    profile = raw["assistant"]["profiles"][0]
    for unsupported in (None, "fa-instruction-v1", "fa-instruction-skills-v1", "fa-instruction-skill-purpose-v1"):
        value = {**profile, "prompt_profile": unsupported}
        with pytest.raises(ValueError):
            AssistantProfile.model_validate_json(json.dumps(value))
        with pytest.raises(ValueError):
            InstructionTask(content="task", conversation_id="conversation", turn_number=1,
                            sdk_start_profile="fa-sdk-start-gate-v2", prompt_profile=unsupported)
    future = copy.deepcopy(raw)
    future["assistant"]["profiles"][0]["sdk_start_profile"] = "fa-sdk-start-gate-future"
    with pytest.raises(ValueError):
        ServerConfiguration.model_validate_json(json.dumps(future))
