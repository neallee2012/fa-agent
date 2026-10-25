"""Signed API -> original Foundry transport -> synthetic N1 replies. No cloud or model."""

import hashlib
import json
import asyncio
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from shared_context.behavior_api import _install_error_handlers
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_foundry import FoundryHttpClient, FOUNDRY_TOKEN_SCOPE
from shared_context.behavior_native_sessions import _RetiredSession
from shared_context.behavior_native_turn_api import NativeTurnGateway, PREFIX
from shared_context.behavior_native_turn_wire import (
    NATIVE_REQUEST, NATIVE_STATE_PROFILE, NATIVE_TURN_HEADER, NativeConversation, NativeHistoryPage,
    NativePublicTurn, NativeReceipt, NativeResultPage, NativeResultReference,
    NativeSessionState, NativeSessionPreparation,
)
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from test_conversation_transfer_startup import synthetic_standing_v1
from test_m3_auth import jwk_client, signing_key, token
from test_instruction_flow import instruction_models
from test_team_agent_routing import team_agent, _register_scope, _agent
from test_native_turn_wire import submit as original_submit


class Credential:
    def get_token(self, scope):
        assert scope == FOUNDRY_TOKEN_SCOPE
        return SimpleNamespace(token="synthetic-native-token")


class NativeTransport:
    def __init__(self, target):
        self.target, self.calls, self.records = target, [], {}
        self.error = None
        self.reply_override = None
        self.lose_submit = False
        self.text = "\u539f\u6587\n" * 20_000
        self.single_text = "\u539f\u6587"
        self.preview = None
        self.finished_at = datetime(2026, 10, 5, 13, 21, 8, tzinfo=UTC)
        self.preparation = None
        self.session_binding = target.session_id
        self.session_etag = "original-agent-etag"
        self.environment = None
        self.following = None
        self.actual_version = None  # a newer image names its own version in the session state (ADR-0009 §1)
        # ADR-0009 §4: the version this VM runs. When set, a request expecting another version is refused before
        # anything is read, as Hosted's ``require_expected_version`` does: a newer image names ``not_dispatched``,
        # ``reason`` and ``actual_version``; ``old_image`` refuses with the bare code.
        self.vm_version = None
        self.old_image = False

    def __call__(self, request):
        command = NATIVE_REQUEST.validate_json(request.content)
        self.calls.append((request, command))
        expected = request.headers.get("x-client-fa-expected-version")
        if self.vm_version is not None and expected != self.vm_version:
            body = {"code": "NATIVE_SESSION_CHANGED", "message": "private"}
            if not self.old_image:
                body.update(not_dispatched=True, reason="version", actual_version=self.vm_version)
            return httpx.Response(409, json={"error": body})
        assert request.method == "POST" and request.url.path.endswith("/endpoint/protocols/invocations")
        assert dict(request.url.params) == {"api-version": "v1", "agent_session_id": self.target.session_id}
        assert request.headers[NATIVE_TURN_HEADER] == NATIVE_STATE_PROFILE
        for header in ("x-agent-invocation-id", "x-agent-foundry-call-id", "x-client-fa-launch-permit"):
            assert header not in request.headers
        if self.error is not None:
            status, code = self.error
            return httpx.Response(status, json={"error": {"code": code, "message": "private upstream details"}})
        if self.reply_override is not None and command.operation not in {"session_read", "session_prepare"}:
            value = self.reply_override(command)
        elif command.operation == "session_read":
            value = NativeSessionState(agent_id=self.target.agent_id, hosted_session_id=self.session_binding,
                                       etag=self.session_etag, preparation=self.preparation,
                                       environment=self.environment, following=self.following,
                                       actual_version=self.actual_version)
        elif command.operation == "session_prepare":
            if self.preparation is None:
                assert (command.expected_session_id, command.expected_etag) == (self.session_binding, self.session_etag)
                self.preparation = NativeSessionPreparation(
                    agent_id=self.target.agent_id, request_id=command.request_id,
                    previous_hosted_session_id=self.session_binding if self.session_binding != self.target.session_id else None,
                    hosted_session_id=self.target.session_id, hosted_agent_version=self.target.agent_version)
                self.session_binding, self.session_etag = self.target.session_id, "prepared-agent-etag"
            value = self.preparation
        elif command.operation == "read":
            value = self.records.get(command.request_id)
            if value is None:
                return httpx.Response(404, json={"error": {"code": "NATIVE_RID_MISSING"}})
        elif command.operation in {"submit", "routine_receive"}:
            # Like Hosted `_submit_locked`: `rid:<RID>` create-or-read is the only dedup.
            existing = self.records.get(command.command.request_id)
            if existing is not None and existing.submit_digest != command.sha256:
                return httpx.Response(409, json={"error": {"code": "NATIVE_RID_CONFLICT", "message": "private"}})
            if (existing is not None and existing.status == "accepted"
                    and existing.hosted_session_id != self.target.session_id):
                return httpx.Response(409, json={"error": {"code": "NATIVE_SESSION_CHANGED", "message": "private"}})
            value = existing or NativeReceipt(
                agent_id=self.target.agent_id, hosted_session_id=self.target.session_id,
                request_id=command.command.request_id,
                # Like Hosted: a routine run without a command CID opens the one its origin names.
                conversation_id=command.command.conversation_id or (
                    command.routine.conversation_id if command.operation == "routine_receive" else "native-cid"),
                command=command.command, capture=command.capture, submit_digest=command.sha256,
                capture_digest=command.capture.sha256, status="accepted", state_profile=command.state_profile,
                routine=getattr(command, "routine", None),
            )
            self.records[value.request_id] = value
            if self.lose_submit:
                raise httpx.ReadTimeout("private lost response", request=request)
        elif command.operation == "user-image-upload":
            from shared_context.behavior_user_images import stored
            value = self.upload_reply(command) if getattr(self, "upload_reply", None) else stored(
                self.target.agent_id, command.png_base64)
        elif command.operation in {"conversation", "history"}:
            original = next(value for value in self.records.values() if value.conversation_id == command.conversation_id)
            value = NativeConversation(
                agent_id=self.target.agent_id, hosted_session_id=original.hosted_session_id,
                conversation_id=original.conversation_id,
                last_turn=original.turn_number or 0, last_request_id=original.request_id,
                capture=original.capture, capture_digest=original.capture_digest,
            )
            if command.operation == "history":
                turns = () if original.result is None or command.after_turn >= original.turn_number else (
                    NativePublicTurn(conversation_id=original.conversation_id, request_id=original.request_id,
                                     turn_number=original.turn_number,
                                     user_text=original.command.content if original.source is None else None,
                                     agent_text=original.command.content if original.source is not None else None,
                                     source=original.source,
                                     result=original.result),)
                value = NativeHistoryPage(conversation=value, turns=turns, next_after_turn=None)
        elif command.operation == "result":
            original = self.records[command.request_id]
            split = len(self.text) // 2
            value = NativeResultPage(
                conversation_id=original.conversation_id, request_id=original.request_id,
                result=original.result, part=command.part, complete=False,
                text=self.text[:split] if command.part == 0 else self.text[split:],
                next_part=1 if command.part == 0 else None,
            )
        elif command.operation == "turn_status":
            from shared_context.behavior_native_turn_wire import NativeTurnStatus
            original = self.records[command.request_id]
            done = original.status == "completed" and original.result is not None
            single = done and original.result.text_parts == 1
            # model_construct lets the double send what a faulty Hosted might, so the API's own check is tested.
            value = NativeTurnStatus.model_construct(
                schema_version="fa-native-turn-status-v1", receipt=original, progress=None,
                text=(self.single_text if single else None),
                next_part=0 if done and not single else None, preview=self.preview,
                finished_at=self.finished_at if done else None)
        elif command.operation == "received":
            from shared_context.behavior_native_turn_wire import NativeReceivedItem, NativeReceivedPage
            from shared_context.native_routine_wire import NativeRoutineDispatch
            # Like Hosted's official list_keys page: newest first, ``after`` is the previous page's last key.
            keys = list(reversed(list(self.records)))
            start = 0 if command.after is None else keys.index(command.after) + 1
            page = keys[start:start + command.limit]
            opened = [item for item in (self.records[key] for key in page) if item.continuation is None
                      and item.command.conversation_id is None and item.status != "busy_refused"]
            value = NativeReceivedPage(
                agent_id=self.target.agent_id, next_after=page[-1] if start + command.limit < len(keys) else None,
                items=tuple(
                    NativeReceivedItem(request_id=item.request_id, conversation_id=item.conversation_id,
                                       state_profile=item.state_profile, status=item.status,
                                       sender_agent_id=item.source.sender.agent_id, message_kind=item.source.message_kind)
                    for item in opened if item.source is not None and item.source.kind == "agent_request"),
                routine_runs=tuple(NativeRoutineDispatch(
                    routine_id=item.routine.occurrence.routine_id, scheduled_for=item.routine.occurrence.scheduled_for,
                    request_id=item.request_id, conversation_id=item.conversation_id, status=item.status)
                    for item in opened if item.routine is not None))
        else:
            original = self.records[command.request_id]
            field = "cancel_requested" if command.operation == "cancel" else "delivered"
            value = NativeReceipt.model_validate({**original.model_dump(), field: True})
            self.records[value.request_id] = value
        return httpx.Response(
            202 if command.operation in {"submit", "routine_receive"} else 200, content=value.model_dump_json().encode("utf-8"),
            headers={"content-type": "application/json", "x-agent-invocation-id": "platform-id-" + str(len(self.calls)),
                     "x-agent-session-id": self.target.session_id},
        )

    def completed(self, rid):
        original = self.records[rid]
        reference = NativeResultReference(
            turn_number=1, turn_key="turn:1", result_digest="a" * 64,
            text_sha256=hashlib.sha256(self.text.encode("utf-8")).hexdigest(),
            text_bytes=len(self.text.encode("utf-8")), text_parts=2, checkpoint_sha256="b" * 64,
        )
        self.records[rid] = NativeReceipt.model_validate({
            **original.model_dump(), "status": "completed", "turn_number": 1, "result": reference,
        })


