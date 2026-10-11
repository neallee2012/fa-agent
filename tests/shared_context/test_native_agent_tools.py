"""The native SDK hands off with its official terminal tool, only after a durable peer receipt."""

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from shared_context.behavior_agent_message_wire import AgentMessageRecipient, CAPABILITY_ID
from shared_context.behavior_capabilities import CapabilityCapture, available_grants
from shared_context.behavior_native_agent_wire import NativePeerDirectory, NativeRelayAcceptance, NativeRelaySend
from shared_context.behavior_native_turn_wire import NativeReceipt, NativeSubmit
from shared_context.native_agent_tools import NOT_SELECTED_REASON
from test_native_agent_relay import sender_case
from test_m2_copilot import runtime_path
from test_native_turns import harness, local_env, native_retry_policy


def collaboration_turn():
    _config, _scope, recipient, _principal, sender = sender_case()
    grant = available_grants(None, (CAPABILITY_ID,))[CAPABILITY_ID]
    selected = sender.capture.model_dump()
    selected["template"]["settings"]["capabilities"] = (grant.ref,)
    selected["capabilities"] = CapabilityCapture(head_selection_id="selected-collaboration", grants=(grant,))
    from shared_context.behavior_native_turn_wire import NativeTurnCapture
    captured = NativeTurnCapture.model_validate(selected)
    request = NativeSubmit(command=sender.command, capture=captured)
    sender = NativeReceipt.model_validate({
        **sender.model_dump(), "capture": captured, "capture_digest": captured.sha256, "submit_digest": request.sha256})
    return sender, recipient


def test_native_send_is_terminal_only_after_the_original_peer_acceptance():
    from shared_context.native_agent_tools import NativeAgentTools

    sender, recipient = collaboration_turn()
    calls = []

    class Client:
        async def directory(self, _conversation_id=None):
            return NativePeerDirectory(agents=(
                AgentMessageRecipient(agent_id=recipient.target.agent_id, display_name="測試助理"),))

        async def send(self, receipt, call_id, message):
            calls.append((receipt, call_id, message))
            return NativeRelayAcceptance(
                message_id="a" * 64, recipient_agent_id=recipient.target.agent_id,
                conversation_id="peer-cid", submit_digest="b" * 64, status="accepted")

    tools = NativeAgentTools(Client(), sender, SimpleNamespace(stop_requested=lambda: False))

    async def run():
        specs = await tools.prepare()
        send = next(tool for tool in specs if tool.name == "send_agent_message")
        assert send.is_terminal is True and tools.handoff_texts == []
        return await send.handler({"tool_call_id": "original-sdk-call", "arguments": {
            "recipient_agent_id": recipient.target.agent_id, "kind": "request", "content": "核對公開樣本。"}})

    result = asyncio.run(run())
    [text] = tools.handoff_texts
    assert "已交給" in text and "測試助理" in text
    assert len(calls) == 1 and calls[0][0] == sender and calls[0][1] == "original-sdk-call"
    assert NativeRelayAcceptance.model_validate_json(result.text).message_id == "a" * 64


def test_the_roster_is_read_once_per_conversation_and_again_after_a_failed_handoff(monkeypatch):
    """Grok Bot freezes its teammate block per session; the API still checks the recipient when a handoff is sent."""
    from shared_context import native_agent_tools
    from shared_context.m2_copilot import ToolFailure
    from shared_context.native_agent_tools import NativeAgentRelayClient, NativeAgentTools

    sender, recipient = collaboration_turn()
    later = sender.model_copy(update={"request_id": "same-cid-later"})
    other = sender.model_copy(update={"conversation_id": "another-human-cid"})
    reads, agents = [], [(AgentMessageRecipient(agent_id=recipient.target.agent_id, display_name="測試助理"),)]
    now = [1000.0]
    monkeypatch.setattr(native_agent_tools, "monotonic", lambda: now[0])

    def relay(request):
        if request.url.path.endswith("/recipients"):
            reads.append(request.url.path)
            return httpx.Response(200, content=NativePeerDirectory(agents=agents[0]).model_dump_json().encode())
        assert request.url.path.endswith("/send")
        return httpx.Response(409, json={"error": {"code": "AGENT_MESSAGE_RECIPIENT_NOT_READY"}})

    async def relay_token():
        return "synthetic-relay-token"

    client = NativeAgentRelayClient("https://api.invalid", relay_token, transport=httpx.MockTransport(relay))
    control = SimpleNamespace(stop_requested=lambda: False)

    async def prepared(receipt):
        tools = NativeAgentTools(client, receipt, control)
        return tools, await tools.prepare()

    async def scenario():
        first, _ = await prepared(sender)
        again, specs = await prepared(later)
        assert len(reads) == 1 and again.directory_sha256 == first.directory_sha256
        assert again.system_section == first.system_section
        await prepared(other)
        assert len(reads) == 2
        send = next(tool for tool in specs if tool.name == "send_agent_message")
        with pytest.raises(ToolFailure):
            await send.handler({"tool_call_id": "refused-call", "arguments": {
                "recipient_agent_id": recipient.target.agent_id, "kind": "request", "content": "核對公開樣本。"}})
        await prepared(later)
        assert len(reads) == 3
        # A teammate added mid-conversation appears once the kept roster is older than the API catalog bound.
        now[0] += native_agent_tools._DIRECTORY_SECONDS
        await prepared(later)
        assert len(reads) == 3
        now[0] += 1
        await prepared(later)
        await prepared(later)
        assert len(reads) == 4
        # No teammate means no handoff that could fail and refresh it, so an empty roster is never kept.
        agents[0] = ()
        empty = sender.model_copy(update={"conversation_id": "empty-roster-cid"})
        for _ in range(2):
            tools, _ = await prepared(empty)
            assert tools.assembly_reason == "no_results"
        assert len(reads) == 6

    asyncio.run(scenario())


