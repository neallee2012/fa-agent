"""OFFLINE synthetic Table/stop/receiver fixtures; no native evidence claim.

The original pin and checksum transition, claimed cancellation, settlement and
inbox ACK producers run on offline adapters. No provider/model calls are made.
"""
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shared_context import behavior_delivery as d, behavior_workers as w
from shared_context.behavior_assistant_wire import Task
from shared_context.behavior_auth import BehaviorAuthSettings, BehaviorJwtVerifier
from shared_context.behavior_foundry import NativeExecutionGrant, native_key
from shared_context.behavior_migration import MigrationStore, installed_origin, installation_record
from shared_context.behavior_operations import control_key
from shared_context.behavior_runtime import runtime_key
from shared_context.behavior_runtime_rotation import (
    CurrentTarget, RotationEvidenceFiles, RotationRetirement, RuntimeRotation, _configuration_change, rotation_key,
)
from shared_context.behavior_server_config import ServerConfiguration
from shared_context.behavior_settlement import RetainedResultSettlement, SettlementRegistration
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_behavior_recovery_history import history as recovery_fixture
from test_behavior_runtime_transition import transition_fixture
from test_behavior_server import NOW
from test_behavior_settlement import OfflineProtection, ack_command, approve, fetch_command
from test_m3_auth import jwk_client, signing_key, token


def write(store, key, data, *, material=None):
    prior = store.read_snapshot((key,))[key]
    request = ControlRequest(request_id=control_key("rotation-offline-fixture", key, digest(data)),
                             request_digest=digest(data), read_set=(key,))
    return store.transact(request, lambda _: ControlChanges(writes=(ControlWrite(
        row_id=key, mode="replace" if prior else "create", data=data, material=material,
    ),), result={"offline_fixture": True}))


