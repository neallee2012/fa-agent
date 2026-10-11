"""OFFLINE real migration/capture/extension EGTs; no installed Azure changes."""
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from azure.core.exceptions import ServiceRequestError
from fastapi.testclient import TestClient

from shared_context.behavior_channels_api import create_channels_app
from shared_context.behavior_channels_migration import (
    ChannelEvidenceFiles, ChannelMigration, ChannelRetirement, channel_key, installed_channels_origin,
)
from shared_context.behavior_migration import MigrationStore, installed_origin
from shared_context.behavior_operations import control_key
from shared_context.behavior_runtime_transition import RetiredTarget
from shared_context.behavior_server_config import ServerConfiguration, ServerConfigurationV2, load_configuration
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from shared_context.source_materials import digest
from shared_context.teams_capture_server import _CaptureStore
from shared_context.teams_identity import PublicCloudTransport
from shared_context.teams_personal import PersonalRouteEnrollment, ROUTE_HANDSHAKE
from test_behavior_migration import fixture
from test_behavior_server import NOW
from test_teams_identity import activity, identity, token
from test_teams_metrics import metrics
from test_m3_auth import jwk_client, signing_key


@pytest.fixture
def channel_migration(metrics, tmp_path, monkeypatch):
    with patch("test_behavior_migration.synthetic_configuration", return_value=metrics.old):
        pin, store, table, original, old, principal = fixture()
    plan = pin.plan(principal, migration_id="old-pin")
    approve = pin.authorize(principal, plan, decision_id="old-pin-approved", expires_at=NOW + timedelta(minutes=5))
    pin.apply(principal, plan, approve)
    setup = metrics.v2.channels.model_copy(update={"valid_until": NOW + timedelta(hours=1)})
    new = ServerConfigurationV2.model_validate_json(json.dumps({
        **old.model_dump(mode="json"), "schema_version": "fa-behavior-server-v2",
        "channels": setup.model_dump(mode="json"),
    }))
    monkeypatch.setattr(PublicCloudTransport, "handle_request",
                        lambda _, request: identity().jwks_transport.handle_request(request))
    capture_store = _CaptureStore(table, old, setup.personal)
    enrollment = PersonalRouteEnrollment(capture_store, setup.personal, clock=lambda: NOW)
    enrollment.capture("Bearer " + token(nbf=int(NOW.timestamp()) - 60, exp=int(NOW.timestamp()) + 300),
                       json.dumps(activity(text=ROUTE_HANDSHAKE)).encode())
    retirement = ChannelRetirement(
        operator_oid=old.identity.human_oid, observer_oid=old.evidence.observer_oid,
        authority_domain=old.table.scope.authority_domain, old_configuration_sha256=old.sha256,
        new_configuration_sha256=new.sha256, old_code_sha256=plan.code_sha256,
        controller_resource_id=old.controller_resource_id, api_resource_id=old.api_resource_id,
        no_active_controller_revision=True, no_running_controller_jobs=True, old_business_admission_disabled=True,
        compute_observation_sha256="c" * 64, observed_at=NOW, valid_until=NOW + timedelta(minutes=15),
        targets=tuple(RetiredTarget(target=t, provider_session_status="idle", provider_observation_sha256="b" * 64)
                      for t in old.active_targets),
    )
    files = ChannelEvidenceFiles(tmp_path / "sources.json", tmp_path / "backups.json", tmp_path / "retirement.json")
    for lane in ("sources", "backups"):
        getattr(files, lane).write_text(pin.observations[lane].model_dump_json(), encoding="utf-8")
    files.retirement.write_text(retirement.model_dump_json(), encoding="utf-8")
    service = ChannelMigration(store, old, new, files, clock=lambda: NOW, allow_apply=True)
    return service, store, table, original, metrics


def approval(service, principal):
    plan = service.plan(principal, transition_id="teams-v2")
    approve = service.authorize(principal, plan, decision_id="approve-fixed-metrics",
                                expires_at=NOW + timedelta(minutes=5))
    return plan, approve


def update(store, key, data, request_id="late-state"):
    old = store.read_snapshot((key,))[key]
    return store.transact(ControlRequest(request_id=request_id, request_digest=digest(data), read_set=(key,)),
                          lambda _: ControlChanges(writes=(ControlWrite(
                              row_id=key, mode="replace" if old else "create", data=data,
                          ),), result={}))