def test_native_terminal_queued_result_never_claims_receiver_acceptance():
    from shared_context.native_agent_tools import NativeAgentTools
    from shared_context.native_relay_delivery import reference_for

    sender, recipient = collaboration_turn()

    class Client:
        async def directory(self, conversation_id):
            return NativePeerDirectory(agents=(
                AgentMessageRecipient(agent_id=recipient.target.agent_id, display_name="B"),))

        async def send(self, receipt, call_id, message):
            return reference_for(receipt, NativeRelaySend(
                conversation_id=receipt.conversation_id, request_id=receipt.request_id, tool_call_id=call_id, message=message))

    tools = NativeAgentTools(Client(), sender, SimpleNamespace(stop_requested=lambda: False))

    async def run():
        tool = (await tools.prepare())[0]
        assert tool.is_terminal
        return await tool.handler({"tool_call_id": "queued-call", "arguments": {
            "recipient_agent_id": recipient.target.agent_id, "kind": "request", "content": "Later work."}})

    output = asyncio.run(run())
    assert json.loads(output.text)["status"] == "queued"
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    boundary = AssistantModelBoundary(None, text_reply=True, capability_results=tools.result_schemas)
    assert json.loads(boundary.tool_result("send_agent_message", output.text))["status"] == "queued"
    assert tools.delegation is None
    assert tools.handoff_texts == ["已交給 B 排隊，B 有空就會處理，結果會回到這段對話。"]
    assert "已收到" not in "".join(tools.handoff_texts)


def test_native_recipient_names_are_data_and_do_not_replace_exact_id_validation():
    from jsonschema import Draft202012Validator
    from shared_context.m2_copilot import ToolFailure
    from shared_context.native_agent_tools import NativeAgentTools

    sender, recipient = collaboration_turn()
    recipients = (
        AgentMessageRecipient(agent_id=recipient.target.agent_id, display_name="test-agent-3"),
        AgentMessageRecipient(agent_id="10000000-0000-0000-0000-000000000003", display_name="test-agent-3"),
    )

    class Client:
        async def directory(self, _conversation_id=None):
            return NativePeerDirectory(agents=recipients)

        async def send(self, *_args):
            pytest.fail("display names, shortened IDs and unknown UUIDs must not reach the relay")

    tools = NativeAgentTools(Client(), sender, SimpleNamespace(stop_requested=lambda: False))
    spec = asyncio.run(tools.prepare())[0]
    for entry in recipients:
        assert json.dumps({"agent_id": entry.agent_id, "display_name": entry.display_name},
                          ensure_ascii=False) in spec.parameters["properties"]["recipient_agent_id"]["description"]
    for invalid in ("test-agent-3", recipient.target.agent_id[:8], "ffffffff-ffff-ffff-ffff-ffffffffffff"):
        arguments = {"recipient_agent_id": invalid, "kind": "request", "content": "Check the public sample."}
        assert not Draft202012Validator(spec.parameters).is_valid(arguments)
        with pytest.raises(ToolFailure) as refused:
            asyncio.run(spec.handler({"tool_call_id": "original-call", "arguments": arguments}))
        assert refused.value.public_code == "NATIVE_RELAY_REFUSED"
    assert tools.delegation is None and tools.handoff_texts == []


