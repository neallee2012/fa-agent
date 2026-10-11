"""Hosted group tools cross the public group HTTP seam; no invented model loop."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from shared_context.behavior_native_turn_wire import NativeTurnAddress
from shared_context.native_group_wire import NativeGroupContext, NativeGroupOpen
from shared_context.native_groups import NativeGroupStore
from conftest import InMemoryRepository
from test_native_agent_relay import sender_case
from test_native_agent_client import token
from test_m2_copilot import runtime_path


@pytest.mark.parametrize("code", ["NATIVE_GROUP_DISABLED", "UPSTREAM_FAILURE"])
def test_unavailable_group_context_does_not_fail_an_ordinary_native_turn(code):
    from shared_context.native_group_tools import NativeGroupClient, NativeGroupTools

    _, _, _, _, receipt = sender_case()
    def unavailable(request):
        assert request.url.path == "/native-groups/v1/context"
        return httpx.Response(503, json={"error": {"code": code}})
    tools = NativeGroupTools(NativeGroupClient("https://api.invalid", token,
        transport=httpx.MockTransport(unavailable)), receipt, SimpleNamespace(stop_requested=lambda: False))
    assert asyncio.run(tools.prepare()) == []
    assert tools.context is None and tools.unavailable_reason


def _address(receipt):
    return NativeTurnAddress(agent_id=receipt.agent_id, conversation_id=receipt.conversation_id,
                             request_id=receipt.request_id)


def _group_client(answers):
    """Answers keyed by API suffix; each item is a NativeGroupContext/Completion or an HTTP status."""
    from shared_context.native_group_tools import NativeGroupClient

    calls = []

    def http(request):
        suffix = request.url.path.rsplit("/", 1)[-1]
        calls.append((suffix, json.loads(request.content)["request_id"]))
        answer = answers[suffix].pop(0)
        if isinstance(answer, int):
            return httpx.Response(answer, json={"error": {"code": "NATIVE_GROUP_UNKNOWN"}})
        return httpx.Response(200, content=answer.model_dump_json().encode())

    return NativeGroupClient("https://api.invalid", token, transport=httpx.MockTransport(http)), calls


def test_only_a_turn_that_could_be_in_a_group_asks_the_group_api():
    """API context() only accepts the orchestrator's new human conversation turn as a group root (start: RID=group)."""
    from shared_context.native_group_wire import NativeGroupCompletion
    from test_native_routine_wire import routine_origin
    from test_native_turn_wire import agent_receive

    _, _, _, _, root = sender_case()
    assert root.command.conversation_id is None
    follow_up = root.model_copy(update={"request_id": "follow-up-rid", "command": root.command.model_copy(
        update={"request_id": "follow-up-rid", "conversation_id": root.conversation_id})})
    routine = root.model_copy(update={"request_id": "routine-rid", "routine": routine_origin()})
    relayed = agent_receive().source
    member = root.model_copy(update={"request_id": "member-rid", "source": relayed.model_copy(
        update={"root": _address(root)})})
    after_follow_up = root.model_copy(update={"request_id": "result-rid", "source": relayed.model_copy(
        update={"root": _address(follow_up)})})
    client, calls = _group_client({"context": [NativeGroupContext()], "complete": []})

    async def scenario():
        for receipt in (follow_up, routine, after_follow_up):
            assert (await client.context(receipt)) == NativeGroupContext()
            assert (await client.complete(receipt)) == NativeGroupCompletion(reason="no_group")
        assert calls == []
        # A new conversation is asked once; its completion and every later turn under that root are not.
        for receipt in (root, member):
            assert (await client.context(receipt)).group is None
            assert (await client.complete(receipt)).reason == "no_group"
        assert calls == [("context", root.request_id)]

    asyncio.run(scenario())