@pytest.fixture
def native_api(request, signing_key, monkeypatch):
    loop = asyncio.new_event_loop()
    c = request.getfixturevalue("synthetic_standing_v1")
    composition = c.h.composition
    from shared_context.behavior_native_sessions import native_session_id, native_sessions
    target = c.native.targets[0]
    target = target.model_copy(update={"agent_version": "41", "session_id": native_session_id(
        target, composition.config.identity.managed_identity_client_id, version="41")})
    remote = NativeTransport(target)
    foundry = FoundryHttpClient(target, Credential(), transport=httpx.MockTransport(remote))
    clients = composition.clients
    composition.clients = tuple(client for client in clients if not isinstance(client, FoundryHttpClient)) + (foundry,)
    # These turn-consumer cases start after explicit preparation. Bootstrap itself has separate contract cases.
    remote.preparation = NativeSessionPreparation(
        agent_id=target.agent_id, request_id="fixture-prepared", previous_hosted_session_id=None,
        hosted_session_id=target.session_id, hosted_agent_version=target.agent_version)
    native_sessions(composition).prepared = remote.preparation
    native_sessions(composition).binding = target
    native_sessions(composition).desired_version = "41"
    monkeypatch.setattr("shared_context.behavior_native_sessions._read_platform_session",
                        lambda client: client.target if client.target.session_id == remote.target.session_id else None)
    monkeypatch.setattr("shared_context.behavior_native_sessions._latest_version_reader", lambda *_: None)
    monkeypatch.setenv("FA_NATIVE_TURNS", NATIVE_STATE_PROFILE)
    app = FastAPI()
    _install_error_handlers(app)
    install_work_policy_routes(app, composition)
    try:
        with TestClient(app, backend_options={"loop_factory": lambda: loop}) as client:
            yield SimpleNamespace(c=c, composition=composition, client=client, remote=remote,
                                  headers=c.h.headers(signing_key), foundry=foundry)
    finally:
        composition.clients = clients
        foundry.close()
        if not loop.is_closed():
            loop.close()


def submit(case, rid="native-rid", **changes):
    body = InstructionSubmit(request_id=rid, content="Original user text", **changes).model_dump(mode="json")
    return case.client.post(PREFIX + "/submit", json=body, headers=case.headers)


def test_signed_submit_has_no_table_registration_or_fake_execution(native_api, monkeypatch):
    case, c = native_api, native_api.c
    before, bindings = c.control.store._guard(), dict(c.h.runtime.bindings)
    monkeypatch.setattr("shared_context.behavior_work_policy.derive_work",
                        lambda *_a, **_k: pytest.fail("native submit must not derive a fake work"))
    accepted = submit(case)
    assert accepted.status_code == 202, accepted.text
    value = NativeReceipt.model_validate_json(accepted.content)
    assert value.request_id == "native-rid" and value.conversation_id == "native-cid"
    assert accepted.headers["x-agent-invocation-id"] != value.request_id
    assert [command.operation for _, command in case.remote.calls if command.operation != "session_read"] == ["submit"]
    assert c.control.store._guard() == before and dict(c.h.runtime.bindings) == bindings
    assert c.control.records == {}


def test_submit_segments_share_the_turns_hashed_id_without_its_rid_or_cid(native_api, caplog):
    """v17 could not see the ~1.3 s an owner submit spent in the API before Hosted admitted it."""
    import logging
    from shared_context.behavior_timing import timing_id
    case = native_api
    assert submit(case).status_code == 202
    case.remote.completed("native-rid")
    caplog.set_level(logging.WARNING, logger="shared_context.behavior_timing")
    assert submit(case, rid="native-rid-2", conversation_id="native-cid").status_code == 202
    timings = [json.loads(record.getMessage().removeprefix("FA_TIMING ")) for record in caplog.records
               if record.getMessage().startswith("FA_TIMING ")]
    assert [(value["phase"], value["outcome"]) for value in timings if value["job_id"] == timing_id("native-rid-2")] == [
        (phase, "returned") for phase in (
            "api_submit_context", "api_submit_session", "api_submit_conversation", "api_submit_capture",
            "api_submit_hosted")]
    assert all("native-rid" not in json.dumps(value) and "native-cid" not in json.dumps(value) for value in timings)


