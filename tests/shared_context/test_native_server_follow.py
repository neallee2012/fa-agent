"""Deployment follows use the original API/Hosted boundary without a portal or model."""

import threading
from types import SimpleNamespace

import pytest

from shared_context.native_environment_sync import follow_agent, follow_all
from shared_context.errors import Problem
from test_native_files_api import (
    file_api, file_loops, follow, instruction_models, jwk_client, legacy_source, local_env,
    native_api, signing_key, synthetic_standing_v1, token,
)
from test_native_agent_api import registry_relay_case, team


@pytest.mark.parametrize("file_api", ["foundry-native-v1", "foundry-responses-v1"], indirect=True)
def test_server_follows_the_published_agent_without_any_console_or_turn(file_api):
    fixture = file_api
    legacy_source(fixture)
    result = follow_agent(fixture.case.composition, fixture.source.agent_id)
    assert result["state"] == "ok" and result["version"] == "42"
    assert result["environment"]["state"] == "ready"
    assert result["environment"]["files_migrated"] is False
    before = list(fixture.files.calls)
    repeated = follow_agent(fixture.case.composition, fixture.source.agent_id)
    assert repeated["environment"] == result["environment"]
    assert fixture.files.calls == before and not fixture.runtime.executor.calls


def test_server_reports_a_real_unconfirmed_upload_without_repeating_it(file_api):
    fixture = file_api
    fixture.files.upload_error, fixture.files.target_read_error = True, 403
    with pytest.raises(Problem) as failed:
        follow_agent(fixture.case.composition, fixture.source.agent_id)
    assert failed.value.body["error"]["code"] == "NATIVE_FILES_ACCESS_DENIED"
    assert sum(call[0] == "put" for call in fixture.files.calls) == 1
    assert not fixture.files.stopped and not fixture.runtime.executor.calls


def test_server_reenters_a_follow_started_by_the_portal_without_replacing_its_id(file_api):
    fixture = file_api
    legacy_source(fixture)
    pending = follow(fixture)
    assert pending.status_code == 409 and pending.json()["error"]["code"] == "NATIVE_FILES_UNSUPPORTED"
    result = follow_agent(fixture.case.composition, fixture.source.agent_id)
    assert result["state"] == "ok"
    assert result["environment"]["request_id"] == fixture.command["request_id"]
    assert result["environment"]["files_migrated"] is False
    assert not fixture.runtime.executor.calls


@pytest.mark.parametrize("finishes", [False, True])
def test_server_keeps_original_pending_work_and_wakes_from_readonly_progress(file_api, finishes):
    fixture = file_api
    legacy_source(fixture)
    clock, finished = [0], [False]

    async def quiet(*_args, **_kwargs):
        return finished[0]

    def sleep(seconds):
        clock[0] += seconds
        if finishes and clock[0] >= 46:
            finished[0] = True

    fixture.runtime.task_observer = SimpleNamespace(quiet=quiet)
    original = fixture.saved.model_dump_json()
    result = follow_agent(fixture.case.composition, fixture.source.agent_id,
                          wait_seconds=60, sleep=sleep, clock=lambda: clock[0])
    assert result["state"] == ("ok" if finishes else "pending")
    assert result["environment"]["state"] == ("ready" if finishes else "pending")
    assert result["environment"]["model_started"] is False
    assert fixture.hosted.portal.call(fixture.runtime.read, fixture.saved.request_id).model_dump_json() == original
    assert not fixture.runtime.executor.calls
    if not finishes:
        assert result["environment"]["reason"] == "task_active"
        assert not fixture.files.stopped
        prepares = [body for body, _ in fixture.controls
                    if body.get("operation") == "session_files_omission" and body.get("stage") == "confirm"]
        assert prepares == []
    identities = {body["request_id"] for body, _ in fixture.controls if body.get("operation") == "session_follow"}
    assert len(identities) == 1


def test_server_stops_if_selector_changes_instead_of_retargeting_its_original_operation(file_api, monkeypatch):
    fixture = file_api
    legacy_source(fixture)
    selected = ["42"]
    async def quiet(*_args, **_kwargs):
        return False
    fixture.runtime.task_observer = SimpleNamespace(quiet=quiet)
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version", lambda *_a, **_k: selected[0])
    with pytest.raises(Problem) as changed:
        follow_agent(fixture.case.composition, fixture.source.agent_id,
                     sleep=lambda _seconds: selected.__setitem__(0, "43"))
    assert changed.value.body["error"]["code"] == "NATIVE_ENVIRONMENT_CONFLICT"
    assert not fixture.files.stopped and not fixture.runtime.executor.calls
    assert {body["request_id"] for body, _ in fixture.controls if body.get("operation") == "session_follow"}
    assert all(version != "43" for _sid, version, _op in fixture.control_versions)


def test_deployment_discovers_configured_and_registered_agents_and_does_not_serialize_them(
    registry_relay_case, monkeypatch,
):
    from shared_context import native_environment_sync
    from shared_context.behavior_agent_versions import _agents

    case = registry_relay_case
    _, listed = _agents(case.base.a)
    expected = {target.agent_id for target, _ in listed}
    assert case.base.b.agent_id in expected
    assert any(kind == "registered" and target.agent_id == case.base.b.agent_id for target, kind in listed)
    barrier = threading.Barrier(len(expected))
    observed = []

    def controlled(_owner, agent_id, **_options):
        observed.append(agent_id)
        barrier.wait(timeout=3)
        return {"agent_id": agent_id, "state": "ok", "version": "42"}

    monkeypatch.setattr(native_environment_sync, "follow_agent", controlled)
    results = follow_all(case.base.a)
    assert {row["agent_id"] for row in results} == expected
    assert set(observed) == expected and len(observed) == len(expected)


def test_a_registered_agents_missing_native_policy_is_not_skipped_as_legacy(registry_relay_case, monkeypatch):
    from shared_context.native_agent_api import NativeAgentRelayGateway

    case = registry_relay_case

    def unavailable(*_args, **_kwargs):
        raise Problem(409, "AGENT_SCOPE_MISSING", "OFFLINE registered native policy is missing")

    monkeypatch.setattr(NativeAgentRelayGateway, "_client", unavailable)
    assert follow_all(case.base.a, agent_id=case.base.b.agent_id) == [{
        "agent_id": case.base.b.agent_id, "state": "failed", "reason": "AGENT_SCOPE_MISSING"}]


def test_server_follow_hints_its_team_once_the_agent_is_on_the_new_version(file_api, monkeypatch):
    from shared_context import realtime_push
    from shared_context.behavior_team_registry import resolve_agent_scope
    from test_realtime_push import ENDPOINT, FakeService

    service = FakeService()
    monkeypatch.setattr(realtime_push, "_publishers", {})
    monkeypatch.setattr(realtime_push, "_client_factory", lambda endpoint, hub, client_id: service)
    monkeypatch.setenv(realtime_push.ENDPOINT_ENV, ENDPOINT)
    fixture = file_api
    legacy_source(fixture)
    composition = fixture.case.composition
    assert follow_agent(composition, fixture.source.agent_id)["state"] == "ok"
    team = resolve_agent_scope(composition.runtime.store, composition.config, fixture.source.agent_id).team_id
    assert service.messages() == [{"schema_version": "fa-realtime-event-v1", "type": "agent.environment",
                                   "team_id": team, "agent_id": fixture.source.agent_id, "version": "42"}]
    service.fail = True
    assert follow_agent(composition, fixture.source.agent_id)["state"] == "ok"
