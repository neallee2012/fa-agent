"""ADR-0009 2d-i: a newer API serves an older Hosted image whose SDK start gate pins its own build (2026-10-17)."""

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from shared_context import behavior_agent_versions, behavior_native_capture, behavior_native_sessions, native_sdk_profiles
from shared_context.behavior_code_identity import code_sha256
from shared_context.behavior_native_sessions import native_session_id, native_sessions
from shared_context.behavior_native_turn_wire import (
    NATIVE_REQUEST, NATIVE_STATE_PROFILE, NativeReceipt, NativeSessionPreparation,
)
from shared_context.errors import Problem
from test_agent_runtime import peer_runtime  # noqa: F401
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_agent_api import relay_case, relay_http, send_command  # noqa: F401
from test_native_binding import bound, claim, switch_view  # noqa: F401
from test_native_responses_api import responses_api  # noqa: F401
from test_native_routine_api import daily_definition, routine_api  # noqa: F401
from test_native_turn_api import native_api, submit  # noqa: F401

API = code_sha256()
OLD = "0" * 64  # the image v41 runs: deployed before this API changed core
OTHER = "1" * 64


class Versions:
    """The API's version resolver: every version is the session's own, each with the ``fa_build`` it was published for."""

    def __init__(self, **builds):
        self.builds = builds

    def current(self, target, require_responses=False):
        return target.agent_version

    def observed(self, target):
        return None

    def profile(self, target, version):
        return NATIVE_STATE_PROFILE

    def build_of(self, target, version):
        return self.builds.get(version)


GATED = {"submit", "routine_receive", "agent_receive"}


def gated_captures(monkeypatch):
    """The template selects the start gate, so every resolved capture first carries this API's own build."""
    real = behavior_native_capture._resolve_capture

    def gated(*args, **kwargs):
        capture = real(*args, **kwargs)
        settings = capture.template.settings.model_copy(update={"sdk_start_profile": "fa-sdk-start-gate-v1"})
        template = capture.template.model_copy(update={"settings": settings})
        return capture.model_copy(update={"template": template, "sdk_start_code_sha256": API})

    monkeypatch.setattr(behavior_native_capture, "_resolve_capture", gated)


def images(monkeypatch, runtime, remote, builds):
    """Each VM runs the build its version was published for (``builds``, also the versions' ``fa_build``)."""
    gated_captures(monkeypatch)
    monkeypatch.setattr(native_sdk_profiles, "code_sha256", lambda: builds[remote.target.agent_version])
    monkeypatch.setattr(native_sdk_profiles.importlib.metadata, "version", lambda _name: "1.0.13")
    monkeypatch.setattr(runtime.agent_message_composition, "hosted_versions", Versions(**builds), raising=False)


def admission(remote, request, command, refusals=None):
    """Hosted ``_submit_locked``: only an RID with no ``rid:<RID>`` runs the admission checks, the image's start gate
    among them; a refusal there is ``not_dispatched``. ``refusals`` are refused first (the Agent is moving)."""
    if command.operation not in GATED or command.command.request_id in remote.records:
        return None
    try:
        if refusals:
            raise refusals.pop(0)
        native_sdk_profiles.check_start_capture(command.capture)
    except Problem as refusal:
        remote.calls.append((request, command))
        return httpx.Response(refusal.status, json={"error": {**refusal.body["error"], "not_dispatched": True}})
    return None


def old_image(case, monkeypatch, refusals=None, **builds):
    """The VM refuses any pin other than its own build before rid:<RID> exists."""
    images(monkeypatch, case.composition.runtime, case.remote, builds)
    remote = case.remote

    def route(request):
        command = NATIVE_REQUEST.validate_json(request.content)
        expected = request.headers.get("x-client-fa-expected-version")
        if remote.vm_version is None or expected == remote.vm_version:
            refused = admission(remote, request, command, refusals)
            if refused is not None:
                return refused
        return remote(request)

    monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(route))


def moving():
    return [Problem(503, "AGENT_VERSION_FOLLOWING", "the Agent is moving to its selected version")]