def test_restart_reads_original_capture_and_completed_submit_never_restarts(native_api, monkeypatch):
    case = native_api
    assert submit(case).status_code == 202
    original = case.remote.records["native-rid"]
    # Changed server settings give the same RID other material: Hosted refuses it, and only then is the original
    # read, so the retry continues with its saved capture instead of a new one.
    monkeypatch.setenv("FA_TEAM_MEMORY_ENABLED", "1")
    gateway = NativeTurnGateway(case.composition, enabled=True)
    before = len(case.remote.calls)
    retained, _ = gateway.submit(case.c.human, original.command)
    assert retained.capture == original.capture
    # The second submit is the one same-material v1 resend a conflict gets (ADR-0009 2d-ii, a crashed v1 admission).
    assert [command.operation for _, command in case.remote.calls[before:]
            if command.operation != "session_read"] == ["submit", "submit", "read", "submit"]
    assert case.remote.calls[-1][1].capture == original.capture
    case.remote.completed("native-rid")
    before = len(case.remote.calls)
    completed, _ = NativeTurnGateway(case.composition, enabled=True).submit(case.c.human, original.command)
    assert completed.status == "completed"
    assert [command.operation for _, command in case.remote.calls[before:]
            if command.operation != "session_read"] == ["submit", "submit", "read"]
    conflict = case.client.post(PREFIX + "/submit", headers=case.headers,
                               json={"request_id": "native-rid", "content": "Changed original"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "NATIVE_RID_CONFLICT"
    assert case.remote.records["native-rid"].status == "completed"


def test_signed_receipt_get_preserves_optional_assembly_and_does_not_reconstruct_old_records(native_api):
    from shared_context.native_assembly import NativeAssembly
    case = native_api
    assert submit(case).status_code == 202
    case.remote.completed("native-rid")
    original = case.remote.records["native-rid"]
    old = case.client.get(PREFIX + "/requests/native-rid", headers=case.headers)
    assert old.status_code == 200 and "assembly" not in old.json()
    assembly = NativeAssembly(state="partial", reason="turn_interrupted", capture_digest=original.capture_digest)
    case.remote.records["native-rid"] = original.model_copy(update={"assembly": assembly})
    for _ in range(2):
        returned = case.client.get(PREFIX + "/requests/native-rid", headers=case.headers)
        assert returned.status_code == 200
        assert returned.json()["assembly"] == assembly.model_dump(mode="json")
        assert returned.json()["submit_digest"] == original.submit_digest
        assert returned.json()["result"] == original.result.model_dump(mode="json")
    denied = case.client.get(PREFIX + "/requests/native-rid")
    assert denied.status_code == 401


def test_lost_ack_only_reconciles_original_rid(native_api):
    case = native_api
    case.remote.lose_submit = True
    lost = submit(case)
    assert lost.status_code == 503 and lost.json()["error"]["code"] == "FOUNDRY_INVOCATION_UNKNOWN"
    assert "private" not in lost.text
    assert [command.operation for _, command in case.remote.calls if command.operation != "session_read"] == ["submit"]
    case.remote.lose_submit = False
    found = case.client.post(PREFIX + "/reconcile", headers=case.headers, json={
        "request_id": "native-rid", "content": "Original user text",
    })
    assert found.status_code == 200 and found.json()["status"] == "accepted"
    assert case.remote.calls[-1][1].operation == "read"


def test_a_conflict_without_a_visible_original_stays_a_conflict(native_api, monkeypatch):
    case = native_api

    def conflict(request):
        if NATIVE_REQUEST.validate_json(request.content).operation == "submit":
            return httpx.Response(409, json={"error": {"code": "NATIVE_RID_CONFLICT", "message": "private"}})
        return case.remote(request)

    monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(conflict))
    result = submit(case)
    assert result.status_code == 409 and result.json()["error"]["code"] == "NATIVE_RID_CONFLICT"
    assert "not_dispatched" not in result.json()["error"]
    assert [command.operation for _, command in case.remote.calls if command.operation != "session_read"] == ["read"]
    assert case.remote.records == {}


def test_an_accepted_rid_retry_continues_while_new_work_is_refused(native_api, monkeypatch):
    from shared_context.behavior_native_sessions import NativeSessions, native_sessions
    case = native_api
    assert submit(case).status_code == 202

    def following(*_args, **_kwargs):
        raise Problem(503, "AGENT_VERSION_FOLLOWING", "OFFLINE the entire Agent is changing environments")

    advances = []
    monkeypatch.setattr(native_sessions(case.composition), "admission", following)
    monkeypatch.setattr(NativeSessions, "advance", lambda _s, client, command, **_k: advances.append(command) or
                        environment_view(client, command, state="pending", reason="task_active"))
    before = len(case.remote.calls)
    retried = submit(case)
    assert retried.status_code == 202 and retried.json()["status"] == "accepted"
    assert [command.operation for _, command in case.remote.calls[before:]
            if command.operation != "session_read"] == ["read", "submit"]
    assert advances == []
    refused = submit(case, rid="new-while-following")
    assert refused.status_code == 503 and refused.json()["error"]["code"] == "AGENT_VERSION_FOLLOWING"
    assert refused.json()["error"]["not_dispatched"] is True
    assert refused.json()["error"]["request_id"] == "new-while-following"
    assert refused.json()["error"]["server_following"] is True and len(advances) == 1
    assert "new-while-following" not in case.remote.records


def environment_view(client, command, *, state, reason=None, **changes):
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView
    return NativeEnvironmentView(**{
        "agent_id": client.target.agent_id, "request_id": command.request_id,
        "previous_hosted_session_id": "OFFLINE-previous-session", "previous_agent_version": "40",
        "hosted_session_id": client.target.session_id, "hosted_agent_version": command.expected_agent_version,
        "state": state, "reason": reason, **changes})


REFUSAL_SOURCES = {
    # source: (status, code) the submit answers while the Agent is still changing versions
    "current": (409, "NATIVE_SESSION_CHANGED"),
    "prepared-without-new-reader": (409, "NATIVE_SESSION_CHANGED"),
    "admission": (503, "AGENT_VERSION_FOLLOWING"),
    "hosted-session-changed": (409, "NATIVE_SESSION_CHANGED"),
    "hosted-following": (503, "AGENT_VERSION_FOLLOWING"),
}


def test_the_server_follow_forgets_the_cached_reader_so_the_next_submit_selects_the_new_session(native_api, monkeypatch):
    """The submit's follow goes through advance's own environment change, which drops the cached reader."""
    from shared_context.behavior_native_sessions import NativeSessions, native_sessions
    case = native_api
    sessions = native_sessions(case.composition)
    assert submit(case, rid="warms-the-reader").status_code == 202
    assert sessions.reader_cached()
    observed = []

    def current(sessions, client, **_kwargs):
        sessions.following_candidate = client.target
        raise Problem(409, "NATIVE_SESSION_CHANGED", "OFFLINE the published version is not current yet")

    def follow(sessions, client, command, *, before_send):
        observed.append(sessions.reader_cached())
        return environment_view(client, command, state="ready")

    monkeypatch.setattr(NativeSessions, "current", current)
    monkeypatch.setattr(NativeSessions, "follow", follow)
    monkeypatch.setattr(NativeTurnGateway, "_ready_handoffs", lambda *_a: None)
    waiting = submit(case, rid="after-publication")
    assert waiting.json()["error"]["server_following"] is True, waiting.text
    assert observed == [False] and not sessions.reader_cached()


@pytest.mark.parametrize("lane,source", [
    ("native", "current"), ("responses", "current"), ("native", "prepared-without-new-reader"),
    ("native", "admission"), ("responses", "admission"),
    ("native", "hosted-session-changed"), ("native", "hosted-following"),
])
def test_every_wait_answer_advanced_the_follow_and_the_same_rid_is_accepted_once(native_api, monkeypatch, lane, source):
    """P06: no path keeps answering server_following without moving the Agent: each such answer ran one advance.

    The follow needs two advances here; the third send of the same RID and body is admitted exactly once."""
    from shared_context.behavior_native_sessions import NativeSessions, native_sessions
    case = native_api
    path = PREFIX + "/submit" if lane == "native" else "/work-policy/v1/responses-turns/submit"
    following, advances, sent = [True], [], []
    original_current, original_admission = NativeSessions.current, NativeSessions.admission
    status, code = REFUSAL_SOURCES[source]

    def current(sessions, client, **kwargs):
        if following[0] and source in {"current", "prepared-without-new-reader"}:
            if source == "current":
                sessions.following_candidate = client.target
            raise Problem(409, "NATIVE_SESSION_CHANGED", "OFFLINE the published version is not current yet")
        return original_current(sessions, client, **kwargs)

    def admission(sessions, client, **kwargs):
        if following[0] and source == "admission":
            raise Problem(503, "AGENT_VERSION_FOLLOWING", "OFFLINE the entire Agent is changing environments")
        return original_admission(sessions, client, **kwargs)

    def hosted(request):
        if following[0] and source.startswith("hosted") and NATIVE_REQUEST.validate_json(request.content).operation == "submit":
            return httpx.Response(status, json={"error": {"code": code, "message": "OFFLINE", "not_dispatched": True}})
        return case.remote(request)

    def advance(_sessions, client, command, *, before_send):
        before_send()
        advances.append(command)
        following[0] = len(advances) < 2
        return environment_view(client, command, state="pending" if following[0] else "ready",
                                reason="source_active" if following[0] else None,
                                hosted_session_id="OFFLINE-new-version-session")

    sessions = native_sessions(case.composition)
    sessions.following_candidate = None
    monkeypatch.setattr(NativeSessions, "current", current)
    monkeypatch.setattr(NativeSessions, "admission", admission)
    monkeypatch.setattr(NativeSessions, "advance", advance)
    monkeypatch.setattr("shared_context.behavior_native_sessions._latest_version_reader",
                        lambda client, _id, _selected=None:
                        case.remote.target if source == "prepared-without-new-reader" else None)
    monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(hosted))
    monkeypatch.setattr(NativeTurnGateway, "_ready_handoffs", lambda _g, _p, view: sent.append(view.state))
    body = InstructionSubmit(request_id="sent-during-follow", content="Original user text").model_dump(mode="json")
    for attempt in (1, 2):
        waiting = case.client.post(path, json=body, headers=case.headers)
        error = waiting.json()["error"]
        assert (waiting.status_code, error["code"]) == (status, code), waiting.text
        assert error["not_dispatched"] is True and error["request_id"] == "sent-during-follow"
        # Every round advanced; the second one finished the follow onto a new session, so it still asks to resend.
        assert error["server_following"] is True and error["reconcile_original"] is False
        assert "OFFLINE" not in error["message"]
        assert len(advances) == attempt and advances[-1].idle_only
    assert sent == ["pending", "ready"] and len({command.request_id for command in advances}) == 1
    assert "sent-during-follow" not in case.remote.records
    if lane == "responses":
        return  # Both routes share NativeTurnGateway.submit; the native lane below proves the single admission.
    accepted = case.client.post(path, json=body, headers=case.headers)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["request_id"] == "sent-during-follow" and len(advances) == 2
    assert [command.command.request_id for _, command in case.remote.calls
            if command.operation == "submit"] == ["sent-during-follow"]


def test_a_hosted_sdk_runtime_still_exiting_is_resent_with_the_same_rid(native_api, monkeypatch):
    """#903 P2-2: Hosted refuses before rid:<RID> while an earlier SDK runtime has not exited (503
    NATIVE_EXECUTION_UNAVAILABLE, not_dispatched). The API answers server_following with the RID, the shape the
    workbench resends unchanged and Teams' relay polls again; the same RID is then accepted once."""
    case = native_api
    refusing = [True]

    def hosted(request):
        if refusing[0] and NATIVE_REQUEST.validate_json(request.content).operation == "submit":
            return httpx.Response(503, json={"error": {"code": "NATIVE_EXECUTION_UNAVAILABLE", "message": "OFFLINE",
                                                       "not_dispatched": True, "retry_after_ms": 1000}})
        return case.remote(request)

    monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(hosted))
    refused = submit(case, rid="while-cli-exits")
    error = refused.json()["error"]
    assert (refused.status_code, error["code"]) == (503, "NATIVE_EXECUTION_UNAVAILABLE"), refused.text
    assert error["not_dispatched"] is True and error["request_id"] == "while-cli-exits"
    assert error["server_following"] is True and error["reconcile_original"] is False
    refusing[0] = False
    accepted = submit(case, rid="while-cli-exits")
    assert accepted.status_code == 202, accepted.text
    assert [command.command.request_id for _, command in case.remote.calls
            if command.operation == "submit"] == ["while-cli-exits"]