def test_a_group_root_and_an_unconfirmed_answer_keep_asking_the_group_api(monkeypatch):
    from shared_context import native_group_tools
    from shared_context.errors import Problem
    from shared_context.native_group_wire import NativeGroupCompletion

    _, _, recipient, _, root = sender_case()
    store = NativeGroupStore(InMemoryRepository(), tenant_id="tenant", team_id="team")
    group = store.open(NativeGroupOpen(request_id="cache-root", orchestrator_agent_id=root.agent_id,
        member_agent_ids=(root.agent_id, recipient.target.agent_id), content="Discuss the original question."))
    in_group = NativeGroupContext(group=group, root=_address(root), role="orchestrator")
    other = root.model_copy(update={"request_id": "other-root", "conversation_id": "other-cid"})
    third = root.model_copy(update={"request_id": "third-root", "conversation_id": "third-cid"})
    client, calls = _group_client({
        "context": [in_group, in_group, 503, NativeGroupContext(), NativeGroupContext(), NativeGroupContext()],
        "complete": [NativeGroupCompletion(group_id=group.group_id, reason="delegated")]})
    monkeypatch.setattr(native_group_tools, "_NO_GROUP_ROOTS", 1)

    async def scenario():
        assert (await client.context(root)).group == group
        assert (await client.context(root)).group == group
        assert (await client.complete(root)).reason == "delegated"
        with pytest.raises(Problem):
            await client.context(other)
        assert (await client.context(other)).group is None
        assert (await client.context(other)).group is None
        # The bounded memory forgets the oldest root and asks the API again; it never invents a group.
        assert (await client.context(third)).group is None
        assert (await client.context(other)).group is None

    asyncio.run(scenario())
    assert [suffix for suffix, _ in calls] == ["context", "context", "complete", "context", "context",
                                               "context", "context"]


@pytest.mark.parametrize(("name", "code", "registered"), [
    ("next_group_round", "NATIVE_GROUP_ROUND_CHANGED", False),
    ("read_group", "NATIVE_GROUP_ROUND_CHANGED", True),
    ("next_group_round", "NATIVE_GROUP_SCOPE_DENIED", True),
    ("next_group_round", "UNRECOGNIZED_PRIVATE_CODE", True),
])
def test_group_failure_guidance_requires_the_registered_tool_and_known_code(name, code, registered):
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    from shared_context.native_group_wire import NativeGroupRoundReceipt

    boundary = AssistantModelBoundary(None, text_reply=True, capability_results=(
        {name: NativeGroupRoundReceipt} if registered else {}))
    assert boundary.public_tool_failure(name, code) is None


@pytest.mark.parametrize("outcome", ["completed", "unconfirmed"])
def test_group_publication_failure_never_prevents_the_original_peer_notification(tmp_path, monkeypatch, outcome):
    from shared_context import native_sdk
    from shared_context.behavior_assistant_hosted import AssistantSupervisor
    from shared_context.errors import Problem
    from shared_context.native_agent_tools import NativeAgentRelayClient
    from shared_context.native_group_tools import NativeGroupClient
    from shared_context.behavior_private_memory import STORE_ENV
    from shared_context.native_team_memory import TEAM_ENV
    from test_native_turns import IDENTITY

    home = tmp_path / "home"
    home.mkdir()
    runtime_path = tmp_path / "OFFLINE-not-executed"
    runtime_path.write_text("OFFLINE SDK boundary only", encoding="utf-8")
    order = []

    async def peer(_client, *_args):
        order.append("peer")

    async def unavailable(_client, _receipt):
        order.append("group")
        raise Problem(503, "NATIVE_GROUP_UNKNOWN", "OFFLINE publication transport failure")

    monkeypatch.setattr(NativeAgentRelayClient, "completed", peer)
    monkeypatch.setattr(NativeAgentRelayClient, "unconfirmed", peer)
    monkeypatch.setattr(NativeGroupClient, "complete", unavailable)
    monkeypatch.delenv(STORE_ENV, raising=False)
    monkeypatch.delenv(TEAM_ENV, raising=False)
    monkeypatch.delenv("FA_NATIVE_HITL_ENABLED", raising=False)
    for name, value in {
        "FA_NATIVE_TURNS": "foundry-native-v1", "FA_NATIVE_GROUPS_ENABLED": "1",
        "FOUNDRY_HOSTING_ENVIRONMENT": "OFFLINE-platform", "AGENTSERVER_TASKS_BACKEND": "hosted",
        "HOME": str(home), "BEHAVIOR_ASSISTANT_RUNTIME_PATH": str(runtime_path),
        "FOUNDRY_PROJECT_ENDPOINT": "https://offline.services.ai.azure.com/api/projects/offline",
        "FOUNDRY_AGENT_NAME": "OFFLINE-agent",
    }.items():
        monkeypatch.setenv(name, value)
    runtime = native_sdk.native_runtime_from_environment(SimpleNamespace(
        identity=IDENTITY, supervisor=AssistantSupervisor(home / ".fa-assistant-v12", runtime_path=runtime_path),
        api=SimpleNamespace(api_url="https://api.invalid", audience="api://OFFLINE")))
    callback = runtime.on_completed(SimpleNamespace(), None) if outcome == "completed" else runtime.on_unconfirmed(SimpleNamespace())
    with pytest.raises(Problem) as unknown:
        asyncio.run(callback)
    assert unknown.value.body["error"]["code"] == "NATIVE_GROUP_UNKNOWN"
    assert order == ["peer", "group"]


