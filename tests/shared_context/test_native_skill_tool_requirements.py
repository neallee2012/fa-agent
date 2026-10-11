"""Declared tools never grant capability; only captured and bound native tools are usable."""

import json

import httpx
import pytest

from shared_context.behavior_capabilities import available_grants
from shared_context.behavior_native_capture import project_capture
from shared_context.m3_models import SkillToolRequirements
from test_native_capability_integration import (
    integrated, local_env, synthetic_standing_v1, instruction_models, runtime_path, signing_key, jwk_client, peer_runtime,
    tool_result,
)
from test_native_skill_resources import resource_capture, execute, NAME, RESOURCES
from test_skill_tool_requirements import requirements
from test_instruction_responses_native import response_bytes


def capture(h, *, delegate):
    tools = SkillToolRequirements.model_validate(requirements())
    captured, reference = resource_capture(h, skill_tools=tools)
    if delegate:
        grants = available_grants(None, ("fa-skills-v1", RESOURCES, "fa-delegate-v1"))
        refs = tuple(grants[name].ref for name in sorted(grants))
        template = captured.template.model_copy(update={
            "settings": captured.template.settings.model_copy(update={"capabilities": refs})})
        captured = project_capture(template, "explicit-delegate", captured.model, head_refs=refs,
                                   head_selection_id="explicit-delegate", available=grants, count_tokens=lambda _: 0)
        h.case.h.runtime.capability_grants = grants
    return captured, reference


@pytest.mark.parametrize("integrated", [None, "responses"], indirect=True, ids=["invocations", "responses"])
@pytest.mark.parametrize("delegate", [False, True], ids=["not-captured", "captured-and-bound"])
def test_manifest_declares_but_does_not_enable_the_existing_task_tool(integrated, delegate):
    h = integrated
    captured, reference = capture(h, delegate=delegate)
    if not delegate:
        def claimed_availability(request, response):
            if request.url.path.endswith("/read-skill") and response.status_code == 200:
                value = response.json()
                value["tool_availability"] = [{
                    **requirements()["entries"][0], "available": True, "reason": None, "authorization": False}]
                return httpx.Response(200, json=value)
            return response
        h.api_response[0] = claimed_availability
    calls = []

    def model(request):
        body = json.loads(request.content)
        calls.append(body)
        count = len(calls)
        assert count <= (7 if delegate else 3)
        if count in {6, 7}:
            assert "task" not in {tool["name"] for tool in body["tools"]}
            if count == 7:
                read = tool_result(body, "call_6")
                assert read["tool_availability"][0]["available"] is False
                assert read["tool_availability"][0]["reason"] == "not_captured"
                assert read["skill"]["metadata_sha256"] == captured.template.skills[0].metadata_sha256
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=response_bytes(
                count, tool="read_skill" if count == 6 else None, arguments={"name": NAME},
                text="The prior child is historical; task is no longer captured." if count == 7 else None))
        if count == 2:
            read = tool_result(body, "call_1")
            assert read["schema_version"] == "fa-approved-skill-read-v4"
            assert read["tool_requirements"] == requirements()
            assert read["tool_availability"] == [{
                **requirements()["entries"][0], "available": delegate,
                "reason": None if delegate else "not_captured", "authorization": False,
            }]
            assert ("task" in {tool["name"] for tool in body["tools"]}) is delegate
        if count == 3:
            assert tool_result(body, "call_2")["text"] == '{"value":42}\n'
        if delegate and count == 4:
            assert not body.get("tools")
        if delegate and count == 5:
            child = tool_result(body, "call_3")
            assert child["status"] == "completed" and child["result"] == "Child confirms value 42."
        tool = "read_skill" if count == 1 else "read_skill_resource" if count == 2 else (
            "task" if delegate and count == 3 else None)
        arguments = ({"agent_type": "fa-delegate-v1", "mode": "sync", "name": "declared-check",
                      "description": "Summarize the supplied approved data.",
                      "prompt": "The approved reference value is 42. Return it without using tools."}
                     if tool == "task" else
                     {"name": NAME, **({"resource": reference.name} if count == 2 else {})})
        text = ("Child confirms value 42." if delegate and count == 4 else
                "The approved data is 42; task is unavailable." if not delegate and count == 3 else
                "The parent integrated the original child: 42." if delegate and count == 5 else None)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=response_bytes(
            count, tool=tool, arguments=arguments, text=text))

    h.runtime.executor.model_transport = httpx.MockTransport(model)
    saved = execute(h, captured, "tool-requirements", responses=h.runtime.responses_enabled)
    assert saved.capture == captured
    assert saved.usage.usage[0].requests == len(calls) == (5 if delegate else 3)
    if delegate:
        grants = available_grants(None, ("fa-skills-v1", RESOURCES, "fa-delegate-v1"))
        narrowed = project_capture(captured.template, "explicit-delegate", captured.model,
            head_refs=tuple(grants[name].ref for name in sorted(("fa-skills-v1", RESOURCES))),
            head_selection_id="delegate-removed", available=grants, count_tokens=lambda _: 0)
        followed = execute(h, narrowed, "requirements-followup", responses=h.runtime.responses_enabled,
                           cid=saved.conversation_id)
        assert followed.turn_number == 2 and followed.usage.usage[0].requests == 2
        assert h.call(h.runtime.read, saved.request_id) == saved and len(calls) == 7


def test_a_captured_requirement_without_a_bound_consumer_never_starts_the_sdk(integrated):
    from shared_context.behavior_assistant_wire import InstructionSubmit
    from shared_context.behavior_native_turn_wire import NativeSubmit

    h = integrated
    captured, _ = capture(h, delegate=True)
    grant = available_grants(None, ("fa-delegate-v1",))["fa-delegate-v1"]
    h.runtime.executor.capability_providers = tuple(
        provider for provider in h.runtime.executor.capability_providers if not provider.accepts(grant))
    refused = h.invoke(NativeSubmit(command=InstructionSubmit(request_id="not-bound", content="Use the guide."),
                                   capture=captured))
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "NATIVE_PROFILE_UNSUPPORTED"
    assert not h.sdk_calls and not h.bindings
