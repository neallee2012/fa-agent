"""ADR-0009 M1 2d-i (H2d): a runtime declaring foundry-native-v2 accepts the v1 and v2 headers, labels new
conversations' ``_Meta`` with v2 and keeps v1 items unlabeled and byte-identical; a v1 runtime accepts only v1.

Official local task/store and host; only SDK/model I/O is synthetic (``test_native_turns``)."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from shared_context import native_sdk
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NATIVE_TURN_HEADER, NativeReceipt, canonical_bytes
from shared_context.errors import Problem
from shared_context.native_checkpoint import conversation_store_name, open_durable_store
from shared_context.native_responses_wire import NATIVE_RESPONSES_PROFILE, NATIVE_V2_PROFILE, UNLABELED_STATE_PROFILE
from shared_context.native_turns import TURNS_STORE, NativeTurnRuntime
from test_native_profile_snapshot import H0_FIXTURE
from test_native_responses import ResponsesHarness, build_responses_host, response_submit
from test_native_turn_wire import submit
from test_native_turns import Harness, IDENTITY, OfflineSdk, local_env, native_retry_policy  # noqa: F401

V1, V2 = UNLABELED_STATE_PROFILE, NATIVE_V2_PROFILE


@pytest.fixture
def declared(request, local_env, tmp_path, native_retry_policy):  # noqa: F811
    """A runtime declaring ``request.param`` (what ``FA_NATIVE_TURNS`` sets in Hosted)."""
    sdk = OfflineSdk()
    cache = str((tmp_path / "OFFLINE-cache").resolve())
    runtime = NativeTurnRuntime(IDENTITY, sdk, cache_root=Path("\\\\?\\" + cache if os.name == "nt" else cache),
                                delivery_retry=native_retry_policy, state_profile=request.param)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        yield Harness(runtime, sdk, client)
        sdk.block = False


def post(harness, command, header):
    return harness.client.post("/invocations", json=command.model_dump(mode="json"), headers={
        NATIVE_TURN_HEADER: header, "x-agent-session-id": IDENTITY.session_id,
        "x-agent-invocation-id": "platform-" + uuid4().hex, "x-agent-foundry-call-id": "OFFLINE-dual-call"})


def item(harness, store_name, key):
    async def read():
        async with await open_durable_store(store_name, create=False) as store:
            return (await store.get_item(key)).value

    return harness.call(read)


@pytest.mark.parametrize("declared, header, admitted", [
    (V1, V1, True), (V1, V2, False), (V2, V1, True), (V2, V2, True), (V2, NATIVE_RESPONSES_PROFILE, False),
], indirect=["declared"])
def test_a_v2_runtime_accepts_v1_and_v2_headers_and_a_v1_runtime_only_v1(declared, header, admitted):
    response = post(declared, submit("dual-header"), header)
    if admitted:
        assert response.status_code == 202, response.text
        assert declared.wait("dual-header", "completed").request_id == "dual-header"
        return
    error = response.json()["error"]
    assert response.status_code == 403 and error["code"] == "NATIVE_TURNS_DISABLED", response.text
    assert (error["not_dispatched"], error["reason"]) == (True, "profile")
    assert not declared.sdk.calls
    with pytest.raises(Problem) as absent:
        declared.call(declared.runtime.read, "dual-header")
    assert absent.value.body["error"]["code"] == "NATIVE_RID_MISSING"


@pytest.mark.parametrize("declared", [V2], indirect=True)
def test_the_v1_alias_does_not_follow_the_send_profile(declared, monkeypatch):
    """ADR-0009 2d-ii flips the send constant; a v2 runtime keeps the fixed v1 alias (header and body) until H-alias."""
    from shared_context import behavior_native_turn_wire, native_responses_wire, native_turns

    for module in (native_responses_wire, behavior_native_turn_wire, native_turns):
        monkeypatch.setattr(module, "NATIVE_STATE_PROFILE", V2)
    request = submit("alias-v1").model_copy(update={"state_profile": V1})
    assert post(declared, request, V1).status_code == 202
    assert declared.wait("alias-v1", "completed").state_profile == V1


@pytest.mark.parametrize("declared", [V1], indirect=True)
def test_a_v1_runtime_refuses_a_v2_body_under_the_v1_header(declared):
    """ADR-0009 boundary 8: the decoded body's profile is checked too, so H0-era runtimes store no v2 receipt."""
    response = post(declared, submit("v2-body").model_copy(update={"state_profile": V2}), V1)
    error = response.json()["error"]
    assert response.status_code == 403 and error["code"] == "NATIVE_TURNS_DISABLED", response.text
    assert (error["not_dispatched"], error["reason"]) == (True, "profile")
    assert not declared.sdk.calls
    with pytest.raises(Problem) as absent:
        declared.call(declared.runtime.read, "v2-body")
    assert absent.value.body["error"]["code"] == "NATIVE_RID_MISSING"


