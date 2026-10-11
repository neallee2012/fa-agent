"""Actual installed migration -> signed activity -> SDK producer -> native sender.

Only remote Table/model/Connector/token/SDK process transports are substitutes.
No installed/history/ACL/rule validator is mocked, and no native claim is made.
"""
import json
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from datetime import timedelta
from dataclasses import replace

import httpx
import pytest
from azure.core.credentials import AccessToken
from fastapi.testclient import TestClient

from shared_context.behavior_channels_migration import (
    channel_key, ChannelMigration, ChannelEvidenceFiles,
    parse_channel_installation, parse_channel_approval,
)
from shared_context.behavior_migration import installed_origin, installation_record, MigrationStore
from shared_context.behavior_operations import control_key
from shared_context.behavior_runtime import runtime_key
from shared_context.behavior_server import build_server, application
from shared_context.behavior_server_config import ServerConfiguration, ServerConfigurationV2
from shared_context.teams_identity import PublicCloudTransport, BOT_SCOPE, digest
from shared_context.teams_native import TeamsNativeEvidence
from test_behavior_channels_pin_refresh import pin_channel, channel_migration, approved, metrics, jwk_client, signing_key
from test_behavior_channels_migration import principal_for
from test_behavior_server import Harness, NOW, TOKENIZER_TEST_CACHE
from test_behavior_store import Credential
from test_behavior_runtime_rotation_sources import QueryTransport
from test_teams_metrics import SdkProtocolExecutor
from test_teams_identity import identity, activity, token
from conftest import InMemoryRepository, FakeMemory
from shared_context.service import ContextService
from shared_context.m3_artifacts import ArtifactService
from shared_context.m3_store import InMemoryArtifactStore
from shared_context.m3_auth import ApprovalSettings
from shared_context.models import CreateWork
from shared_context.errors import Problem
from shared_context.source_materials import SourceMaterials, AuthenticatedSourceContext
from shared_context.source_reviews import DurableHumanReviewVerifier, HumanReviewPolicy, HumanReviewCommand
from shared_context.memory_governance import MemoryGovernance, ReviewedClaim


@pytest.fixture(autouse=True)
def offline_socket_guard(monkeypatch):
    original = socket.socket.connect
    fallback = getattr(socket, "_fallback_socketpair", lambda: None).__code__
    def connect(sock, address):
        if sys._getframe(1).f_code is fallback and address[0] in {"127.0.0.1", "::1"}:
            return original(sock, address)
        raise AssertionError("external socket forbidden in installed Teams regression")
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("external connection forbidden"))


def reviewed_runtime_template(store, config, principal):
    """A new ordinary fixture review keeps the prior immutable claim intact."""
    b, policy = config.bindings[0], config.policies[0].policy
    old = next(ReviewedClaim.model_validate_json(json.dumps(row.payload["data"]))
               for row in store.read_inventory().rows.values()
               if row.payload["data"].get("candidate", {}).get("claim_id") == config.assistant.profiles[0].claim_id)
    context = AuthenticatedSourceContext(b.tenant_id, b.team_id, principal.source_actor_id, b.ingress_id, policy.allowed_purposes)
    sources = SourceMaterials(store, clock=lambda: NOW)
    reviewer = DurableHumanReviewVerifier(sources, HumanReviewPolicy(policy_version=config.review_policy_version), clock=lambda: NOW)
    candidate = old.candidate.model_copy(update={
        "version": 2, "supersedes": old.reference.sha256, "text": "The accepted metrics source has been reviewed.",
    })
    preview = reviewer.preview(principal, context, candidate)
    command = HumanReviewCommand(review_id="offline-current-approved-template", candidate=candidate,
        candidate_sha256=preview.candidate_sha256, evidence_sha256=preview.evidence_sha256,
        policy_version=preview.policy_version, decision="approve",
        reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"),
        reason="Offline current fixed metrics template.")
    reviewer.commit_decision(principal, context, reviewer.plan_decision(principal, context, command))
    memory = MemoryGovernance(sources, clock=lambda: NOW, token_counter=len, review_verifier=reviewer)
    memory.commit_claim(context, memory.plan_claim(context, candidate))


