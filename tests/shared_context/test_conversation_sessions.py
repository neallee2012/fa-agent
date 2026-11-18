"""Conversation-owned Hosted identities; offline SDK/HTTP only."""
from types import SimpleNamespace
import json

import httpx
import pytest

from shared_context.behavior_foundry import FoundryExecutor, FoundryHttpClient, instruction_target
from shared_context.errors import Problem
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_foundry_start_diagnostics import Credential, SessionTransport, inject_sdk
from test_behavior_foundry import TARGET, PERMIT


@pytest.mark.parametrize("missing", [False, True])
def test_existing_session_is_read_without_create_or_silent_replacement(monkeypatch, missing):
    credential = Credential()
    transport = SessionTransport(fail_phase="get_session" if missing else None, fail_status=404)
    inject_sdk(monkeypatch, credential, transport)
    client = FoundryHttpClient(TARGET, credential, transport=httpx.MockTransport(
        lambda _: pytest.fail("verification cannot invoke an agent")))
    try:
        if missing:
            with pytest.raises(Problem) as error:
                client.verify_session(before_send=lambda: None)
            assert error.value.body["error"]["code"] == "CONVERSATION_SESSION_UNAVAILABLE"
        else:
            assert client.verify_session(before_send=lambda: None).agent_session_id == TARGET.session_id
        assert [call["phase"] for call in transport.calls] == ["get_session"]
    finally:
        client.close()


def test_executor_creates_once_reuses_same_sid_and_does_not_stop_shared_turns(monkeypatch):
    creates, verifies, posts = [], [], []
    shared = instruction_target(TARGET, "first", session_id="fa-chat-original")
    records = {job: SimpleNamespace(hosted_session_id=shared.session_id,
        profile=SimpleNamespace(instruction=SimpleNamespace(turn_number=number)))
        for number, job in enumerate(("first", "second"), 1)}
    monkeypatch.setattr(FoundryHttpClient, "create_session",
                        lambda self, **_: creates.append(self.target.session_id))
    monkeypatch.setattr(FoundryHttpClient, "verify_session",
                        lambda self, **_: verifies.append(self.target.session_id))
    def response(request):
        posts.append(request)
        body = json.loads(request.content)
        return httpx.Response(202, headers={"x-agent-invocation-id": "inv-original",
            "x-agent-session-id": request.url.params["agent_session_id"]},
            json={"status": "pending", "launch_id": body["launch_id"], "invocation_id": "inv-original"})
    client = FoundryHttpClient(TARGET, Credential(), transport=httpx.MockTransport(response))
    control = SimpleNamespace(_job_record=lambda job: records[job], check_dispatch=lambda *_: None)
    authority = SimpleNamespace(workers=SimpleNamespace(instruction=lambda _: True, work_policy=control),
        prepare=lambda command: (SimpleNamespace(target=shared, launch_id="launch-"+command.binding.job_id,
                                                 api_binding_id="binding"), PERMIT),
        launch=lambda _: SimpleNamespace(native_invocation_id=None))
    executor = FoundryExecutor.__new__(FoundryExecutor)
    executor.authority, executor.clients = authority, {TARGET.executor_id: client}
    monkeypatch.setattr(executor, "_configured", lambda: None)
    try:
        for job in records:
            command = SimpleNamespace(binding=SimpleNamespace(job_id=job, start_request_id=("a" if job=="first" else "b")*64))
            executor.start(command)
            # A started (claimed) launch; the unclaimed-version-drift branch (45a8be2f) does not apply.
            executor._cleanup_instruction_session(SimpleNamespace(start=command, native_invocation_id="original-" + job))
        # One create; turn 2 verifies before reuse, and every invocation is verified afterwards for
        # version drift (45a8be2f): 1 + 2 reads of the same original session, never a second create.
        assert creates == ["fa-chat-original"] and verifies == ["fa-chat-original"] * 3
        assert [r.url.params["agent_session_id"] for r in posts] == ["fa-chat-original"]*2
    finally:
        client.close()