def _one_handoff_turn(results):
    sender, recipient = collaboration_turn()
    calls = []

    class Client:
        async def directory(self, _conversation_id=None):
            return NativePeerDirectory(agents=(
                AgentMessageRecipient(agent_id=recipient.target.agent_id, display_name="測試助理"),))

        async def send(self, _receipt, call_id, _message):
            calls.append(call_id)
            await asyncio.sleep(0)
            outcome = results[len(calls) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return NativeRelayAcceptance(
                message_id=("9" if outcome == "busy" else outcome) * 64, recipient_agent_id=recipient.target.agent_id,
                conversation_id="peer-cid", submit_digest="b" * 64,
                status="busy_refused" if outcome == "busy" else "accepted")

    def packet(call):
        return {"tool_call_id": call, "arguments": {
            "recipient_agent_id": recipient.target.agent_id, "kind": "request", "content": "核對公開樣本。"}}

    return sender, Client(), calls, packet


@pytest.mark.parametrize("parallel", [False, True], ids=["after-acceptance", "parallel-calls"])
def test_native_send_accepts_one_handoff_per_turn(parallel):
    """The original RID retains one delegation; a second accepted handoff would leave the whole turn unsaved."""
    from shared_context.m2_copilot import ToolFailure
    from shared_context.native_agent_tools import NativeAgentTools

    sender, client, calls, packet = _one_handoff_turn(["1", "2"])
    tools = NativeAgentTools(client, sender, SimpleNamespace(stop_requested=lambda: False))

    async def run():
        send = next(tool for tool in await tools.prepare() if tool.name == "send_agent_message")
        if parallel:
            return await asyncio.gather(send.handler(packet("first-call")), send.handler(packet("second-call")),
                                        return_exceptions=True)
        first = await send.handler(packet("first-call"))
        return [first, *await asyncio.gather(send.handler(packet("second-call")), return_exceptions=True)]

    first, second = asyncio.run(run())
    assert calls == ["first-call"]
    assert NativeRelayAcceptance.model_validate_json(first.text).message_id == "1" * 64
    assert isinstance(second, ToolFailure) and second.public_code == "NATIVE_RELAY_ALREADY_HANDED_OFF"
    assert tools.delegation.message_id == "1" * 64 and len(tools.handoff_texts) == 1


@pytest.mark.parametrize("first,sent_nothing", [
    ("NATIVE_RELAY_RECIPIENT_UNAVAILABLE", True),
    ("NATIVE_RELAY_REFUSED", True),
    ("NATIVE_GROUP_REPLY_REQUIRED", True),
    ("busy", True),
    ("NATIVE_RELAY_OUTCOME_UNKNOWN", False),
    ("AGENT_VERSION_FOLLOWING", False),
], ids=["recipient-unavailable", "refused", "group-reply", "queue-full", "outcome-unknown", "recipient-following"])
def test_native_send_hands_off_again_only_after_a_failure_that_sent_nothing(first, sent_nothing):
    """An unconfirmed send may already be B's work, so it still counts as this turn's one handoff."""
    from shared_context.errors import Problem
    from shared_context.m2_copilot import ToolFailure
    from shared_context.native_agent_tools import NativeAgentTools

    outcome = first if first == "busy" else Problem(503, first, "OFFLINE first relay outcome")
    sender, client, calls, packet = _one_handoff_turn([outcome, "2"])
    tools = NativeAgentTools(client, sender, SimpleNamespace(stop_requested=lambda: False))

    async def run():
        send = next(tool for tool in await tools.prepare() if tool.name == "send_agent_message")
        with pytest.raises(ToolFailure):
            await send.handler(packet("first-call"))
        return await asyncio.gather(send.handler(packet("second-call")), return_exceptions=True)

    [second] = asyncio.run(run())
    if sent_nothing:
        assert calls == ["first-call", "second-call"]
        assert NativeRelayAcceptance.model_validate_json(second.text).message_id == "2" * 64
        assert tools.delegation.message_id == "2" * 64 and len(tools.handoff_texts) == 1
    else:
        assert calls == ["first-call"]
        assert isinstance(second, ToolFailure) and second.public_code == "NATIVE_RELAY_ALREADY_HANDED_OFF"
        assert tools.delegation is None and tools.handoff_texts == []


@pytest.mark.parametrize("runner_error", [False, True], ids=["terminal-return", "interrupted-after-acceptance"])
def test_native_sdk_saves_the_verified_handoff_text_when_a_terminal_tool_has_no_model_final(
    tmp_path, monkeypatch, runner_error,
):
    from shared_context import native_sdk
    from shared_context.behavior_sdk_state import NativeCheckpointScope
    from shared_context.native_agent_tools import NativeAgentTools
    from shared_context.native_turns import NativeSdkFailure, NativeTurnControl
    from test_native_sdk import MODEL_ROOT

    sender, recipient = collaboration_turn()

    class Client:
        async def directory(self, _conversation_id=None):
            return NativePeerDirectory(agents=(
                AgentMessageRecipient(agent_id=recipient.target.agent_id, display_name="收件助理"),))

        async def send(self, _receipt, _call_id, _message):
            return NativeRelayAcceptance(
                message_id="a" * 64, recipient_agent_id=recipient.target.agent_id,
                conversation_id="peer-cid", submit_digest="b" * 64, status="accepted")

    async def sdk(**options):
        send = next(tool for tool in options["tools"] if tool.name == "send_agent_message")
        assert send.is_terminal
        await send.handler({"tool_call_id": "original-sdk-call", "arguments": {
            "recipient_agent_id": recipient.target.agent_id, "kind": "request", "content": "核對公開資料。", "refs": []}})
        sid = options["sdk_session_id"]
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": sid}}) + "\n",
            "workspace.yaml": "id: " + sid + "\n"})
        if runner_error:
            raise RuntimeError("synthetic SDK interruption after the child accepted")
        return {"final_message": "", "sdk_stop": {
            "pid": 123, "exit_code": 0, "graceful": True, "runtime_version": "1.0.83"}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path, runtime_path=tmp_path / "not-executed", api_url="https://api.invalid",
        model_url=MODEL_ROOT, token_provider=lambda: "synthetic-token",
        agent_tools=lambda receipt, control: NativeAgentTools(Client(), receipt, control))
    scope = NativeCheckpointScope(
        agent_id=sender.agent_id, conversation_id=sender.conversation_id, sdk_session_id="original-sdk-session",
        model_provider=sender.capture.template.settings.model_provider)
    if runner_error:
        with pytest.raises(NativeSdkFailure) as failure:
            asyncio.run(executor(sender, scope, None, NativeTurnControl()))
        assert failure.value.delegation is not None and failure.value.delegation.message_id == "a" * 64
        return
    result = asyncio.run(executor(sender, scope, None, NativeTurnControl()))
    assert result.text.startswith("已交給 收件助理。")
    assert result.sdk_stop.exit_code == 0 and result.state.sdk_session_id == scope.sdk_session_id
    assert result.delegation is not None and result.delegation.message_id == "a" * 64