@pytest.mark.parametrize("after", ["recovery-same-session", "follow-finished-new-session"])
def test_hosted_task_recovery_is_not_a_follow_to_wait_for(native_api, monkeypatch, after):
    """Hosted's 503 during its own task recovery: the follow has nothing to move, so no server_following loop.

    When this request's advance finished a real follow instead, the Agent left the refusing session: resend."""
    from shared_context.behavior_native_sessions import NativeSessions
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView
    case = native_api
    advances = []
    refusing = case.remote.target.session_id

    def hosted(request):
        if NATIVE_REQUEST.validate_json(request.content).operation == "submit":
            return httpx.Response(503, json={"error": {
                "code": "AGENT_VERSION_FOLLOWING", "message": "OFFLINE", "not_dispatched": True}})
        return case.remote(request)

    def advance(_sessions, client, command, **_kwargs):
        advances.append(command)
        same = after == "recovery-same-session"
        return NativeEnvironmentView(
            agent_id=client.target.agent_id, request_id=command.request_id,
            previous_hosted_session_id=refusing if same else "OFFLINE-previous-session",
            previous_agent_version=command.expected_agent_version if same else "40",
            hosted_session_id=refusing if same else "OFFLINE-new-session",
            hosted_agent_version=command.expected_agent_version, state="current" if same else "ready")

    monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(hosted))
    monkeypatch.setattr(NativeSessions, "advance", advance)
    monkeypatch.setattr(NativeTurnGateway, "_ready_handoffs", lambda *_a: None)
    refused = submit(case, rid="during-recovery")
    error = refused.json()["error"]
    assert (refused.status_code, error["code"]) == (503, "AGENT_VERSION_FOLLOWING"), refused.text
    assert error["not_dispatched"] is True and error["request_id"] == "during-recovery" and len(advances) == 1
    assert error.get("server_following") is (None if after == "recovery-same-session" else True)


@pytest.mark.parametrize("outcome,status,code", [
    ("raised-files-unknown", 503, "NATIVE_FILES_UNKNOWN"),
    ("files-reconcile-required", 503, "NATIVE_FILES_UNKNOWN"),
    ("task-access-denied", 403, "NATIVE_ENVIRONMENT_UNAVAILABLE"),
    ("raised-task-access-denied", 403, "NATIVE_ENVIRONMENT_UNAVAILABLE"),
    ("revoked", 403, "WORK_POLICY_REVOKED"),
])
def test_a_follow_that_cannot_resolve_by_resending_keeps_its_own_code(native_api, monkeypatch, outcome, status, code):
    """Unconfirmed file effects never become a no-files confirmation card: Hosted refuses that omission for people too."""
    from shared_context.behavior_native_sessions import NativeSessions
    from shared_context.behavior_native_turn_api import _OmissionBeforeDispatch
    case = native_api

    def current(sessions, client, **_kwargs):
        sessions.following_candidate = client.target
        raise Problem(409, "NATIVE_SESSION_CHANGED", "OFFLINE the published version is not current yet")

    def advance(_sessions, client, command, *, before_send):
        if outcome == "raised-files-unknown":
            raise Problem(503, "NATIVE_FILES_UNKNOWN", "OFFLINE the original upload has no confirmed result")
        if outcome == "raised-task-access-denied":
            raise _OmissionBeforeDispatch("OFFLINE-system-policy", "NATIVE_FILES_CONFIRMATION_REQUIRED",
                                          "OFFLINE task denied", files_check={"category": "task_access_denied"})
        if outcome == "revoked":
            monkeypatch.setattr(case.composition.runtime.work_policy, "installed", lambda: None)
            before_send()
        return environment_view(client, command, state="pending", **(
            {"reason": "files_pending", "files_reconcile_required": True} if outcome == "files-reconcile-required"
            else {"reason": "task_access_denied"}))

    monkeypatch.setattr(NativeSessions, "current", current)
    monkeypatch.setattr(NativeSessions, "advance", advance)
    refused = submit(case, rid="not-admitted")
    error = refused.json()["error"]
    assert (refused.status_code, error["code"]) == (status, code), refused.text
    assert error["not_dispatched"] is True and error["request_id"] == "not-admitted"
    assert "server_following" not in error and "OFFLINE" not in error["message"]
    assert not any(command.operation == "submit" for _, command in case.remote.calls)


@pytest.mark.parametrize("platform", ["no-session", "session-without-preparation"])
def test_a_submit_never_bootstraps_an_unprepared_agent(native_api, monkeypatch, platform):
    """A never-prepared Agent keeps its refusal, also when a deploy-time follow already created a session for it."""
    from shared_context.behavior_native_sessions import NativeSessions
    case = native_api
    case.composition._native_sessions = NativeSessions(case.composition)
    case.remote.preparation = None

    def current(_sessions, _client, **_kwargs):
        raise Problem(409, "NATIVE_SESSION_CHANGED", "OFFLINE the current version needs a reader")

    monkeypatch.setattr(NativeSessions, "current", current)
    monkeypatch.setattr(NativeSessions, "advance", lambda *_a, **_k: pytest.fail("a submit must not bootstrap"))
    monkeypatch.setattr("shared_context.behavior_native_sessions._latest_version_reader",
                        lambda client, _id, _selected=None:
                        case.remote.target if platform == "session-without-preparation" else None)
    refused = submit(case, rid="before-prepare")
    error = refused.json()["error"]
    assert (refused.status_code, error["code"]) == (409, "NATIVE_SESSION_CHANGED")
    assert error["not_dispatched"] is True and error["request_id"] == "before-prepare"
    assert "server_following" not in error
    assert [command.operation for _, command in case.remote.calls] == (
        ["session_read"] if platform == "session-without-preparation" else [])


def test_a_version_this_api_build_does_not_serve_is_not_followed_but_marked(native_api, monkeypatch):
    """During a deploy the old API refuses with 503 AGENT_VERSION_FOLLOWING: marked, and never followed from here."""
    from shared_context.behavior_native_sessions import NativeSessions
    case = native_api

    def selected(_sessions, _client):
        raise Problem(503, "AGENT_VERSION_FOLLOWING", "OFFLINE the selected version belongs to another API build")

    monkeypatch.setattr(NativeSessions, "selected_version", selected)
    monkeypatch.setattr(NativeSessions, "advance", lambda *_a, **_k: pytest.fail("an old API build must not follow"))
    refused = submit(case, rid="during-deploy")
    error = refused.json()["error"]
    assert (refused.status_code, error["code"]) == (503, "AGENT_VERSION_FOLLOWING")
    assert error["not_dispatched"] is True and error["request_id"] == "during-deploy"
    assert "server_following" not in error and case.remote.calls == []


def test_a_reconcile_of_an_accepted_responses_rid_is_neither_followed_nor_marked(native_api, monkeypatch):
    """Responses reconcile passes its accepted original into submit; a version change there keeps the plain refusal."""
    from shared_context.behavior_native_sessions import NativeSessions
    from shared_context.behavior_native_turn_api import _BeforeAdmission
    case = native_api
    assert submit(case).status_code == 202
    original = case.remote.records["native-rid"]

    def current(sessions, client, **_kwargs):
        sessions.following_candidate = client.target
        raise Problem(409, "NATIVE_SESSION_CHANGED", "OFFLINE the published version is not current yet")

    monkeypatch.setattr(NativeSessions, "current", current)
    monkeypatch.setattr(NativeSessions, "advance", lambda *_a, **_k: pytest.fail("a reconcile must not follow"))
    with pytest.raises(Problem) as refused:
        NativeTurnGateway(case.composition, enabled=True).submit(case.c.human, original.command, original=original)
    assert refused.value.body["error"]["code"] == "NATIVE_SESSION_CHANGED"
    assert not isinstance(refused.value, _BeforeAdmission)


@pytest.mark.parametrize("status,code", [
    (403, "ATTACHMENTS_DISABLED"), (403, "USER_TEXT_FILES_DISABLED"), (403, "ATTACHMENT_SOURCE_DENIED"),
    (422, "ATTACHMENT_FORMAT"), (413, "ATTACHMENT_SOURCE_LIMIT"), (403, "BEHAVIOR_SOURCE_DENIED"),
    (403, "SOURCE_SCOPE_DENIED"), (403, "SOURCE_WITHDRAWN"), (409, "SOURCE_EXPIRED"), (409, "SOURCE_ACCESS_CHANGED"),
    (409, "SOURCE_REQUIRED_MISSING"), (409, "SOURCE_GRAPH_INVALID"), (413, "SOURCE_GRAPH_LIMIT"),
    (409, "SOURCE_REFERENCE_MISMATCH"), (502, "SOURCE_RECORD_INVALID"), (502, "SOURCE_RECEIPT_MISMATCH"),
    (502, "SOURCE_INTEGRITY_ERROR"), (503, "SOURCE_BLOB_UNAVAILABLE"), (422, "BEHAVIOR_UNSAFE_CONTENT"),
    (422, "CAPABILITY_MODEL_UNSUPPORTED"), (422, "INSTRUCTION_MODEL_UNAVAILABLE"), (503, "MODEL_CAPACITY_INVALID"),
    (503, "SKILL_CATALOG_CAPACITY_UNKNOWN"), (413, "SKILL_CATALOG_BUDGET_TOO_SMALL"), (422, "NATIVE_PROFILE_UNSUPPORTED"),
])
def test_admission_refusals_keep_a_fixed_code_for_plain_language(native_api, monkeypatch, status, code):
    """P03: attachment, source, text-file and model refusals are not a 500 or a generic code."""
    case = native_api

    def refuse(*_args, **_kwargs):
        raise Problem(status, code, "OFFLINE private admission detail")

    monkeypatch.setattr("shared_context.behavior_native_turn_api.resolve_capture", refuse)
    refused = submit(case, rid="refused-input")
    error = refused.json()["error"]
    assert (refused.status_code, error["code"]) == (status, code), refused.text
    assert "OFFLINE" not in error["message"] and error["automatic_retry"] is False
    # A definite 4xx before Hosted's rid:<RID> lets the same RID be sent again; a 5xx outcome stays unmarked.
    assert error.get("not_dispatched") is (True if status < 500 else None)
    assert error.get("request_id") == ("refused-input" if status < 500 else None)
    assert "refused-input" not in case.remote.records