@pytest.fixture
def rotated(tmp_path, monkeypatch, request):
    transition, store, table, original, pinned, old, principal = transition_fixture()
    first = transition.plan(principal, transition_id="original-sdk")
    first_approval = transition.authorize(principal, first, decision_id="original-sdk-approved",
                                          expires_at=NOW + timedelta(minutes=5))
    transition.apply(principal, first, first_approval)
    # Reuse the synthetic recovery producer with this already-installed chain.
    # Its seed now replaces the existing reservation; no fixture is native proof.
    transact = store.transact

    def seed(request, decide, **kwargs):
        if request.request_id != "offline-claimed-sdk":
            return transact(request, decide, **kwargs)
        target_key = native_key("target", old.targets[0].executor_id)
        def updated(rows):
            changes = decide(rows)
            return changes.model_copy(update={"writes": tuple(
                item.model_copy(update={"mode": "replace"}) if item.row_id == target_key else item
                for item in changes.writes)})
        return transact(request.model_copy(update={"read_set": (*request.read_set, target_key)}), updated, **kwargs)

    with patch("test_behavior_recovery_history.fixture", return_value=(None, store, table, old, old, principal)), \
            patch.object(store, "transact", seed):
        h = recovery_fixture.__wrapped__(SimpleNamespace(
            param="pre-sdk" if getattr(request, "param", None) == "pre-sdk" else 409,
            include_reports=getattr(request, "param", None) == "native-reports",
        ), monkeypatch)
    b = old.bindings[0]
    core = h.service.delivery
    require_keys = {
        control_key("delivery-conversation", b.owner.conversation_id): d._Conversation(
            configuration_digest=core.configuration_digest(), evidence_mode="native",
            conversation_id=b.owner.conversation_id, owner_record_id=b.owner.owner_record_id, epoch=1, sequence=1),
        control_key("delivery-intent", control_key("delivery-intent-id", b.event_id)): d._Intent(
            configuration_digest=core.configuration_digest(), evidence_mode="native",
            intent_id=control_key("delivery-intent-id", b.event_id), obligation_id=b.obligation_id,
            action="process_event", consumer_version=old.delivery.consumer_version, generation=0,
            state_row_id=control_key("delivery-work-state", b.obligation_id)),
    }
    for key, value in require_keys.items():
        write(store, key, value.model_dump(mode="json"))
    # The recovery-only fixture did not admit a delivery quota. Seed exactly its
    # pending obligation so actual settlement can release all three reservations.
    from shared_context.behavior_delivery import PendingEntry
    for kind, value in (("root", b.owner.root_budget_id), ("team", old.identity.team_id),
                        ("entry", old.delivery.entry_id)):
        key = control_key("delivery-quota", kind, value)
        data = store.read_snapshot((key,))[key].payload["data"]
        write(store, key, {**data, "pending": 1})
    key = control_key("delivery-index", b.owner.root_budget_id)
    data = store.read_snapshot((key,))[key].payload["data"]
    entry = PendingEntry(obligation_id=b.obligation_id, owner_record_id=b.owner.owner_record_id,
                         conversation_id=b.owner.conversation_id, review_at=NOW, generation=0,
                         automation="active", due_pending=False)
    write(store, key, {**data, "entries": [entry.model_dump(mode="json")]})
    h.time = NOW + timedelta(hours=2)
    deps = SimpleNamespace(delivery=core, inbox=core.registration.channel_adapter, workers=h.worker)
    runtime = SimpleNamespace(store=store, dep=deps, mode="native", bindings={b.binding_id: b},
                              _now=lambda: h.time,
                              binding_digest=lambda binding: digest(["fa-behavior-api-v1", "native", binding.model_dump(mode="json")]),
                              job_input=lambda binding: h.worker.inspect(binding.job_id).spec.input,
                              _gate_keys=lambda binding: (
                                  runtime_key("policy", b.binding_id), control_key("owner", b.owner.owner_record_id),
                                  control_key("policy", b.owner.policy_id), control_key("budget", b.owner.root_budget_id)))
    observer = OfflineProtection(h.time, mode="native")
    registration = SettlementRegistration(
        settlement_id="offline-settlement", binding_id=b.binding_id, configuration_sha256=old.sha256,
        expires_at=h.time + timedelta(minutes=10), protection_evidence_id="offline-observation",
        protection_verifier_version="offline-retention-v1", protection_verifier=observer)
    settlement = RetainedResultSettlement(old, runtime, registration)
    deps.inbox.register_retained_results(settlement)
    principal = replace(principal, actions=principal.actions | {"acknowledge"})
    grant = approve(settlement, principal)
    received = settlement.fetch(principal, fetch_command(grant))
    settlement.acknowledge(principal, ack_command(grant, received))

    raw = old.model_dump(mode="json")
    raw["deployment_id"] = "sdk-rotation"
    for field in ("binding_id", "event_id", "job_id", "operation_id", "attempt_id", "grant_id"):
        raw["bindings"][0][field] = "new-" + raw["bindings"][0][field]
    for field in ("owner_record_id", "conversation_id", "policy_id"):
        raw["bindings"][0]["owner"][field] = "new-" + raw["bindings"][0]["owner"][field]
    raw["bindings"][0]["work"]["work_id"] = "new-" + b.work.work_id
    raw["bindings"][0]["claim_ids"] = ["new-claim"]
    raw["policies"][0]["binding_id"] = raw["bindings"][0]["binding_id"]
    raw["policies"][0]["policy"]["binding_digest"] = digest(["fa-behavior-api-v1", "native", raw["bindings"][0]])
    raw["policies"][0]["policy"]["valid_until"] = (h.time + timedelta(hours=1)).isoformat()
    raw["operations"][0]["binding_id"] = raw["bindings"][0]["binding_id"]
    profile = raw["assistant"]["profiles"][0]
    profile.update(binding_id=raw["bindings"][0]["binding_id"], claim_id="new-claim", artifact_id="new-artifact")
    raw["targets"][0].update(agent_version="100", session_id="new-mi-owned-sdk-session")
    new = ServerConfiguration.model_validate_json(json.dumps(raw))
    b2, p2 = new.bindings[0], new.assistant.profiles[0]
    task = Task(job_id=b2.job_id, work_id=b2.work.work_id, definition_digest=digest(b2.work.model_dump(mode="json")),
                profile_digest=digest(p2.model_dump(mode="json")), source=p2.source,
                artifact_id=p2.artifact_id, artifact_version=p2.artifact_version, operation_id=b2.operation_id)
    new_grant = NativeExecutionGrant(
        grant_id=native_key("bootstrap-grant", b2.job_id), controller_id=new.controller_id,
        owner=b2.owner, policy_revision=1, job_id=b2.job_id, definition=b2.work,
        input_digest=hashlib.sha256(task.model_dump_json().encode()).hexdigest(),
        targets=new.active_targets, active=True, remaining_uses=1, not_before=h.time,
        expires_at=h.time + timedelta(minutes=30), workload_version="assistant-v1.2")
    retirement = RotationRetirement(
        **{**transition.retirement.model_dump(), "schema_version": "fa-runtime-rotation-retirement-v1",
           "old_configuration_sha256": old.sha256, "new_configuration_sha256": new.sha256,
           "old_code_sha256": first.code_sha256, "targets": tuple(
               target.model_copy(update={"target": actual}) for target, actual in zip(transition.retirement.targets, old.active_targets)),
           "observed_at": h.time, "valid_until": h.time + timedelta(minutes=15)},
        observer_oid=old.evidence.observer_oid, current_targets=(CurrentTarget(
            target=new.targets[0], session_owner_oid="88888888-8888-4888-8888-888888888888",
            credential_client_id=new.identity.managed_identity_client_id,
            credential_principal_oid="88888888-8888-4888-8888-888888888888",
            credential_tenant_id=new.identity.tenant_id, identity_observation_sha256="b" * 64,
            agent_image_sha256="c" * 64, provider_session_status="idle",
            provider_observation_sha256="a" * 64),))
    paths = RotationEvidenceFiles(tmp_path / "sources.json", tmp_path / "backups.json", tmp_path / "retirement.json")
    for lane, observation in transition.observations.items():
        data = observation.model_dump(mode="json")
        def fresh(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"observed_at", "inventory_observed_at"}:
                        value[key] = h.time.isoformat()
                    elif key in {"valid_until", "inventory_valid_until"}:
                        value[key] = (h.time + timedelta(minutes=15)).isoformat()
                    else:
                        fresh(item)
            elif isinstance(value, list):
                for item in value:
                    fresh(item)
        fresh(data)
        getattr(paths, lane).write_text(json.dumps(data), encoding="utf-8")
    paths.retirement.write_text(retirement.model_dump_json(), encoding="utf-8")
    service = RuntimeRotation(store, old, new, paths, native_grants=(new_grant,),
                              clock=lambda: h.time, allow_apply=True)
    return SimpleNamespace(service=service, store=store, table=table, old=old, new=new, original=original,
                           principal=principal, h=h, paths=paths, settlement=settlement)