@pytest.mark.parametrize("declared", [V2], indirect=True)
def test_v1_receipts_continue_a_v2_labelled_conversation(declared):
    assert post(declared, submit("v2-first"), V1).status_code == 202
    first = declared.wait("v2-first", "completed")
    meta = item(declared, conversation_store_name(first.conversation_id), "meta")
    assert meta["state_profile"] == V2  # _Meta is labelled with the declared profile, written explicitly
    for rid, header in (("v1-next", V1), ("v2-header-next", V2)):
        assert post(declared, submit(rid, first.conversation_id, text="Continue " + rid), header).status_code == 202
        assert declared.wait(rid, "completed").conversation_id == first.conversation_id
    assert [call[0] for call in declared.sdk.calls] == ["v2-first", "v1-next", "v2-header-next"]
    assert all(call[2] is not None for call in declared.sdk.calls[1:])  # each restored the previous checkpoint
    view = declared.call(declared.runtime.conversation, first.conversation_id)
    assert (view.state_profile, view.last_turn, view.last_request_id) == (V2, 3, "v2-header-next")


def test_a_v1_conversation_continues_under_a_v2_body_on_a_v2_runtime(local_env):  # noqa: F811
    """ADR-0009 §3: admission (1234) and the run (1549) compare the v1 ``_Meta`` with a v2 body by channel."""
    first = NativeTurnRuntime(IDENTITY, OfflineSdk(), state_profile=V1)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=first)) as client:
        harness = Harness(first, first.executor, client)
        assert post(harness, submit("v1-base"), V1).status_code == 202
        cid = harness.wait("v1-base", "completed").conversation_id
    second = NativeTurnRuntime(IDENTITY, OfflineSdk(), state_profile=V2)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=second)) as client:
        harness = Harness(second, second.executor, client)
        request = submit("v2-body-next", cid, "Continue").model_copy(update={"state_profile": V2})
        assert post(harness, request, V2).status_code == 202
        assert harness.wait("v2-body-next", "completed").state_profile == V2
        assert item(harness, conversation_store_name(cid), "meta")["state_profile"] == V1
        assert second.executor.calls[0][2] is not None  # the v1 checkpoint was restored


def test_official_recovery_continues_a_v1_root_in_a_v2_labelled_conversation(local_env):  # noqa: F811
    """The API copies the conversation's v2 label into the continuation (``turn_profile``); the v1 root still owns it."""
    from shared_context.behavior_native_turn_wire import NativeContinuationRead, NativeContinuationSubmit
    from shared_context.native_effects import NativeEffects

    first_sdk = OfflineSdk()
    first = NativeTurnRuntime(IDENTITY, first_sdk, state_profile=V2)

    async def tracked(receipt, scope, previous, control):
        await NativeEffects(first, receipt, control).start_operations()
        return await first_sdk(receipt, scope, previous, control)

    first.executor = tracked
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=first)) as client:
        harness = Harness(first, first_sdk, client)
        assert post(harness, submit("labelled-base"), V1).status_code == 202
        cid = harness.wait("labelled-base", "completed").conversation_id
        first_sdk.block = True
        assert post(harness, submit("v1-root", cid, "Continue the original work."), V1).status_code == 202
        root = harness.wait("v1-root", "started")
    assert root.state_profile == V1

    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk, state_profile=V2)
    refused = []

    async def deliver(view):
        conversation = await runtime.conversation(view.conversation_id)
        original = await runtime.read(view.original_request_id)
        try:
            return await runtime.continue_turn(NativeContinuationSubmit(
                command=original.command.model_copy(update={
                    "request_id": view.request_id, "conversation_id": original.conversation_id}),
                capture=original.capture, state_profile=conversation.state_profile, continuation=view.continuation))
        except Problem as error:
            refused.append(error.body["error"]["code"])
            raise

    runtime.on_continuation = deliver
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        recovered = Harness(runtime, sdk, client)
        recovered.wait(root.request_id, "unknown")
        view = recovered.invoke(NativeContinuationRead(request_id=root.request_id)).json()
        assert view["request_id"] is not None and view["continuation"]["continuation_of"] == root.request_id
        derived = recovered.wait(view["request_id"], "completed")
        assert not refused
        assert (derived.state_profile, derived.conversation_id) == (V2, cid)
        assert sdk.calls[0][2] is not None


