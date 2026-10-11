"""fa-instruction-agent-role-v4 = agent-role v3 plus the same five passages as fa-instruction-v4.

Agents added with "+" get the same purchase, fact-source, current-tool, ask-or-assume and ask_user guidance as A and B.
Offline text and wiring only; whether the real model follows it is checked with the stage script (README "SP v4").
"""

import hashlib
import json
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from shared_context import native_sdk
from shared_context.behavior_agent_role_prompt import (
    AGENT_ROLE_V2_PROFILE, AGENT_ROLE_V3_PROFILE, AGENT_ROLE_V4_PROFILE, ROLE_SECTION, ROLE_V2_CAPABILITIES,
    ROLE_V3_PROMPT, ROLE_V4_PROMPT, RoleV3PromptAssemblyRecord, RoleV4PromptAssemblyRecord, RolePromptCapture,
    assemble_role_v4_prompt, with_role_text,
)
from shared_context.behavior_hosted import create_host
from shared_context.behavior_instruction_prompt import (
    V3_FORMS, V3_PURCHASES, V4_CLARIFY, V4_CURRENT_TOOLS, V4_FACT_SOURCES, V4_FORMS, V4_PURCHASES,
)
from shared_context.behavior_native_turn_wire import NativeSubmit, NativeTurnCapture
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.native_local_capabilities import local_capability_provider
from shared_context.native_turns import NativeTurnRuntime
from test_native_local_capabilities import MODEL_ROOT, Handlers, browser_grant, with_grants
from test_native_turn_wire import submit as native_submit
from test_native_turns import Harness, IDENTITY, local_env  # noqa: F401

V2_SHA256 = "a2ebe72807770127e06e2cd7f3b7b600cb9377912740cfdbeb8114b916076334"
V3_SHA256 = "e28722ad651d1f539cf6eba326700976e97f14648e89a69471fa58b8ee00ccdd"
HANDOVER = "Stop and hand over to the human at any login, submit, payment, permission or security prompt."


def sections(text):
    head, *parts = text.split("\n\n## ")
    return head, {part.split("\n", 1)[0]: part for part in parts}


def test_v4_is_v3_word_for_word_except_the_five_fa_instruction_v4_passages():
    # The published v2 and v3 are unchanged; v4 is only a new version next to them.
    assert hashlib.sha256(assemble_prompt(AGENT_ROLE_V2_PROFILE).system_message.encode()).hexdigest() == V2_SHA256
    assert hashlib.sha256(ROLE_V3_PROMPT.encode()).hexdigest() == V3_SHA256
    v3_head, v3 = sections(ROLE_V3_PROMPT)
    v4_head, v4 = sections(ROLE_V4_PROMPT)
    assert (v3_head, v4_head) == ("FA instruction profile: fa-instruction-agent-role-v3.",
                                  "FA instruction profile: fa-instruction-agent-role-v4.")
    assert list(v4) == list(v3)
    controls = "Do not invent tools, connectors, skills or UI controls. "
    expected = {
        "Configured capabilities": v3["Configured capabilities"].replace(controls, controls + V4_CURRENT_TOOLS + " "),
        "Facts and evidence": v3["Facts and evidence"] + " " + V4_FACT_SOURCES,
        "Questions": v3["Questions"] + " " + V4_CLARIFY,
        "Purchases": v3["Purchases"].replace(V3_PURCHASES, V4_PURCHASES),
        "Unavailable capabilities": v3["Unavailable capabilities"].replace(V3_FORMS, V4_FORMS),
    }
    assert v3["Configured capabilities"].count(controls) == 1 and v3["Questions"].endswith("Silence is not consent.")
    assert v3["Purchases"] == "Purchases\n" + V3_PURCHASES and v3["Unavailable capabilities"].count(V3_FORMS) == 1
    assert {title for title in v3 if v3[title] != v4[title]} == set(expected)
    for title, value in expected.items():
        assert v4[title] == value, title
    # The same five passages as the fa-instruction-v4 base that A and B use.
    base = assemble_prompt("fa-instruction-v4").system_message
    for passage in (V4_PURCHASES, V4_FACT_SOURCES, V4_CURRENT_TOOLS, V4_CLARIFY, V4_FORMS):
        assert passage in ROLE_V4_PROMPT and passage in base
    # The v3 device rule still governs "on my computer"; the ask-or-assume example is the assistant's own computer.
    assert "never quietly do it on another computer" in v4["Where you work"]
    assert len(ROLE_V4_PROMPT.encode()) == 13203 < 16 * 1024
    assert hashlib.sha256(ROLE_V4_PROMPT.encode()).hexdigest() == (
        "ebfb5d144b90b12d1e5fea25483509b17afebf0e693b91b414342b296f3d43ae")