def approved(h):
    plan = h.service.plan(h.principal, rotation_id="next-sdk")
    auth = h.service.authorize(h.principal, plan, decision_id="new-human-approval",
                               expires_at=h.h.time + timedelta(minutes=5))
    return plan, auth


@pytest.mark.parametrize("rotated", ["native-reports"], indirect=True)
def test_retained_unknown_native_reports_are_classified_without_promoting_them_to_stop_proof(rotated):
    h = rotated
    before = h.store.read_inventory().entity_sha256
    plan, _ = approved(h)
    assert plan.liability.model_request_sha256 == "e" * 64
    assert h.store.read_inventory().entity_sha256 == before


def test_rotation_preserves_unknown_model_and_all_old_state_in_one_egt(rotated):
    h = rotated
    before = h.store.read_inventory()
    plan, auth = approved(h)
    writes = len(h.table.submissions)
    receipt = h.service.apply(h.principal, plan, auth)
    assert len(h.table.submissions) == writes + 1
    result = h.service.verify(h.principal, plan, auth)
    assert result["activation_performed"] is False
    assert result["liability"]["model_outcome"] == "unknown"
    assert result["liability"]["model_request_sha256"] == h.h.service.scope.model_request_sha256
    after = h.store.read_inventory()
    changed = {image.row_id for image in plan.images}
    for key, row in before.rows.items():
        if key not in changed:
            assert after.rows[key] == row
    for key, row in before.receipts.items():
        assert after.receipts[key] == row
    assert installed_origin(MigrationStore(h.table, h.store._scope), h.new) == h.original
    assert receipt.payload["result"]["counter_sha256"] == plan.counter_sha256
    assert h.h.calls == [h.old.active_targets[0]]


