"""Real native admission crosses the metadata-read / follow-prepare boundary; no model is run."""

import httpx
import pytest

from shared_context.behavior_native_turn_wire import NATIVE_REQUEST, NativeEnvironmentView, NativeSubmit, canonical_bytes
from shared_context.errors import Problem
from shared_context.native_environment_sync import follow_agent
from shared_context.native_turns import TURNS_STORE, _Rid, _changed, _json
from test_native_automatic_environment import advance
from test_native_files_api import (
    file_api, file_loops, follow, instruction_models, jwk_client, local_env, native_api,
    signing_key, synthetic_standing_v1, token,
)
from test_native_turns import sdk_result


@pytest.mark.parametrize("settled,after_follow", [
    (False, False), (True, False), (True, True),
], ids=["admitting", "accepted", "previously-followed"])
def test_automatic_follow_waits_when_original_admission_changes_the_etag_before_prepare(
    file_api, monkeypatch, settled, after_follow,
):
    fixture = file_api
    submitted = NativeSubmit(
        command=fixture.saved.command.model_copy(update={"request_id": "during-follow", "conversation_id": None}),
        capture=fixture.saved.capture)
    receipt = _changed(fixture.saved, request_id=submitted.command.request_id,
                       conversation_id="during-follow-cid", command=submitted.command,
                       submit_digest=submitted.sha256, status="accepted", result=None, turn_number=None, usage=None)
    inserted, prepared = [], []
    transport = fixture.case.foundry.http._transport

    if after_follow:
        async def previous_follow():
            current, etag = await fixture.original._agent_record()
            ready = NativeEnvironmentView(
                agent_id=fixture.source.agent_id, request_id="previous-follow",
                previous_hosted_session_id="OFFLINE-before-original", previous_agent_version="40",
                hosted_session_id=fixture.source.session_id, hosted_agent_version=fixture.source.agent_version,
                state="ready", files_profile=current.preparation.files_profile)
            async with fixture.original._store(TURNS_STORE) as store:
                await store.set_item("agent", _json(_changed(
                    current, environment=ready, following_supported=True)), if_match=etag)
        fixture.hosted.portal.call(previous_follow)

    async def admit():
        await fixture.original._require_original_session(admitting=True, request=submitted)
        if settled:
            async with fixture.runtime._store(TURNS_STORE) as store:
                await store.create_item("rid:" + receipt.request_id, _json(_Rid(
                    receipt=receipt, call_id="OFFLINE-existing-owner")))
            await fixture.original._settle_admission()
            return canonical_bytes(receipt)
        return None

    def crossing(packet):
        command = NATIVE_REQUEST.validate_json(packet.content)
        response = transport.handle_request(packet)
        if command.operation == "session_read" and not inserted and response.status_code == 200:
            inserted.append(fixture.hosted.portal.call(admit))
        if command.operation == "session_follow" and command.stage == "prepare":
            prepared.append((command, response.status_code, response.json()))
        return response

    monkeypatch.setattr(fixture.case.foundry.http, "_transport", httpx.MockTransport(crossing))
    result = advance(fixture)
    assert result.status_code == 200, (result.text, prepared)
    assert result.json()["state"] == "pending"
    assert result.json()["reason"] == ("accepted_or_started" if settled else "admission_pending")
    assert not fixture.files.stopped and not fixture.runtime.executor.calls
    assert prepared[0][1] == 409
    assert prepared[0][2]["error"]["code"] == "NATIVE_ENVIRONMENT_CONFLICT"
    assert "files_check" not in prepared[0][2]["error"]
    assert len({item[0].request_id for item in prepared}) == 1
    assert len(prepared) == 2
    if settled:
        kept = fixture.hosted.portal.call(fixture.runtime.read, submitted.command.request_id)
        assert canonical_bytes(kept) == inserted[0]

    async def finish_original():
        try:
            current = await fixture.original._rid(submitted.command.request_id)
        except Problem as error:
            assert error.body["error"]["code"] == "NATIVE_RID_MISSING"
            async with fixture.original._store(TURNS_STORE) as store:
                await store.create_item("rid:" + receipt.request_id, _json(_Rid(
                    receipt=receipt, call_id="OFFLINE-existing-owner")))
            current = await fixture.original._rid(submitted.command.request_id)
        await fixture.original._settle_admission()
        started, owned = await fixture.original._cas(current, _changed(
            current.value, run_token="OFFLINE-already-running",
            receipt=_changed(current.value.receipt, status="started")))
        assert owned
        allocated, meta, previous = await fixture.original._allocate(started)
        assert previous is None
        return await fixture.original._save_turn(
            allocated, meta, sdk_result(fixture.original._scope(meta), "Original work completed."))

    elapsed, completed = [0], []

    def sleeping(seconds):
        elapsed[0] += seconds
        if not completed:
            completed.append(fixture.hosted.portal.call(finish_original))

    ready = follow_agent(fixture.case.composition, fixture.source.agent_id,
                         wait_seconds=60, sleep=sleeping, clock=lambda: elapsed[0])
    assert ready["state"] == "ok" and ready["environment"]["state"] == "ready"
    assert ready["environment"]["request_id"] == result.json()["request_id"]
    assert fixture.hosted.portal.call(fixture.runtime.read, submitted.command.request_id) == completed[0]
    assert len(fixture.files.stopped) == 1 and not fixture.runtime.executor.calls
    assert len(fixture.original.executor.calls) == 1, "The original fixture turn is never replayed by follow"