def test_attached_source_codes_are_not_widened_on_reads(native_api, monkeypatch):
    """The P03 allowance is for submit only; a read keeps #688's behavior (generic code, no automatic re-read)."""
    case = native_api
    assert submit(case).status_code == 202

    def refuse(*_args, **_kwargs):
        raise Problem(502, "SOURCE_RECEIPT_MISMATCH", "OFFLINE private source detail")

    monkeypatch.setattr("shared_context.behavior_attachment_catalog.read_attachment_catalog", refuse)
    read = case.client.get(PREFIX + "/conversations/native-cid/attachment-sources", headers=case.headers)
    error = read.json()["error"]
    assert read.status_code == 502 and error["code"] != "SOURCE_RECEIPT_MISMATCH", read.text
    assert error["automatic_retry"] is False and "OFFLINE" not in error["message"]


@pytest.mark.parametrize("stage", ["entry", "capture", "capture-unreadable-rid"])
def test_a_refusal_before_the_rid_exists_lets_the_same_rid_be_admitted_once(native_api, monkeypatch, stage):
    case = native_api
    with monkeypatch.context() as patch:
        if stage == "entry":
            patch.setattr(case.composition.runtime.work_policy, "installed", lambda: None)
        else:
            patch.setattr("shared_context.behavior_native_turn_api.resolve_capture", lambda *_a, **_k: (_ for _ in ()).throw(
                Problem(403, "ATTACHMENTS_DISABLED", "OFFLINE attachments are off")))
        if stage == "capture-unreadable-rid":
            patch.setattr(NativeTurnGateway, "_existing", lambda *_a: (_ for _ in ()).throw(
                Problem(503, "FOUNDRY_INVOCATION_UNKNOWN", "OFFLINE the original RID could not be read")))
        refused = submit(case, rid="sent-again")
    error = refused.json()["error"]
    assert refused.status_code == 403, refused.text
    if stage == "capture-unreadable-rid":
        # Whether an earlier attempt admitted this RID is unknown, so it is not marked.
        assert "not_dispatched" not in error and "request_id" not in error
        return
    assert error["not_dispatched"] is True and error["request_id"] == "sent-again"
    accepted = submit(case, rid="sent-again")
    assert accepted.status_code == 202, accepted.text
    assert [command.command.request_id for _, command in case.remote.calls
            if command.operation == "submit"] == ["sent-again"]


@pytest.mark.parametrize("status,code", [
    (422, "NATIVE_IMAGE_UNSUPPORTED"), (409, "NATIVE_IMAGE_MISSING"),
    (409, "NATIVE_IMAGE_INVALID"), (409, "NATIVE_IMAGE_STATE_FULL"), (503, "NATIVE_STATE_STORE_UNKNOWN"),
])
def test_hosted_image_refusals_pass_through_with_their_code(native_api, monkeypatch, status, code):
    """Hosted marks a definite refusal before rid:<RID>; the API passes it on for submit only, never for an unknown 5xx.

    A marked Hosted version refusal is followed instead (see the server_following test above)."""
    case = native_api
    original = case.foundry.http._transport

    def refuse(request):
        if NATIVE_REQUEST.validate_json(request.content).operation == "submit":
            return httpx.Response(status, json={"error": {
                "code": code, "message": "OFFLINE private", "not_dispatched": True}})
        return case.remote(request)

    monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(refuse))
    refused = submit(case, rid="refused-image")
    error = refused.json()["error"]
    assert (refused.status_code, error["code"]) == (status, code), refused.text
    assert "OFFLINE" not in error["message"]
    if code == "NATIVE_STATE_STORE_UNKNOWN":
        assert "not_dispatched" not in error and "request_id" not in error
        return
    assert error["not_dispatched"] is True and error["request_id"] == "refused-image"
    assert "server_following" not in error
    monkeypatch.setattr(case.foundry.http, "_transport", original)
    assert submit(case, rid="refused-image").status_code == 202
    assert [command.command.request_id for _, command in case.remote.calls
            if command.operation == "submit"] == ["refused-image"]


@pytest.mark.parametrize("status,code", [
    (404, "NATIVE_STATE_STORE_MISSING"), (409, "NATIVE_STATE_STORE_TTL"), (503, "NATIVE_STATE_STORE_UNKNOWN"),
])
def test_submit_does_not_turn_store_errors_into_fresh_work(native_api, monkeypatch, status, code):
    case = native_api
    case.remote.error = status, code
    monkeypatch.setattr("shared_context.behavior_native_turn_api.resolve_capture",
                        lambda *_a, **_k: pytest.fail("missing or unknown store is not a fresh RID"))
    result = submit(case)
    assert result.status_code == status and result.json()["error"]["code"] == code
    assert case.remote.calls and all(command.operation in {"read", "session_read"} for _, command in case.remote.calls)


def test_actual_escaped_unicode_submit_envelope_is_not_cut_short(native_api):
    case = native_api
    body = {"request_id": "unicode-native-rid", "content": "\U0001f600" * 2046}
    raw = json.dumps(body, ensure_ascii=True).encode("utf-8")
    assert len(raw) > 24 * 1024
    result = case.client.post(PREFIX + "/submit", content=raw,
                              headers={**case.headers, "content-type": "application/json"})
    assert result.status_code == 202, result.text
    assert result.json()["command"] == {**body, "authorize_own_result": True}


@pytest.mark.parametrize("status", ["accepted", "completed"])
def test_changed_session_can_read_but_cannot_start_another_turn(native_api, status):
    case = native_api
    assert submit(case).status_code == 202
    original_sid = "old-native-session"
    case.remote.records["native-rid"] = case.remote.records["native-rid"].model_copy(
        update={"hosted_session_id": original_sid})
    if status == "completed":
        case.remote.completed("native-rid")
    saved = case.remote.records["native-rid"]
    original = case.client.get(PREFIX + "/requests/native-rid", headers=case.headers)
    assert original.status_code == 200 and original.json()["hosted_session_id"] == original_sid
    # Hosted's create-or-read refuses to restart an accepted RID of another session and returns a terminal one.
    replay = submit(case)
    assert replay.status_code == (409 if status == "accepted" else 202)
    if status == "accepted":
        assert replay.json()["error"]["code"] == "NATIVE_SESSION_CHANGED"
    else:
        assert replay.json()["status"] == "completed"
    another = submit(case, rid="new-rid-in-old-cid", conversation_id="native-cid")
    assert another.status_code == 409 and another.json()["error"]["code"] == "NATIVE_SESSION_CHANGED"
    assert case.remote.records["native-rid"] == saved and "new-rid-in-old-cid" not in case.remote.records


@pytest.mark.parametrize("vm_first", [False, True])
def test_vm_primary_and_terminal_read_use_the_same_real_ledger_record(native_api, signing_key, vm_first):
    from shared_context.behavior_agent_template import AgentTemplate, TemplateSettings, export_template
    from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
    from shared_context.behavior_turn_usage import AGENT_TURN_USAGE_PATH, AgentTurnUsageReport
    from test_agent_turn_usage_ledger import METERED, saved
    from test_turn_usage_ledger import group, ledger, report
    from test_conversation_transfer_startup import read_startup
    case, c = native_api, native_api.c
    template = export_template(c.control, c.authority, c.human)
    template = AgentTemplate.model_validate({
        **template.model_dump(), "settings": TemplateSettings(
            **{**template.settings.model_dump(), "model_provider": "openai-responses"}),
    })
    select_profile(c.control, c.authority, c.human, SelectConversationProfile(
        request_id="native-usage-profile", expected_selection_id=None, template=template,
    ))
    assert submit(case).status_code == 202
    case.remote.completed("native-rid")
    usage = report(group())
    case.remote.records["native-rid"] = case.remote.records["native-rid"].model_copy(update={"usage": usage})
    body = AgentTurnUsageReport(request_id="native-rid", usage=usage).model_dump(mode="json")
    agent_headers = {"Authorization": "Bearer " + token(signing_key, {
        "oid": c.control.policy.template.agent_oid, "idtyp": "app", "roles": ["Behavior.Agent"],
    }, remove=("scp",))}
    before = ledger(c)
    for caller in (("vm", "reader") if vm_first else ("reader", "vm")):
        result = (case.client.post(AGENT_TURN_USAGE_PATH, json=body, headers=agent_headers) if caller == "vm" else
                  case.client.get(PREFIX + "/requests/native-rid", headers=case.headers))
        assert result.status_code == 200, result.text
    after = ledger(c)
    assert after.model_metered_microusd == before.model_metered_microusd + METERED
    assert after.model_unmetered_requests == before.model_unmetered_requests + 1
    assert saved(c, "native-rid").usage == usage
    revision = c.control.store._guard()
    assert case.client.get(PREFIX + "/requests/native-rid", headers=case.headers).status_code == 200
    assert c.control.store._guard() == revision and ledger(c) == after
    assert c.control.records == {}
    read_startup(c)