@pytest.mark.parametrize("failure", ["after", "before"])
def test_unknown_commit_reconciles_only_original_request(rotated, failure):
    h = rotated
    plan, auth = approved(h)
    h.table.failure = failure
    with pytest.raises(Problem):
        h.service.apply(h.principal, plan, auth)
    h.table.failure = None
    count = len(h.table.submissions)
    if failure == "after":
        assert h.service.reconcile(h.principal, plan, auth).payload["result"]["rotation_id"] == plan.rotation_id
        h.service.verify(h.principal, plan, auth)
    else:
        with pytest.raises(Problem):
            h.service.reconcile(h.principal, plan, auth)
    assert len(h.table.submissions) == count


@pytest.mark.parametrize("mutation", ["unknown", "malformed", "pending", "active", "ack-loss", "model-known"])
def test_invalid_or_unsettled_history_is_not_a_rotation_waiver(rotated, mutation):
    h, b = rotated, rotated.old.bindings[0]
    if mutation in {"unknown", "malformed"}:
        key, data, material = "unclassified", {"state": "unknown"} if mutation == "unknown" else {"kind": "worker_job"}, None
    elif mutation == "pending":
        key = control_key("delivery-obligation", b.obligation_id)
        data = {**h.store.read_snapshot((key,))[key].payload["data"], "fulfillment": "pending"}
        material = None
    elif mutation == "active":
        key = native_key("target", h.old.targets[0].executor_id)
        data = {**h.store.read_snapshot((key,))[key].payload["data"], "active_launch_id": "not-idle"}
        material = None
    elif mutation == "ack-loss":
        ob = h.store.read_snapshot((control_key("delivery-obligation", b.obligation_id),))[control_key("delivery-obligation", b.obligation_id)]
        key = control_key("inbox-exchange", ob.payload["data"]["final_delivery_id"])
        row = h.store.read_snapshot((key,))[key]
        data = {**row.payload["data"], "acknowledged": False}
        material = d._material(data["authorization_evidence"])
    else:
        from shared_context.behavior_assistant import key as assistant_key
        sid = assistant_key("session-id", h.h.approval.plan.binding.start_request_id)
        key = assistant_key("model-intent", sid, h.h.service.scope.model_request_sha256)
        data, material = {"request_sha256": h.h.service.scope.model_request_sha256, "consumed": False}, None
    write(h.store, key, data, material=material)
    before = h.store.read_inventory().entity_sha256
    with pytest.raises(Problem):
        h.service.plan(h.principal, rotation_id="denied")
    assert h.store.read_inventory().entity_sha256 == before


@pytest.mark.parametrize("field", ["identity", "roots", "pool", "target", "same-session", "claim", "policy"])
def test_configuration_cannot_escape_original_scope_or_reuse_ids(rotated, field):
    h = rotated
    raw = h.new.model_dump(mode="json")
    if field == "identity":
        raw["identity"]["admin_client_id"] = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    elif field == "roots":
        raw["roots"][0]["units"] += 1
    elif field == "pool":
        raw["workers"]["workspace_id"] = "new-pool"
    elif field == "target":
        raw["targets"][0]["agent_name"] = "other-target"
    elif field == "same-session":
        raw["targets"][0]["session_id"] = h.old.targets[0].session_id
    elif field == "claim":
        raw["bindings"][0]["claim_ids"] = list(h.old.bindings[0].claim_ids)
        raw["assistant"]["profiles"][0]["claim_id"] = h.old.assistant.profiles[0].claim_id
    else:
        raw["bindings"][0]["owner"]["policy_id"] = h.old.bindings[0].owner.policy_id
    raw["policies"][0]["policy"]["binding_digest"] = digest(["fa-behavior-api-v1", "native", raw["bindings"][0]])
    with pytest.raises(Problem):
        _configuration_change(h.old, ServerConfiguration.model_validate_json(json.dumps(raw)))


def test_default_disabled_constructor_has_no_io(rotated, monkeypatch):
    h = rotated
    monkeypatch.setattr(RotationEvidenceFiles, "read", lambda _: pytest.fail("constructor I/O"))
    service = RuntimeRotation(h.store, h.old, h.new, h.paths, native_grants=h.service.grants, clock=lambda: NOW)
    with pytest.raises(Problem) as denied:
        service.apply(h.principal, None, None)
    assert denied.value.body["error"]["code"] == "ROTATION_APPLY_DISABLED"


