"""The Hosted collaboration tool sends only a bounded public brief and pinned refs."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from shared_context.behavior_agent_message_wire import CAPABILITY_ID, AgentMessage, AgentMessageProjection, AgentMessageRead
from shared_context.behavior_agent_messages import WakeDecision
from shared_context.behavior_capabilities import BUILTIN_CAPABILITIES, BuiltinCapability, CapabilityGrant
from shared_context.errors import Problem
from shared_context.m2_copilot import ToolFailure
from shared_context.source_materials import digest
from test_m2_copilot import runtime_path, workspace


@pytest.fixture
def tool_case(monkeypatch):
    builtin = BuiltinCapability(CAPABILITY_ID, ("send_agent_message",), vision=False)
    monkeypatch.setitem(BUILTIN_CAPABILITIES, CAPABILITY_ID, builtin)
    grant = CapabilityGrant(entry_id=CAPABILITY_ID, entry_sha256=builtin.entry_sha256,
                            kind="fa-builtin", tools=builtin.tools, vision=False)
    sent, reads = [], []

    def send(message):
        sent.append(message)
        silence = message.kind in {"fyi", "receipt"}
        return AgentMessageRead(message=AgentMessageProjection(
            message_key="original-message-key", message_id=message.message_id, root_request_id="root",
            sender_agent_id="agent-a", recipient_agent_id=message.recipient_agent_id,
            from_work_id="work-a", to_work_id="work-b", from_conversation_id="conversation-a",
            to_conversation_id="conversation-b", kind=message.kind, hop=1,
            status="silenced" if silence else "queued",
            decision=WakeDecision(action="silence" if silence else "wake", reason="original")),
            content=message.content, refs=tuple(ref.model_dump(mode="json") for ref in message.refs),
            payload_sha256=digest(message.model_dump(mode="json")), fresh=True,
            response_message_key=None if silence else "original-response-key")

    def read(key):
        reads.append(key)
        if key == "original-response-key":
            source = send_result[0]
            command = AgentMessage(message_id="original-b-result", recipient_agent_id="agent-a", kind="result",
                content="The public task was reviewed.", in_reply_to=source.message.message_key, refs=source.refs)
            return AgentMessageRead(message=AgentMessageProjection(
                message_key=key, message_id=command.message_id, root_request_id="root",
                sender_agent_id="agent-b", recipient_agent_id="agent-a", from_work_id="work-b", to_work_id="work-a",
                from_conversation_id="conversation-b", to_conversation_id="conversation-a", kind="result", hop=2,
                status="queued", decision=WakeDecision(action="wake", reason="original_result")),
                content=command.content, refs=source.refs, payload_sha256=digest(command.model_dump(mode="json")))
        return send_result[0].model_copy(update={"fresh": False})

    send_result = []

    def recorded(message):
        value = send(message)
        send_result[:] = [value]
        return value

    session = SimpleNamespace(_authority_lock=asyncio.Lock(), client=SimpleNamespace(
        assignment=SimpleNamespace(agent_message_recipient=SimpleNamespace(agent_id="agent-b", display_name="B"),
            task=SimpleNamespace(job_id="job-a", work_id="work-a",
            instruction=SimpleNamespace(conversation_id="conversation-a"))),
        send_agent_message=recorded, read_agent_message=read))
    return SimpleNamespace(session=session, grant=grant, sent=sent, reads=reads)


def test_tool_uses_original_job_and_sdk_call_identity_and_rechecks_the_saved_message(tool_case):
    from shared_context.behavior_agent_message_tools import agent_message_capability
    from shared_context.behavior_operations import control_key
    c = tool_case
    tools, results = agent_message_capability(c.session, c.grant)
    assert len(tools) == 1 and tools[0].name == "send_agent_message"
    assert set(tools[0].parameters["properties"]) == {"recipient_agent_id", "kind", "content", "in_reply_to", "refs"}
    assert results == {"send_agent_message": AgentMessageRead}
    packet = {"tool_call_id": "sdk-call", "arguments": {
        "recipient_agent_id": "agent-b", "kind": "handoff", "content": "Check the two approved sources."}}
    first = asyncio.run(tools[0].handler(packet))
    repeated = asyncio.run(tools[0].handler(packet))
    assert len(c.sent) == 1 and c.reads == ["original-response-key", "original-message-key", "original-response-key"]
    assert c.sent[0].message_id == control_key("agent-message-sdk", "job-a", "sdk-call")
    assert AgentMessageRead.model_validate_json(first.text).message.kind == "result"
    assert AgentMessageRead.model_validate_json(repeated.text).fresh is False
    assert AgentMessageRead.model_validate_json(repeated.text).message.decision.model_dispatch_authorized is False


def test_tool_schema_supplies_only_the_peer_from_the_original_assignment(tool_case):
    from shared_context.behavior_agent_message_tools import agent_message_capability
    c = tool_case
    c.session.client.assignment.agent_message_recipient = SimpleNamespace(
        agent_id="00000000-0000-0000-0000-00000000005a", display_name="B")
    tool = agent_message_capability(c.session, c.grant)[0][0]
    assert tool.parameters["properties"]["recipient_agent_id"].get("enum") == [
        "00000000-0000-0000-0000-00000000005a"]
    assert c.sent == [] and c.reads == []


@pytest.mark.parametrize("recipient", ["B", "agent-a", "another-agent"])
def test_recipient_names_or_other_agents_are_rejected_before_any_send(tool_case, recipient):
    from shared_context.behavior_agent_message_tools import agent_message_capability
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    c = tool_case
    tools, results = agent_message_capability(c.session, c.grant)
    with pytest.raises(ToolFailure) as refused:
        asyncio.run(tools[0].handler({"tool_call_id": "wrong-peer", "arguments": {
            "recipient_agent_id": recipient, "kind": "handoff", "content": "Public task."}}))
    assert refused.value.public_code == "AGENT_MESSAGE_RECIPIENT_DENIED"
    boundary = AssistantModelBoundary(None, text_reply=True, capability_results=results)
    assert "訊息沒有送達B，也沒有任何B結果" in boundary.public_tool_failure(tools[0].name, refused.value.public_code)
    assert c.sent == [] and c.reads == []


def test_missing_original_recipient_does_not_guess_or_fetch_a_new_directory(tool_case):
    from shared_context.behavior_agent_message_tools import agent_message_capability
    c = tool_case
    c.session.client.assignment.agent_message_recipient = None
    with pytest.raises(Problem) as refused:
        agent_message_capability(c.session, c.grant)
    assert refused.value.body["error"]["code"] == "AGENT_MESSAGE_UNAVAILABLE"
    assert c.sent == [] and c.reads == []


def test_handoff_tool_waits_for_the_correlated_b_result_inside_the_original_sdk_call(tool_case):
    from shared_context.behavior_agent_message_tools import agent_message_capability
    from shared_context.behavior_agent_message_wire import AgentMessage
    c = tool_case
    send = c.session.client.send_agent_message
    original = []
    def accepted(message):
        value = send(message).model_copy(update={"response_message_key": "original-result-key"})
        original.append(value)
        return value
    def returned(key):
        c.reads.append(key)
        assert key == "original-result-key"
        source = original[0]
        command = AgentMessage(message_id="b-result", recipient_agent_id="agent-a",
            kind="result", content="The two sources agree.", in_reply_to=source.message.message_key)
        return AgentMessageRead(message=AgentMessageProjection(
            message_key=key, message_id="b-result", root_request_id="root", sender_agent_id="agent-b",
            recipient_agent_id="agent-a", from_work_id="work-b", to_work_id="work-a",
            from_conversation_id="conversation-b", to_conversation_id="conversation-a", kind="result",
            hop=2, status="queued", decision=WakeDecision(action="wake", reason="original_result")),
            content=command.content, refs=(), payload_sha256=digest(command.model_dump(mode="json")))
    c.session.client.send_agent_message = accepted
    c.session.client.read_agent_message = returned
    tool = agent_message_capability(c.session, c.grant)[0][0]
    outcome = asyncio.run(tool.handler({"tool_call_id": "original-sdk-call", "arguments": {
        "recipient_agent_id": "agent-b", "kind": "handoff", "content": "Compare these two public sources."}}))
    result = AgentMessageRead.model_validate_json(outcome.text)
    assert result.message.kind == "result" and result.message.to_work_id == "work-a"
    assert result.content == "The two sources agree."
    assert len(c.sent) == 1 and c.reads == ["original-result-key"]


def test_waiting_handoff_reads_only_the_original_message_and_does_not_hold_the_authority_lock(tool_case):
    from shared_context.behavior_agent_message_tools import agent_message_capability
    c = tool_case
    send, read = c.session.client.send_agent_message, c.session.client.read_agent_message
    deadline = datetime.now(timezone.utc) + timedelta(seconds=10)
    c.session.client.send_agent_message = lambda message: send(message).model_copy(
        update={"response_message_key": None, "root_deadline": deadline})
    c.session.client.read_agent_message = lambda key: read(key).model_copy(update={"root_deadline": deadline})
    tool = agent_message_capability(c.session, c.grant)[0][0]
    async def run():
        waiting = asyncio.create_task(tool.handler({"tool_call_id": "wait-original", "arguments": {
            "recipient_agent_id": "agent-b", "kind": "handoff", "content": "Review this public brief."}}))
        await asyncio.sleep(0.05)
        await asyncio.wait_for(c.session._authority_lock.acquire(), timeout=0.5)
        c.session._authority_lock.release()
        return await waiting
    result = AgentMessageRead.model_validate_json(asyncio.run(run()).text)
    assert result.message.kind == "result"
    assert len(c.sent) == 1 and c.reads == ["original-message-key", "original-response-key"]


@pytest.mark.parametrize("failure", ["receiver_unknown", "read_failed", "wait_expired"])
def test_confirmed_handoff_failure_says_received_not_rejected(tool_case, failure):
    from shared_context.behavior_agent_message_tools import agent_message_capability
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    c = tool_case
    send = c.session.client.send_agent_message

    def accepted(message):
        value = send(message)
        if failure == "receiver_unknown":
            return value.model_copy(update={"response_message_key": None,
                "message": value.message.model_copy(update={"status": "unknown"})})
        if failure == "wait_expired":
            return value.model_copy(update={"response_message_key": None,
                "root_deadline": datetime.now(timezone.utc) - timedelta(seconds=1)})
        return value

    def unreadable(message_key):
        c.reads.append(message_key)
        raise Problem(503, "AGENT_MESSAGE_UNKNOWN", "PRIVATE read failure")

    c.session.client.send_agent_message = accepted
    if failure == "read_failed":
        c.session.client.read_agent_message = unreadable
    tools, results = agent_message_capability(c.session, c.grant)
    with pytest.raises(ToolFailure) as pending:
        asyncio.run(tools[0].handler({"tool_call_id": "accepted-pending", "arguments": {
            "recipient_agent_id": "agent-b", "kind": "handoff", "content": "Original public task."}}))
    assert pending.value.public_code == "AGENT_MESSAGE_RESULT_UNCONFIRMED"
    boundary = AssistantModelBoundary(lambda _: None, text_reply=True, capability_results=results)
    feedback = boundary.public_tool_failure(tools[0].name, pending.value.public_code)
    assert "已接收" in feedback and "B 的結果尚未確認" in feedback
    assert "Authority refused" not in feedback and "PRIVATE" not in feedback
    assert len(c.sent) == 1


def test_expired_handoff_wait_is_explicit_and_does_not_resend_or_read(tool_case):
    from shared_context.behavior_agent_message_tools import agent_message_capability
    c = tool_case
    send = c.session.client.send_agent_message
    c.session.client.send_agent_message = lambda message: send(message).model_copy(update={
        "response_message_key": None, "root_deadline": datetime.now(timezone.utc) - timedelta(seconds=1)})
    tool = agent_message_capability(c.session, c.grant)[0][0]
    with pytest.raises(ToolFailure) as refused:
        asyncio.run(tool.handler({"tool_call_id": "expired-wait", "arguments": {
            "recipient_agent_id": "agent-b", "kind": "handoff", "content": "Original work."}}))
    assert refused.value.public_code == "AGENT_MESSAGE_RESULT_UNCONFIRMED"
    assert len(c.sent) == 1 and c.reads == []


def test_wrong_b_result_cannot_resume_the_original_a_work(tool_case):
    from shared_context.behavior_agent_message_tools import agent_message_capability
    c = tool_case
    read = c.session.client.read_agent_message
    def wrong(key):
        result = read(key)
        return result.model_copy(update={"message": result.message.model_copy(update={"to_work_id": "another-a-work"})})
    c.session.client.read_agent_message = wrong
    tool = agent_message_capability(c.session, c.grant)[0][0]
    with pytest.raises(ToolFailure) as refused:
        asyncio.run(tool.handler({"tool_call_id": "wrong-peer", "arguments": {
            "recipient_agent_id": "agent-b", "kind": "handoff", "content": "Original work."}}))
    assert refused.value.public_code == "AGENT_MESSAGE_RECEIPT_INVALID"
    assert len(c.sent) == 1 and c.reads == ["original-response-key"]


@pytest.mark.parametrize("field", ["message_id", "root_request_id", "from_work_id", "sender_agent_id",
                                  "scope", "hop", "budget", "private_memory"])
def test_model_cannot_add_ids_authority_or_private_context_inputs(tool_case, field):
    from shared_context.behavior_agent_message_tools import agent_message_capability
    c = tool_case
    tool = agent_message_capability(c.session, c.grant)[0][0]
    with pytest.raises(ToolFailure) as error:
        asyncio.run(tool.handler({"tool_call_id":"call", "arguments":{
            "recipient_agent_id":"agent-b","kind":"handoff","content":"Public task.",field:"UNTRUSTED"}}))
    assert error.value.public_code == "AGENT_MESSAGE_ARGUMENTS"
    assert c.sent == [] and c.reads == []


def test_unknown_send_is_not_reissued_and_raw_errors_are_not_exposed(tool_case):
    from shared_context.behavior_agent_message_tools import agent_message_capability
    c = tool_case
    calls = []

    def lost(message):
        calls.append(message)
        raise Problem(503, "AGENT_MESSAGE_UNKNOWN", "PRIVATE_TRANSPORT_DIAGNOSTIC")

    c.session.client.send_agent_message = lost
    tool = agent_message_capability(c.session, c.grant)[0][0]
    packet = {"tool_call_id":"unknown-call", "arguments":{
        "recipient_agent_id":"agent-b","kind":"handoff","content":"Public task."}}
    for _ in range(2):
        with pytest.raises(ToolFailure) as error:
            asyncio.run(tool.handler(packet))
        assert error.value.public_code == "AGENT_MESSAGE_UNKNOWN" and "PRIVATE" not in str(error.value)
    assert len(calls) == 1 and c.reads == []


def test_known_route_rejection_keeps_its_code_and_cannot_be_reported_as_a_b_result(tool_case):
    from shared_context.behavior_agent_message_tools import agent_message_capability
    c = tool_case
    calls = []

    def denied(message):
        calls.append(message)
        raise Problem(403, "AGENT_MESSAGE_ROUTE_DENIED", "PRIVATE_ROUTE_DIAGNOSTIC")

    c.session.client.send_agent_message = denied
    tool = agent_message_capability(c.session, c.grant)[0][0]
    with pytest.raises(ToolFailure) as error:
        asyncio.run(tool.handler({"tool_call_id": "denied-route", "arguments": {
            "recipient_agent_id": "agent-b", "kind": "handoff", "content": "Public task."}}))
    assert error.value.public_code == "AGENT_MESSAGE_ROUTE_DENIED"
    assert "沒有任何已確認的B結果" in str(error.value)
    assert "不得把A自己的計算或推測說成B的回覆" in str(error.value)
    assert "PRIVATE_ROUTE_DIAGNOSTIC" not in str(error.value)
    assert len(calls) == 1 and c.reads == []


@pytest.mark.parametrize(("code", "expected"), [
    ("AGENT_MESSAGE_ROUTE_DENIED", "AGENT_MESSAGE_ROUTE_DENIED"),
    ("WORK_POLICY_SCOPE_CHANGED", "WORK_POLICY_SCOPE_CHANGED"),
    ("BEHAVIOR_CURRENT_POLICY_DENIED", "BEHAVIOR_CURRENT_POLICY_DENIED"),
    ("PRIVATE_UPSTREAM_CODE", "AGENT_MESSAGE_UNKNOWN"),
])
def test_hosted_peer_refusal_codes_are_allowlisted_without_reading_error_bodies(tool_case, caplog, code, expected):
    import httpx
    from pydantic import SecretStr
    from shared_context.behavior_assistant_hosted import AssistantClient
    c = tool_case
    assignment = c.session.client.assignment
    assignment.binding_id, assignment.session_id = "binding-a", "a" * 64
    calls = []

    def reject(request):
        calls.append(request.url.path)
        return httpx.Response(403, headers={"x-behavior-assistant-error-code": code},
                              json={"error": "PRIVATE_UPSTREAM_BODY"})

    client = AssistantClient("https://context.invalid", assignment, SecretStr("synthetic-ticket"),
        token_provider=lambda: "synthetic-token", transport=httpx.MockTransport(reject))
    try:
        with pytest.raises(Problem) as refused:
            client.send_agent_message(AgentMessage(message_id="original", recipient_agent_id="agent-b",
                                                   kind="handoff", content="PRIVATE_BRIEF"))
    finally:
        client.close()
    assert refused.value.body["error"]["code"] == expected and len(calls) == 1
    assert any("AGENT_MESSAGE_REQUEST_FAILED" in record.message and expected in record.message
               for record in caplog.records)
    for value in ("PRIVATE_UPSTREAM_CODE", "PRIVATE_UPSTREAM_BODY", "PRIVATE_BRIEF",
                  "synthetic-token", "synthetic-ticket"):
        assert value not in str(refused.value.body) and value not in caplog.text


def test_concurrent_same_sdk_call_does_not_send_the_peer_message_twice(tool_case):
    from shared_context.behavior_agent_message_tools import agent_message_capability
    c = tool_case
    tool = agent_message_capability(c.session, c.grant)[0][0]
    packet = {"tool_call_id":"same-concurrent-call", "arguments":{
        "recipient_agent_id":"agent-b","kind":"handoff","content":"One original task."}}

    async def run():
        return await asyncio.gather(tool.handler(packet), tool.handler(packet))

    first, second = asyncio.run(run())
    assert len(c.sent) == 1 and c.reads.count("original-message-key") == 1
    assert c.reads.count("original-response-key") == 2
    results = [AgentMessageRead.model_validate_json(value.text) for value in (first, second)]
    assert all(value.message.kind == "result" and value.fresh is False for value in results)
    assert len({value.message.message_id for value in results}) == 1


def test_reused_sdk_id_cannot_change_the_message_or_return_a_cached_read_after_revocation(tool_case):
    from shared_context.behavior_agent_message_tools import agent_message_capability
    c = tool_case
    tool = agent_message_capability(c.session, c.grant)[0][0]
    packet = {"tool_call_id":"stable", "arguments":{
        "recipient_agent_id":"agent-b","kind":"handoff","content":"Original task."}}
    asyncio.run(tool.handler(packet))
    with pytest.raises(ToolFailure) as changed:
        asyncio.run(tool.handler({**packet, "arguments":{**packet["arguments"], "content":"Changed task."}}))
    assert changed.value.public_code == "AGENT_MESSAGE_CONFLICT"

    def revoked(key):
        c.reads.append(key)
        raise Problem(403, "AGENT_MESSAGE_REFS_DENIED", "PRIVATE_READ_DENIAL")

    c.session.client.read_agent_message = revoked
    with pytest.raises(ToolFailure) as denied:
        asyncio.run(tool.handler(packet))
    assert denied.value.public_code == "AGENT_MESSAGE_REFS_DENIED" and "PRIVATE" not in str(denied.value)
    assert len(c.sent) == 1 and c.reads == ["original-response-key", "original-message-key"]


@pytest.mark.parametrize("kind,content,in_reply_to", [("fyi", "Public FYI.", None), ("receipt", "", "original-request-key")])
def test_fyi_and_receipt_only_return_data_and_do_not_dispatch_or_poll(tool_case, kind, content, in_reply_to):
    from shared_context.behavior_agent_message_tools import agent_message_capability
    c = tool_case
    tool = agent_message_capability(c.session, c.grant)[0][0]
    result = asyncio.run(tool.handler({"tool_call_id":"silent", "arguments":{
        "recipient_agent_id":"agent-b","kind":kind,"content":content,"in_reply_to":in_reply_to}}))
    value = AgentMessageRead.model_validate_json(result.text)
    assert value.message.decision.action == "silence"
    assert value.message.decision.model_dispatch_authorized is False
    assert value.message.human_obligation_fulfilled is False
    assert len(c.sent) == 1 and c.reads == []


@pytest.mark.parametrize("fault", ["payload", "content", "work", "conversation"])
def test_mismatched_result_never_becomes_a_confirmed_message(tool_case, fault):
    from shared_context.behavior_agent_message_tools import agent_message_capability
    c = tool_case
    send = c.session.client.send_agent_message

    def wrong(message):
        result = send(message)
        if fault == "payload":
            return result.model_copy(update={"payload_sha256":"f"*64})
        if fault == "content":
            return result.model_copy(update={"content":"Other message."})
        field = "from_work_id" if fault == "work" else "from_conversation_id"
        return result.model_copy(update={"message":result.message.model_copy(update={field:"other"})})

    c.session.client.send_agent_message = wrong
    tool = agent_message_capability(c.session, c.grant)[0][0]
    packet = {"tool_call_id":"wrong-result", "arguments":{
        "recipient_agent_id":"agent-b","kind":"handoff","content":"Original task."}}
    with pytest.raises(ToolFailure) as invalid:
        asyncio.run(tool.handler(packet))
    assert invalid.value.public_code == "AGENT_MESSAGE_RECEIPT_INVALID"
    with pytest.raises(ToolFailure) as unknown:
        asyncio.run(tool.handler(packet))
    assert unknown.value.public_code == "AGENT_MESSAGE_UNKNOWN"
    assert len(c.sent) == 1


def test_refs_require_explicit_kind_and_the_whole_brief_must_fit_the_wire_bound(tool_case):
    from shared_context.behavior_agent_message_tools import agent_message_capability
    from shared_context.behavior_store import MAX_INLINE_BYTES
    c = tool_case
    tool = agent_message_capability(c.session, c.grant)[0][0]
    reference = {"visibility":"team","tenant_id":"tenant","team_id":"team",
                 "resource_id":"source","version":"1","sha256":"a"*64}
    for arguments in (
        {"recipient_agent_id":"agent-b","kind":"handoff","content":"Public task.","refs":[reference]},
        {"recipient_agent_id":"agent-b","kind":"handoff","content":"字"*MAX_INLINE_BYTES},
        {"recipient_agent_id":"agent-b","kind":"handoff","content":"Public task.","refs":[
            {**reference, "resource_kind":"source", "resource_id":"https://example.org/private"}]},
        {"recipient_agent_id":"agent-b","kind":"handoff","content":"Public task.","refs":[
            {**reference, "resource_kind":"source", "version":"latest"}]},
    ):
        with pytest.raises(ToolFailure):
            asyncio.run(tool.handler({"tool_call_id":"invalid", "arguments":arguments}))
    assert c.sent == []


def test_explicit_team_refs_are_sent_verbatim_and_ids_stay_stable_across_factory_recreation(tool_case):
    from shared_context.behavior_agent_message_tools import agent_message_capability
    c = tool_case
    arguments = {"recipient_agent_id":"agent-b","kind":"handoff","content":"Read this fixed Team source.","refs":[{
        "resource_kind":"source","visibility":"team","tenant_id":"tenant","team_id":"team",
        "resource_id":"source","version":"1","sha256":"a"*64}]}
    for _ in range(2):
        tool = agent_message_capability(c.session, c.grant)[0][0]
        result = asyncio.run(tool.handler({"tool_call_id":"stable-after-process", "arguments":arguments}))
        assert AgentMessageRead.model_validate_json(result.text).refs[0].resource_kind == "source"
    assert c.sent[0].message_id == c.sent[1].message_id
    assert c.sent[0].refs == c.sent[1].refs


def test_hosted_collaboration_tool_imports_without_api_or_storage_packages():
    source = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"
    script = """