def test_v4_has_its_own_fixed_record_and_the_same_readers_and_role_placement():
    prompt = assemble_prompt(AGENT_ROLE_V4_PROFILE)
    raw = prompt.system_message.encode()
    assert prompt.system_message == ROLE_V4_PROMPT == assemble_role_v4_prompt().system_message
    record = prompt.record
    assert isinstance(record, RoleV4PromptAssemblyRecord)
    assert (record.profile, record.prefix_sha256, record.prefix_utf8_bytes) == (
        AGENT_ROLE_V4_PROFILE, hashlib.sha256(raw).hexdigest(), len(raw))
    assert record.declared_capabilities == ROLE_V2_CAPABILITIES
    assert record.capability_catalog_version == assemble_prompt(AGENT_ROLE_V3_PROFILE).record.capability_catalog_version
    # A record names exactly one fixed text: neither version's digest validates as the other.
    with pytest.raises(ValueError):
        RoleV4PromptAssemblyRecord(prefix_sha256=V3_SHA256, prefix_utf8_bytes=len(ROLE_V3_PROMPT.encode()))
    with pytest.raises(ValueError):
        RoleV3PromptAssemblyRecord(prefix_sha256=record.prefix_sha256, prefix_utf8_bytes=record.prefix_utf8_bytes)
    with pytest.raises(ValueError):
        assemble_role_v4_prompt(capabilities=("read_context", "execute_operation"))
    text = "ROLE-ALPHA keeps answers short."
    role = RolePromptCapture(version=1, sha256=hashlib.sha256(text.encode()).hexdigest(), text=text)
    later = ROLE_V4_PROMPT + "\n\n## Native turn origin\nOrigin text."
    assert with_role_text(later, role) == later + ROLE_SECTION + "\n\n" + text


def test_new_agents_are_onboarded_on_v4_and_older_versions_stay_selectable():
    from shared_context import behavior_agent_onboarding
    from shared_context.behavior_capabilities import CAPABILITY_BASE_PROFILES, SKILLIFY_ENTRY_ID, base_supports
    from shared_context.behavior_profile_selection import SELECTABLE_PROFILES

    assert behavior_agent_onboarding.NEW_AGENT_PROFILE == AGENT_ROLE_V4_PROFILE
    assert {AGENT_ROLE_V2_PROFILE, AGENT_ROLE_V3_PROFILE, AGENT_ROLE_V4_PROFILE} <= SELECTABLE_PROFILES
    assert AGENT_ROLE_V4_PROFILE in CAPABILITY_BASE_PROFILES and base_supports(SKILLIFY_ENTRY_ID, AGENT_ROLE_V4_PROFILE)


def test_native_turn_sends_role_v4_then_the_handover_and_the_role_text_last(local_env, monkeypatch, tmp_path):
    data = with_grants("fa-desktop-v1", "fa-shell-v1", browser=browser_grant()).model_dump(mode="json")
    record = assemble_prompt(AGENT_ROLE_V4_PROFILE).record
    data["template"]["role"] = {"profile": record.profile, "prefix_sha256": record.prefix_sha256,
                                "capability_catalog_version": record.capability_catalog_version}
    data["template"]["settings"]["user_text_file_profile"] = "fa-user-text-files-v1"
    text = "You book trains for the team."
    data["role_prompt"] = {"version": 1, "sha256": hashlib.sha256(text.encode()).hexdigest(), "text": text}
    selected = NativeTurnCapture.model_validate_json(json.dumps(data))
    seen = []

    async def sdk(**options):
        seen.append(options["system_message"])
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": options["sdk_session_id"]}}) + "\n",
            "workspace.yaml": "id: " + options["sdk_session_id"] + "\n",
        })
        return {"final_message": "done", "sdk_stop": {"pid": 1, "exit_code": 0, "graceful": True,
                                                      "runtime_version": "1.0.83"}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native-turns", runtime_path=tmp_path / "OFFLINE-not-executed",
        api_url="https://api.offline.invalid", model_url=MODEL_ROOT, token_provider=lambda: "OFFLINE-token",
        capability_providers=(local_capability_provider(Handlers().handlers),),
    )
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        h = Harness(runtime, SimpleNamespace(controls={}), client)
        assert h.invoke(NativeSubmit(command=native_submit().command, capture=selected)).status_code == 202
        h.wait("rid-1", "completed")
    [message] = seen
    assert message.startswith(ROLE_V4_PROMPT) and message.endswith(ROLE_SECTION + "\n\n" + text)
    assert message.index(HANDOVER) < message.index(ROLE_SECTION)
    assert "## Purchases\n" + V4_PURCHASES in message and V3_PURCHASES not in message