def test_changed_approval_preimage_and_observation_deny_without_writes(rotated):
    h = rotated
    plan, auth = approved(h)
    raw = json.loads(h.paths.retirement.read_bytes())
    raw["compute_observation_sha256"] = "b" * 64
    h.paths.retirement.write_text(json.dumps(raw), encoding="utf-8")
    before = h.store.read_inventory().entity_sha256
    with pytest.raises(Problem):
        h.service.apply(h.principal, plan, auth)
    assert h.store.read_inventory().entity_sha256 == before


def test_old_writer_and_new_writer_cannot_rewrite_retired_policy(rotated):
    h = rotated
    stale = MigrationStore(h.table, h.store._scope,
                           expected_installation=(installation_record(h.old)[0], h.old.sha256))
    installed_origin(stale, h.old)
    plan, auth = approved(h)
    h.service.apply(h.principal, plan, auth)
    with pytest.raises(Problem):
        stale.transact(ControlRequest(request_id="old-writer", request_digest="a" * 64),
                       lambda _: ControlChanges(result={}))
    current = MigrationStore(h.table, h.store._scope)
    installed_origin(current, h.new)
    key = runtime_key("policy", h.old.bindings[0].binding_id)
    with pytest.raises(Problem):
        write(current, key, {**current.read_snapshot((key,))[key].payload["data"], "active": False})


def test_signed_operator_api_default_off_and_no_business_routes(rotated, signing_key, jwk_client):
    from fastapi.testclient import TestClient
    from shared_context.behavior_runtime_rotation_api import create_rotation_app
    h, identity = rotated, rotated.old.identity
    verifier = BehaviorJwtVerifier(identity.auth(), BehaviorAuthSettings(
        identity.human_oid, identity.admin_client_id, identity.team_id), jwk_client)
    claims = {"tid": identity.tenant_id, "iss": f"https://sts.windows.net/{identity.tenant_id}/",
              "aud": identity.audience, "oid": identity.human_oid, "appid": identity.admin_client_id,
              "scp": "Behavior.Read Behavior.Approve"}
    headers = {"Authorization": "Bearer " + token(signing_key, claims)}
    client = TestClient(create_rotation_app(h.service, verifier))
    base = "/operator/v1/runtime-rotations/api-next"
    assert client.post(base + "/plan", json={}).status_code == 401
    assert client.post(base + "/plan", json={"idle": True}, headers=headers).status_code == 422
    response = client.post(base + "/plan", json={}, headers=headers)
    assert response.status_code == 200, response.text
    plan = response.json()
    response = client.post(base + "/authorize", json={"plan": plan, "decision_id": "api-approval",
                           "expires_at": (h.h.time + timedelta(minutes=5)).isoformat()}, headers=headers)
    assert response.status_code == 200, response.text
    command = {"plan": plan, "authorization": response.json()}
    assert client.post(base + "/apply", json=command, headers=headers).status_code == 403
    enabled = TestClient(create_rotation_app(h.service, verifier, allow_apply=True))
    assert enabled.post(base + "/apply", json=command, headers=headers).status_code == 200
    response = enabled.post(base + "/verify", json=command, headers=headers)
    assert response.status_code == 200, response.text
    assert enabled.get("/healthz").json()["work_admissions"] == "disabled"
    assert enabled.post("/behavior/v1/assistant/submit", json={}).status_code == 404


@pytest.mark.parametrize("field", ["session_owner_oid", "credential_client_id", "target"])
def test_target_observation_must_prove_original_identity_and_exact_new_session(rotated, field):
    h = rotated
    raw = json.loads(h.paths.retirement.read_bytes())
    if field == "target":
        raw["current_targets"][0]["target"]["session_id"] = "wrong-session"
    else:
        raw["current_targets"][0][field] = "wrong-owner"
    h.paths.retirement.write_text(json.dumps(raw), encoding="utf-8")
    count = len(h.table.submissions)
    with pytest.raises(Problem):
        h.service.plan(h.principal, rotation_id="wrong-target")
    assert len(h.table.submissions) == count