@pytest.mark.parametrize("accepted,refusal", [
    (True, None),
    (False, "FOUNDRY_INVOCATION_UNKNOWN"),
    (False, "NATIVE_RELAY_RECIPIENT_UNAVAILABLE"),
    (False, "NATIVE_AGENT_SOURCE_INVALID"),
    (False, "NATIVE_GROUP_REPLY_REQUIRED"),
], ids=["accepted", "unconfirmed", "recipient-unavailable", "sender-refused", "group-circular-request"])
def test_real_sdk_terminal_handoff_has_one_model_request_and_no_poll(
    tmp_path, runtime_path, monkeypatch, accepted, refusal,
):
    from types import SimpleNamespace
    from azure.ai.agentserver.core.tasks import Task, TaskCancelled
    from shared_context import native_sdk
    from shared_context.behavior_native_turn_wire import NativeTurnCapture
    from shared_context.behavior_sdk_state import NativeCheckpointScope
    from shared_context.native_agent_tools import NativeAgentRelayClient, NativeAgentTools
    from shared_context.native_turns import NativeTurnControl
    from test_m2_copilot import ScriptedEndpoint, tool_call
    from test_native_sdk import MODEL_ROOT

    original, recipient = collaboration_turn()
    values = original.capture.model_dump(mode="json")
    values["template"]["settings"]["model_provider"] = None
    captured = NativeTurnCapture.model_validate_json(json.dumps(values))
    submitted = NativeSubmit(command=original.command, capture=captured)
    sender = NativeReceipt.model_validate({
        **original.model_dump(), "capture": captured, "capture_digest": captured.sha256,
        "submit_digest": submitted.sha256})
    # The verified handoff text leads; the model's own words beside the handoff call follow it, never dropped.
    endpoint = ScriptedEndpoint([{**tool_call("send_agent_message", {
        "recipient_agent_id": recipient.target.agent_id, "kind": "request",
        "content": "Inspect the public sample.", "refs": []}, "original-terminal-call"), "content": "我先交給對方。"},
        *([] if accepted else [{"content": "接收尚未確認，不會自動重新交辦。"}])])
    endpoint.expected_url = MODEL_ROOT + "/chat/completions"
    endpoint.expected_model = sender.capture.model.id
    endpoint.unbounded = True
    sent = []
    recipients = (
        AgentMessageRecipient(agent_id="10000000-0000-0000-0000-000000000001", display_name="test agent"),
        AgentMessageRecipient(agent_id="10000000-0000-0000-0000-000000000002", display_name="test agent 2"),
        AgentMessageRecipient(agent_id=recipient.target.agent_id, display_name="test agent 3"),
        AgentMessageRecipient(agent_id="10000000-0000-0000-0000-000000000004", display_name="測試助理３"),
    )

    def relay(request):
        if request.url.path.endswith("/recipients"):
            return httpx.Response(200, content=NativePeerDirectory(agents=recipients).model_dump_json().encode())
        assert request.url.path.endswith("/send")
        command = NativeRelaySend.model_validate_json(request.content)
        sent.append((command.request_id, command.tool_call_id, command.message))
        if not accepted:
            status = {"FOUNDRY_INVOCATION_UNKNOWN": 503, "NATIVE_GROUP_REPLY_REQUIRED": 409}.get(refusal, 403)
            return httpx.Response(status,
                                  json={"error": {"code": refusal, "message": "PRIVATE_UPSTREAM_DETAILS"}})
        receipt = NativeRelayAcceptance(
            message_id="a" * 64, recipient_agent_id=recipient.target.agent_id, conversation_id="peer-cid",
            submit_digest="b" * 64, status="accepted")
        return httpx.Response(202, content=receipt.model_dump_json().encode())

    async def relay_token():
        return "synthetic-relay-token"

    client = NativeAgentRelayClient("https://api.invalid", relay_token, transport=httpx.MockTransport(relay))
    registered_follows = []

    async def cancelled_follow():
        raise TaskCancelled()

    async def register_follow(_task, **options):
        registered_follows.append(options)
        return SimpleNamespace(result=cancelled_follow)

    # This fixture runs Copilot SDK, not the Hosted task service; real task recovery is covered separately.
    monkeypatch.setattr(Task, "start", register_follow)

    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path, runtime_path=runtime_path, api_url="https://api.invalid", model_url=MODEL_ROOT,
        token_provider=endpoint.token, model_transport=endpoint.transport,
        agent_tools=lambda receipt, control: NativeAgentTools(client, receipt, control))
    scope = NativeCheckpointScope(
        agent_id=sender.agent_id, conversation_id=sender.conversation_id, sdk_session_id="terminal-peer-session")

    async def one_turn():
        try:
            return await asyncio.wait_for(executor(sender, scope, None, NativeTurnControl()), timeout=90)
        finally:
            # ADR-0005: the idle turn keeps its SDK; stop it on this loop, as VM shutdown does.
            await executor.aclose()

    result = asyncio.run(one_turn())
    assert len(endpoint.requests) == (1 if accepted else 2) and len(sent) == 1
    assert len(registered_follows) == (1 if refusal == "FOUNDRY_INVOCATION_UNKNOWN" else 0)
    if registered_follows:
        command = registered_follows[0]["input"]
        assert (command.request_id, command.tool_call_id, command.message) == sent[0]
        assert command.conversation_id == sender.conversation_id
    send_schema = next(tool["function"] for tool in endpoint.requests[0]["tools"]
                       if tool["function"]["name"] == "send_agent_message")
    recipient_schema = send_schema["parameters"]["properties"]["recipient_agent_id"]
    assert recipient_schema["enum"] == [entry.agent_id for entry in recipients]
    for entry in recipients:
        assert json.dumps({"agent_id": entry.agent_id, "display_name": entry.display_name},
                          ensure_ascii=False) in recipient_schema.get("description", "")
    assert sent[0][2].recipient_agent_id == recipient.target.agent_id
    assert result.text == ("已交給 test agent 3。對方會依序處理，結果會在本對話另開回合回覆。\n\n我先交給對方。" if accepted
                           else "接收尚未確認，不會自動重新交辦。")
    assert result.sdk_stop.graceful and result.sdk_stop.release == "fa-resident-worker-poc-v1"
    if not accepted:
        feedback = [message["content"] for message in endpoint.requests[-1]["messages"] if message["role"] == "tool"]
        assert len(feedback) == 1 and "PRIVATE_UPSTREAM_DETAILS" not in feedback[0]
        if refusal == "NATIVE_RELAY_RECIPIENT_UNAVAILABLE":
            assert "對方目前無法接收" in feedback[0] and "這次沒有新增投遞" in feedback[0]
            assert "先前" in feedback[0] and "未能確認" in feedback[0] and "不要換 ID 重送" in feedback[0]
            assert "Authority refused" not in feedback[0]
        elif refusal == "NATIVE_AGENT_SOURCE_INVALID":
            assert "Authority refused" in feedback[0] and "對方目前無法接收" not in feedback[0]
        elif refusal == "NATIVE_GROUP_REPLY_REQUIRED":
            assert all(word in feedback[0] for word in ("主持者", "循環", "原結果", "send_agent_message"))
            assert "Authority refused" not in feedback[0]
        else:
            assert "Peer outcome is unconfirmed" in feedback[0]
            assert "Authority refused" not in feedback[0] and "對方目前無法接收" not in feedback[0]