def test_terminal_unknown_is_recorded_once_without_guessing_zero(native_api):
    from test_agent_turn_usage_ledger import saved
    from test_turn_usage_ledger import ledger
    case, c = native_api, native_api.c
    assert submit(case).status_code == 202
    case.remote.records["native-rid"] = case.remote.records["native-rid"].model_copy(update={
        "status": "unknown", "failure_code": "NATIVE_TURN_UNKNOWN",
    })
    before = ledger(c)
    for _ in range(2):
        assert case.client.get(PREFIX + "/requests/native-rid", headers=case.headers).status_code == 200
    assert ledger(c).model_unknown_turns == before.model_unknown_turns + 1
    assert saved(c, "native-rid").usage is None


def test_a_busy_refused_turn_without_usage_is_not_counted_as_an_unknown_turn(native_api):
    from test_turn_usage_ledger import ledger
    case, c = native_api, native_api.c
    assert submit(case).status_code == 202
    case.remote.records["native-rid"] = case.remote.records["native-rid"].model_copy(update={
        "status": "busy_refused", "failure_code": "NATIVE_TURN_BUSY",
    })
    before = ledger(c)
    response = case.client.get(PREFIX + "/requests/native-rid", headers=case.headers)
    assert response.status_code == 200 and response.json()["status"] == "busy_refused"
    assert ledger(c) == before


def test_different_terminal_usage_is_refused_without_rewriting_the_original(native_api):
    from shared_context.behavior_instruction_cost import record_agent_turn_usage
    from test_agent_turn_usage_ledger import saved
    from test_turn_usage_ledger import ledger, report
    case, c = native_api, native_api.c
    assert submit(case).status_code == 202
    case.remote.completed("native-rid")
    record_agent_turn_usage(c.control, rid="native-rid", usage=report())
    before = ledger(c)
    response = case.client.get(PREFIX + "/requests/native-rid", headers=case.headers)
    assert response.status_code == 409
    assert saved(c, "native-rid").usage == report() and ledger(c) == before
    assert case.remote.calls[-1][1].operation == "read"


def test_unknown_usage_write_is_reconciled_without_another_model_submit(native_api, monkeypatch):
    from shared_context.behavior_instruction_cost import agent_turn_usage_request_id
    from test_turn_usage_ledger import ledger
    case, c = native_api, native_api.c
    assert submit(case).status_code == 202
    case.remote.completed("native-rid")
    before = ledger(c)
    original = c.control.store.transact
    expected = agent_turn_usage_request_id(c.control.policy.template.agent_oid, "native-rid")
    lost = False

    def acknowledge_lost(request, decide):
        nonlocal lost
        result = original(request, decide)
        if request.request_id == expected and not lost:
            lost = True
            raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "synthetic lost write acknowledgment")
        return result

    monkeypatch.setattr(c.control.store, "transact", acknowledge_lost)
    failed = case.client.get(PREFIX + "/requests/native-rid", headers=case.headers)
    assert failed.status_code == 503 and failed.json()["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    assert failed.json()["error"]["automatic_retry"] is True, "the owner's read is the retry that records it"
    after = ledger(c)
    assert after.model_unknown_turns == before.model_unknown_turns + 1
    retried = case.client.get(PREFIX + "/requests/native-rid", headers=case.headers)
    assert retried.status_code == 200 and retried.json()["status"] == "completed"
    assert ledger(c) == after
    assert [command.operation for _, command in case.remote.calls if command.operation != "session_read"] == [
        "submit", "read", "read"]


def test_agent_history_keeps_its_source_and_cannot_be_replayed_as_human(native_api, monkeypatch):
    from shared_context.behavior_native_turn_wire import (
        NativeAgentCommand, NativeAgentOrigin, NativeAgentReceive, NativeTurnAddress,
    )
    case = native_api
    seed = original_submit()
    address = NativeTurnAddress(agent_id="original-sender", conversation_id="sender-cid", request_id="sender-rid")
    source = NativeAgentOrigin(
        kind="agent_request", message_kind="request", message_id="agent-message-rid", sender=address,
        recipient_agent_id=case.remote.target.agent_id, root_id="scoped-original-root", root=address, hop=1,
    )
    receive = NativeAgentReceive(command=NativeAgentCommand(
        request_id=source.message_id, conversation_id="native-cid", content="Original Agent brief"),
        capture=seed.capture, source=source)
    case.remote.records[source.message_id] = NativeReceipt(state_profile=receive.state_profile,
        agent_id=case.remote.target.agent_id, hosted_session_id=case.remote.target.session_id,
        request_id=source.message_id, conversation_id="native-cid", command=receive.command,
        capture=receive.capture, source=source, submit_digest=receive.sha256,
        capture_digest=receive.capture.sha256, status="accepted",
    )
    case.remote.completed(source.message_id)
    history = case.client.get(PREFIX + "/conversations/native-cid/history", headers=case.headers)
    assert history.status_code == 200, history.text
    original = history.json()["turns"][0]
    assert original["user_text"] is None and original["agent_text"] == "Original Agent brief"
    assert original["source"]["message_id"] == source.message_id
    saved = case.remote.records[source.message_id]
    monkeypatch.setattr("shared_context.behavior_native_turn_api.resolve_capture", lambda *_a, **_k: seed.capture)
    human = case.client.post(PREFIX + "/submit", headers=case.headers, json={
        "request_id": source.message_id, "conversation_id": "native-cid", "content": "Original Agent brief",
    })
    assert human.status_code == 409 and human.json()["error"]["code"] == "NATIVE_RID_CONFLICT"
    assert case.remote.records[source.message_id] == saved
    assert all(command.operation != "agent_receive" for _, command in case.remote.calls)


@pytest.mark.parametrize("status,code", [
    (404, "NATIVE_STATE_STORE_MISSING"), (409, "NATIVE_STATE_STORE_TTL"),
    (503, "NATIVE_STATE_STORE_UNKNOWN"), (404, "NATIVE_RID_MISSING"),
])
def test_read_refusals_remain_distinct_and_never_submit(native_api, status, code):
    case = native_api
    case.remote.error = status, code
    result = case.client.get(PREFIX + "/requests/original-rid", headers=case.headers)
    assert result.status_code == status and result.json()["error"]["code"] == code
    # A read changes nothing: a transient 5xx is polled again; a definite 4xx is not.
    assert result.json()["error"]["automatic_retry"] is (status >= 500) and "private" not in result.text
    assert [command.operation for _, command in case.remote.calls] == ["read"]


@pytest.mark.parametrize("path", [
    "/requests/native-rid", "/conversations/native-cid/requests/native-rid/status",
    "/conversations/native-cid/requests/native-rid/progress",
])
def test_a_transient_read_failure_is_retryable_but_the_same_failure_on_submit_is_not(native_api, path):
    case = native_api
    case.remote.error = 503, "NATIVE_STATE_STORE_UNKNOWN"
    read = case.client.get(PREFIX + path, headers=case.headers)
    assert read.status_code == 503 and read.json()["error"]["automatic_retry"] is True
    sent = submit(case)
    assert sent.status_code == 503 and sent.json()["error"]["code"] == "NATIVE_STATE_STORE_UNKNOWN"
    assert sent.json()["error"]["automatic_retry"] is False


@pytest.mark.parametrize("error,retry", [
    (Problem(503, "BEHAVIOR_READ_FAILED", "authority read failed"), True),
    (Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "usage commit acknowledgement lost"), True),
    (Problem(503, "NATIVE_TURNS_DISABLED", "native turns are not enabled on this API"), False),
    (_RetiredSession(503, "NATIVE_BOOTSTRAP_UNKNOWN", "the original session is retired"), False),
])
def test_only_a_read_failure_that_can_clear_invites_another_read(native_api, monkeypatch, error, retry):
    case = native_api

    def refuse(*_args, **_kwargs):
        raise error
    monkeypatch.setattr("shared_context.behavior_native_turn_api.NativeTurnGateway.read", refuse)
    result = case.client.get(PREFIX + "/requests/native-rid", headers=case.headers)
    assert result.status_code == 503 and result.json()["error"]["code"] == error.body["error"]["code"]
    assert result.json()["error"]["automatic_retry"] is retry and result.json()["error"]["reconcile_original"] is (
        error.body["error"]["code"] in {"NATIVE_BOOTSTRAP_UNKNOWN", "BEHAVIOR_COMMIT_UNKNOWN"})


@pytest.mark.parametrize("error,retry", [
    (Problem(412, "BEHAVIOR_CONFLICT", "usage entry lost its CAS races"), True),
    (Problem(409, "BEHAVIOR_CONFLICT", "a real conflict"), False),
    (Problem(409, "AGENT_USAGE_CONFLICT", "this turn's usage is already recorded differently"), False),
    (Problem(409, "BEHAVIOR_REQUEST_CONFLICT", "the request ID names another body"), False),
])
def test_a_receipt_read_that_lost_its_cas_races_invites_another_read_but_a_real_conflict_does_not(
        native_api, monkeypatch, error, retry):
    case = native_api

    def refuse(*_args, **_kwargs):
        raise error
    monkeypatch.setattr("shared_context.behavior_native_turn_api.NativeTurnGateway.read", refuse)
    result = case.client.get(PREFIX + "/requests/native-rid", headers=case.headers)
    assert result.status_code == error.status and result.json()["error"]["automatic_retry"] is retry
    # The same 412 on a write never invites a resend.
    monkeypatch.setattr("shared_context.behavior_native_turn_api.NativeTurnGateway.submit", refuse)
    sent = submit(case)
    assert sent.status_code == error.status and sent.json()["error"]["automatic_retry"] is False