import importlib.abc, sys
class NoApiPackages(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if any(name == prefix or name.startswith(prefix + ".") for prefix in (
            "fastapi", "azure.ai.projects", "azure.storage.blob", "azure.data.tables",
            "shared_context.behavior_runtime", "shared_context.behavior_agent_message_refs"
        )):
            raise ModuleNotFoundError("API-only package in Hosted tool: " + name)
sys.meta_path.insert(0, NoApiPackages())
sys.path.insert(0, sys.argv[1])
import shared_context.behavior_agent_message_tools
print("Hosted collaboration imports with only its existing dependencies")
"""
    result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", script, str(source)],
                            capture_output=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr


def test_pinned_sdk_invokes_the_narrow_peer_tool_without_dispatching_another_agent(tool_case, workspace, runtime_path):
    from shared_context.behavior_agent_message_tools import agent_message_capability
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    from shared_context.m2_copilot import run_copilot
    from test_m2_copilot import ScriptedEndpoint, tool_call
    c = tool_case
    tools, results = agent_message_capability(c.session, c.grant)
    arguments = {"recipient_agent_id":"agent-b","kind":"handoff","content":"Review this public task.","refs":[]}
    endpoint = ScriptedEndpoint([tool_call("send_agent_message", arguments, "original-sdk-call"),
                                 {"content":"The peer reviewed the public task."}])
    endpoint.unbounded = True
    reservations = []

    async def reserve(identity):
        reservations.append(identity)

    output = asyncio.run(run_copilot(
        state_dir=workspace/"state", working_directory=workspace/"work", sdk_session_id="peer-tool-native",
        resume=False, prompt="Send one public task to the configured peer, then report the saved result.",
        tools=tools, api_url="https://context.invalid", token_provider=endpoint.token, on_event=lambda _:None,
        timeout_seconds=30, runtime_path=runtime_path, http_transport=endpoint.transport, instruction=True,
        behavior_boundary=AssistantModelBoundary(reserve, text_reply=True, streaming=True, capability_results=results)))
    assert len(c.sent) == 1 and c.reads == ["original-response-key"]
    assert len(endpoint.requests) == len(reservations) == 2
    tool_messages = [message for message in endpoint.requests[-1]["messages"] if message["role"]=="tool"]
    assert len(tool_messages) == 1
    value = AgentMessageRead.model_validate_json(tool_messages[0]["content"])
    assert value.message.status == "queued" and value.message.decision.model_dispatch_authorized is False
    assert value.message.kind == "result" and value.content == "The public task was reviewed."
    assert output["sdk_stop"]["graceful"] is True
    assert "peer reviewed" in output["final_message"]


@pytest.mark.parametrize("failure", ["recipient", "unknown", "accepted_unknown"])
def test_pinned_sdk_peer_failure_tells_the_model_there_is_no_b_result(
        tool_case, workspace, runtime_path, failure):
    from shared_context.behavior_agent_message_tools import agent_message_capability
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    from shared_context.m2_copilot import run_copilot
    from test_m2_copilot import ScriptedEndpoint, tool_call
    c = tool_case
    if failure == "unknown":
        def lost(message):
            c.sent.append(message)
            raise Problem(503, "AGENT_MESSAGE_UNKNOWN", "PRIVATE_TRANSPORT_DIAGNOSTIC")
        c.session.client.send_agent_message = lost
    elif failure == "accepted_unknown":
        send = c.session.client.send_agent_message
        def accepted(message):
            value = send(message)
            return value.model_copy(update={"response_message_key": None,
                "message": value.message.model_copy(update={"status": "unknown"})})
        c.session.client.send_agent_message = accepted
    tools, results = agent_message_capability(c.session, c.grant)
    arguments = {"recipient_agent_id": "B" if failure == "recipient" else "agent-b",
                 "kind": "handoff", "content": "Review this public task.", "refs": []}
    endpoint = ScriptedEndpoint([tool_call("send_agent_message", arguments, "original-sdk-call"),
                                 {"content": "沒有取得B結果；我不會把自己的計算說成B的回覆。"}])
    endpoint.unbounded = True
    reservations = []

    async def reserve(identity):
        reservations.append(identity)

    output = asyncio.run(run_copilot(
        state_dir=workspace / "state", working_directory=workspace / "work", sdk_session_id="peer-failure-native",
        resume=False, prompt="Ask the configured peer to review the public task; report any failure honestly.",
        tools=tools, api_url="https://context.invalid", token_provider=endpoint.token, on_event=lambda _: None,
        timeout_seconds=30, runtime_path=runtime_path, http_transport=endpoint.transport, instruction=True,
        behavior_boundary=AssistantModelBoundary(reserve, text_reply=True, streaming=True, capability_results=results)))
    assert len(endpoint.requests) == len(reservations) == 2
    tool_messages = [message for message in endpoint.requests[-1]["messages"] if message["role"] == "tool"]
    assert len(tool_messages) == 1
    feedback = tool_messages[0]["content"]
    if failure == "recipient":
        assert "訊息沒有送達B，也沒有任何B結果" in feedback
        assert c.sent == []
    elif failure == "accepted_unknown":
        assert "AGENT_MESSAGE_RESULT_UNCONFIRMED" in feedback
        assert "已接收" in feedback and "B 的結果尚未確認" in feedback
        assert "Authority refused" not in feedback and len(c.sent) == 1
        assert any(event.get("public_progress", {}).get("summary") == "AGENT_MESSAGE_RESULT_UNCONFIRMED"
                   for event in output["events"])
    else:
        assert "AGENT_MESSAGE_UNKNOWN" in feedback and "沒有任何已確認的B結果" in feedback
        assert "訊息沒有送達B" not in feedback and len(c.sent) == 1
    assert "PRIVATE_TRANSPORT_DIAGNOSTIC" not in feedback and c.reads == []
    assert output["sdk_stop"]["graceful"] is True


@pytest.mark.parametrize("failure", ["recipient", "unknown", "accepted_unknown"])
def test_pinned_responses_peer_failure_keeps_the_assigned_recipient_and_honest_feedback(
        tool_case, workspace, runtime_path, failure):
    import json
    import httpx
    from shared_context.behavior_agent_message_tools import agent_message_capability
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    from shared_context.behavior_sdk_files import SdkSessionFiles
    from shared_context.m2_copilot import run_copilot
    from test_instruction_responses_native import response_bytes
    c = tool_case
    requests, reservations = [], []
    if failure == "unknown":
        def lost(message):
            c.sent.append(message)
            raise Problem(503, "AGENT_MESSAGE_UNKNOWN", "PRIVATE_TRANSPORT_DIAGNOSTIC")
        c.session.client.send_agent_message = lost
    elif failure == "accepted_unknown":
        send = c.session.client.send_agent_message
        def accepted(message):
            value = send(message)
            return value.model_copy(update={"response_message_key": None,
                "message": value.message.model_copy(update={"status": "unknown"})})
        c.session.client.send_agent_message = accepted
    tools, results = agent_message_capability(c.session, c.grant)
    arguments = {"recipient_agent_id": "B" if failure == "recipient" else "agent-b",
                 "kind": "handoff", "content": "Review this public task.", "refs": []}

    def provider(request):
        assert str(request.url) == "https://context.invalid/model/v1/responses"
        body = json.loads(request.content)
        requests.append(body)
        tool = next(item for item in body["tools"] if item.get("name") == "send_agent_message")
        assert tool["parameters"]["properties"]["recipient_agent_id"]["enum"] == ["agent-b"]
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
            content=response_bytes(len(requests), tool="send_agent_message" if len(requests) == 1 else None,
                                   arguments=arguments))

    async def reserve(identity):
        reservations.append(identity)

    files = SdkSessionFiles("peer-responses-failure", model_provider="openai-responses")
    output = asyncio.run(run_copilot(
        state_dir=workspace / "state", working_directory=workspace / "work", sdk_session_id="peer-responses-failure",
        resume=False, prompt="Ask the configured peer to review the public task; report any failure honestly.",
        tools=tools, api_url="https://context.invalid", token_provider=lambda: "synthetic-token", on_event=lambda _: None,
        timeout_seconds=30, runtime_path=runtime_path, http_transport=httpx.MockTransport(provider), instruction=True,
        model="gpt-5.4-mini", model_provider="openai-responses", session_files=files,
        behavior_boundary=AssistantModelBoundary(reserve, text_reply=True, streaming=True, capability_results=results)))
    assert len(requests) == len(reservations) == 2
    replies = [item["output"] for item in requests[-1]["input"] if item.get("type") == "function_call_output"]
    assert len(replies) == 1 and c.reads == []
    if failure == "recipient":
        assert "訊息沒有送達B，也沒有任何B結果" in replies[0] and c.sent == []
    elif failure == "accepted_unknown":
        assert "AGENT_MESSAGE_RESULT_UNCONFIRMED" in replies[0]
        assert "已接收" in replies[0] and "B 的結果尚未確認" in replies[0]
        assert "Authority refused" not in replies[0] and len(c.sent) == 1
        assert any(event.get("public_progress", {}).get("summary") == "AGENT_MESSAGE_RESULT_UNCONFIRMED"
                   for event in output["events"])
    else:
        assert "AGENT_MESSAGE_UNKNOWN" in replies[0] and "沒有任何已確認的B結果" in replies[0]
        assert "訊息沒有送達B" not in replies[0] and len(c.sent) == 1
    assert "PRIVATE_TRANSPORT_DIAGNOSTIC" not in replies[0]
    assert output["sdk_stop"]["graceful"] is True