@pytest.mark.parametrize("field", ["code", "budget", "policy", "authorization", "before-image", "readonly"])
def test_changed_approved_inputs_never_commit(rotated, monkeypatch, field):
    h = rotated
    plan, auth = approved(h)
    if field == "code":
        monkeypatch.setattr("shared_context.behavior_migration.code_sha256", lambda: "0" * 64)
    elif field == "budget":
        key = control_key("budget", h.old.bindings[0].owner.root_budget_id)
        row = h.store.read_snapshot((key,))[key]
        write(h.store, key, {**row.payload["data"], "remaining_units": row.payload["data"]["remaining_units"] - 1})
    elif field == "policy":
        key = runtime_key("policy", h.old.bindings[0].binding_id)
        row = h.store.read_snapshot((key,))[key]
        write(h.store, key, {**row.payload["data"], "valid_until": (h.h.time + timedelta(hours=1)).isoformat()})
    elif field == "authorization":
        auth = auth.model_copy(update={"operator_oid": "other-human"})
    elif field == "before-image":
        image = plan.images[0].model_copy(update={"before_etag": 'W/"tampered"'})
        plan = plan.model_copy(update={"images": (image, *plan.images[1:])})
    else:
        h.principal = replace(h.principal, actions=frozenset({"read"}))
    before = h.store.read_inventory().entity_sha256
    with pytest.raises(Problem):
        h.service.apply(h.principal, plan, auth)
    assert h.store.read_inventory().entity_sha256 == before


def test_typed_arbitrary_unknown_business_operation_and_wrong_key_are_denied(rotated):
    from shared_context.behavior_operations import _Operation
    h, b = rotated, rotated.old.bindings[0]
    operation = _Operation(operation_id=b.operation_id, owner_record_id=b.owner.owner_record_id,
                           conversation_id=b.owner.conversation_id, root_budget_id=b.owner.root_budget_id,
                           description_digest="a" * 64, adapter_contract_digest="b" * 64,
                           effect_state="unknown", current_attempt_id="unresolved-real-operation")
    write(h.store, control_key("operation", b.operation_id), operation.model_dump(mode="json"))
    with pytest.raises(Problem) as denied:
        h.service.plan(h.principal, rotation_id="real-operation-unknown")
    assert denied.value.body["error"]["code"] == "ROTATION_OPERATION_UNKNOWN"


def test_typed_budget_under_arbitrary_key_is_not_classified(rotated):
    h = rotated
    key = control_key("budget", h.old.bindings[0].owner.root_budget_id)
    write(h.store, "unclassified-budget-copy", h.store.read_snapshot((key,))[key].payload["data"])
    with pytest.raises(Problem):
        h.service.plan(h.principal, rotation_id="arbitrary-typed-row")


def test_late_immutable_revocation_and_current_work_do_not_renew_old_grants(rotated):
    from shared_context.behavior_settlement import RevokeSettlement
    h = rotated
    plan, auth = approved(h)
    h.service.apply(h.principal, plan, auth)
    h.service.verify(h.principal, plan, auth)
    writer = MigrationStore(h.table, h.store._scope)
    installed_origin(writer, h.new)
    old_plan = h.settlement.registration.settlement_id
    from shared_context.behavior_settlement import settlement_key
    authorization_id = settlement_key("authorization", old_plan)
    # New independent operator only appends an exact revocation, never fetches or
    # ACKs with this old settlement service after the installation epoch changes.
    from shared_context.behavior_settlement import _Revocation
    command = RevokeSettlement(authorization_id=authorization_id, decision_id="late-revoke")
    original = writer.read_snapshot((authorization_id,))[authorization_id].payload["data"]
    rev = _Revocation(command=command, principal_id=h.principal.oid,
                      authorization_sha256=digest(original), revoked_at=h.h.time + timedelta(hours=1))
    key = settlement_key("revocation", old_plan)
    request = ControlRequest(request_id=key, request_digest=digest(command.model_dump(mode="json")),
                             read_set=(authorization_id,))
    writer.transact(request, lambda _: ControlChanges(writes=(ControlWrite(
        row_id=key, mode="create", data=rev.model_dump(mode="json"), material=d._material(rev.model_dump(mode="json"))),),
        result={"authorization_id": authorization_id, "revoked": True}))
    count = len(h.table.submissions)
    assert installed_origin(MigrationStore(h.table, h.store._scope), h.new) == h.original
    assert len(h.table.submissions) == count
    # The new installation may consume its own budget; it cannot reset the old
    # catalogue or be mistaken for an immediate post-cutover verification.
    budget_key = control_key("budget", h.new.roots[0].root_budget_id)
    budget = writer.read_snapshot((budget_key,))[budget_key].payload["data"]
    write(writer, budget_key, {**budget, "remaining_units": budget["remaining_units"] - 1})
    assert installed_origin(MigrationStore(h.table, h.store._scope), h.new) == h.original
    with pytest.raises(Problem):
        h.service.verify(h.principal, plan, auth)