def test_group_tools_only_expose_original_context_and_fyi_never_starts_a_peer():
    from shared_context.native_group_tools import NativeGroupClient, NativeGroupTools

    _, _, recipient, _, sender = sender_case()
    store = NativeGroupStore(InMemoryRepository(), tenant_id="tenant", team_id="team")
    group = store.open(NativeGroupOpen(request_id="tools-root", orchestrator_agent_id=sender.agent_id,
        member_agent_ids=(sender.agent_id, recipient.target.agent_id), content="Discuss the original question."))
    context = NativeGroupContext(group=group, root=NativeTurnAddress(
        agent_id=sender.agent_id, conversation_id=sender.conversation_id, request_id=sender.request_id),
        role="orchestrator")
    calls = []

    def http(request):
        calls.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        if request.url.path.endswith("/context"):
            return httpx.Response(200, content=context.model_dump_json().encode())
        if request.url.path.endswith("/fyi"):
            from shared_context.native_group_wire import NativeGroupPost
            body = json.loads(request.content)
            posted = store.post(group.group_id, sender.agent_id, NativeGroupPost(
                request_id="fyi-key", round=1, kind="fyi", content=body["content"],
                recipient_agent_id=body["recipient_agent_id"]))
            return httpx.Response(200, content=posted.model_dump_json().encode())
        raise AssertionError(request.url)

    async def scenario():
        tools = NativeGroupTools(NativeGroupClient("https://api.invalid", token, transport=httpx.MockTransport(http)),
                                 sender, SimpleNamespace(stop_requested=lambda: False))
        offered = await tools.prepare()
        assert {tool.name for tool in offered} == {"read_group", "share_group_fyi", "next_group_round"}
        assert "silence" in tools.system_section and "10" in tools.system_section
        fyi = next(tool for tool in offered if tool.name == "share_group_fyi")
        assert not fyi.is_terminal
        await fyi.handler({"tool_call_id": "sdk-original-fyi", "arguments": {
            "content": "This observation needs no reply.", "recipient_agent_id": recipient.target.agent_id}})

    asyncio.run(scenario())
    assert [path for _, path, _ in calls] == ["/native-groups/v1/context", "/native-groups/v1/fyi"]
    body = calls[-1][2]
    assert body["request_id"] == sender.request_id and body["tool_call_id"] == "sdk-original-fyi"
    assert "group_id" not in body and "sender" not in body
    assert store.read(group.group_id).awaiting_final


