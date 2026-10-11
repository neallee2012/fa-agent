"""Witness fixture contracts only: synthetic bytes, no SDK runtime or network."""

import asyncio
from importlib import metadata
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from copilot import CopilotClient
from copilot.session_events import (
    AssistantTurnRetryData, SessionErrorData, ToolExecutionCompleteData,
    ToolExecutionCompleteError, ToolExecutionStartData,
)

from test_skill_isolation_native import (
    CANARY_MARKER, CANARY_NAME, IsolationObservation, ProbeBlocked, WITNESS_URL, WitnessProvider, owned_path,
)


@pytest.fixture(autouse=True)
def no_runtime_or_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("ZERO_RUNTIME_CONTRACT_MUST_NOT_START_PROCESS_OR_SEND_HTTP")

    async def denied_async(*args, **kwargs):
        denied()

    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", denied_async)
    monkeypatch.setattr(CopilotClient, "start", denied_async)
    monkeypatch.setattr(httpx.Client, "send", denied)
    monkeypatch.setattr(httpx.AsyncClient, "send", denied_async)


def provider_request(*, streaming=True, catalog=True, user_names_canary=False):
    body = {
        "model": "gpt-4.1-mini", "stream": streaming,
        "messages": [{"role": "user", "content": CANARY_NAME if user_names_canary else "Read the available fixture."}],
        "tools": [{"type": "function", "function": {
            "name": "skill",
            "description": "Available skills: " + CANARY_NAME if catalog else "Load a named skill.",
            "parameters": {"type": "object", "properties": {"skill": {"type": "string"}}, "required": ["skill"]},
        }}],
    }
    return httpx.Request("POST", WITNESS_URL, json=body)


def witness():
    observation = SimpleNamespace(requests=[], violations=[], blocker=None, diagnostic_hint=None)
    return WitnessProvider(observation), observation


def test_probe_rejects_an_unpinned_sdk_before_creating_task_state(tmp_path, monkeypatch):
    installed_version = metadata.version
    monkeypatch.setattr(metadata, "version", lambda distribution_name: (
        "0.0.0" if distribution_name == "github-copilot-sdk" else installed_version(distribution_name)
    ))
    root = tmp_path / "uncreated"
    with pytest.raises(AssertionError, match="SDK_VERSION_DIFFERS"):
        IsolationObservation(root)
    assert not root.exists()


def test_witness_streamed_response_has_the_expected_readonly_tool_call():
    provider, observation = witness()
    response = asyncio.run(provider.send_request(provider_request(), SimpleNamespace()))
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    frames = [json.loads(line[6:]) for line in response.content.split(b"\n") if line.startswith(b"data: {")]
    call = frames[0]["choices"][0]["delta"]["tool_calls"][0]
    assert call["function"]["name"] == "skill"
    assert json.loads(call["function"]["arguments"]) == {"skill": CANARY_NAME}
    assert frames[1]["choices"][0]["finish_reason"] == "tool_calls"
    assert observation.blocker is None and len(observation.requests) == 1


