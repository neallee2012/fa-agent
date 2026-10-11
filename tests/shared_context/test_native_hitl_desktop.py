"""HITL around the real desktop handler: a desktop_act refused before any input keeps its own code and guidance.

2026-10-05 dev (A v109): a new turn's desktop_act named the previous turn's observation; the desktop refused it
before input (DESKTOP_OBSERVATION_STALE), but HITL reported NATIVE_HITL_EFFECT_UNKNOWN, so the model stopped
instead of observing again. The approval API, JWT, control CAS, NativeEffects and desktop toolbox are real;
the X display, model reviewer and StateStore I/O are scripted.
"""

import asyncio
import json

import pytest

from shared_context import behavior_desktop_capability as capability
from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_desktop import OwnedDesktop
from shared_context.behavior_desktop_tools import DESKTOP_TOOLS, DesktopObservation, DesktopToolbox
from shared_context.m2_copilot import ToolFailure
from shared_context.native_capabilities import guard_native_tool
from test_behavior_desktop import FakeRunner, private_root
from test_m3_auth import jwk_client, signing_key
from test_native_hitl import hitl
from test_native_hitl_h2 import human_decide, pending_view, permit, setup_gate, verdict


def desktop_gate(hitl, tmp_path, answers):
    case = setup_gate(hitl, tmp_path, answers, entries=("fa-desktop-v1",))
    runner = FakeRunner()
    toolbox = DesktopToolbox(private_root(tmp_path, "desktop"),
                             desktop_factory=lambda root: OwnedDesktop(root, runner=runner), hardened=lambda: True)
    handlers = {tool.name: guard_native_tool(case.context, capability._spec(toolbox, tool)).handler
                for tool in DESKTOP_TOOLS}
    return case, runner, toolbox, handlers


def act(case, observation_id, call_id="call-one"):
    return {**case.packet, "tool_call_id": call_id, "tool_name": "desktop_act",
            "arguments": {"observation_id": observation_id, "action": {"type": "move", "x": 10, "y": 20}}}


def observe(toolbox):
    return json.loads(toolbox.call("desktop_observe", {}).text)


async def allowed(case, packet, ask):
    if not ask:
        assert await permit(case, packet) == (True, None)
        return None
    permission = asyncio.create_task(permit(case, packet))
    pending = await pending_view(case)
    human_decide(case, pending)
    assert await permission == (True, None)
    return pending


def model_feedback(code):
    boundary = AssistantModelBoundary(None, text_reply=True, capability_results={"desktop_act": DesktopObservation})
    return boundary.public_tool_failure("desktop_act", code)


@pytest.mark.parametrize("ask", [False, True])
@pytest.mark.parametrize("refusal", ["stale_after_new_turn", "halted", "invalid_arguments"])
def test_act_refused_before_input_reaches_the_model_with_desktop_guidance(hitl, tmp_path, ask, refusal):
    answers = (verdict("ask" if ask else "allow"), verdict("allow"))
    case, runner, toolbox, handlers = desktop_gate(hitl, tmp_path, answers)
    try:
        observation = observe(toolbox)
        if refusal == "stale_after_new_turn":
            toolbox.renew()
            code, hint = "DESKTOP_OBSERVATION_STALE", "Call desktop_observe"
        elif refusal == "halted":
            runner.add_window("Untrusted popup", None)
            observation = observe(toolbox)
            code, hint = "DESKTOP_HALTED", "Stop desktop work"
        else:
            code, hint = "DESKTOP_ACTION", "published schema"
        packet = act(case, observation["observation_id"])
        if refusal == "invalid_arguments":
            packet["arguments"]["action"]["x"] = 5000

        async def check():
            pending = await allowed(case, packet, ask)
            with pytest.raises(ToolFailure) as refused:
                await handlers["desktop_act"](packet)
            assert refused.value.public_code == code
            feedback = model_feedback(refused.value.public_code)
            assert hint in feedback and "NATIVE_HITL_EFFECT_UNKNOWN" not in feedback
            assert runner.inputs == []
            # The same call is never executed again; it stays an unfinished claim, not a success.
            with pytest.raises(ToolFailure):
                await handlers["desktop_act"](packet)
            assert case.gate.calls["call-one"].claim.record.status == "pending"
            if pending is not None:
                assert (await case.client.read(pending.approval.pending_id)).status == "interrupted"
            if refusal == "stale_after_new_turn":
                # The model's next step: observe again, then one new call on the fresh screenshot runs once.
                later = act(case, observe(toolbox)["observation_id"], "call-two")
                assert await permit(case, later) == (True, None)
                assert json.loads((await handlers["desktop_act"](later)).text)["halted"] is None
                assert runner.inputs == [["xdotool", "mousemove", "10", "20"]]
            await case.gate.close()
            if pending is not None:
                assert (await case.client.read(pending.approval.pending_id)).status == "interrupted"

        asyncio.run(check())
    finally:
        toolbox.close()


@pytest.mark.parametrize("ask", [False, True])
def test_input_that_failed_after_it_started_stays_unknown(hitl, tmp_path, ask):
    case, runner, toolbox, handlers = desktop_gate(hitl, tmp_path, (verdict("ask" if ask else "allow"),))
    try:
        packet = act(case, observe(toolbox)["observation_id"])
        runner.fail_input = True

        async def check():
            pending = await allowed(case, packet, ask)
            with pytest.raises(ToolFailure) as unknown:
                await handlers["desktop_act"](packet)
            assert unknown.value.public_code == "NATIVE_HITL_EFFECT_UNKNOWN"
            assert runner.inputs == [["xdotool", "mousemove", "10", "20"]]
            if pending is not None:
                assert (await case.client.read(pending.approval.pending_id)).status == "effect_unknown"
            await case.gate.close()

        asyncio.run(check())
    finally:
        toolbox.close()


def test_the_same_code_from_another_desktop_tool_does_not_claim_nothing_happened(hitl, tmp_path):
    case = setup_gate(hitl, tmp_path, (verdict("allow"),), entries=("fa-desktop-v1",))
    case.packet.update(tool_name="desktop_open_file", arguments={"name": "notes.txt", "create": True})

    async def handler(_packet):
        raise ToolFailure("not a desktop_act refusal", public_code="DESKTOP_OBSERVATION_STALE")

    async def check():
        assert await permit(case) == (True, None)
        with pytest.raises(ToolFailure) as unknown:
            await case.gate.invoke(case.packet, handler)
        assert unknown.value.public_code == "NATIVE_HITL_EFFECT_UNKNOWN"
        await case.gate.close()

    asyncio.run(check())