@pytest.mark.parametrize("change", ["unchanged", "source", "preparation", "selector", "keep-changing", "manual"])
def test_prepare_recheck_is_bounded_and_never_rebinds_a_changed_environment(file_api, monkeypatch, change):
    fixture = file_api
    original_transport = fixture.case.foundry.http._transport
    reads, prepares = [], []

    async def change_agent():
        current, etag = await fixture.runtime._agent_record()
        value = current
        if change == "source":
            value = _changed(current, hosted_session_id="OFFLINE-other-source", preparation=_changed(
                current.preparation, hosted_session_id="OFFLINE-other-source"))
        elif change == "preparation":
            value = _changed(current, preparation=_changed(current.preparation, request_id="OFFLINE-other-preparation"))
        async with fixture.runtime._store(TURNS_STORE) as store:
            await store.set_item("agent", _json(value), if_match=etag)
        if change == "selector":
            monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_a, **_k: "43")

    def crossing(packet):
        command = NATIVE_REQUEST.validate_json(packet.content)
        if command.operation == "session_follow":
            prepares.append(command)
            if change == "unchanged":
                return httpx.Response(409, json={"error": {
                    "code": "NATIVE_ENVIRONMENT_CONFLICT", "message": "OFFLINE different operation"}})
        result = original_transport.handle_request(packet)
        if command.operation == "session_read":
            reads.append(command)
            if change != "unchanged" and (len(reads) == 1 or change == "keep-changing"):
                fixture.hosted.portal.call(change_agent)
        return result

    monkeypatch.setattr(fixture.case.foundry.http, "_transport", httpx.MockTransport(crossing))
    if change == "manual":
        fixture.command["idle_only"] = False
    original_receipt = fixture.saved.model_dump_json()
    failed = follow(fixture)
    assert failed.status_code == 409 and failed.json()["error"]["code"] == "NATIVE_ENVIRONMENT_CONFLICT", failed.text
    assert len(prepares) == (2 if change == "keep-changing" else 1)
    assert len({command.request_id for command in prepares}) == 1
    assert not fixture.files.stopped
    assert not any(call[0] in {"list", "get", "put"} for call in fixture.files.calls)
    assert not fixture.runtime.executor.calls
    kept = fixture.hosted.portal.call(fixture.runtime._rid, fixture.saved.request_id)
    assert kept.value.receipt.model_dump_json() == original_receipt


@pytest.mark.parametrize("status,code,detail", [
    (409, "NATIVE_ENVIRONMENT_CONFLICT", {
        "files_check": {"category": "page_changed", "stage": "list", "key_category": "receipt", "cursor_source": "none"}}),
    (503, "NATIVE_ENVIRONMENT_UNKNOWN", {}),
    (403, "NATIVE_FILES_ACCESS_DENIED", {}),
])
def test_changed_etag_does_not_retry_file_checks_unknown_results_or_denied_access(
    file_api, monkeypatch, status, code, detail,
):
    fixture = file_api
    transport = fixture.case.foundry.http._transport
    commands = []
    submitted = NativeSubmit(
        command=fixture.saved.command.model_copy(update={"request_id": "during-follow", "conversation_id": None}),
        capture=fixture.saved.capture)

    async def admit():
        await fixture.original._require_original_session(admitting=True, request=submitted)

    def crossing(packet):
        command = NATIVE_REQUEST.validate_json(packet.content)
        commands.append(command.operation)
        if command.operation == "session_follow":
            return httpx.Response(status, json={"error": {"code": code, "message": "OFFLINE", **detail}})
        response = transport.handle_request(packet)
        if command.operation == "session_read" and commands.count("session_read") == 1:
            fixture.hosted.portal.call(admit)
        return response

    monkeypatch.setattr(fixture.case.foundry.http, "_transport", httpx.MockTransport(crossing))
    failed = follow(fixture)
    assert failed.status_code == status and failed.json()["error"]["code"] == code, failed.text
    assert commands == ["session_read", "session_follow"]
    assert not fixture.files.stopped and not fixture.runtime.executor.calls