def test_teammates_are_a_versioned_section_only_when_this_turn_can_hand_work_to_them(tmp_path, monkeypatch):
    """B1 PA-03: the same directory the tool validates against, as data; none made up when there is none."""
    from shared_context import native_sdk
    from shared_context.behavior_sdk_state import NativeCheckpointScope
    from shared_context.native_agent_tools import NativeAgentTools
    from shared_context.native_turns import NativeTurnControl
    from test_native_sdk import MODEL_ROOT

    sender, recipient = collaboration_turn()
    listed = [AgentMessageRecipient(agent_id="20000000-0000-0000-0000-000000000002", display_name='Ann "B"'),
              AgentMessageRecipient(agent_id=recipient.target.agent_id, display_name="收件助理")]

    class Client:
        agents = tuple(listed)

        async def directory(self, _conversation_id=None):
            return NativePeerDirectory(agents=self.agents)

    tools = NativeAgentTools(Client(), sender, SimpleNamespace(stop_requested=lambda: False))
    assert [spec.name for spec in asyncio.run(tools.prepare())] == ["send_agent_message"]
    section = tools.system_section
    assert section.startswith("## Teammates\nDirectory version ") and tools.unavailable_reason is None
    lines = section.splitlines()[2:]
    assert lines == sorted(lines, key=lambda line: line.rsplit("agent_id ", 1)[1].rstrip(")"))
    assert '- "Ann \\"B\\"" (agent_id 20000000-0000-0000-0000-000000000002)' in lines
    reordered = Client()
    reordered.agents = tuple(reversed(listed))
    again = NativeAgentTools(reordered, sender, SimpleNamespace(stop_requested=lambda: False))
    asyncio.run(again.prepare())
    assert again.system_section == section

    empty = Client()
    empty.agents = ()
    nobody = NativeAgentTools(empty, sender, SimpleNamespace(stop_requested=lambda: False))
    assert asyncio.run(nobody.prepare()) == [] and nobody.system_section is None
    assert "沒有可交辦" in nobody.unavailable_reason

    seen = []

    async def sdk(**options):
        seen.append((options["system_message"], [tool.name for tool in options["tools"]],
                     options["capability_session"].custom_tools))
        sid = options["sdk_session_id"]
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": sid}}) + "\n",
            "workspace.yaml": "id: " + sid + "\n"})
        return {"final_message": "ok", "sdk_stop": {"pid": 1, "exit_code": 0, "graceful": True,
                                                    "runtime_version": "1.0.83"}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    scope = NativeCheckpointScope(
        agent_id=sender.agent_id, conversation_id=sender.conversation_id, sdk_session_id="original-sdk-session",
        model_provider=sender.capture.template.settings.model_provider)
    for client in (Client(), empty):
        executor = native_sdk.NativeSdkExecutor(
            root=tmp_path / str(len(seen)), runtime_path=tmp_path / "not-executed", api_url="https://api.invalid",
            model_url=MODEL_ROOT, token_provider=lambda: "synthetic-token",
            agent_tools=lambda receipt, control, client=client: NativeAgentTools(client, receipt, control))
        asyncio.run(executor(sender, scope, None, NativeTurnControl()))
    (with_team, tools_named, needs), (without, no_tools, no_needs) = seen
    assert section in with_team and "send_agent_message" in tools_named and "send_agent_message" in needs
    assert "## Teammates" not in without and "沒有可交辦" in without
    assert "send_agent_message" not in no_tools and "send_agent_message" not in no_needs
    assert NOT_SELECTED_REASON not in with_team and NOT_SELECTED_REASON not in without


def test_a_turn_without_collaboration_says_so_where_the_roster_would_be(tmp_path, monkeypatch):
    """PA-03: collaboration turned off mid-conversation; the stated absence keeps an earlier roster from being reused."""
    from shared_context import native_sdk
    from shared_context.behavior_sdk_state import NativeCheckpointScope
    from shared_context.native_agent_tools import NativeAgentTools
    from shared_context.native_turns import NativeTurnControl
    from test_native_sdk import MODEL_ROOT

    _config, _scope, _recipient, _principal, sender = sender_case()
    assert sender.capture.capabilities is None or all(
        grant.entry_id != CAPABILITY_ID for grant in sender.capture.capabilities.grants)

    class Directory:
        calls = 0

        async def directory(self, _conversation_id=None):
            Directory.calls += 1
            return NativePeerDirectory(agents=())

    tools = NativeAgentTools(Directory(), sender, SimpleNamespace(stop_requested=lambda: False))
    assert asyncio.run(tools.prepare()) == [] and Directory.calls == 0
    assert tools.unavailable_reason == NOT_SELECTED_REASON
    assert tools.system_section is None and tools.assembly_reason == "not_selected"

    # fa-delegate-v1 alone is a child via task, not a teammate: its text stays and the sentence does not cover it.
    from shared_context.behavior_capability_prompt import assemble_capabilities
    delegate = available_grants(None, ("fa-delegate-v1",))["fa-delegate-v1"]
    values = sender.capture.model_dump()
    values["template"]["settings"]["capabilities"] = (delegate.ref,)
    values["capabilities"] = CapabilityCapture(head_selection_id="selected-delegate-only", grants=(delegate,))
    from shared_context.behavior_native_turn_wire import NativeTurnCapture
    delegate_only = sender.model_copy(update={"capture": NativeTurnCapture.model_validate(values)})
    only = NativeAgentTools(Directory(), delegate_only, SimpleNamespace(stop_requested=lambda: False))
    assert asyncio.run(only.prepare()) == [] and only.unavailable_reason == NOT_SELECTED_REASON
    addendum = assemble_capabilities(delegate_only.capture.template.role.profile, delegate_only.capture.capabilities)
    assert "- fa-delegate-v1: one synchronous child via task" in addendum.system_message
    assert "task" not in NOT_SELECTED_REASON and "send_agent_message" in NOT_SELECTED_REASON

    seen = []

    async def sdk(**options):
        seen.append((options["system_message"], [tool.name for tool in options["tools"]]))
        sid = options["sdk_session_id"]
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": sid}}) + "\n",
            "workspace.yaml": "id: " + sid + "\n"})
        return {"final_message": "ok", "sdk_stop": {"pid": 1, "exit_code": 0, "graceful": True,
                                                    "runtime_version": "1.0.83"}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    scope = NativeCheckpointScope(
        agent_id=sender.agent_id, conversation_id=sender.conversation_id, sdk_session_id="original-sdk-session",
        model_provider=sender.capture.template.settings.model_provider)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path, runtime_path=tmp_path / "not-executed", api_url="https://api.invalid",
        model_url=MODEL_ROOT, token_provider=lambda: "synthetic-token",
        agent_tools=lambda receipt, control: NativeAgentTools(Directory(), receipt, control))
    asyncio.run(executor(sender, scope, None, NativeTurnControl()))
    [(system_message, names)] = seen
    assert "\n\n" + NOT_SELECTED_REASON in system_message and "## Teammates" not in system_message
    assert "send_agent_message" not in names and Directory.calls == 0