def test_v2_one_egt_preserves_every_business_row_receipt_and_spent_counter(channel_migration):
    service, store, table, original, h = channel_migration
    principal = principal_for(service)
    plan, approved = approval(service, principal)
    before, writes = store.read_inventory(), len(table.submissions)
    result = service.apply(principal, plan, approved)
    assert len(table.submissions) == writes + 1
    verified = service.verify(principal, plan, approved)
    assert verified["activation_performed"] is False
    after = store.read_inventory()
    from shared_context.behavior_migration import installation_record
    installation_key = installation_record(service.old)[0]
    for key, row in before.rows.items():
        if key != installation_key:
            assert after.rows[key].payload == row.payload
    assert all(after.receipts[key].payload == value.payload for key, value in before.receipts.items())
    assert installed_channels_origin(store, service.new) == original
    assert service.new.predecessor().sha256 == service.old.sha256
    budget = after.rows[control_key("budget", "root")].payload["data"]
    assert (budget["remaining_attempts"], budget["remaining_units"]) == (7, 11)


def principal_for(service):
    from shared_context.behavior_auth import BehaviorPrincipal
    return BehaviorPrincipal(service.old.identity.tenant_id, service.old.identity.human_oid,
                             service.old.identity.team_id, "human", None, frozenset({"read", "approve"}))


@pytest.mark.parametrize("pending", ["delivery", "slot", "lease", "completion", "native-launch"])
def test_any_active_pending_or_uncertain_capacity_denies_plan_and_apply(channel_migration, pending):
    service, store, table, _, _ = channel_migration
    principal = principal_for(service)
    plan, approved = approval(service, principal)
    if pending == "delivery":
        key = control_key("delivery-quota", "root", "root")
        data = {**store.read_snapshot((key,))[key].payload["data"], "pending": 1}
    elif pending in {"slot", "lease", "completion"}:
        key = worker_key("pool", service.old.workers.workspace_id)
        data = dict(store.read_snapshot((key,))[key].payload["data"])
        if pending == "completion":
            data["completions"] = ["original-r2"]
        elif pending == "lease":
            data["slots"][0]["lease_until"] = (NOW + timedelta(minutes=1)).isoformat()
        else:
            data["pending"] = ["original-r2"]
    else:
        from shared_context.behavior_foundry import _TargetReservation, native_key
        key = native_key("target", service.old.targets[0].executor_id)
        data = _TargetReservation(
            target=service.old.targets[0], active_launch_id="original-r2",
            configuration_digest=digest({"contract": service.old.workers.model_dump(mode="json"), "mode": "native"}),
        ).model_dump(mode="json")
    update(store, key, data)
    before = len(table.submissions)
    with pytest.raises(Problem):
        service.plan(principal, transition_id="new-id-does-not-escape")
    with pytest.raises(Problem):
        service.apply(principal, plan, approved)
    assert len(table.submissions) == before


def test_lost_apply_ack_reconciles_original_transition_without_reapplying(channel_migration, monkeypatch):
    service, store, table, _, _ = channel_migration
    principal = principal_for(service)
    plan, approved = approval(service, principal)
    original = table.submit_transaction
    failed = False
    def lose(operations, **kwargs):
        nonlocal failed
        result = original(operations, **kwargs)
        key = "behavior:v1:row:" + channel_key("current", service.old.table.scope.authority_domain)
        if not failed and any(op[1]["RowKey"] == key for op in operations):
            failed = True
            raise ServiceRequestError("OFFLINE lost apply ACK")
        return result
    monkeypatch.setattr(table, "submit_transaction", lose)
    with pytest.raises(Problem):
        service.apply(principal, plan, approved)
    before = len(table.submissions)
    assert service.reconcile(principal, plan, approved).payload["result"]["activation_performed"] is False
    service.verify(principal, plan, approved)
    assert len(table.submissions) == before
    with pytest.raises(Problem):
        service.apply(principal, plan, approved)
    assert len(table.submissions) == before