@pytest.mark.parametrize("streaming", [False, None, "omitted"])
def test_witness_nonstream_response_uses_a_complete_json_message(streaming):
    provider, observation = witness()
    request = provider_request(streaming=streaming)
    if streaming == "omitted":
        packet = json.loads(request.content)
        packet.pop("stream")
        request = httpx.Request("POST", WITNESS_URL, json=packet)
    response = asyncio.run(provider.send_request(request, SimpleNamespace()))
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    completion = response.json()
    assert completion["object"] == "chat.completion"
    assert completion["model"] == "gpt-4.1-mini"
    choice = completion["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] is None
    call = choice["message"]["tool_calls"][0]
    assert set(call) == {"id", "type", "function"}
    assert call["type"] == "function"
    assert call["function"]["name"] == "skill"
    assert json.loads(call["function"]["arguments"]) == {"skill": CANARY_NAME}
    recorded = observation.requests[0]
    assert recorded["requested_streaming"] is (False if streaming is False else None)
    assert recorded["stream_field_present"] is (streaming != "omitted")
    assert recorded["response_content_type"] == "application/json"
    assert observation.blocker is None


@pytest.mark.parametrize("streaming", [0, 1, "true", {"secret": "PRIVATE_STREAM_CANARY"}])
def test_witness_rejects_invalid_stream_flags_without_retaining_their_value(streaming):
    provider, observation = witness()
    with pytest.raises(ProbeBlocked, match="WITNESS_STREAM_SHAPE_UNSUPPORTED"):
        asyncio.run(provider.send_request(provider_request(streaming=streaming), SimpleNamespace()))
    assert observation.requests[0]["requested_streaming"] is None
    assert "PRIVATE_STREAM_CANARY" not in json.dumps(observation.requests)


def test_witness_nonstream_followup_returns_a_complete_final_message():
    provider, observation = witness()

    async def run():
        request = provider_request(streaming=False)
        first = await provider.send_request(request, SimpleNamespace())
        message = first.json()["choices"][0]["message"]
        packet = json.loads(request.content)
        packet["messages"] += [message, {
            "role": "tool", "tool_call_id": message["tool_calls"][0]["id"], "content": CANARY_MARKER,
        }]
        return await provider.send_request(httpx.Request("POST", WITNESS_URL, json=packet), SimpleNamespace())

    final = asyncio.run(run()).json()
    assert final["object"] == "chat.completion"
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["choices"][0]["message"] == {
        "role": "assistant", "content": "The inert isolation witness was observed.",
    }
    assert observation.blocker is None and len(observation.requests) == 2


def test_identical_callback_retry_is_not_classified_as_a_post_tool_turn():
    provider, observation = witness()
    request = provider_request()

    async def run():
        await provider.send_request(request, SimpleNamespace())
        with pytest.raises(ProbeBlocked, match="REPEATED_WITNESS_REQUEST"):
            await provider.send_request(request, SimpleNamespace())

    asyncio.run(run())
    assert len(observation.requests) == 2
    assert len({item["body_sha256"] for item in observation.requests}) == 1
    assert observation.blocker is None
    assert observation.diagnostic_hint == "REPEATED_WITNESS_REQUEST"


def test_user_prompt_name_is_not_evidence_of_model_facing_skill_catalog():
    provider, observation = witness()
    asyncio.run(provider.send_request(provider_request(catalog=False, user_names_canary=True), SimpleNamespace()))
    assert observation.requests[0]["canary_name_visible"] is True
    assert observation.requests[0]["catalog_name_visible"] is False


def test_witness_rejects_websocket_without_constructing_a_forwarder(monkeypatch):
    import copilot.copilot_request_handler as handler

    provider, observation = witness()
    constructed = []
    monkeypatch.setattr(handler, "CopilotWebSocketForwarder", lambda context: constructed.append(True))
    with pytest.raises(ProbeBlocked, match="UNEXPECTED_WEBSOCKET_TRANSPORT"):
        asyncio.run(provider.open_websocket(SimpleNamespace(url="wss://sk12.invalid/unexpected")))
    assert constructed == []
    assert observation.violations == ["UNEXPECTED_WEBSOCKET_TRANSPORT"]


@pytest.mark.parametrize("fault", ["destination", "method"])
def test_witness_rejects_unexpected_http_before_any_response_or_outbound_send(fault):
    provider, observation = witness()
    request = provider_request()
    request = httpx.Request(
        "GET" if fault == "method" else "POST",
        "https://unexpected.invalid/" if fault == "destination" else WITNESS_URL,
        content=request.content,
    )
    with pytest.raises(ProbeBlocked, match="UNEXPECTED_HTTP_DESTINATION"):
        asyncio.run(provider.send_request(request, SimpleNamespace()))
    assert observation.violations == ["UNEXPECTED_HTTP_DESTINATION"]
    assert observation.requests == []


def test_canonical_owned_directory_may_be_valid_before_it_exists(tmp_path):
    root = tmp_path / "owned"
    root.mkdir()
    candidate = root / "data" / "skills"
    assert not candidate.exists()
    assert owned_path(candidate, root) == candidate
    assert not candidate.exists()


@pytest.mark.parametrize("fault", ["relative", "outside", "parent-component"])
def test_unsafe_discovery_path_is_rejected_before_filesystem_resolution(tmp_path, monkeypatch, fault):
    root = tmp_path / "owned"
    root.mkdir()
    candidates = {
        "relative": Path("relative-skills"),
        "outside": tmp_path / "not-owned" / "skills",
        "parent-component": root / ".." / "not-owned",
    }

    def forbidden_resolution(*args, **kwargs):
        raise AssertionError("UNSAFE_PATH_MUST_NOT_BE_RESOLVED")

    with monkeypatch.context() as guard:
        guard.setattr(Path, "resolve", forbidden_resolution)
        with pytest.raises(ProbeBlocked, match="DISCOVERY_ROOT_OUTSIDE_TASK"):
            owned_path(candidates[fault], root)


def test_task_root_reparse_metadata_is_rejected_before_following_it(tmp_path, monkeypatch):
    root = tmp_path / "owned"
    root.mkdir()
    link = root / "link"
    original = Path.is_symlink
    with monkeypatch.context() as guard:
        guard.setattr(Path, "is_symlink", lambda value: value == link or original(value))
        with pytest.raises(ProbeBlocked, match="DISCOVERY_ROOT_REPARSE_POINT"):
            owned_path(link / "skills", root)


@pytest.mark.parametrize("kind", ["assistant.turn_retry", "session.error"])
def test_public_error_observer_retains_only_safe_diagnostic_fields(tmp_path, kind):
    observation = IsolationObservation(tmp_path.resolve())
    callbacks = []
    observation.observe_session(SimpleNamespace(session_id="synthetic-session", on=callbacks.append))
    data = (AssistantTurnRetryData(turn_id="synthetic-turn", reason="PRIVATE_RETRY_REASON_CANARY")
            if kind == "assistant.turn_retry" else SessionErrorData(
                error_type="PRIVATE_ERROR_TYPE_CANARY", error_code="PRIVATE_ERROR_CODE_CANARY",
                status_code=502, message="PRIVATE_MESSAGE_CANARY must not be stored.",
                stack="PRIVATE_STACK_CANARY", url="https://private.invalid/?token=PRIVATE_URL_CANARY",
            ))
    callbacks[0](SimpleNamespace(type=SimpleNamespace(value=kind), data=data))
    assert len(observation.events) == 1
    recorded = observation.events[0]
    assert recorded["type"] == kind
    if kind == "assistant.turn_retry":
        assert recorded["status_code"] is None and len(recorded["reason_sha256"]) == 64
        assert recorded["message_sha256"] is None
    else:
        assert recorded["status_code"] == 502 and recorded["reason_sha256"] is None
        assert all(len(recorded[name + "_sha256"]) == 64 for name in ("error_type", "error_code", "message"))
    assert "stack" not in recorded and "url" not in recorded
    assert "PRIVATE_" not in json.dumps(recorded)


@pytest.mark.parametrize("fault", ["none", "call-id", "arguments", "native-invocation"])
def test_denial_observer_allows_only_the_frozen_attempt_not_a_native_load(tmp_path, fault):
    observation = IsolationObservation(tmp_path.resolve(), denied_skill_probe=True)
    observation.phase = "fa-create"
    callbacks, aborted = [], []

    async def abort():
        aborted.append(True)

    observation.observe_session(SimpleNamespace(session_id="synthetic-session", on=callbacks.append, abort=abort))
    kind = "skill.invoked" if fault == "native-invocation" else "tool.execution_start"
    data = ToolExecutionStartData(
        tool_call_id="other-call" if fault == "call-id" else "unlisted-fa-create",
        tool_name="skill",
        arguments={"skill": "other-skill" if fault == "arguments" else CANARY_NAME},
    ) if kind == "tool.execution_start" else SimpleNamespace()

    async def run():
        callbacks[0](SimpleNamespace(type=SimpleNamespace(value=kind), data=data))
        await asyncio.sleep(0)

    asyncio.run(run())
    assert observation.events[0]["expected_denial_attempt"] is (fault == "none")
    assert observation.violations == ([] if fault == "none" else ["UNEXPECTED_NATIVE_TOOL"])
    assert aborted == ([] if fault == "none" else [True])


def test_public_failed_tool_receipt_keeps_status_and_hashes_without_error_text(tmp_path):
    import hashlib

    observation = IsolationObservation(tmp_path.resolve(), denied_skill_probe=True)
    observation.phase = "fa-create"
    callbacks = []
    observation.observe_session(SimpleNamespace(session_id="synthetic-session", on=callbacks.append))
    data = ToolExecutionCompleteData(
        success=False, tool_call_id="PRIVATE_CALL_MARKER",
        error=ToolExecutionCompleteError(message="PRIVATE_ERROR_MESSAGE", code="PRIVATE_ERROR_CODE"),
    )
    callbacks[0](SimpleNamespace(type=SimpleNamespace(value="tool.execution_complete"), data=data))
    recorded = observation.events[0]
    assert recorded["success"] is False and recorded["error_present"] is True
    assert recorded["tool_call_sha256"] == hashlib.sha256(b"PRIVATE_CALL_MARKER").hexdigest()
    assert recorded["error_code_sha256"] == hashlib.sha256(b"PRIVATE_ERROR_CODE").hexdigest()
    assert "PRIVATE_" not in json.dumps(recorded)
    assert observation.violations == []


@pytest.mark.parametrize("phase", ["fa-create", "fa-resume"])
@pytest.mark.parametrize("receipt", ["unexpected-success", "expected-failure", "other-success"])
def test_exact_unlisted_tool_success_is_a_violation_not_missing_evidence(tmp_path, phase, receipt):
    observation = IsolationObservation(tmp_path.resolve(), denied_skill_probe=True)
    observation.phase = phase
    callbacks, aborted = [], []

    async def abort():
        aborted.append(True)

    observation.observe_session(SimpleNamespace(session_id="synthetic-session", on=callbacks.append, abort=abort))
    data = ToolExecutionCompleteData(
        success=receipt != "expected-failure",
        tool_call_id="other-call" if receipt == "other-success" else "unlisted-" + phase,
        error=ToolExecutionCompleteError(message="Synthetic denial") if receipt == "expected-failure" else None,
    )

    async def run():
        callbacks[0](SimpleNamespace(type=SimpleNamespace(value="tool.execution_complete"), data=data))
        await asyncio.sleep(0)

    asyncio.run(run())
    assert observation.events[0]["success"] is (receipt != "expected-failure")
    assert observation.violations == (["UNLISTED_NATIVE_TOOL_SUCCEEDED"] if receipt == "unexpected-success" else [])
    assert aborted == ([True] if receipt == "unexpected-success" else [])


@pytest.mark.parametrize("ending,expected_status", [
    ("capability-blocked", "BLOCKED"),
    ("violation-and-blocker", "FAILED"),
    ("sdk-error-with-blocker-hint", "FAILED"),
    ("error-after-completed-flag", "FAILED"),
])
def test_probe_proof_matches_actual_termination_not_a_stale_hint(
    tmp_path, tmp_path_factory, monkeypatch, ending, expected_status,
):
    import test_skill_isolation_native as probe

    proof_root = tmp_path / "safe-proof"
    monkeypatch.setenv("FA_SK12_ISOLATION_EVIDENCE_DIR", str(proof_root))

    async def synthetic_witness(observation, runtime):
        observation.blocker = "SYNTHETIC_DIAGNOSTIC_HINT"
        if ending == "violation-and-blocker":
            observation.violations.append("UNEXPECTED_HTTP_DESTINATION")
        if ending == "error-after-completed-flag":
            observation.completed = True
        if ending in {"sdk-error-with-blocker-hint", "error-after-completed-flag"}:
            raise RuntimeError("SYNTHETIC_ACTUAL_FAILURE")
        raise ProbeBlocked("SYNTHETIC_CAPABILITY_NOT_AVAILABLE")

    monkeypatch.setattr(probe, "establish_witness", synthetic_witness)
    with pytest.raises(BaseException) as terminal:
        probe.test_task_local_personal_canary_is_visible_then_excluded_from_fa_create_and_cold_resume(
            None, tmp_path / "runtime-must-not-start", monkeypatch, tmp_path_factory,
        )
    recorded = json.loads((proof_root / "isolation-proof.json").read_text(encoding="utf-8"))
    assert recorded["status"] == expected_status
    if ending == "capability-blocked":
        assert isinstance(terminal.value, pytest.skip.Exception)
        assert recorded["blocker"] == "SYNTHETIC_CAPABILITY_NOT_AVAILABLE"
    elif ending == "violation-and-blocker":
        assert isinstance(terminal.value, AssertionError)
        assert recorded["blocker"] is None
    else:
        assert isinstance(terminal.value, RuntimeError)
        assert recorded["blocker"] is None
    assert recorded["runtime_starts"] == recorded["runtime_stops"] == []