@pytest.fixture(autouse=True)
def isolated_test_paths_and_finite_initial_budget(monkeypatch, tmp_path):
    import test_teams_metrics as producer
    import test_behavior_migration as migration
    original = producer.build_config
    def configured(*args):
        old, new = original(*args)
        output = []
        for model, config in ((ServerConfiguration, old), (ServerConfigurationV2, new)):
            raw = config.model_dump(mode="json")
            raw["evidence"]["path"] = str(tmp_path / "parent-evidence.json")
            raw["tokenizer"]["cache_directory"] = str(TOKENIZER_TEST_CACHE)
            if "channels" in raw:
                raw["channels"]["native_evidence_path"] = str(tmp_path / "teams-native.json")
            output.append(model.model_validate_json(json.dumps(raw)))
        return tuple(output)
    monkeypatch.setattr(producer, "build_config", configured)
    budget, quota = migration.BudgetRecord, migration.QuotaRecord
    def initial_budget(**data):
        if data.get("remaining_attempts") == 7 and data.get("remaining_units") == 11:
            data.update(remaining_attempts=20, remaining_units=40)
        return budget(**data)
    def initial_quota(**data):
        if data.get("available_control_units") == 13:
            data.update(available_control_units=1000, scans_remaining=64)
        return quota(**data)
    monkeypatch.setattr(migration, "BudgetRecord", initial_budget)
    monkeypatch.setattr(migration, "QuotaRecord", initial_quota)


@pytest.fixture(params=("pin-v2", "channels-v1"))
def live_parent(pin_channel, monkeypatch, jwk_client, request, tmp_path):
    service, store, table, original, h = pin_channel
    reviewed_runtime_template(store, service.old, principal_for(service))
    if request.param == "channels-v1":
        new = ServerConfigurationV2.model_validate_json(json.dumps({
            **service.old.model_dump(mode="json"), "schema_version": "fa-behavior-server-v2",
            "channels": service.new.channels.model_dump(mode="json"),
        }))
        retirement = service.evidence.read()["retirement"].model_copy(update={"new_configuration_sha256": new.sha256})
        retirement_path = tmp_path / "legacy-channel-retirement.json"
        retirement_path.write_text(retirement.model_dump_json(), encoding="utf-8")
        service = ChannelMigration(store, service.old, new, ChannelEvidenceFiles(
            service.evidence.previous_sources, service.evidence.previous_backups, retirement_path),
            clock=lambda: NOW, allow_apply=True)
    principal, plan, authorization = approved(service)
    before = store.read_inventory()
    service.apply(principal, plan, authorization)
    service.verify(principal, plan, authorization)
    transport = QueryTransport()
    transport.backing = table.backing
    parent = build_server(service.new, credential=Credential(), table_transport=transport,
        key_client=jwk_client, clock=lambda: NOW,
        source_transport=httpx.MockTransport(lambda _: pytest.fail("source Blob IO forbidden")),
        native_transports={t.executor_id: httpx.MockTransport(lambda _: pytest.fail("Foundry IO forbidden"))
                           for t in service.new.active_targets})
    auth = service.new.identity.auth()
    actor = auth.actors[service.new.bindings[0].agent_oid]
    context = ContextService(InMemoryRepository(), FakeMemory(), auth)
    context.create(actor, actor.team, service.new.assistant.profiles[0].legacy_source_work, CreateWork(goal="Original metrics owner"))
    h.artifacts = ArtifactService(context, InMemoryArtifactStore(auth.tenant_id), ApprovalSettings(
        service.new.identity.human_oid, service.new.identity.admin_client_id, actor.team))
    parent.assistant.artifacts = h.artifacts
    monkeypatch.setattr("test_teams_metrics.A", actor)
    parent.assistant.start()
    h.runtime = parent.runtime
    h.assistant = parent.assistant.authority
    h.sdk = SdkProtocolExecutor(h)
    parent.runtime.dep.workers.adapter = replace(h.sdk.adapter(), verify_readiness=parent.catalog.worker)
    values = service.evidence.read()
    evidence = Harness.evidence(SimpleNamespace(config=service.new, runtime=parent.runtime,
                                                sources=values["sources"], backups=values["backups"]))
    Path(service.new.evidence.path).write_text(evidence.model_dump_json(), encoding="utf-8")
    sent, acquired, token_hook = [], [], [None]
    def network(_, request):
        if request.url.host == "login.botframework.com":
            return identity().jwks_transport.handle_request(request)
        sent.append(request)
        return httpx.Response(200, json={"id": "offline-installed-provider-receipt"})
    class BotCredential:
        def __init__(self, **_):
            pass
        def get_token(self, scope):
            acquired.append(scope)
            if token_hook[0] is not None:
                token_hook[0]()
            return AccessToken("OFFLINE-ONLY-TOKEN", int(NOW.timestamp()) + 600)
        def close(self):
            pass
    monkeypatch.setattr(PublicCloudTransport, "handle_request", network)
    monkeypatch.setattr("shared_context.teams_personal.ManagedIdentityCredential", BotCredential)
    parent.installation()
    parent.runtime.dep.workers.publish_readiness(evidence.worker.evidence_id)
    parent.runtime.dep.inbox.publish_readiness(evidence.inbox.evidence_id)
    parent.catalog.require_activation()
    parent.runtime.validate_startup()
    teams = parent.teams
    capture_key = control_key("teams-capture-route-binding", service.new.channels.personal.configuration_digest)
    capture = parent.runtime.store.read_snapshot((capture_key,))[capture_key].payload["data"]
    ikey, installation = installation_record(service.new)
    native = TeamsNativeEvidence(
        evidence_id=service.new.channels.native_evidence_id,
        configuration_digest=parent.runtime.dep.delivery.configuration_digest(),
        server_configuration_sha256=service.new.sha256, control_binding=service.new.delivery.control_binding,
        personal_configuration_digest=service.new.channels.personal.configuration_digest,
        settings_digest=digest(teams.channel.adapter.settings.model_dump(mode="json")),
        installation_row_id=ikey, installation_data_sha256=digest(installation),
        route_witness_sha256=capture["route_material_sha256"], capture_binding_sha256=digest(capture),
        bot_registration_sha256="b"*64, personal_installation_sha256="c"*64, package_sha256="d"*64,
        callback_url=service.new.channels.personal.endpoint, bot_app_id=service.new.channels.personal.uami_client_id,
        bot_principal_id=service.new.channels.personal.uami_principal_id, readiness=evidence.delivery,
        observed_at=NOW, expires_at=NOW+timedelta(minutes=1),
    )
    Path(service.new.channels.native_evidence_path).write_text(native.model_dump_json(), encoding="utf-8")
    teams.channel.adapter.publish_installation()
    client = TestClient(application(parent), raise_server_exceptions=True)
    yield SimpleNamespace(service=service, parent=parent, client=client, h=h, before=before,
                          sent=sent, acquired=acquired, original=original, authorization=authorization,
                          kind=request.param, table=table, token_hook=token_hook)
    client.close()
    parent.close()