@pytest.mark.parametrize("outcome", ["success", "lost-ack", "503"])
def test_real_table_sdk_single_egt_and_no_hidden_retry(rotated, outcome):
    import base64
    from urllib.parse import parse_qs, urlsplit
    from azure.data.tables import TableClient
    from test_behavior_store import Credential, WireTransport, json_response

    h = rotated
    class QueryTransport(WireTransport):
        def send(self, request, **kwargs):
            if request.method == "GET" and urlsplit(request.url).path.rstrip("()") == "/Behavior":
                self.requests.append(request)
                assert parse_qs(urlsplit(request.url).query)["$filter"] == ["PartitionKey eq 'control-one'"]
                items = [{**entity, "Payload": base64.b64encode(entity["Payload"]).decode(),
                          "Payload@odata.type": "Edm.Binary", "odata.etag": entity.metadata["etag"]}
                         for (table, partition, _), entity in sorted(self.backing.rows.items())
                         if table == "Behavior" and partition == h.store._scope.partition_key]
                return json_response(request, 200, {"value": items}, {})
            return super().send(request, **kwargs)
    transport = QueryTransport()
    transport.backing = h.table.backing
    with TableClient(h.old.table.endpoint, h.old.table.scope.table_name, credential=Credential(),
                     transport=transport) as client:
        store = MigrationStore(client, h.old.table.scope)
        h.service = RuntimeRotation(store, h.old, h.new, h.paths, native_grants=h.service.grants,
                                    clock=lambda: h.h.time, allow_apply=True)
        # Receipt-only diagnostic reservations consume the SAME root. Result
        # receipts add no row/grant/job and never authorize a model send.
        key = control_key("budget", h.old.roots[0].root_budget_id)
        budget = store.read_snapshot((key,))[key].payload["data"]
        diagnostic = ControlRequest(request_id="root-model-diagnostic-reservation",
                                    request_digest="c" * 64, read_set=(key,))
        store.transact(diagnostic, lambda _: ControlChanges(writes=(ControlWrite(
            row_id=key, mode="replace", data={**budget, "remaining_attempts": budget["remaining_attempts"] - 1,
                                             "remaining_units": budget["remaining_units"] - 1}),),
            result={"max_model_posts": 1, "model_posts": 0, "root_budget_id": h.old.roots[0].root_budget_id}))
        for index in range(100):
            store.transact(ControlRequest(request_id="diagnostic-receipt-" + str(index), request_digest="d" * 64),
                           lambda _: ControlChanges(result={"receipt_only_offline_diagnostic": True}))
        before = store.read_inventory()
        plan, auth = approved(h)
        posts = len([request for request in transport.requests if request.method == "POST"])
        transport.after_apply = "lost" if outcome == "lost-ack" else None
        transport.fail_post = outcome == "503"
        if outcome == "success":
            h.service.apply(h.principal, plan, auth)
        else:
            with pytest.raises(Problem) as unknown:
                h.service.apply(h.principal, plan, auth)
            assert unknown.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
        assert len([request for request in transport.requests if request.method == "POST"]) == posts + 1
        transport.after_apply, transport.fail_post = None, False
        if outcome != "503":
            h.service.reconcile(h.principal, plan, auth)
            h.service.verify(h.principal, plan, auth)
            after = store.read_inventory()
            assert after.rows[key] == before.rows[key]
            assert all(after.receipts[key] == row for key, row in before.receipts.items())
            assert len(transport.parts[-1]) < 100
            assert transport.parts[-1][0][2]["If-Match"] == plan.guard.etag
        else:
            with pytest.raises(Problem):
                h.service.reconcile(h.principal, plan, auth)
        assert len([request for request in transport.requests if request.method == "POST"]) == posts + 1