def test_original_typed_history_survives_shared_budget_updates(channel_migration):
    service, store, table, original, _ = channel_migration
    principal = principal_for(service)
    plan, approved = approval(service, principal)
    service.apply(principal, plan, approved)
    service.verify(principal, plan, approved)
    writer = MigrationStore(table, service.old.table.scope,
                            expected_installation=(control_key("server-installation", service.old.table.scope.authority_domain),
                                                   service.new.sha256))
    assert installed_origin(writer, service.new) == original
    key = control_key("budget", "root")
    value = writer.read_snapshot((key,))[key].payload["data"]
    update(writer, key, {**value, "remaining_attempts": value["remaining_attempts"] - 1}, "new-work-reservation")
    assert installed_origin(writer, service.new) == original
    # The original installation is fenced, not silently upgraded or rebootstraped.
    stale = MigrationStore(table, service.old.table.scope,
                           expected_installation=(control_key("server-installation", service.old.table.scope.authority_domain),
                                                  service.old.sha256))
    with pytest.raises(Problem):
        stale.read_snapshot((key,))


def test_operator_api_requires_delegated_scope_and_remains_review_only(channel_migration):
    service, store, table, _, h = channel_migration
    client = TestClient(create_channels_app(service, h.verifier, allow_apply=False))
    base = "/operator/v2/channels/teams-v2/"
    assert client.post(base + "plan", json={}).status_code == 401
    assert client.post(base + "plan", json={}, headers=h.headers("reader")).status_code == 403
    planned = client.post(base + "plan", json={}, headers=h.headers("human"))
    assert planned.status_code == 200, planned.text
    authorized = client.post(base + "authorize", json={
        "plan": planned.json(), "decision_id": "human-policy", "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
    }, headers=h.headers("human"))
    assert authorized.status_code == 200, authorized.text
    before = len(table.submissions)
    applied = client.post(base + "apply", json={"plan": planned.json(), "authorization": authorized.json()},
                          headers=h.headers("human"))
    assert applied.status_code == 403
    assert len(table.submissions) == before


def test_frozen_v1_configuration_still_has_its_original_hash():
    path = Path(".azure") / "v12-sse-current-0917" / "new-config.json"
    if not path.is_file():
        pytest.skip("optional local frozen configuration is not present")
    old = load_configuration(path, environ={})
    assert type(old) is ServerConfiguration
    assert old.sha256 == "868a0560e741ac886649bdfbd65fbb152d8a9946870e13b01fff098f14d4056f"


def test_v1_synthetic_canonical_hash_matches_the_verified_handoff_implementation():
    from shared_context.behavior_server_config import synthetic_configuration
    original = synthetic_configuration()
    assert original.sha256 == "e853591b3bfb0e941a212792d2aff967d303c21481a499924647d8bbf9811067"
    assert len(original.model_dump_json().encode()) == 14917
    assert "channels" not in original.model_dump(mode="json")
    assert "assistant" not in original.model_dump(mode="json")


def test_retirement_cannot_name_an_unrelated_installed_code(channel_migration):
    service, store, table, _, _ = channel_migration
    original = service.evidence.read()["retirement"]
    service.evidence.retirement.write_text(
        original.model_copy(update={"old_code_sha256": "f" * 64}).model_dump_json(), encoding="utf-8")
    before = len(table.submissions)
    with pytest.raises(Problem) as denied:
        service.plan(principal_for(service), transition_id="wrong-installed-code")
    assert denied.value.body["error"]["code"] == "CHANNELS_MIGRATION_DENIED"
    assert len(table.submissions) == before


def test_operator_cli_exports_schema_and_proposes_only_a_new_local_file(metrics, tmp_path):
    import subprocess
    import sys
    command = [sys.executable, "-m", "shared_context.behavior_channels_api"]
    schema = subprocess.run([*command, "schema", "channels"], capture_output=True, text=True,
                            encoding="utf-8", check=True, timeout=30)
    value = json.loads(schema.stdout)
    assert value["properties"]["required_confirmation_level"]["const"] == "provider_accepted"
    old, channels, output = (tmp_path / name for name in ("old.json", "channels.json", "proposed.json"))
    old.write_text(metrics.old.model_dump_json(), encoding="utf-8")
    channels.write_text(metrics.v2.channels.model_dump_json(), encoding="utf-8")
    result = subprocess.run([*command, "propose", "--old-config", str(old),
                             "--channels", str(channels), "--output", str(output)],
                            capture_output=True, text=True, encoding="utf-8", check=True, timeout=30)
    assert json.loads(result.stdout) == {
        "old_sha256": metrics.old.sha256, "new_sha256": metrics.v2.sha256, "applied": False,
    }
    assert ServerConfigurationV2.model_validate_json(output.read_text(encoding="utf-8")) == metrics.v2