@pytest.mark.parametrize("declared", [V2], indirect=True)
def test_v1_submit_bytes_and_same_rid_resend_are_unchanged_on_a_v2_runtime(declared):
    request = submit("v1-bytes")
    assert b"state_profile" not in canonical_bytes(request)
    assert post(declared, request, V1).status_code == 202
    completed = declared.wait("v1-bytes", "completed")
    stored = item(declared, TURNS_STORE, "rid:v1-bytes")["receipt"]
    # a receipt always names its profile (v1, as H0 writes it); the digest is over the unlabeled v1 submit
    assert stored["state_profile"] == V1 and stored["submit_digest"] == request.sha256 == completed.submit_digest
    assert canonical_bytes(NativeReceipt.model_validate_json(json.dumps(stored))) == canonical_bytes(completed)
    for header in (V1, V2):  # the same RID under either header is the same submission, never a conflict
        resent = post(declared, request, header)
        assert resent.status_code in {200, 202}, resent.text
        assert (resent.json()["request_id"], resent.json()["submit_digest"]) == ("v1-bytes", request.sha256)
    assert item(declared, TURNS_STORE, "rid:v1-bytes")["receipt"] == stored
    assert [call[0] for call in declared.sdk.calls] == ["v1-bytes"]


@pytest.mark.parametrize("declared", [V2], indirect=True)
def test_the_agent_record_emits_no_key_outside_h0_on_a_v2_runtime(declared):
    """ADR-0009 M1: H0 decodes ``agent`` strictly until 2d-ii, so H2d writes only the fields H0 knows."""
    h0_fields = json.loads(H0_FIXTURE.read_text(encoding="utf-8"))["models"]["native_turns._AgentSession"]["fields"]
    assert post(declared, submit("agent-shape"), V2).status_code == 202
    declared.wait("agent-shape", "completed")
    assert set(item(declared, TURNS_STORE, "agent")) <= set(h0_fields)


@pytest.fixture
def responses_v2(local_env, monkeypatch, tmp_path):  # noqa: F811
    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk, state_profile=V2)
    contexts = {}
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    journal_root = tmp_path / "journal"
    journal_root.mkdir()
    with TestClient(build_responses_host(runtime, journal_root, contexts)) as client:
        yield ResponsesHarness(runtime, sdk, client, contexts)
        sdk.block = False


def test_a_responses_v1_conversation_continues_on_a_v2_runtime(responses_v2):
    assert post(responses_v2, response_submit("resp-1"), V1).status_code == 202
    first = responses_v2.call(responses_v2.runtime.read, "resp-1")
    assert responses_v2.respond(first).status_code == 200
    first = responses_v2.wait("resp-1", "completed")
    assert post(responses_v2, response_submit("resp-2", first.conversation_id, "Continue"), V2).status_code == 202
    second = responses_v2.call(responses_v2.runtime.read, "resp-2")
    assert responses_v2.respond(second).status_code == 200
    assert responses_v2.wait("resp-2", "completed").state_profile == NATIVE_RESPONSES_PROFILE
    meta = item(responses_v2, conversation_store_name(first.conversation_id), "meta")
    assert meta["state_profile"] == NATIVE_RESPONSES_PROFILE  # the Responses channel keeps its own label
    assert [call[0] for call in responses_v2.sdk.calls] == ["resp-1", "resp-2"]


@pytest.mark.parametrize("profile", [V1, V2, NATIVE_RESPONSES_PROFILE, "foundry-native-v3"])
def test_the_runtime_declares_the_environment_profile_and_refuses_any_other(local_env, monkeypatch, tmp_path,  # noqa: F811
                                                                            profile):
    from shared_context.behavior_assistant_hosted import AssistantSupervisor

    runtime_path = tmp_path / "OFFLINE-not-executed"
    runtime_path.write_text("OFFLINE SDK boundary only", encoding="utf-8")
    hosted = SimpleNamespace(
        identity=IDENTITY, supervisor=AssistantSupervisor(tmp_path / ".fa-assistant-v12", runtime_path=runtime_path),
        api=SimpleNamespace(api_url="https://api.offline.invalid", audience="api://OFFLINE"))
    monkeypatch.setattr("azure.identity.aio.ManagedIdentityCredential", lambda client_id=None: None)
    monkeypatch.setenv("FA_NATIVE_TURNS", profile)
    monkeypatch.setenv("FOUNDRY_HOSTING_ENVIRONMENT", "OFFLINE-platform")
    monkeypatch.setenv("AGENTSERVER_TASKS_BACKEND", "hosted")
    monkeypatch.setenv("BEHAVIOR_ASSISTANT_RUNTIME_PATH", str(runtime_path))
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://offline-fixture.services.ai.azure.com/api/projects/offline")
    if profile in {V1, V2}:
        assert native_sdk.native_runtime_from_environment(hosted).state_profile == profile
        return
    with pytest.raises(Problem) as refused:
        native_sdk.native_runtime_from_environment(hosted)
    assert refused.value.body["error"]["code"] == "NATIVE_TURNS_DISABLED"