def test_a_refused_native_turn_logs_its_rid_prefix_never_the_rid(native_api, monkeypatch, caplog):
    from shared_context.behavior_timing import timing_id
    case = native_api

    def refuse(*_args, **_kwargs):
        raise Problem(503, "BEHAVIOR_READ_FAILED", "authority read failed")
    monkeypatch.setattr("shared_context.behavior_native_turn_api.NativeTurnGateway.read", refuse)
    assert case.client.get(PREFIX + "/requests/native-rid", headers=case.headers).status_code == 503
    refused = [r.getMessage() for r in caplog.records if "NATIVE_TURN_REFUSED" in r.getMessage()]
    assert refused == ["NATIVE_TURN_REFUSED code=BEHAVIOR_READ_FAILED rid=" + timing_id("native-rid")]
    assert "native-rid" not in caplog.text


def test_one_rid_status_carries_receipt_and_a_verified_single_page_reply(native_api):
    case = native_api
    assert submit(case).status_code == 202
    path = PREFIX + "/conversations/native-cid/requests/native-rid/status"
    running = case.client.get(path, headers=case.headers)
    assert running.status_code == 200, running.text
    assert running.json()["receipt"]["status"] == "accepted"
    assert running.json().get("text") is None and running.json().get("next_part") is None
    case.remote.completed("native-rid")
    paged = case.client.get(path, headers=case.headers).json()
    assert paged["receipt"]["status"] == "completed" and paged["next_part"] == 0 and paged.get("text") is None
    text = case.remote.single_text
    original = case.remote.records["native-rid"]
    case.remote.records["native-rid"] = original.model_copy(update={"result": original.result.model_copy(update={
        "text_parts": 1, "text_bytes": len(text.encode("utf-8")),
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})})
    inline = case.client.get(path, headers=case.headers).json()
    assert inline["text"] == text and inline.get("next_part") is None
    assert inline["finished_at"] == "2026-10-05T13:21:08Z" and "finished_at" not in inline["receipt"]
    case.remote.single_text = "\u5225\u7684"
    changed = case.client.get(path, headers=case.headers)
    assert changed.status_code == 502 and changed.json()["error"]["code"] == "NATIVE_RESULT_INCOMPLETE"
    case.remote.single_text = text
    other = case.client.get(PREFIX + "/conversations/other-cid/requests/native-rid/status", headers=case.headers)
    assert other.status_code == 502 and other.json()["error"]["code"] == "NATIVE_STATE_SCOPE"
    assert {command.operation for _, command in case.remote.calls} <= {"session_read", "submit", "turn_status"}


def test_reply_preview_passes_only_with_a_started_receipt(native_api):
    case = native_api
    assert submit(case).status_code == 202
    path = PREFIX + "/conversations/native-cid/requests/native-rid/status"
    original = case.remote.records["native-rid"]
    case.remote.records["native-rid"] = original.model_copy(update={"status": "started"})
    case.remote.preview = "\u9810\u89bd\u56de\u8986"
    started = case.client.get(path, headers=case.headers)
    assert started.status_code == 200, started.text
    assert started.json()["preview"] == "\u9810\u89bd\u56de\u8986" and started.json().get("text") is None
    from shared_context.behavior_native_turn_wire import NativeTurnStatus
    with pytest.raises(ValueError, match="Only a completed original result"):
        NativeTurnStatus(receipt=case.remote.records["native-rid"], finished_at=case.remote.finished_at)
    case.remote.records["native-rid"] = original
    accepted = case.client.get(path, headers=case.headers)
    assert accepted.status_code == 502 and accepted.json()["error"]["code"] == "NATIVE_STATE_INVALID"
    case.remote.preview = None
    case.remote.completed("native-rid")
    done = case.client.get(path, headers=case.headers).json()
    assert done["receipt"]["status"] == "completed" and "preview" not in done


def test_public_history_and_large_result_are_explicit_pages(native_api):
    case = native_api
    assert submit(case).status_code == 202
    case.remote.completed("native-rid")
    before = len(case.remote.calls)
    page = case.client.get(PREFIX + "/conversations/native-cid/history?after_turn=0&limit=1", headers=case.headers)
    assert page.status_code == 200, page.text
    assert page.json()["turns"][0]["user_text"] == "Original user text"
    parts = []
    for part in (0, 1):
        reply = case.client.get(PREFIX + f"/conversations/native-cid/requests/native-rid/result?part={part}",
                                headers=case.headers)
        assert reply.status_code == 200, reply.text
        value = NativeResultPage.model_validate_json(reply.content)
        assert len(value.text.encode("utf-8")) > 32 * 1024 and value.complete is False
        parts.append(value.text)
    whole = "".join(parts).encode("utf-8")
    assert hashlib.sha256(whole).hexdigest() == value.result.text_sha256
    assert len(whole) == value.result.text_bytes
    assert all(command.operation != "submit" for _, command in case.remote.calls[before:])


def test_cancel_and_delivery_are_specific_record_commands(native_api):
    case = native_api
    assert submit(case).status_code == 202
    before = len(case.remote.calls)
    cancelled = case.client.post(PREFIX + "/cancel", headers=case.headers, json={
        "conversation_id": "native-cid", "request_id": "native-rid",
    })
    assert cancelled.status_code == 200 and cancelled.json()["cancel_requested"]
    assert cancelled.json()["status"] == "accepted"
    case.remote.completed("native-rid")
    delivered = case.client.post(PREFIX + "/delivered", headers=case.headers, json={
        "conversation_id": "native-cid", "request_id": "native-rid", "result_digest": "a" * 64,
    })
    assert delivered.status_code == 200 and delivered.json()["delivered"]
    assert [command.operation for _, command in case.remote.calls[before:]
            if command.operation != "session_read"] == ["cancel", "delivered"]


def test_one_complete_page_must_match_original_text_hash(native_api):
    case = native_api
    assert submit(case).status_code == 202
    case.remote.completed("native-rid")
    reference = case.remote.records["native-rid"].result.model_copy(update={"text_parts": 1})
    case.remote.reply_override = lambda _: NativeResultPage(
        conversation_id="native-cid", request_id="native-rid", result=reference,
        part=0, text="incorrect complete result", next_part=None, complete=True,
    )
    response = case.client.get(PREFIX + "/conversations/native-cid/requests/native-rid/result", headers=case.headers)
    assert response.status_code == 502 and response.json()["error"]["code"] == "NATIVE_RESULT_INCOMPLETE"


@pytest.mark.parametrize("field,value", [
    ("capture", {}), ("call_id", "caller-forged"), ("team_id", "other-team"),
    ("binding_id", "fake-binding"),
])
def test_public_submit_rejects_server_owned_fields(native_api, field, value):
    case = native_api
    result = case.client.post(PREFIX + "/submit", headers=case.headers, json={
        "request_id": "native-rid", "content": "Original", field: value,
    })
    assert result.status_code == 422 and case.remote.calls == []


def test_wrong_owner_and_disabled_route_do_not_invoke(native_api):
    case = native_api
    command = InstructionSubmit(request_id="refused", content="No call")
    with pytest.raises(Problem) as denied:
        NativeTurnGateway(case.composition, enabled=True).submit(
            replace(case.c.human, team="other-team"), command)
    assert denied.value.status == 403
    with pytest.raises(Problem) as disabled:
        NativeTurnGateway(case.composition, enabled=False).submit(case.c.human, command)
    assert disabled.value.body["error"]["code"] == "NATIVE_TURNS_DISABLED"
    assert case.remote.calls == []


def test_current_owner_can_read_and_cancel_when_new_admission_is_revoked(native_api):
    case = native_api
    assert submit(case).status_code == 202
    case.c.control.revoke(case.c.human, "native-admission-revoked")
    assert submit(case, rid="new-denied").status_code == 403
    read = case.client.get(PREFIX + "/requests/native-rid", headers=case.headers)
    assert read.status_code == 200
    cancel = case.client.post(PREFIX + "/cancel", headers=case.headers, json={
        "conversation_id": "native-cid", "request_id": "native-rid",
    })
    assert cancel.status_code == 200 and cancel.json()["cancel_requested"]
    assert [command.operation for _, command in case.remote.calls if command.operation != "session_read"] == [
        "submit", "read", "cancel"]


def test_legacy_cid_is_not_rehomed_or_readmitted(native_api):
    case = native_api
    record, _ = case.c.control.submit(
        case.c.human, InstructionSubmit(request_id="legacy-original", content="Legacy"),
    )
    cid = case.c.control.records[record.binding_id].profile.instruction.conversation_id
    result = submit(case, rid="native-cannot-take-old", conversation_id=cid)
    assert result.status_code == 409 and result.json()["error"]["code"] == "NATIVE_STATE_SCOPE"
    assert case.remote.calls == []


