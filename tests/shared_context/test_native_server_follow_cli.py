"""Deployment main builds a fresh API composition; only platform/Storage I/O is synthetic."""

import json

import pytest

from shared_context import behavior_agent_versions as versions, behavior_server as server
from shared_context.behavior_native_sessions import NativeSessions
from shared_context.behavior_native_turn_wire import NativeEnvironmentView
from shared_context.errors import Problem
from shared_context.native_environment_sync import follow_all
from test_agent_versions import Foundry, REAL_CURRENT
from test_fresh_installation import fresh_case, initialize, writes
from test_m3_auth import jwk_client, signing_key


@pytest.mark.parametrize("mode,native_outcome", [
    ("follow", "ready"), ("follow", "failed"), ("follow", "pending"), ("follow", "scope-error"),
    ("defer", "ready"), ("candidate", "ready"),
])
def test_real_main_initializes_policy_and_follows_native_but_reports_legacy_skipped(
    fresh_case, monkeypatch, tmp_path, capsys, mode, native_outcome,
):
    case = fresh_case
    initialize(case)
    original_writes = writes(case)
    config = tmp_path / "deployment-config.json"
    config.write_text(case.config.model_dump_json(), encoding="utf-8")
    monkeypatch.delenv("BEHAVIOR_SERVER_CONFIG_JSON", raising=False)
    foundry = Foundry(*case.config.targets, ready_after=0)
    for target in case.config.targets:
        foundry.add(target.agent_name, 41, "previous-build")
    monkeypatch.setattr(versions, "current_hosted_version", REAL_CURRENT)
    monkeypatch.setattr(versions, "_POLL_SECONDS", 0)
    build, created, advanced = server.build_server, [], []

    def fresh(configuration):
        composition = build(configuration, **case.options,
                            hosted_versions=versions.HostedVersions(lambda _endpoint: foundry))
        assert composition.runtime.work_policy is None
        assert composition._peers == {}
        created.append(composition)
        return composition

    def observed_environment(sessions, client, command, *, before_send, advancing):
        assert sessions.composition.runtime.work_policy is not None
        before_send()
        if advancing:
            advanced.append(client.target.agent_id)
        peer = client.target.agent_id == case.config.targets[1].agent_id
        if peer and native_outcome in {"failed", "scope-error"}:
            raise Problem(503, "NATIVE_FILES_UNKNOWN" if native_outcome == "failed" else "AGENT_SCOPE_MISSING",
                          "OFFLINE native follow failure, not a legacy policy absence")
        return NativeEnvironmentView(
            agent_id=client.target.agent_id, request_id=command.request_id,
            previous_hosted_session_id=client.target.session_id,
            previous_agent_version=client.target.agent_version,
            hosted_session_id="OFFLINE-published-session-" + client.target.agent_id,
            hosted_agent_version=command.expected_agent_version,
            state="pending" if peer and native_outcome == "pending" else "ready",
            reason="task_active" if peer and native_outcome == "pending" else None)

    monkeypatch.setattr(server, "build_server", fresh)
    monkeypatch.setattr(NativeSessions, "advance", lambda self, *a, **kw: observed_environment(
        self, *a, **kw, advancing=True))
    monkeypatch.setattr(NativeSessions, "observe_automatic", lambda self, *a, **kw: observed_environment(
        self, *a, **kw, advancing=False))
    args = ["sync", "--config", str(config), "--image", "offline-new:image", "--follow-wait-seconds", "1"]
    if mode != "candidate":
        args.append("--route")
    if mode == "defer":
        args.append("--defer-follow")
    result = versions.main(args)
    output = capsys.readouterr()
    failing = mode == "follow" and native_outcome != "ready"
    assert result == int(failing), output.out + output.err
    rows = [json.loads(line) for line in output.out.splitlines()]
    by_id = {row["agent_id"]: row for row in rows}
    native = {target.agent_id for target in case.config.targets[:2]}
    legacy = {target.agent_id for target in case.config.targets[2:]}
    assert len(created) == 1 and created[0].closed
    assert set(by_id) == native | legacy
    assert writes(case) == original_writes
    if mode != "follow":
        assert advanced == [] and created[0].runtime.work_policy is None and created[0]._peers == {}
        assert all(row["state"] == "ok" and "environment_follow" not in row for row in rows)
        return
    assert set(advanced) == native
    assert all(row["route"] == "ok" for row in rows)
    assert by_id[case.config.targets[0].agent_id]["environment_follow"]["state"] == "ok"
    peer = by_id[case.config.targets[1].agent_id]
    if failing:
        assert peer["state"] == "failed" and peer["version_state"] == "ok"
        assert peer["environment_follow"]["state"] == ("pending" if native_outcome == "pending" else "failed")
        assert peer["reason"] == {
            "pending": "NATIVE_ENVIRONMENT_PENDING", "failed": "NATIVE_FILES_UNKNOWN", "scope-error": "AGENT_SCOPE_MISSING",
        }[native_outcome]
    else:
        assert all(row["state"] == "ok" for row in rows)
        assert len(advanced) == len(native)
        assert peer["environment_follow"]["state"] == "ok"
    assert all(by_id[identifier]["environment_follow"] == {
        "agent_id": identifier, "state": "skipped", "reason": "AGENT_SCOPE_MISSING",
    } for identifier in legacy)


def test_missing_attached_policy_is_reported_without_a_traceback_or_fabricated_installation(fresh_case):
    initialize(fresh_case)
    assert fresh_case.composition.runtime.work_policy is None
    before = writes(fresh_case)
    results = follow_all(fresh_case.composition)
    assert {row["agent_id"] for row in results} == {target.agent_id for target in fresh_case.config.targets}
    assert all(row["state"] == "failed" and row["reason"] == "WORK_POLICY_DISABLED" for row in results)
    assert fresh_case.composition.runtime.work_policy is None and not fresh_case.calls
    assert writes(fresh_case) == before