HANDED = "已交給 fa-stage-b。對方會依序處理，結果會在本對話另開回合回覆。"
ROUTE_ANSWER = "這個回合已交辦過；走路路線我下一個回合再交給 B。"


async def _no_follow(_task, **_options):
    pytest.fail("an accepted handoff starts no follow")


def _handoff_then_append(script, append):
    """A real SDK turn whose accepted handoff is followed by an owner append, as on stage A 2026-10-04."""
    import dataclasses
    from shared_context import native_sdk
    from shared_context.behavior_native_turn_wire import NativeTurnCapture
    from shared_context.native_agent_tools import NativeAgentRelayClient, NativeAgentTools
    from test_m2_copilot import ScriptedEndpoint, tool_call
    from test_native_sdk import MODEL_ROOT

    original, recipient = collaboration_turn()
    values = original.capture.model_dump(mode="json")
    values["template"]["settings"]["model_provider"] = None
    captured = NativeTurnCapture.model_validate_json(json.dumps(values))
    request = NativeSubmit(command=original.command, capture=captured)
    sender = NativeReceipt.model_validate({
        **original.model_dump(), "capture": captured, "capture_digest": captured.sha256,
        "submit_digest": request.sha256})

    def handoff(content, call):
        return tool_call("send_agent_message", {"recipient_agent_id": recipient.target.agent_id, "kind": "request",
                                                "content": content, "refs": []}, call)

    endpoint = ScriptedEndpoint(script(handoff))
    endpoint.expected_url = MODEL_ROOT + "/chat/completions"
    endpoint.expected_model = sender.capture.model.id
    endpoint.unbounded = True
    sent = []

    def relay(request):
        if request.url.path.endswith("/recipients"):
            return httpx.Response(200, content=NativePeerDirectory(agents=(
                AgentMessageRecipient(agent_id=recipient.target.agent_id, display_name="fa-stage-b"),
            )).model_dump_json().encode())
        sent.append(NativeRelaySend.model_validate_json(request.content).tool_call_id)
        receipt = NativeRelayAcceptance(
            message_id=str(len(sent)) * 64, recipient_agent_id=recipient.target.agent_id,
            conversation_id="peer-cid", submit_digest="b" * 64, status="accepted")
        return httpx.Response(202, content=receipt.model_dump_json().encode())

    async def relay_token():
        return "synthetic-relay-token"

    client = NativeAgentRelayClient("https://api.invalid", relay_token, transport=httpx.MockTransport(relay))

    def tools(receipt, control):
        bound = NativeAgentTools(client, receipt, control)
        prepare = bound.prepare

        async def appended_prepare():
            specs = []
            for spec in await prepare():
                if spec.name == "send_agent_message":
                    handler = spec.handler

                    async def handoff_then_append(packet, handler=handler):
                        output = await handler(packet)
                        # The owner appends while the turn is still running, as on stage A.
                        assert await control.steering.send(append)
                        return output

                    spec = dataclasses.replace(spec, handler=handoff_then_append)
                specs.append(spec)
            return specs

        bound.prepare = appended_prepare
        return bound

    def executor(root, runtime_path):
        return native_sdk.NativeSdkExecutor(
            root=root, runtime_path=runtime_path, api_url="https://api.invalid", model_url=MODEL_ROOT,
            token_provider=endpoint.token, model_transport=endpoint.transport, agent_tools=tools)

    return SimpleNamespace(request=request, sender=sender, endpoint=endpoint, sent=sent, executor=executor)


