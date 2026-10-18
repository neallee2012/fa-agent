"""#880 review P2: a never-started receipt vN accepted, adopted by the vN+1 SID (ADR-0009 M9), runs there on its own
pin, through the real native executor and SDK start gate; only the SDK process is an offline double."""

import json

import pytest
from starlette.testclient import TestClient
from types import SimpleNamespace

from shared_context import native_sdk, native_sdk_profiles
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeReceipt, NativeSubmit
from shared_context.native_turns import TURNS_STORE, NativeTurnRuntime, _json, _Rid
from test_native_capabilities import selected_capture
from test_native_turn_wire import submit
from test_native_turns import Harness, IDENTITY, local_env  # noqa: F401

OLD = "0" * 64  # the build of the vN image that accepted the receipt
NEW = "2" * 64  # this (vN+1) image's own build
EARLIER = "OFFLINE-earlier-session"


def gated(pin, **role):
    data = selected_capture().model_dump(mode="json")
    data["template"]["settings"]["sdk_start_profile"] = "fa-sdk-start-gate-v1"
    data["template"]["role"].update(role)
    data["sdk_start_code_sha256"] = pin
    return type(selected_capture()).model_validate_json(json.dumps(data))


@pytest.fixture
def image(local_env, monkeypatch, tmp_path):  # noqa: F811
    entered = []

    async def sdk(**options):
        options["sdk_start_gate"].enter(options["sdk_session_id"])  # as run_copilot does, before SDK construction
        entered.append(options["sdk_session_id"])
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {
                "sessionId": options["sdk_session_id"]}}) + "\n",
            "workspace.yaml": "id: " + options["sdk_session_id"] + "\n",
        })
        return {"final_message": "adopted answer",
                "sdk_stop": {"pid": 123, "exit_code": 0, "graceful": True, "runtime_version": "1.0.83"}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    monkeypatch.setattr(native_sdk_profiles, "code_sha256", lambda: NEW)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=tmp_path / "OFFLINE-not-executed",
        api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE-token")
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        yield Harness(runtime, SimpleNamespace(controls={}), client), entered


def left_by_vn(harness, rid, capture):
    """A receipt the vN SID accepted and never started; the Agent is now this (vN+1) session's."""
    command = NativeSubmit(command=submit(rid).command, capture=capture)
    receipt = NativeReceipt(
        agent_id=IDENTITY.agent_id, hosted_session_id=EARLIER, request_id=rid, conversation_id="native-" + rid,
        command=command.command, capture=capture, submit_digest=command.sha256, capture_digest=capture.sha256,
        status="accepted")

    async def write():
        async with harness.runtime._store(TURNS_STORE) as store:
            await store.create_item("rid:" + rid, _json(_Rid(receipt=receipt, call_id="OFFLINE-seed")))

    harness.call(write)
    return receipt


def adopted_run(harness, rid, capture):
    left = left_by_vn(harness, rid, capture)
    # The API's read of the RID (``_observed_read`` -> ``observe_left`` -> ``adopt``) moves it to this SID.
    assert harness.call(harness.runtime._observed_read, rid).hosted_session_id == IDENTITY.session_id
    return left, harness.wait(rid, "completed", "unknown")


def test_an_adopted_never_started_receipt_from_vn_is_served_on_the_new_sid(image):
    harness, entered = image
    left, receipt = adopted_run(harness, "adopted-rid", gated(OLD))
    assert (receipt.status, receipt.failure_code) == ("completed", None), receipt
    assert entered and receipt.hosted_session_id == IDENTITY.session_id
    # Rules 5 and 7: same RID, request and capture bytes; the pin stays the vN image's.
    assert (receipt.request_id, receipt.command, receipt.capture, receipt.submit_digest, receipt.capture_digest) == (
        left.request_id, left.command, left.capture, left.submit_digest, left.capture_digest)
    assert receipt.capture.sdk_start_code_sha256 == OLD


def test_a_fresh_rid_with_a_foreign_pin_is_still_refused(image):
    harness, entered = image
    refused = harness.invoke(NativeSubmit(command=submit("fresh-rid").command, capture=gated(OLD)))
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "SDK_START_GATE_CODE_CHANGED", refused.text
    assert not entered
    accepted = harness.invoke(NativeSubmit(command=submit("own-rid").command, capture=gated(NEW)))
    assert accepted.status_code == 202, accepted.text
    assert harness.wait("own-rid", "completed", "unknown").status == "completed"


def test_an_adopted_receipt_from_an_incompatible_version_is_still_refused(image):
    """vN's prompt differs from this image's: this image's own preflight refuses it, on its own RID (rule 7)."""
    harness, entered = image
    _, receipt = adopted_run(harness, "incompatible-rid", gated(OLD, prefix_sha256="f" * 64))
    assert (receipt.status, receipt.failure_code) == ("unknown", "NATIVE_CAPTURE_CHANGED"), receipt
    assert not entered and receipt.request_id == "incompatible-rid"