@pytest.mark.parametrize("change", ["invalid-json", "expired", "wrong-observer"])
def test_bad_server_observations_are_not_user_waivers(rotated, change):
    h = rotated
    if change == "invalid-json":
        h.paths.retirement.write_bytes(b"not-json")
    elif change == "expired":
        h.h.time += timedelta(hours=1)
    else:
        data = json.loads(h.paths.retirement.read_bytes())
        data["observer_oid"] = h.old.identity.human_oid
        h.paths.retirement.write_text(json.dumps(data), encoding="utf-8")
    count = len(h.table.submissions)
    with pytest.raises(Problem):
        h.service.plan(h.principal, rotation_id="bad-observation")
    assert len(h.table.submissions) == count


@pytest.mark.parametrize("changes", [
    {"scp": "Behavior.Read"},
    {"oid": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"},
    {"appid": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"},
])
def test_nonoperator_http_is_rejected_before_parsing_or_inventory(rotated, signing_key, jwk_client, changes):
    from fastapi.testclient import TestClient
    from shared_context.behavior_runtime_rotation_api import create_rotation_app
    h, identity = rotated, rotated.old.identity
    verifier = BehaviorJwtVerifier(identity.auth(), BehaviorAuthSettings(
        identity.human_oid, identity.admin_client_id, identity.team_id), jwk_client)
    claims = {"tid": identity.tenant_id, "iss": f"https://sts.windows.net/{identity.tenant_id}/",
              "aud": identity.audience, "oid": identity.human_oid, "appid": identity.admin_client_id,
              "scp": "Behavior.Read Behavior.Approve", **changes}
    headers = {"Authorization": "Bearer " + token(signing_key, claims), "Content-Type": "application/json"}
    with patch.object(h.store, "read_inventory", side_effect=AssertionError("unauthorized inventory read")):
        response = TestClient(create_rotation_app(h.service, verifier)).post(
            "/operator/v1/runtime-rotations/not-authorized/plan", content=b"not-json" * 4000, headers=headers)
    assert response.status_code in {401, 403}


def test_bootstrap_remains_forbidden_and_late_unknown_rows_fail_closed(rotated):
    from shared_context.behavior_server import bootstrap
    h = rotated
    plan, auth = approved(h)
    h.service.apply(h.principal, plan, auth)
    count = len(h.table.submissions)
    composition = SimpleNamespace(config=h.new, runtime=SimpleNamespace(store=h.store), clients=(h.table,))
    with patch("shared_context.behavior_server._validate_approval", return_value=()):
        with pytest.raises(Problem) as denied:
            bootstrap(composition, None, stage="records")
    assert denied.value.body["error"]["code"] == "MIGRATION_BOOTSTRAP_DISABLED"
    assert len(h.table.submissions) == count
    write(h.store, "late-unknown-row", {"unknown_business_effect": True})
    with pytest.raises(Problem):
        installed_origin(MigrationStore(h.table, h.store._scope), h.new)


def test_existing_subject_index_can_advance_without_editing_old_claims_or_policies(rotated):
    from shared_context.memory_governance import SubjectState
    from shared_context.behavior_runtime_rotation_history import binding_scopes
    h = rotated
    b = h.old.bindings[0]
    scope = binding_scopes(h.old, b)[0]
    key = control_key("memory-subject", scope.key, b.subject)
    state = SubjectState(scope=scope, subject=b.subject, sequence=0)
    write(h.store, key, state.model_dump(mode="json"))
    plan, auth = approved(h)
    h.service.apply(h.principal, plan, auth)
    writer = MigrationStore(h.table, h.store._scope)
    installed_origin(writer, h.new)
    write(writer, key, state.model_copy(update={"sequence": 1}).model_dump(mode="json"))
    assert installed_origin(MigrationStore(h.table, h.store._scope), h.new) == h.original
    old_policy = runtime_key("policy", b.binding_id)
    assert writer.read_snapshot((old_policy,))[old_policy].payload["data"] == h.old.policies[0].policy.model_dump(mode="json")