def signed_receive(h, **updates):
    header = {"Authorization": "Bearer " + token(nbf=int(NOW.timestamp())-60, exp=int(NOW.timestamp())+300)}
    return h.client.post("/behavior/v1/teams/activities", json=activity(**{"text": "FA metrics", **updates}), headers=header)


def controller_step(h, binding):
    try:
        return h.parent.runtime.controller.step(binding.binding_id)
    except Exception:
        if h.h.sdk.error is not None:
            raise h.h.sdk.error
        raise


def save_result(name, data):
    if directory := os.environ.get("LIVE_READER_RESULTS"):
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        with (root / (name + ".json")).open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)


def test_applied_pin_version_signed_metrics_reaches_one_native_send_and_original_receipt(live_parent):
    h = live_parent
    before_budget = h.parent.runtime.store.read_snapshot((control_key("budget", "root"),))[control_key("budget", "root")]
    pointer = h.parent.teams.pointer()
    assert pointer.schema_version == ("fa-channel-pin-installation-v2" if h.kind == "pin-v2" else "fa-channel-installation-v1")
    response = signed_receive(h)
    assert response.status_code == 200, response.text
    record = h.parent.teams.for_obligation(response.json()["obligation_id"])
    assert record.binding.job_id != h.service.old.bindings[0].job_id
    assert record.profile.source == h.service.old.assistant.profiles[0].source
    result = controller_step(h, record.binding)
    assert result.state == "succeeded" and result.obligation_fulfilled
    assert len(h.sent) == 1 and h.acquired == [BOT_SCOPE]
    adapter = h.parent.teams.channel.adapter
    send = adapter._send_row(result.delivery_id)
    assert send is not None and send.native_evidence_id == h.service.new.channels.native_evidence_id
    receipt = adapter.inspect(result.delivery_id)
    assert receipt["confirmation_level"] == "provider_accepted"
    grant_key = control_key("grant", channel_key("send-grant", result.delivery_id))
    grant = h.parent.runtime.store.read_snapshot((grant_key,))[grant_key].payload["data"]
    assert grant["approval_id"] == h.authorization.decision_id
    assert grant["root_budget_id"] == h.service.old.roots[0].root_budget_id
    again = signed_receive(h)
    assert again.status_code == 200
    assert len(h.sent) == 1 and len(h.h.sdk.starts) == 1
    after = h.parent.runtime.store.read_inventory()
    assert all(after.receipts[key] == row for key, row in h.before.receipts.items())
    personal_digest = h.service.new.channels.personal.configuration_digest
    preserved_keys = {
        control_key("teams-personal-route", personal_digest),
        control_key("teams-capture-route-binding", personal_digest),
        runtime_key("policy", h.service.old.bindings[0].binding_id),
    }
    preserved_keys.update(key for key, row in h.before.rows.items()
                          if row.payload["data"].get("candidate") is not None
                          or row.payload["data"].get("reference", {}).get("source_id") is not None)
    assert all(after.rows[key] == h.before.rows[key] for key in preserved_keys)
    assert installed_origin(h.parent.runtime.store, h.service.new) == h.original
    after_budget = after.rows[control_key("budget", "root")]
    assert after_budget.payload["data"]["remaining_attempts"] < before_budget.payload["data"]["remaining_attempts"]
    save_result("signed-native-send-" + h.kind, {
        "actual_environment": "NOT_RUN", "installation_schema": pointer.schema_version,
        "approval_schema": h.authorization.schema_version, "approval_action": h.authorization.action,
        "signed_activity_http": response.status_code, "state": result.state,
        "obligation_fulfilled": result.obligation_fulfilled,
        "dynamic_job_id": record.binding.job_id, "template_job_id": h.service.old.bindings[0].job_id,
        "source_exact": record.profile.source.model_dump(mode="json"),
        "one_original_root": grant["root_budget_id"], "send_approval_id": grant["approval_id"],
        "connector_posts": len(h.sent), "model_requests_offline": len(h.h.sdk.model_requests),
        "send_guard": {"native_evidence_id": send.native_evidence_id,
                       "gated_at": send.gated_at.isoformat(), "not_after": send.not_after.isoformat()},
        "receipt": receipt, "no_additional_send_on_replay": True, "all_original_receipts_preserved": True,
        "original_claims_sources_policy_route_capture_unchanged": True,
        "before_budget": before_budget.payload["data"], "after_budget": after_budget.payload["data"],
        "fresh_installed_history_verified_after_business_writes": True,
    })