@pytest.mark.parametrize("operation", ["fyi", "round-mismatch", "scope-denied"])
def test_pinned_sdk_consumes_group_tools_without_a_second_loop_or_peer_wake(tmp_path, runtime_path, operation):
    from shared_context.behavior_native_turn_wire import NativeReceipt, NativeSubmit, NativeTurnCapture
    from shared_context.behavior_sdk_state import NativeCheckpointScope
    from shared_context.native_group_tools import NativeGroupClient
    from shared_context.errors import Problem
    from shared_context.native_group_wire import NativeGroupAdvance, NativeGroupPost, NativeGroupTranscript
    from shared_context.native_sdk import NativeSdkExecutor
    from shared_context.native_turns import NativeTurnControl
    from test_native_turn_wire import submit
    from test_m2_copilot import ScriptedEndpoint, tool_call
    from test_native_sdk import MODEL_ROOT

    request = submit("group-sdk-original")
    values = request.capture.model_dump(mode="json")
    values["template"]["settings"]["model_provider"] = None
    captured = NativeTurnCapture.model_validate_json(json.dumps(values))
    request = NativeSubmit(command=request.command, capture=captured)
    receipt = NativeReceipt(agent_id="group-agent-a", hosted_session_id="group-hosted-sid",
        request_id=request.command.request_id, conversation_id="group-cid", command=request.command,
        capture=captured, capture_digest=captured.sha256, submit_digest=request.sha256, status="started", turn_number=1)
    store = NativeGroupStore(InMemoryRepository(), tenant_id="tenant", team_id="team")
    group = store.open(NativeGroupOpen(request_id="sdk-group", orchestrator_agent_id=receipt.agent_id,
        member_agent_ids=(receipt.agent_id, "group-agent-b"), content=request.command.content))
    context = NativeGroupContext(group=group, root=NativeTurnAddress(
        agent_id=receipt.agent_id, conversation_id=receipt.conversation_id, request_id=receipt.request_id),
        role="orchestrator")
    requests = []

    def relay(packet):
        requests.append(packet.url.path)
        if packet.url.path.endswith("/context"):
            return httpx.Response(200, content=context.model_dump_json().encode())
        if packet.url.path.endswith("/transcript"):
            return httpx.Response(200, content=NativeGroupTranscript(
                group=store.read(group.group_id), posts=()).model_dump_json().encode())
        if packet.url.path.endswith("/next-round"):
            if operation == "scope-denied":
                return httpx.Response(403, json={"error": {
                    "code": "NATIVE_GROUP_SCOPE_DENIED", "message": "PRIVATE_UPSTREAM_DETAILS"}})
            body = json.loads(packet.content)
            try:
                advanced = store.advance(group.group_id, receipt.agent_id, NativeGroupAdvance(
                    request_id=body["tool_call_id"], expected_round=body["expected_round"]))
            except Problem as error:
                return httpx.Response(error.status, json={"error": {
                    "code": error.body["error"]["code"], "message": "PRIVATE_UPSTREAM_DETAILS"}})
            return httpx.Response(200, content=advanced.model_dump_json().encode())
        assert packet.url.path.endswith("/fyi")
        body = json.loads(packet.content)
        assert body["request_id"] == receipt.request_id and body["tool_call_id"] == "sdk-group-fyi"
        posted = store.post(group.group_id, receipt.agent_id, NativeGroupPost(
            request_id="original-fyi", round=1, kind="fyi", content=body["content"],
            recipient_agent_id=body["recipient_agent_id"]))
        return httpx.Response(200, content=posted.model_dump_json().encode())

    calls = ([tool_call("share_group_fyi", {
        "content": "A useful fact requiring no reply.", "recipient_agent_id": "group-agent-b"}, "sdk-group-fyi")]
        if operation == "fyi" else [tool_call("next_group_round", {"expected_round": 2}, "sdk-wrong-round")])
    if operation == "round-mismatch":
        calls.extend([tool_call("read_group", {}, "sdk-read-round"),
                      tool_call("next_group_round", {"expected_round": 1}, "sdk-current-round")])
    endpoint = ScriptedEndpoint([*calls, {"content": "The original group conclusion."}])
    endpoint.expected_url = MODEL_ROOT + "/chat/completions"
    endpoint.expected_model = receipt.capture.model.id
    endpoint.unbounded = True
    executor = NativeSdkExecutor(
        root=tmp_path, runtime_path=runtime_path, api_url="https://api.invalid", model_url=MODEL_ROOT,
        token_provider=endpoint.token, model_transport=endpoint.transport,
        group_client=NativeGroupClient("https://api.invalid", token, transport=httpx.MockTransport(relay)))
    async def one_turn():
        try:
            return await executor(receipt, NativeCheckpointScope(
                agent_id=receipt.agent_id, conversation_id=receipt.conversation_id,
                sdk_session_id="original-group-sdk"), None, NativeTurnControl())
        finally:
            await executor.aclose()  # ADR-0005: stop the kept SDK on this loop, as VM shutdown does

    result = asyncio.run(one_turn())
    assert result.text == "The original group conclusion." and result.sdk_stop.graceful
    assert len(endpoint.requests) == len(calls) + 1
    group = store.read(group.group_id)
    if operation == "fyi":
        assert requests == ["/native-groups/v1/context", "/native-groups/v1/fyi"]
        assert len(group.posts) == 1 and group.awaiting_final
        return
    feedback = [message["content"] for message in endpoint.requests[1]["messages"] if message["role"] == "tool"]
    assert len(feedback) == 1 and "PRIVATE_UPSTREAM_DETAILS" not in feedback[0]
    assert not group.posts
    if operation == "scope-denied":
        assert "Authority refused" in feedback[0] and "輪次不符" not in feedback[0]
        assert group.round == 1 and requests == ["/native-groups/v1/context", "/native-groups/v1/next-round"]
        return
    assert all(word in feedback[0] for word in ("輪次不符", "read_group", "目前輪次", "不是權限拒絕")), feedback[0]
    assert "已到原預期輪次就不要再換輪" in feedback[0]
    assert "Authority refused" not in feedback[0]
    schema = next(tool["function"] for tool in endpoint.requests[0]["tools"]
                  if tool["function"]["name"] == "next_group_round")
    assert "目前輪次" in schema["parameters"]["properties"]["expected_round"]["description"]
    assert group.round == 2 and requests == [
        "/native-groups/v1/context", "/native-groups/v1/next-round",
        "/native-groups/v1/groups/" + group.group_id + "/transcript", "/native-groups/v1/next-round",
    ]
