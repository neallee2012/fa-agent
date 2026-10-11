"""fa-instruction-v4: v3 word for word except five reviewed passages; v3 stays pinned.

The passages: the purchase rule (prepare up to the final step, then hand over), facts only from tool results,
capabilities from this turn only, ask-or-assume with lasting answers kept through memory, and ask_user as the one
in-chat question form. These tests prove text and wiring only, not that a real model follows the guidance.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from shared_context import native_sdk
from shared_context.behavior_capabilities import BUILTIN_CAPABILITIES, CAPABILITY_BASE_PROFILES, base_supports
from shared_context.behavior_hosted import create_host
from shared_context.behavior_instruction_prompt import (
    PromptAssemblyRecord, V3_FORMS, V3_PURCHASES, V4_CLARIFY, V4_CURRENT_TOOLS, V4_FACT_SOURCES, V4_FORMS,
    V4_PURCHASES, assemble_instruction_prompt,
)
from shared_context.behavior_native_turn_wire import NativeSubmit, NativeTurnCapture
from shared_context.behavior_profile_selection import SELECTABLE_PROFILES
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.native_local_capabilities import local_capability_provider
from shared_context.native_turns import NativeTurnRuntime
from test_behavior_assistant import assistant  # noqa: F401
from test_conversation_profile_selection import configured, select, template
from test_instruction_flow import check_history, instruction_models, runtime_path  # noqa: F401
from test_instruction_prompt import saved_turn, sdk_options, submit
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_local_capabilities import MODEL_ROOT, Handlers, browser_grant, with_grants
from test_native_turn_wire import submit as native_submit
from test_native_turns import Harness, IDENTITY, local_env  # noqa: F401

PROFILE = "fa-instruction-v4"
V3 = "fa-instruction-v3"
ROOT = Path(__file__).resolve().parents[2]
HANDOVER = "Stop and hand over to the human at any login, submit, payment, permission or security prompt."


def test_v3_text_and_digest_stay_pinned():
    v3 = assemble_instruction_prompt(V3)
    assert v3.record.prefix_utf8_bytes == 7149
    assert v3.record.prefix_sha256 == "eac73c98235499a0caa2784f7e038468a02176aa3312849552e634616c8950a0"
    assert "## Purchases\n" + V3_PURCHASES in v3.system_message and V3_FORMS in v3.system_message
    for added in (V4_PURCHASES, V4_FACT_SOURCES, V4_CURRENT_TOOLS, V4_CLARIFY, V4_FORMS):
        assert added not in v3.system_message


def test_v4_is_v3_except_the_five_reviewed_passages():
    old_sections = assemble_instruction_prompt(V3).system_message.split("\n\n")
    updated = assemble_instruction_prompt(PROFILE)
    assert updated == assemble_prompt(PROFILE)
    new_sections = updated.system_message.split("\n\n")
    assert new_sections[0] == "FA instruction profile: fa-instruction-v4."
    controls = "Do not invent tools, connectors, skills or UI controls."
    for before, after in zip(old_sections[1:], new_sections[1:], strict=True):
        title = before.split("\n", 1)[0]
        if title == "## Configured capabilities":
            assert before.count(controls + "\n") == 1
            assert after == before.replace(controls + "\n", controls + " " + V4_CURRENT_TOOLS + "\n")
        elif title == "## Facts and evidence":
            assert after == before + " " + V4_FACT_SOURCES
        elif title == "## Questions":
            assert before.endswith("Silence is not consent.") and after == before + " " + V4_CLARIFY
        elif title == "## Purchases":
            assert (before, after) == ("## Purchases\n" + V3_PURCHASES, "## Purchases\n" + V4_PURCHASES)
        elif title == "## Unavailable capabilities":
            assert before.count(V3_FORMS) == 1 and after == before.replace(V3_FORMS, V4_FORMS)
        else:
            assert after == before
    assert updated.record.profile == PROFILE
    assert updated.record.prefix_utf8_bytes == 9730
    assert updated.record.prefix_sha256 == "059f0960dc12a61ea0c3cf38f06666fc4789b0ef92ac66dfdc17085e1d0a5ccf"
    assert updated.record.omissions == assemble_instruction_prompt(V3).record.omissions
    assert updated.record.declared_capabilities == ("read_context", "execute_operation")
    # The intent of each passage, as reviewed.
    for phrase in ("preparing an order are not purchases", "do this preparation yourself",
                   "the desktop tool (fa-desktop-v1) is offered, so the browser shows on the live desktop",
                   "Without the desktop tool nobody can see or continue a form you fill",
                   "Stop before the final submit, payment, sign-in, verification code",
                   "take over on the live desktop", "never press a final payment button yourself",
                   "If the item or price changes", "do not place a second order"):
        assert phrase in V4_PURCHASES
    for phrase in ("include only items that a tool result in this task actually showed",
                   "cite the page or tool", "never fill gaps from memory or guesses"):
        assert phrase in V4_FACT_SOURCES
    for phrase in ("follows the tools and sections offered in this turn", "a teammate list",
                   "do not claim you can still use it", "say it is unavailable now"):
        assert phrase in V4_CURRENT_TOOLS
    for phrase in ("first settle it from this conversation, recalled memory", "costly or hard to undo",
                   "one only the user knows, such as the stations for a ticket", "the ask_user tool when it is offered",
                   "2 to 4 likely choices and still allow a free answer", "neither refuse nor guess",
                   "act on the most likely reading and state that assumption", "\"the VM\" means your own Hosted computer",
                   "requests passed on by a teammate", "restate it in your final reply as their standing preference",
                   "do not present a one-time answer as lasting"):
        assert phrase in V4_CLARIFY
    assert "except an offered ask_user question" in V4_FORMS
    # No tools at all still carries the sentence, before the catalog.
    empty = assemble_instruction_prompt(PROFILE, capabilities=()).system_message
    assert V4_CURRENT_TOOLS + "\n- No tools configured." in empty


def test_v4_records_validate_and_do_not_cross_with_v3():
    for capabilities in (("read_context", "execute_operation"), ("read_context",), ()):
        record = assemble_instruction_prompt(PROFILE, capabilities=capabilities).record
        assert PromptAssemblyRecord.model_validate_json(record.model_dump_json()) == record
    for source, claimed in ((V3, PROFILE), (PROFILE, V3)):
        data = assemble_instruction_prompt(source).record.model_dump(mode="json")
        data["profile"] = claimed
        with pytest.raises(ValueError):
            PromptAssemblyRecord.model_validate_json(json.dumps(data))


def test_v4_is_selectable_and_composes_like_v3():
    assert PROFILE in SELECTABLE_PROFILES and PROFILE in CAPABILITY_BASE_PROFILES
    for entry_id in BUILTIN_CAPABILITIES:
        assert base_supports(entry_id, PROFILE) is base_supports(entry_id, V3), entry_id


def test_stage_new_conversations_default_to_v4():
    stage = json.loads((ROOT / "deploy" / "stage" / "stage.json").read_text(encoding="utf-8"))
    assert stage["conversation_defaults"]["prompt_profile"] == PROFILE


def test_selected_v4_starts_new_conversations_and_old_v3_conversations_keep_v3(assistant, monkeypatch):
    h, control, _ = configured(assistant)
    chosen, _ = select(h, request_id="choose-v3", value=template(V3))
    assert chosen.status_code == 200, chosen.text
    calls, options = sdk_options(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "v3 answer."}, {"content": "v3 follow-up."}, {"content": "v4 answer."}])
    first, _ = submit(h, "v3-original", "Find the 11:00 to 12:00 trains.")
    assert h.runtime.controller.step(first["binding_id"]).state == "succeeded"
    changed, _ = select(h, request_id="choose-v4", previous=chosen.json()["result"]["selection_id"],
                        value=template(PROFILE))
    assert changed.status_code == 200, changed.text
    assert changed.json()["result"]["existing_conversations_changed"] is False
    second, _ = submit(h, "v3-followup", "Book train 1313, aisle seat.", first["conversation_id"])
    third, _ = submit(h, "v4-new", "Book train 1313, aisle seat.")
    for accepted in (second, third):
        assert h.runtime.controller.step(accepted["binding_id"]).state == "succeeded"
    assert control.records[second["binding_id"]].profile.prompt_profile == V3
    assert control.records[third["binding_id"]].profile.prompt_profile == PROFILE
    assert len(calls) == 3
    for configuration, profile in zip(options, (V3, V3, PROFILE), strict=True):
        assert configuration["system_message"] == {"mode": "replace", "content": assemble_prompt(profile).system_message}
    assert saved_turn(h, h.runtime.bindings[third["binding_id"]].job_id).prompt_assembly == assemble_prompt(PROFILE).record
    check_history(h)


def test_native_turn_sends_v4_before_the_desktop_and_browser_handover(local_env, monkeypatch, tmp_path):
    data = with_grants("fa-desktop-v1", "fa-shell-v1", browser=browser_grant()).model_dump(mode="json")
    role = assemble_prompt(PROFILE).record
    data["template"]["role"] = {"profile": role.profile, "prefix_sha256": role.prefix_sha256,
                                "capability_catalog_version": role.capability_catalog_version}
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
    assert message.startswith(assemble_prompt(PROFILE).system_message)
    assert "## Purchases\n" + V4_PURCHASES in message and V3_PURCHASES not in message
    assert V4_CURRENT_TOOLS in message and V4_FACT_SOURCES in message and V4_CLARIFY in message
    assert HANDOVER in message and "playwright" in message