def _run_executor(case, tmp_path, runtime_path, session):
    from shared_context.behavior_sdk_state import NativeCheckpointScope
    from shared_context.native_turns import NativeTurnControl

    scope = NativeCheckpointScope(
        agent_id=case.sender.agent_id, conversation_id=case.sender.conversation_id, sdk_session_id=session)
    run = case.executor(tmp_path, runtime_path)(case.sender, scope, None, NativeTurnControl())
    return asyncio.run(asyncio.wait_for(run, timeout=90))


def _second_handoff_case():
    return _handoff_then_append(lambda handoff: [
        handoff("Check the VM resources.", "first-handoff"),
        handoff("Also find the walking route.", "second-handoff"),
        {"content": ROUTE_ANSWER},
    ], "ASUS 立功大樓到 Costco 的走路路線？")


def test_real_sdk_append_taken_after_a_terminal_handoff_keeps_its_own_answer(tmp_path, runtime_path, monkeypatch):
    """Stage A 2026-10-04: A handed the first request to B, the SDK then answered an append in a later round, and
    the saved reply was only the fixed handoff text; the follow-up answer was lost."""
    from azure.ai.agentserver.core.tasks import Task

    case = _handoff_then_append(lambda handoff: [
        handoff("Check the VM resources.", "original-terminal-call"),
        {"content": "ASUS 立功大樓到最近的 Costco 走路約 2 分鐘、110 公尺。"},
    ], "ASUS 立功大樓離最近的 Costco 多遠？")
    monkeypatch.setattr(Task, "start", _no_follow)
    result = _run_executor(case, tmp_path, runtime_path, "terminal-append-session")
    assert len(case.endpoint.requests) == 2 and "Costco" in json.dumps(case.endpoint.requests[1], ensure_ascii=False)
    assert result.text == f"{HANDED}\n\nASUS 立功大樓到最近的 Costco 走路約 2 分鐘、110 公尺。"
    assert case.sent == ["original-terminal-call"]
    assert result.delegation is not None and result.delegation.message_id == "1" * 64