@pytest.mark.parametrize("version", (None, "unknown-version", "fa-channel-pin-installation-v3"))
def test_unknown_installation_never_falls_back_to_v1(pin_channel, version):
    service, store, _, _, _ = pin_channel
    principal, plan, authorization = approved(service)
    service.apply(principal, plan, authorization)
    raw = dict(store.read_inventory().rows[channel_key("current", service.new.table.scope.authority_domain)].payload["data"])
    if version is None:
        raw.pop("schema_version")
    else:
        raw["schema_version"] = version
    with pytest.raises(Problem) as denied:
        parse_channel_installation(raw)
    assert denied.value.body["error"]["code"] == "CHANNELS_RECORD_VERSION_DENIED"


@pytest.mark.parametrize("change", ("mixed-v1", "unknown", "missing-version", "wrong-action", "wrong-human", "wrong-decision"))
def test_installation_approval_version_identity_and_action_must_match(pin_channel, change):
    service, store, _, _, _ = pin_channel
    principal, plan, authorization = approved(service)
    service.apply(principal, plan, authorization)
    pointer = parse_channel_installation(
        store.read_inventory().rows[channel_key("current", service.new.table.scope.authority_domain)].payload["data"])
    data = authorization.model_dump(mode="json")
    if change == "mixed-v1":
        data.update(schema_version="fa-channel-approval-v1", action="enable-bounded-teams-metrics")
        data.pop("core_code_sha256")
        data.pop("operator_code_sha256")
    elif change == "unknown":
        data["schema_version"] = "unknown-version"
    elif change == "missing-version":
        data.pop("schema_version")
    elif change == "wrong-action":
        data["action"] = "enable-bounded-teams-metrics"
    elif change == "wrong-human":
        data["operator_oid"] = "someone-else"
    else:
        data["decision_id"] = "unrelated-decision"
    with pytest.raises(Problem):
        parse_channel_approval(data, pointer, operator_oid=service.new.identity.human_oid)