@pytest.mark.parametrize("changed", ["agent", "rid", "extra"])
def test_wrong_response_is_not_a_native_receipt(native_api, changed):
    case = native_api
    assert submit(case).status_code == 202
    original = case.remote.records["native-rid"]
    if changed == "agent":
        value = original.model_copy(update={"agent_id": "different-agent"})
    elif changed == "rid":
        other = original.command.model_copy(update={"request_id": "another-rid"})
        from shared_context.behavior_native_turn_wire import NativeSubmit
        value = NativeReceipt.model_validate({
            **original.model_dump(), "request_id": other.request_id, "command": other,
            "submit_digest": NativeSubmit(command=other, capture=original.capture,
                                          state_profile=original.state_profile).sha256,
        })
    else:
        value = SimpleNamespace(model_dump_json=lambda: json.dumps({
            **original.model_dump(mode="json"), "sdk_events": "must-not-return",
        }))
    case.remote.reply_override = lambda _: value
    result = case.client.get(PREFIX + "/requests/native-rid", headers=case.headers)
    assert result.status_code == 502 and result.json()["error"]["code"] in {"NATIVE_STATE_SCOPE", "NATIVE_STATE_INVALID"}
    assert "must-not-return" not in result.text


def test_registered_agent_uses_its_real_prefix_owner_and_native_client(team_agent, signing_key, monkeypatch):
    from shared_context.behavior_native_turn_api import install_native_turn_routes
    from shared_context.behavior_peer_runtime import install_peer_routing
    case = team_agent
    _register_scope(case)
    peer = case.a.for_agent(case.target.agent_id)
    from shared_context.behavior_agent_versions import current_hosted_version
    from shared_context.behavior_native_sessions import native_session_id, native_sessions
    from azure.ai.projects.models import AgentSessionResource
    from azure.core.exceptions import ResourceNotFoundError
    from test_native_session_prepare_api import project_client
    version = current_hosted_version(peer.runtime, case.target)
    target = case.target.model_copy(update={"agent_version": version, "session_id": native_session_id(
        case.target, peer.config.identity.managed_identity_client_id, version=version)})

    def metadata(name, sid):
        assert name == target.agent_name
        if sid != target.session_id:
            raise ResourceNotFoundError("no such session")
        return AgentSessionResource({
            "agent_session_id": sid, "status": "active",
            "version_indicator": {"type": "version_ref", "agent_version": version}})

    project_client(monkeypatch, get_session=metadata)
    remote = NativeTransport(target)
    native_sessions(peer).prepared = NativeSessionPreparation(
        agent_id=target.agent_id, request_id="fixture-peer-prepared", previous_hosted_session_id=None,
        hosted_session_id=target.session_id, hosted_agent_version=target.agent_version)
    command = original_submit()
    remote.records[command.command.request_id] = NativeReceipt(state_profile=command.state_profile,
        agent_id=target.agent_id, hosted_session_id=target.session_id,
        request_id=command.command.request_id, conversation_id="native-peer-cid",
        command=command.command, capture=command.capture, submit_digest=command.sha256,
        capture_digest=command.capture.sha256, status="accepted",
    )
    client = FoundryHttpClient(target, Credential(), transport=httpx.MockTransport(remote))
    originals = peer.clients
    peer.clients = tuple(c for c in originals if not isinstance(c, FoundryHttpClient)) + (client,)

    def app_for(composition):
        app = FastAPI()
        _install_error_handlers(app)
        install_native_turn_routes(app, composition, enabled=True)
        return app

    app = app_for(case.a)
    install_peer_routing(app, case.a, app_for)
    try:
        with TestClient(app, backend_options={"loop_factory": lambda: case.loop}) as browser:
            path = "/agents/" + case.target.agent_id + PREFIX + "/requests/" + command.command.request_id
            allowed = browser.get(path, headers=case.c.h.headers(signing_key))
            assert allowed.status_code == 200 and allowed.json()["agent_id"] == case.target.agent_id
            agent = browser.get(path, headers=_agent(signing_key, case.target.agent_oid))
            assert agent.status_code == 403
            assert len(remote.calls) == 1
    finally:
        peer.clients = originals
        client.close()


def test_native_api_import_has_no_hosted_sdk_dependency():
    script = """
import importlib.abc
import sys
class RejectHosted(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'copilot' or fullname.startswith(('copilot.', 'azure.ai.agentserver')):
            raise AssertionError('Hosted SDK import: ' + fullname)
sys.meta_path.insert(0, RejectHosted())
from shared_context.behavior_native_turn_api import NativeTurnGateway
from shared_context.behavior_native_capture import project_capture
from shared_context.native_routine_api import install_native_routine_routes
from shared_context.native_routine_clock import NativeRoutineClock
from shared_context.native_cut import turn_ended
print('sdk-independent')
"""
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", script],
                            env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "sdk-independent"


def test_owner_uploads_one_png_to_its_agent_and_a_changed_reply_is_refused(native_api):
    from shared_context.behavior_user_images import NativeUserImageStored
    from test_user_images_native import encoded, png
    case = native_api
    uploaded = case.client.post(PREFIX + "/images", headers=case.headers, json={"png_base64": encoded()})
    assert uploaded.status_code == 200, uploaded.text
    value = NativeUserImageStored.model_validate_json(uploaded.content)
    assert value.sha256 == hashlib.sha256(png()).hexdigest() and value.agent_id == case.remote.target.agent_id
    assert [command.operation for _, command in case.remote.calls][-1] == "user-image-upload"
    jpeg = case.client.post(PREFIX + "/images", headers=case.headers, json={"png_base64": "/9j/4AAQSkZJRgABAQAAAQABAAD/"})
    assert jpeg.status_code == 422
    case.remote.upload_reply = lambda command: NativeUserImageStored(
        agent_id=case.remote.target.agent_id, sha256="f" * 64, byte_length=len(png()))
    changed = case.client.post(PREFIX + "/images", headers=case.headers, json={"png_base64": encoded()})
    assert changed.status_code == 502 and changed.json()["error"]["code"] == "NATIVE_STATE_INVALID"


def in_flight(monkeypatch, transport=None):
    """Wraps each Hosted invocation; returns the most invocations of one SID seen at once. Each one is held briefly
    so two sent together always meet."""
    import threading
    import time

    transport = NativeTransport if transport is None else transport
    lock, live, most, handle = threading.Lock(), {}, {}, transport.__call__

    def counted(self, request):
        sid = request.url.params["agent_session_id"]
        with lock:
            live[sid] = live.get(sid, 0) + 1
            most[sid] = max(most.get(sid, 0), live[sid])
        try:
            time.sleep(0.05)
            return handle(self, request)
        finally:
            with lock:
                live[sid] -= 1

    monkeypatch.setattr(transport, "__call__", counted)
    return most


def test_a_follow_up_invokes_its_session_one_call_at_a_time_and_prefetches_only_its_capture(native_api, monkeypatch):
    """#707 dev: session (~295 ms), conversation (~465 ms) and capture (~263 ms) ran one after another; the capture
    (Table only) is computed while the session is confirmed. Dev 2026-10-22: the conversation read overlapping the
    session read on one SID was held ~2 s by the platform, so the conversation is read after the session read."""
    import threading
    from shared_context import behavior_native_turn_api

    case = native_api
    assert submit(case).status_code == 202
    case.remote.completed("native-rid")
    confirming, overlapped, handle = threading.Event(), [], NativeTransport.__call__

    def gated(self, request):
        if NATIVE_REQUEST.validate_json(request.content).operation == "session_read":
            confirming.set()
        return handle(self, request)

    computed, resolve = [], behavior_native_turn_api.resolve_capture

    def counted(*args, **kwargs):
        computed.append(threading.current_thread().name.startswith("fa-submit-prefetch"))
        overlapped.append(confirming.wait(2))
        return resolve(*args, **kwargs)

    monkeypatch.setattr(NativeTransport, "__call__", gated)
    most = in_flight(monkeypatch)
    monkeypatch.setattr(behavior_native_turn_api, "resolve_capture", counted)
    for number in (2, 3):
        confirming.clear()
        before = len(case.remote.calls)
        assert submit(case, rid=f"native-rid-{number}", conversation_id="native-cid").status_code == 202
        operations = [command.operation for _, command in case.remote.calls[before:]]
        assert operations == ["session_read", "conversation", "submit"]
        case.remote.completed(f"native-rid-{number}")
    # Never two invocations of the SID at once; both follow-ups had their capture computed once, on the prefetch
    # thread while the session was read, from the template the admitted receipt (then the previous read) kept.
    assert most == {case.remote.target.session_id: 1}
    assert computed == [True, True] and overlapped == [True, True]


def test_a_prefetched_capture_is_used_only_when_it_equals_the_ordinary_read():
    from concurrent.futures import Future
    from shared_context.behavior_native_turn_api import _SubmitPrefetch

    def done(value):
        future = Future()
        future.set_result(value) if not isinstance(value, Exception) else future.set_exception(value)
        return future

    conversation = SimpleNamespace(conversation_id="cid-a", capture=SimpleNamespace(template="template-a"))
    assert _SubmitPrefetch(done("captured"), conversation).capture(conversation) == "captured"
    changed = SimpleNamespace(conversation_id="cid-a", capture=SimpleNamespace(template="template-b"))
    assert _SubmitPrefetch(done("captured"), conversation).capture(changed) is None
    assert _SubmitPrefetch(done(Problem(403, "WORK_POLICY_REVOKED", "x")), conversation).capture(conversation) is None