def test_real_sdk_second_handoff_in_one_turn_is_refused_and_every_answer_stays(tmp_path, runtime_path, monkeypatch):
    """Stage A follow-up: the original RID retains one delegation, so a second accepted handoff left the whole
    turn unsaved. The second call is refused before the relay with correctable guidance; every answer stays."""
    from azure.ai.agentserver.core.tasks import Task

    case = _second_handoff_case()
    monkeypatch.setattr(Task, "start", _no_follow)
    result = _run_executor(case, tmp_path, runtime_path, "two-handoffs-session")
    assert case.sent == ["first-handoff"] and len(case.endpoint.requests) == 3
    refusal = json.dumps(case.endpoint.requests[2], ensure_ascii=False)
    assert "[NATIVE_RELAY_ALREADY_HANDED_OFF] 這個回合已有一則交辦正在送出或已送出" in refusal and "Authority refused" not in refusal
    assert result.text == f"{HANDED}\n\n{ROUTE_ANSWER}"
    assert result.delegation is not None and result.delegation.message_id == "1" * 64


def test_native_turn_with_a_refused_second_handoff_completes_and_saves_every_answer(harness, runtime_path, tmp_path):
    """The formal N1 path retains the delegation on the original RID; before the refusal the second accepted
    handoff conflicted there and the RID never completed."""
    from shared_context.behavior_native_turn_wire import NativeResult

    case = _second_handoff_case()
    root = tmp_path / "sdk-work"
    root.mkdir()
    executor = case.executor(root, runtime_path)
    executor.reader = harness.runtime
    harness.runtime.executor = executor
    assert harness.invoke(case.request).status_code == 202
    rid = case.request.command.request_id
    deadline = time.monotonic() + 90
    while (receipt := harness.call(harness.runtime.read, rid)).status in {"accepted", "started"}:
        assert time.monotonic() < deadline, "the original turn did not finish"
        time.sleep(0.05)
    assert receipt.status == "completed", (receipt.status, receipt.failure_code)
    assert case.sent == ["first-handoff"] and len(case.endpoint.requests) == 3
    assert receipt.delegation is not None and receipt.delegation.message_id == "1" * 64
    page = harness.call(harness.runtime.result, NativeResult(conversation_id=receipt.conversation_id, request_id=rid))
    assert page.complete and page.text == f"{HANDED}\n\n{ROUTE_ANSWER}"
