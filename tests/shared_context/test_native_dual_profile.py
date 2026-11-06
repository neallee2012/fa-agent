"""ADR-0009 M1: a runtime declaring foundry-native-v2 accepts only the v2 header (the v1 alias is retired), still
admits an unlabeled v1 body (a stored v1 original resent with its digest), labels new conversations' ``_Meta`` with v2
and keeps v1 items unlabeled and byte-identical; a v1 runtime (no longer declarable) accepts only v1. ADR-0009 3d-i:
a v3 runtime admits v1/v2 bodies (permanent) and labels new ``_Meta`` v3; 3d step 3 retired its v2 header alias.

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
from shared_context.native_responses_wire import (
    NATIVE_RESPONSES_PROFILE, NATIVE_V2_PROFILE, NATIVE_V3_PROFILE, UNLABELED_STATE_PROFILE)
from shared_context.native_turns import TURNS_STORE, NativeTurnRuntime
from test_native_profile_snapshot import H0_FIXTURE
from test_native_responses import ResponsesHarness, build_responses_host, response_submit
from test_native_turn_wire import submit
from test_native_turns import Harness, IDENTITY, OfflineSdk, local_env, native_retry_policy

V1, V2, V3 = UNLABELED_STATE_PROFILE, NATIVE_V2_PROFILE, NATIVE_V3_PROFILE


@pytest.fixture
def declared(request, local_env, tmp_path, native_retry_policy):
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
    (V2, V1, False), (V2, V2, True), (V2, NATIVE_RESPONSES_PROFILE, False), (V2, V3, False),
    # ADR-0009 3d step 3: the v2 header alias 3d-i added is retired with the v2-sending API
    (V3, V3, True), (V3, V2, False), (V3, V1, False), (V3, NATIVE_RESPONSES_PROFILE, False),
], indirect=["declared"])
def test_each_runtime_accepts_only_its_own_header(declared, header, admitted):
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
def test_a_v1_body_is_still_admitted_under_the_v2_header(declared):
    """ADR-0009 M1: the v1 header alias is retired, but a stored v1 original is resent with its own (unlabeled) body."""
    request = submit("original-v1").model_copy(update={"state_profile": V1})
    assert post(declared, request, V2).status_code == 202
    assert declared.wait("original-v1", "completed").state_profile == V1


@pytest.mark.parametrize("declared", [V2], indirect=True)
def test_v1_receipts_continue_a_v2_labelled_conversation(declared):
    assert post(declared, submit("v2-first"), V2).status_code == 202
    first = declared.wait("v2-first", "completed")
    meta = item(declared, conversation_store_name(first.conversation_id), "meta")
    assert meta["state_profile"] == V2  # _Meta is labelled with the declared profile, written explicitly
    for rid, header in (("v1-next", V2), ("v2-header-next", V2)):
        assert post(declared, submit(rid, first.conversation_id, text="Continue " + rid), header).status_code == 202
        assert declared.wait(rid, "completed").conversation_id == first.conversation_id
    assert [call[0] for call in declared.sdk.calls] == ["v2-first", "v1-next", "v2-header-next"]
    assert all(call[2] is not None for call in declared.sdk.calls[1:])  # each restored the previous checkpoint
    view = declared.call(declared.runtime.conversation, first.conversation_id)
    assert (view.state_profile, view.last_turn, view.last_request_id) == (V2, 3, "v2-header-next")


def test_a_v1_conversation_continues_under_a_v2_body_on_a_v2_runtime(local_env):
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


@pytest.mark.parametrize("declared, body, admitted", [
    (V3, V1, True), (V3, V2, True), (V3, V3, True), (V2, V3, False),
], indirect=["declared"])
def test_stored_v1_and_v2_bodies_stay_admitted_on_a_v3_runtime(declared, body, admitted):
    """ADR-0009 3d-i: body profiles are separate from the header; a stored v1 or v2 original keeps its own."""
    request = submit("body-" + body[-2:]).model_copy(update={"state_profile": body})
    response = post(declared, request, declared.runtime.state_profile)
    if not admitted:
        assert response.status_code == 403 and response.json()["error"]["reason"] == "profile", response.text
        return
    assert response.status_code == 202, response.text
    completed = declared.wait(request.command.request_id, "completed")
    meta = item(declared, conversation_store_name(completed.conversation_id), "meta")
    assert (completed.state_profile, meta["state_profile"]) == (body, V3)  # a new conversation is labelled v3


def test_a_v2_conversation_continues_on_a_v3_runtime(local_env):
    """ADR-0009 3d-i: a conversation moved from a v2 SID keeps its v2 ``_Meta``; the v3 runtime reads it by channel."""
    first = NativeTurnRuntime(IDENTITY, OfflineSdk(), state_profile=V2)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=first)) as client:
        harness = Harness(first, first.executor, client)
        request = submit("v2-base").model_copy(update={"state_profile": V2})
        assert post(harness, request, V2).status_code == 202
        cid = harness.wait("v2-base", "completed").conversation_id
    second = NativeTurnRuntime(IDENTITY, OfflineSdk(), state_profile=V3)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=second)) as client:
        harness = Harness(second, second.executor, client)
        request = submit("v2-next", cid, "Continue").model_copy(update={"state_profile": V2})
        assert post(harness, request, V3).status_code == 202
        assert harness.wait("v2-next", "completed").state_profile == V2
        assert item(harness, conversation_store_name(cid), "meta")["state_profile"] == V2
        assert second.executor.calls[0][2] is not None  # the v2 checkpoint was restored
        assert harness.call(second.conversation, cid).state_profile == V2


def test_official_recovery_continues_a_v1_root_in_a_v2_labelled_conversation(local_env):
    """The API copies the conversation's v2 label into the continuation (``turn_profile``); the v1 root still owns it.
    Recovery runs on the v3 runtime the conversation moved to (ADR-0009 3d-ii: the API reaches no v2 runtime)."""
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
        assert post(harness, submit("labelled-base"), V2).status_code == 202
        cid = harness.wait("labelled-base", "completed").conversation_id
        first_sdk.block = True
        assert post(harness, submit("v1-root", cid, "Continue the original work."), V2).status_code == 202
        root = harness.wait("v1-root", "started")
    assert root.state_profile == V1

    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk, state_profile=V3)
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
    assert post(declared, request, V2).status_code == 202
    completed = declared.wait("v1-bytes", "completed")
    stored = item(declared, TURNS_STORE, "rid:v1-bytes")["receipt"]
    # a receipt always names its profile (v1, as H0 writes it); the digest is over the unlabeled v1 submit
    assert stored["state_profile"] == V1 and stored["submit_digest"] == request.sha256 == completed.submit_digest
    assert canonical_bytes(NativeReceipt.model_validate_json(json.dumps(stored))) == canonical_bytes(completed)
    resent = post(declared, request, V2)  # the same RID is the same submission, never a conflict
    assert resent.status_code in {200, 202}, resent.text
    assert (resent.json()["request_id"], resent.json()["submit_digest"]) == ("v1-bytes", request.sha256)
    assert post(declared, request, V1).status_code == 403  # the retired v1 header alias
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
def responses_v2(local_env, monkeypatch, tmp_path):
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
    assert post(responses_v2, response_submit("resp-1"), V2).status_code == 202
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


@pytest.mark.parametrize("profile", [V1, V2, NATIVE_V3_PROFILE, NATIVE_RESPONSES_PROFILE, "foundry-native-v4"])
def test_the_runtime_declares_the_environment_profile_and_refuses_any_other(local_env, monkeypatch, tmp_path,
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
    # ADR-0009 M1: v1 is no longer declarable; 3d-ii: nor is v2, so only v3 starts.
    if profile == NATIVE_V3_PROFILE:
        assert native_sdk.native_runtime_from_environment(hosted).state_profile == profile
        return
    with pytest.raises(Problem) as refused:
        native_sdk.native_runtime_from_environment(hosted)
    assert refused.value.body["error"]["code"] == "NATIVE_TURNS_DISABLED"
    assert "must be explicitly enabled" in refused.value.body["error"]["message"]


@pytest.mark.parametrize("declared", [None, "pin", "1.0.13/1.0.83", "1.0.26", "undeclared-newer"])
def test_the_runtime_refuses_a_version_that_declares_another_sdk_release(local_env, monkeypatch, tmp_path, declared):
    """ADR-0009 §3 (c): the API trusts the pair a version's definition declares (FA_SDK_RELEASE) for Guard A/B, so an
    image under a definition naming another pair never starts; an undeclared one (published before it) still does."""
    from shared_context.behavior_assistant_hosted import AssistantSupervisor
    from shared_context.sdk_pin import RUNTIME_VERSION, SDK_VERSION

    runtime_path = tmp_path / "OFFLINE-not-executed"
    runtime_path.write_text("OFFLINE SDK boundary only", encoding="utf-8")
    hosted = SimpleNamespace(
        identity=IDENTITY, supervisor=AssistantSupervisor(tmp_path / ".fa-assistant-v12", runtime_path=runtime_path),
        api=SimpleNamespace(api_url="https://api.offline.invalid", audience="api://OFFLINE"))
    monkeypatch.setattr("azure.identity.aio.ManagedIdentityCredential", lambda client_id=None: None)
    for key, value in {"FA_NATIVE_TURNS": NATIVE_V3_PROFILE, "FOUNDRY_HOSTING_ENVIRONMENT": "OFFLINE-platform",
                       "AGENTSERVER_TASKS_BACKEND": "hosted", "BEHAVIOR_ASSISTANT_RUNTIME_PATH": str(runtime_path),
                       "FOUNDRY_PROJECT_ENDPOINT": "https://offline-fixture.services.ai.azure.com/api/projects/offline"
                       }.items():
        monkeypatch.setenv(key, value)
    if declared in {None, "undeclared-newer"}:
        monkeypatch.delenv("FA_SDK_RELEASE", raising=False)
        if declared:  # a newer image under a definition that declares nothing: the API takes it for 1.0.26/1.0.103
            monkeypatch.setattr(native_sdk, "PINNED_RELEASE_TEXT", "1.0.28/1.0.105")
    else:
        monkeypatch.setenv("FA_SDK_RELEASE", f"{SDK_VERSION}/{RUNTIME_VERSION}" if declared == "pin" else declared)
    if declared in {None, "pin"}:
        assert native_sdk.native_runtime_from_environment(hosted).state_profile == NATIVE_V3_PROFILE
        return
    with pytest.raises(Problem) as refused:
        native_sdk.native_runtime_from_environment(hosted)
    assert refused.value.body["error"]["code"] == "NATIVE_TURNS_DISABLED"
    assert "another SDK/runtime release" in refused.value.body["error"]["message"]