def test_a_newer_api_core_is_served_by_the_running_older_image(native_api, monkeypatch):  # noqa: F811
    """dev 2026-10-17 (4-3/5-1): V_rec 41 runs an older core than this API. Before the fix every send was refused
    SDK_START_GATE_CODE_CHANGED; now the capture names the v41 image's own build and the turn is admitted there."""
    case = native_api
    assert API != OLD
    old_image(case, monkeypatch, **{"41": OLD})
    accepted = submit(case)
    assert accepted.status_code == 202, accepted.text
    receipt = NativeReceipt.model_validate_json(accepted.content)
    assert receipt.capture.sdk_start_code_sha256 == OLD
    assert [command.operation for _, command in case.remote.calls if command.operation != "session_read"] == ["submit"]


def test_drift_to_an_older_image_resends_with_that_images_pin(bound, monkeypatch):  # noqa: F811
    """§4 step 6: V_rec 40, the VM woke on 41 and says so; the same RID goes to 41 pinned to 41's build, not 40's."""
    case = bound
    claim(case, observed="40")
    case.remote.vm_version = "41"
    old_image(case, monkeypatch, **{"40": OTHER, "41": OLD})
    accepted = submit(case)
    assert accepted.status_code == 202, accepted.text
    assert NativeReceipt.model_validate_json(accepted.content).capture.sdk_start_code_sha256 == OLD


def test_the_pin_changes_only_a_selected_gate_and_a_known_build(native_api):  # noqa: F811
    from shared_context.behavior_native_capture import start_pinned
    from shared_context.behavior_native_turn_wire import NativeTurnCapture

    class Runtime:
        agent_message_composition = type("C", (), {"hosted_versions": Versions(**{"41": OLD})})()

    target = native_api.remote.target
    plain = NativeTurnCapture.model_validate(
        NativeReceipt.model_validate_json(submit(native_api).content).capture.model_dump())
    # No start profile selected: no pin, nothing to change.
    assert plain.sdk_start_code_sha256 is None and start_pinned(plain, Runtime, target) is plain
    pinned = plain.model_copy(update={"sdk_start_code_sha256": API})
    assert start_pinned(pinned, Runtime, target).sdk_start_code_sha256 == OLD
    # An unknown build (no fa_build, or no resolver) keeps this API's pin, as before.
    assert start_pinned(pinned, Runtime, target.model_copy(update={"agent_version": "42"})) is pinned
    assert start_pinned(pinned, object(), target) is pinned


def move(case, monkeypatch, version):
    """U7 committed: the Agent's new SID runs ``version`` and the row records it; its conversations continue there."""
    sid = native_session_id(case.remote.target, "unused", version=version)
    case.store.record_switch(case.agent_id, switch_view(case, files_migrated=False, to=sid, version=version))
    target = case.remote.target.model_copy(update={"session_id": sid, "agent_version": version})
    case.remote.target, case.remote.session_binding = target, sid
    case.remote.preparation = NativeSessionPreparation(
        agent_id=case.agent_id, request_id="moved", previous_hosted_session_id=case.sid, hosted_session_id=sid,
        hosted_agent_version=version)
    sessions = native_sessions(case.composition)
    sessions.prepared, sessions.binding, sessions.desired_version = case.remote.preparation, target, version
    monkeypatch.setattr(behavior_agent_versions, "current_hosted_version", lambda *_args, **_kwargs: version)
    return target


