"""Fake SDK/runtime and transport tests: no child process or network connection."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject

from shared_context.m2_copilot import MODEL, ToolOutput, ToolSpec, _ModelGateway, run_copilot


SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
USAGE = {"type": "assistant.usage", "data": {"input_tokens": 123, "output_tokens": 7}}


@pytest.mark.parametrize("milestone,prefix", [("m2", ""), ("m3", "/m3"), ("m4", "/m4")])
@pytest.mark.parametrize("resume", [False, True])
def test_sdk_prefix_and_session_contract_preserve_auth_discovery_and_tool_permissions(
    tmp_path, monkeypatch, milestone, prefix, resume,
):
    clients, requests, events, tool_packets = [], [], [], []
    monkeypatch.setenv("GITHUB_TOKEN", "poisoned-host-token")
    monkeypatch.setenv("CONTEXT_M4_RUN_ID", "must-not-reach-child")

    async def handler(packet):
        tool_packets.append(packet)
        return ToolOutput("synthetic context")

    class FakeSession:
        session_id = "same-sdk-session"

        def __init__(self, options, client):
            self.options, self.client = options, client

        async def send_and_wait(self, prompt, timeout):
            options = self.options
            assert prompt == "Read current context using the permitted tool."
            assert options["provider"]["base_url"] == f"https://context.invalid{prefix}/model/v1"
            assert options["provider"]["model_id"] == MODEL
            assert options["enable_config_discovery"] is False
            assert options["enable_managed_settings"] is False
            assert options["mcp_servers"] == {}
            for field in ("custom_agents", "skill_directories", "plugin_directories", "instruction_directories"):
                assert options[field] == []
            assert options["excluded_tools"] is not None
            assert len(options["tools"]) == 1 and options["tools"][0].name == "read_context"
            assert options["tools"][0].skip_permission is False
            permission = options["on_permission_request"]
            request = SimpleNamespace(
                kind="custom-tool", tool_name="read_context", tool_call_id="call-1",
                managed_approval_required=False, args={},
            )
            assert isinstance(permission(request, {"session_id": self.session_id}), PermissionDecisionApproveOnce)
            for changes in (
                {"kind": "shell"}, {"tool_name": "bash"}, {"args": {"path": "arbitrary-file"}},
                {"managed_approval_required": True},
            ):
                denied = SimpleNamespace(**{**vars(request), **changes})
                assert isinstance(permission(denied, {"session_id": self.session_id}), PermissionDecisionReject)
            assert isinstance(permission(request, {"session_id": "foreign-session"}), PermissionDecisionReject)
            result = await options["tools"][0].handler(SimpleNamespace(
                session_id=self.session_id, tool_call_id="call-1", tool_name="read_context", arguments={},
            ))
            assert result.text_result_for_llm == "synthetic context"
            token = await options["provider"]["bearer_token_provider"](None)
            assert token == "synthetic-api-token"
            response = await self.client.kwargs["request_handler"].send_request(
                httpx.Request("POST", f"https://context.invalid{prefix}/model/v1/chat/completions",
                              headers={"Authorization": f"Bearer {token}"},
                              json={"model": MODEL, "messages": [{"role": "user", "content": prompt}]}),
                SimpleNamespace(request_id="request-1", session_id=self.session_id),
            )
            assert response.status_code == 200
            options["on_event"](USAGE)
            return SimpleNamespace(data=SimpleNamespace(content="synthetic completion"))

        async def get_events(self):
            return [USAGE]

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs, self.stopped, self.resumed, self.created = kwargs, False, None, None
            clients.append(self)

        async def start(self):
            assert self.kwargs["use_logged_in_user"] is False and self.kwargs["mode"] == "empty"
            assert "GITHUB_TOKEN" not in self.kwargs["env"] and "CONTEXT_M4_RUN_ID" not in self.kwargs["env"]

        async def create_session(self, session_id, **options):
            self.created = session_id
            return FakeSession(options, self)

        async def resume_session(self, session_id, continue_pending_work, **options):
            assert continue_pending_work is False
            self.resumed = session_id
            return FakeSession(options, self)

        async def stop(self):
            self.stopped = True

    async def send(request):
        requests.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"synthetic_transport": True})

    monkeypatch.setattr("shared_context.m2_copilot.CopilotClient", FakeClient)
    work = tmp_path / "work"
    work.mkdir()
    result = asyncio.run(run_copilot(
        state_dir=tmp_path / "sdk", working_directory=work, sdk_session_id="same-sdk-session",
        resume=resume, prompt="Read current context using the permitted tool.",
        tools=[ToolSpec("read_context", "Read context.", SCHEMA, handler)], api_url="https://context.invalid",
        token_provider=lambda: "synthetic-api-token", on_event=events.append,
        runtime_path=Path(__file__), http_transport=httpx.MockTransport(send), milestone=milestone,
    ))
    client = clients[0]
    assert client.stopped and (client.resumed if resume else client.created) == "same-sdk-session"
    assert result["sdk_session_id"] == "same-sdk-session"
    assert result["history"] == [USAGE] and USAGE in result["events"] and USAGE in events
    assert len(requests) == 1 and requests[0][0] == f"https://context.invalid{prefix}/model/v1/chat/completions"
    assert requests[0][1]["max_tokens"] == 1024
    assert tool_packets == [{"session_id": "same-sdk-session", "tool_call_id": "call-1",
                             "tool_name": "read_context", "arguments": {}}]
    assert "synthetic-api-token" not in json.dumps(result)


@pytest.mark.parametrize("hook_fails", [False, True])
def test_reply_hook_sees_final_parts_before_the_runtime_stops_and_never_changes_the_result(
        tmp_path, monkeypatch, hook_fails):
    order, events = [], []

    class FakeSession:
        session_id = "same-sdk-session"

        async def send_and_wait(self, prompt, timeout):
            return SimpleNamespace(data=SimpleNamespace(content="synthetic completion"))

        async def get_events(self):
            return []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def start(self):
            pass

        async def create_session(self, session_id, **options):
            return FakeSession()

        async def stop(self):
            order.append("stop")

    async def on_reply(parts):
        order.append(("reply", parts))
        if hook_fails:
            raise RuntimeError("synthetic preview store failure")

    monkeypatch.setattr("shared_context.m2_copilot.CopilotClient", FakeClient)
    work = tmp_path / "work"
    work.mkdir()
    result = asyncio.run(run_copilot(
        state_dir=tmp_path / "sdk", working_directory=work, sdk_session_id="same-sdk-session", resume=False,
        prompt="Read context.", tools=[], api_url="https://context.invalid",
        token_provider=lambda: "synthetic-api-token", on_event=events.append, runtime_path=Path(__file__),
        http_transport=httpx.MockTransport(lambda request: httpx.Response(500)), on_reply=on_reply,
    ))
    assert order == [("reply", ["synthetic completion"]), "stop"]
    assert result["final_message"] == "synthetic completion" and result["final_parts"] == ["synthetic completion"]
    failed = [event for event in events if event.get("type") == "m2.reply_preview_failed"]
    assert failed == ([{"type": "m2.reply_preview_failed", "data": {"exception_type": "RuntimeError"}}]
                      if hook_fails else [])


@pytest.mark.parametrize("milestone", ["m5", "", "../m3", "M4"])
def test_sdk_does_not_accept_arbitrary_model_prefix(tmp_path, milestone):
    with pytest.raises(ValueError, match="model milestone"):
        asyncio.run(run_copilot(
            state_dir=tmp_path, working_directory=tmp_path, sdk_session_id="synthetic-session",
            resume=False, prompt="Read context.", tools=[], api_url="https://context.invalid",
            token_provider=lambda: pytest.fail("token must not be requested"), on_event=lambda event: None,
            runtime_path=Path(__file__), milestone=milestone,
        ))


@pytest.mark.parametrize("failure", ["transport", "http_status", "duplicate"])
def test_m4_model_failure_and_duplicate_payloads_cannot_replay(failure):
    calls, events = [], []
    endpoint = httpx.URL("https://context.invalid/m4/model/v1/chat/completions")

    async def send(request):
        calls.append(request)
        if failure == "transport":
            raise httpx.ConnectError("synthetic transport failure", request=request)
        return httpx.Response(503 if failure == "http_status" else 200, json={})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
            gateway = _ModelGateway(endpoint, http, events.append)
            ctx = SimpleNamespace(request_id="synthetic-request", session_id="same-sdk-session")

            def request(content="original"):
                return httpx.Request("POST", endpoint, json={
                    "model": MODEL, "messages": [{"role": "user", "content": content}],
                })

            if failure == "transport":
                with pytest.raises(RuntimeError, match="transport failed"):
                    await gateway.send_request(request(), ctx)
            else:
                await gateway.send_request(request(), ctx)
            with pytest.raises(RuntimeError, match="replay refused" if failure == "duplicate" else "retry refused"):
                await gateway.send_request(request("original" if failure == "duplicate" else "different"), ctx)

    asyncio.run(run())
    assert len(calls) == 1
    assert any(event["type"] == "m2.provider_failed" for event in events)


@pytest.mark.parametrize("path", [
    "/model/v1/chat/completions", "/m3/model/v1/chat/completions",
    "/m4/model/v1/budget", "/m4/model/v1/chat/completions/extra",
])
def test_m4_gateway_rejects_m2_m3_and_other_model_destinations(path):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: pytest.fail("denied destination must never reach HTTP"),
        )) as http:
            gateway = _ModelGateway(httpx.URL("https://context.invalid/m4/model/v1/chat/completions"), http, lambda _: None)
            with pytest.raises(RuntimeError, match="destination is not allowed"):
                await gateway.send_request(
                    httpx.Request("POST", "https://context.invalid" + path, json={"model": MODEL}),
                    SimpleNamespace(request_id="request-1", session_id="same-session"),
                )
            assert gateway.attempts == 0

    asyncio.run(run())


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_sdk_turn_phases_share_the_turn_timing_id_and_record_a_failed_outcome(
    tmp_path, monkeypatch, caplog, resume, fails,
):
    from shared_context.behavior_timing import work_timing

    class FakeSession:
        session_id = "timed-session"

        async def send_and_wait(self, prompt, timeout):
            if fails:
                raise OSError("offline model failure")
            return SimpleNamespace(data=SimpleNamespace(content="synthetic completion"))

        async def get_events(self):
            return [USAGE]

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def start(self):
            pass

        async def create_session(self, session_id, **options):
            return FakeSession()

        async def resume_session(self, session_id, continue_pending_work, **options):
            return FakeSession()

        async def stop(self):
            pass

    async def turn():
        # As native_executor does: phases inside the SDK turn inherit its timing ID.
        with work_timing("job-sdk"):
            return await run_copilot(
                state_dir=tmp_path / "sdk", working_directory=tmp_path, sdk_session_id="timed-session",
                resume=resume, prompt="Answer.", tools=[], api_url="https://context.invalid",
                token_provider=lambda: "synthetic-api-token", on_event=lambda event: None,
                runtime_path=Path(__file__),
            )

    monkeypatch.setattr("shared_context.m2_copilot.CopilotClient", FakeClient)
    caplog.set_level(logging.WARNING, logger="shared_context.behavior_timing")
    if fails:
        with pytest.raises(RuntimeError, match="Copilot turn failed"):
            asyncio.run(turn())
    else:
        assert asyncio.run(turn())["final_message"] == "synthetic completion"
    timings = {value["phase"]: value for value in (
        json.loads(record.getMessage().removeprefix("FA_TIMING ")) for record in caplog.records
        if record.getMessage().startswith("FA_TIMING "))}
    opened = "sdk_session_resume" if resume else "sdk_session_create"
    expected = {"sdk_process_start", opened, "sdk_send_until_idle", "sdk_process_stop"}
    assert set(timings) == expected | (set() if fails else {"sdk_get_events"})
    assert {value["job_id"] for value in timings.values()} == {"job-sdk"}
    assert timings["sdk_send_until_idle"]["outcome"] == ("failed" if fails else "returned")
    assert all(timings[name]["outcome"] == "returned" for name in timings if name != "sdk_send_until_idle")
    assert "offline model failure" not in caplog.text