def test_v1_installation_cannot_use_a_v2_approval(pin_channel):
    from shared_context.behavior_channels_migration import ChannelInstallation
    service, _, _, _, _ = pin_channel
    _, plan, authorization = approved(service)
    pointer = ChannelInstallation(configuration_sha256=service.new.sha256, predecessor_sha256=service.old.sha256,
        registry_sha256=digest(service.new.channels.model_dump(mode="json")), history_id="offline-history",
        history_sha256="a" * 64, approval_id=channel_key("approval", authorization.decision_id))
    with pytest.raises(Problem):
        parse_channel_approval(authorization.model_dump(mode="json"), pointer, operator_oid=service.new.identity.human_oid)


def corrupt_authority_row(h, row_id, change):
    key = h.table.table_name, h.service.old.table.scope.partition_key, "behavior:v1:row:" + row_id
    entity = h.table.backing.rows[key]
    payload = json.loads(entity["Payload"])
    change(payload["data"])
    entity["Payload"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def test_unknown_version_refused_by_pointer_shared_registry_and_signed_entry(live_parent):
    h = live_parent
    corrupt_authority_row(h, h.parent.teams.current_key, lambda data: data.update(schema_version="unknown-channel-format"))
    for read in (h.parent.teams.pointer,
                 lambda: h.parent.teams.channel.core.shared_channels.check_current(h.parent.teams.channel.core)):
        with pytest.raises(Problem) as denied:
            read()
        assert denied.value.body["error"]["code"] == "CHANNELS_RECORD_VERSION_DENIED"
    received = signed_receive(h)
    assert received.status_code == 502
    assert h.sent == h.acquired == h.h.sdk.starts == []


def test_mixed_approval_does_not_bypass_installed_history_on_signed_entry(live_parent):
    h = live_parent
    pointer = h.parent.teams.pointer()
    def mixed(data):
        if h.kind == "pin-v2":
            data.update(schema_version="fa-channel-approval-v1", action="enable-bounded-teams-metrics")
            data.pop("core_code_sha256")
            data.pop("operator_code_sha256")
        else:
            data.update(schema_version="fa-channel-pin-approval-v2",
                        action="enable-bounded-teams-metrics-with-protection-pin-refresh",
                        core_code_sha256="a" * 64, operator_code_sha256="b" * 64)
    corrupt_authority_row(h, pointer.approval_id, mixed)
    received = signed_receive(h)
    assert received.status_code in {403, 409, 502}
    assert h.sent == h.acquired == h.h.sdk.starts == []


def test_revoked_rule_denies_signed_activity_without_dynamic_work(live_parent):
    h = live_parent
    response = h.client.post("/behavior/v2/channels/teams/revoke", headers=h.h.headers("human"))
    assert response.status_code == 200, response.text
    received = signed_receive(h)
    assert received.status_code == 403
    assert received.json()["error"]["code"] == "CHANNELS_RULE_DENIED"
    assert h.sent == h.acquired == h.h.sdk.starts == []


def test_native_send_guard_still_rechecks_acl_after_cold_token(live_parent):
    from test_behavior_channels_migration import update
    h = live_parent
    response = signed_receive(h)
    assert response.status_code == 200, response.text
    record = h.parent.teams.for_obligation(response.json()["obligation_id"])
    def revoke():
        key = control_key("teams-route-access", h.service.new.channels.route_id)
        data = h.parent.runtime.store.read_snapshot((key,))[key].payload["data"]
        update(h.parent.runtime.store, key, {**data, "active": False}, "offline-revoke-at-native-token")
    h.token_hook[0] = revoke
    with pytest.raises(Problem):
        controller_step(h, record.binding)
    assert h.sent == [] and h.acquired == [BOT_SCOPE]
    assert len(h.h.sdk.starts) == 1
    assert h.parent.runtime.dep.workers.inspect(record.binding.job_id).state == "succeeded"