@pytest.mark.parametrize("routine_api", ["clock"], indirect=True)
def test_a_routine_refused_on_v41_is_served_by_v42_with_its_intent_and_digest_unchanged(
        routine_api, bound, monkeypatch):  # noqa: F811
    """#880 review P2-1: the occurrence goes to v41 and is refused before rid:<RID> exists; the Agent moves to v42.
    A pin saved in the intent made every later tick SDK_START_GATE_CODE_CHANGED. The intent never names it: the
    tick is pinned to v42's build at dispatch, and the resend of the accepted RID carries the receipt's digest."""
    from shared_context.native_routines import NativeRoutineService

    case = routine_api
    # The cut to v42 committed while this tick was on its way: v41 refuses it before rid:<RID> exists.
    refusals = [Problem(409, "NATIVE_SESSION_CHANGED", "the Agent moved to another session")]
    old_image(case, monkeypatch, refusals, **{"41": OLD, "42": OTHER})
    due = case.c.h.time.replace(second=0, microsecond=0) + timedelta(minutes=1)
    definition = daily_definition(enabled=True, schedule={
        "kind": "daily", "time_zone": "UTC", "local_time": due.strftime("%H:%M")})
    assert case.client.post(case.routine_prefix + "/definitions", json=definition, headers=case.headers).status_code == 200
    case.c.h.time = due + timedelta(seconds=2)
    body = {"routine_id": "daily-summary", "scheduled_for": due.isoformat()}
    refused = case.trigger(body)
    assert refused.status_code == 409 and not refusals, refused.text
    rid = next(command.command.request_id for _, command in case.remote.calls if command.operation == "routine_receive")
    assert rid not in case.remote.records
    control, service = case.composition.runtime.work_policy, NativeRoutineService(case.composition, enabled=True)
    intent = service._intent(control, case.agent_id, rid)
    target = move(case, monkeypatch, "42")
    served = case.trigger(body)
    assert served.status_code == 202, served.text
    receipt = case.remote.records[rid]
    assert (receipt.hosted_session_id, receipt.capture.sdk_start_code_sha256) == (target.session_id, OTHER)
    resent = case.trigger(body)
    assert resent.status_code == 202, resent.text
    sent = [command for _, command in case.remote.calls if command.operation == "routine_receive"]
    assert sent[-1].sha256 == receipt.submit_digest and case.remote.records[rid] == receipt
    assert service._intent(control, case.agent_id, rid) == intent


def test_a_handoff_refused_on_its_first_version_is_served_after_the_recipient_moves(
        relay_http, monkeypatch):  # noqa: F811
    """#880 review P2-1: an Agent handoff reaches the recipient's v41 image and is refused before admission; the
    recipient then moves to v42. The resend keeps its RID and relay allowance and is pinned to v42's build."""
    from shared_context.behavior_native_agent_wire import NATIVE_AGENT_PREFIX
    from test_native_environment_api import environment_at

    case, client, headers, _ = relay_http
    peer, remote = case.base.b, case.remotes[1]
    first = case.clients[1].target.agent_version
    builds = {first: OLD, "42": OTHER}
    images(monkeypatch, peer.runtime, remote, builds)
    selected, read = behavior_agent_versions.current_hosted_version, behavior_native_sessions._read_platform_session
    original_http = case.clients[1].http._transport
    source, calls, sessions, views = environment_at(
        SimpleNamespace(remote=remote, foundry=case.clients[1]), monkeypatch)
    peer_read = behavior_native_sessions._read_platform_session
    moved = []
    monkeypatch.setattr(behavior_agent_versions, "current_hosted_version",
                        lambda runtime, target, **kwargs: "42" if moved and target.agent_id == peer.agent_id
                        else selected(runtime, target, **kwargs))
    monkeypatch.setattr(behavior_native_sessions, "_read_platform_session",
                        lambda target, **options: peer_read(target, **options)
                        if moved and target.target.agent_id == peer.agent_id else read(target, **options))
    environment_http = case.clients[1].http._transport
    refusals = moving()

    def hosted(packet):
        command = NATIVE_REQUEST.validate_json(packet.content)
        if command.operation in {"read", "agent_receive"}:
            remote.target = sessions.get(packet.url.params["agent_session_id"], remote.target)
            return admission(remote, packet, command, refusals) or original_http.handle_request(packet)
        return environment_http.handle_request(packet)

    monkeypatch.setattr(case.clients[1].http, "_transport", httpx.MockTransport(hosted))
    body = send_command(case).model_dump(mode="json")
    refused = client.post(NATIVE_AGENT_PREFIX + "/send", headers=headers, json=body)
    assert refused.status_code == 503 and not refusals, refused.text
    assert not remote.records and len(case.repository.data) == 1
    allowance = dict(case.repository.data)
    moved.append("42")
    following = client.post(NATIVE_AGENT_PREFIX + "/send", headers=headers, json=body)
    assert following.status_code == 503 and following.json()["error"]["code"] == "AGENT_VERSION_FOLLOWING"
    ready = client.post(NATIVE_AGENT_PREFIX + "/environment", headers=headers, json=body)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    accepted = client.post(NATIVE_AGENT_PREFIX + "/send", headers=headers, json=body)
    assert accepted.status_code == 202, accepted.text
    (receipt,) = remote.records.values()
    assert receipt.request_id == accepted.json()["message_id"]
    assert sessions[receipt.hosted_session_id].agent_version == "42"
    assert receipt.capture.sdk_start_code_sha256 == OTHER
    assert case.repository.data == allowance