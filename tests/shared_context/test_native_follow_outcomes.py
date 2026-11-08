"""ADR-0009 §7, §7a, §10: what ``follow`` sends to the destination, which the other follow tests do not check.

Same harness as ``test_native_files_api.py``: real source and destination Hosted runtimes behind the real API, with
the platform as an offline double. ``fixture.controls`` is every command the destination answered, in order.
"""

import json
from types import SimpleNamespace

import httpx
from azure.core.exceptions import HttpResponseError

from shared_context.behavior_native_sessions import NativeSessions
from shared_context.behavior_native_turn_api import NativeTurnGateway
from shared_context.behavior_native_turn_wire import NATIVE_REQUEST
from shared_context.native_binding import NativeBindingStore
import test_native_abandon_source as abandon
from test_native_abandon_source import binding_rows  # noqa: F401 (autouse fixture)
from test_native_automatic_environment import advance
from test_native_follow_no_wake import source_calls
from test_native_files_api import (  # noqa: F401 (fixtures)
    file_api, file_loops, follow, instruction_models, jwk_client, local_env, native_api, signing_key,
    synthetic_standing_v1, token,
)


def _sent(fixture, since=0):
    return [(command.get("operation"), command.get("stage")) for command, _ in fixture.controls[since:]]


def test_every_follow_step_is_sent_right_after_the_owners_check(file_api, monkeypatch):
    """The owner's permission (``before_send``) is checked again between the last command and each follow step, and
    before the source is read for its status and stopped."""
    fixture = file_api
    checked, order = set(), []
    context = NativeTurnGateway._context

    def recorded(self, principal, action, **options):
        checked.add(len(fixture.controls))
        order.append("check")
        return context(self, principal, action, **options)

    monkeypatch.setattr(NativeTurnGateway, "_context", recorded)
    fixture.files.on_stop = lambda: order.append("stop")
    result = follow(fixture)
    assert result.status_code == 200 and result.json()["state"] == "ready", result.text
    steps = [index for index, (operation, _) in enumerate(_sent(fixture)) if operation == "session_follow"]
    assert {_sent(fixture)[index][1] for index in steps} == {"prepare", "commit"}
    assert [index for index in steps if index not in checked] == []
    assert order.count("stop") == 1 and order[order.index("stop") - 1] == "check"


def test_a_follow_naming_another_current_session_is_refused_by_hosted(file_api):
    """``expected_session_id`` is sent as the caller named it, never replaced by the session Hosted reads now."""
    fixture = file_api
    stale = "fa-native-not-the-current-session"
    result = follow(fixture, command={**fixture.command, "expected_session_id": stale})
    assert result.status_code == 409, result.text
    assert result.json()["error"]["code"] == "NATIVE_ENVIRONMENT_CONFLICT", result.text
    sent = [command for command, _ in fixture.controls if command.get("operation") == "session_follow"]
    assert [command["expected_session_id"] for command in sent] == [stale]
    assert source_calls(fixture) == ([], [], [])
    assert not fixture.target_workspace.root.exists()


def test_an_abandon_switch_is_the_last_command_the_follow_sends(file_api, monkeypatch):
    """§7a: once the abandon-source switch commits, the follow returns it; no follow step is sent after it."""
    fixture = file_api
    abandon.incompatible_idle(fixture, monkeypatch)
    before = len(fixture.controls)
    abandon.assert_abandoned(advance(fixture), fixture)
    sent = [operation for operation, _ in _sent(fixture, before)]
    assert sent.count("session_abandon") == 1 and "session_follow" not in sent, sent


def test_a_prepare_another_replica_already_committed_still_records_the_switch(file_api, monkeypatch):
    """A replica that read the Agent before another one's commit gets ``ready`` from its own prepare; the binding
    row write that commit lost (a Table failure) is made by this one (ADR-0009 §7a (j))."""
    fixture = file_api
    invoked, record_switch = NativeTurnGateway._invoke, NativeBindingStore.record_switch
    reads, lost = [], []

    def recorded(client, command):
        result = invoked(client, command)
        if command.operation == "session_read":
            reads.append(result)
        return result

    def lose_once(self, *args, **options):
        if not lost:
            lost.append(args[0])
            raise RuntimeError("OFFLINE the Table write was lost")
        return record_switch(self, *args, **options)

    monkeypatch.setattr(NativeTurnGateway, "_invoke", staticmethod(recorded))
    monkeypatch.setattr(NativeBindingStore, "record_switch", lose_once)
    first = follow(fixture)
    assert first.status_code == 200 and first.json()["state"] == "ready", first.text
    assert lost and abandon.row(fixture).hosted_session_id == fixture.source.session_id
    stale = [reads[0]]  # read before that follow's prepare

    def stale_first(client, command):
        return stale.pop() if command.operation == "session_read" and stale else invoked(client, command)

    monkeypatch.setattr(NativeTurnGateway, "_invoke", staticmethod(stale_first))
    fixture.case.composition._native_sessions = NativeSessions(fixture.case.composition)
    before = len(fixture.controls)
    again = follow(fixture)
    assert again.status_code == 200 and again.json() == first.json(), again.text
    assert ("session_follow", "prepare") in _sent(fixture, before) and not stale
    assert abandon.row(fixture).hosted_session_id == fixture.destination.session_id


def test_a_stop_required_answer_without_a_manifest_never_stops_the_source(file_api, monkeypatch):
    """``source_stop_required`` with no verified workspace manifest (and no confirmed omission) is refused before the
    source is read for files or stopped."""
    fixture = file_api
    transport = fixture.case.foundry.http._transport

    def without_manifest(packet):
        response = transport.handle_request(packet)
        body = response.json() if response.status_code == 200 else None
        if (NATIVE_REQUEST.validate_json(packet.content).operation == "session_follow"
                and body is not None and body.get("reason") == "source_stop_required"):
            body.pop("files_manifest_sha256", None)
            headers = {k: v for k, v in response.headers.items() if k.lower() not in {"content-length", "content-encoding"}}
            return httpx.Response(200, content=json.dumps(body).encode(), headers=headers)
        return response

    monkeypatch.setattr(fixture.case.foundry.http, "_transport", httpx.MockTransport(without_manifest))
    result = follow(fixture)
    assert result.status_code == 409 and result.json()["error"]["code"] == "NATIVE_FILES_UNSUPPORTED", result.text
    assert not fixture.files.stopped and source_calls(fixture)[2] == []


def test_a_source_the_platform_already_stopped_is_not_recorded_as_fas_stop(file_api):
    """ADR-0009 §10: only a stop the platform accepted is recorded; "already stopped" (not FA's stop) leaves the
    source's files unread and the follow refused."""
    fixture = file_api
    stopped_at = []

    def already_stopped():
        stopped_at.append(len(fixture.controls))
        error = HttpResponseError("OFFLINE the session was already stopped")
        error.status_code, error.error = 409, SimpleNamespace(code="session_already_stopped")
        raise error

    fixture.files.on_stop = already_stopped
    result = follow(fixture)
    assert result.status_code == 409 and result.json()["error"]["code"] == "NATIVE_FILES_SOURCE_UNAVAILABLE", result.text
    assert len(stopped_at) == 1 and _sent(fixture, stopped_at[0]) == []  # refused before any further step
