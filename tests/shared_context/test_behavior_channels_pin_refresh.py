"""Versioned channels + exact protection-pin update through the public operator."""
import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shared_context.behavior_channels_api import create_channels_app
from shared_context.behavior_migration import installed_origin, protection_keys, installation_record
from shared_context.behavior_migration import MigrationStore
from shared_context.behavior_channels_pin_refresh import ChannelPinRefresh
from shared_context.behavior_channels_migration import channel_key
from shared_context.behavior_operations import control_key
from shared_context.errors import Problem
from shared_context.behavior_server_config import ProtectionObservation, ServerConfigurationV2
from shared_context.source_materials import configuration_evidence_fingerprint
from test_behavior_channels_migration import channel_migration, principal_for
from test_behavior_server import NOW
from test_teams_metrics import metrics
from test_m3_auth import jwk_client, signing_key


def review_source(store, config, *, review_id="offline-reviewed-pins"):
    from shared_context.source_materials import SourceMaterials, SourceInput, AuthenticatedSourceContext
    from shared_context.source_reviews import DurableHumanReviewVerifier, HumanReviewPolicy, HumanReviewCommand
    from shared_context.memory_governance import MemoryGovernance, ClaimCandidate
    from test_behavior_api import METRICS
    from types import SimpleNamespace
    b, policy, profile = config.bindings[0], config.policies[0].policy, config.assistant.profiles[0]
    principal = principal_for(SimpleNamespace(old=config))
    context = AuthenticatedSourceContext(b.tenant_id, b.team_id, principal.source_actor_id, b.ingress_id, policy.allowed_purposes)
    sources = SourceMaterials(store, clock=lambda: NOW)
    plan = sources.plan(context, SourceInput(source_id=profile.source.source_id, version=profile.source.version,
        visibility=policy.source_visibility, purpose=policy.purpose, schema_id="fa-artifact-metrics-v1",
        media_type="application/json"), METRICS)
    assert plan.reference.sha256 == profile.source.sha256
    sources.commit(context, plan)
    reviewer = DurableHumanReviewVerifier(sources, HumanReviewPolicy(policy_version=config.review_policy_version),
                                         clock=lambda: NOW)
    candidate = ClaimCandidate(claim_id=profile.claim_id, version=1, subject=b.subject,
        visibility=policy.source_visibility, purpose=policy.purpose, kind="note",
        text="The fixed metrics source is reviewed.", supporting=(plan.reference,),
        effective_from=NOW, expires_at=NOW + timedelta(minutes=2))
    preview = reviewer.preview(principal, context, candidate)
    command = HumanReviewCommand(review_id=review_id, candidate=candidate,
        candidate_sha256=preview.candidate_sha256, evidence_sha256=preview.evidence_sha256,
        policy_version=preview.policy_version, decision="approve",
        reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"),
        reason="Offline explicit designated human review.")
    reviewer.commit_decision(principal, context, reviewer.plan_decision(principal, context, command))
    memory = MemoryGovernance(sources, clock=lambda: NOW, token_counter=len, review_verifier=reviewer)
    memory.commit_claim(context, memory.plan_claim(context, candidate))


@pytest.fixture
def pin_channel(channel_migration, tmp_path):
    from shared_context.behavior_channels_pin_refresh import (
        ChannelPinRefresh, PinRefreshEvidenceFiles, OperatorCodeDirectory,
    )
    old_service, store, table, original, metrics = channel_migration
    old, raw = old_service.old, old_service.new.model_dump(mode="json")
    review_source(store, old)
    paths = {}
    for lane in ("sources", "backups"):
        previous = ProtectionObservation.model_validate_json(getattr(old_service.evidence, lane).read_bytes())
        previous_path = tmp_path / ("previous-" + lane + ".json")
        previous_path.write_text(previous.model_dump_json(), encoding="utf-8")
        version = "offline-observed-code-inventory-v3"
        fingerprint = configuration_evidence_fingerprint(
            blob_service_etag=previous.blob_service.etag, management_policy_etag=previous.management_policy.etag,
            blob_service_evidence=previous.blob_service, management_policy_evidence=previous.management_policy,
            automatic_delete_consumers=previous.automatic_delete_consumers, inventory_version=version,
        )
        fresh = previous.model_dump(mode="json")
        fresh["inventory_version"] = version
        fresh["configuration"]["evidence_sha256"] = fingerprint
        current = ProtectionObservation.model_validate_json(json.dumps(fresh))
        getattr(old_service.evidence, lane).write_text(current.model_dump_json(), encoding="utf-8")
        raw[lane]["protection"] = current.configuration.model_dump(mode="json")
        paths[lane] = previous_path
    new = ServerConfigurationV2.model_validate_json(json.dumps(raw))
    retirement = json.loads(old_service.evidence.retirement.read_bytes())
    retirement["new_configuration_sha256"] = new.sha256
    old_service.evidence.retirement.write_text(json.dumps(retirement), encoding="utf-8")
    operator_dir = tmp_path / "operator-code"
    operator_dir.mkdir()
    operator_dir.joinpath("offline_entrypoint.py").write_text("SCOPE = 'offline-only'\n", encoding="utf-8")
    evidence = PinRefreshEvidenceFiles(
        old_service.evidence.sources, old_service.evidence.backups, old_service.evidence.retirement,
        paths["sources"], paths["backups"],
    )
    service = ChannelPinRefresh(store, old, new, evidence, OperatorCodeDirectory(operator_dir),
                                clock=lambda: NOW, allow_apply=True)
    return service, store, table, original, metrics


def test_one_approved_egt_refreshes_two_pins_and_preserves_every_old_receipt(pin_channel):
    service, store, table, original, metrics = pin_channel
    before = store.read_inventory()
    submissions = len(table.submissions)
    with TestClient(create_channels_app(service, metrics.verifier, allow_apply=True)) as client:
        base = "/operator/v2/channels/versioned-pins/"
        plan = client.post(base + "plan", json={}, headers=metrics.headers("human"))
        assert plan.status_code == 200, plan.text
        assert plan.json()["schema_version"] == "fa-channel-pin-plan-v2"
        approval = client.post(base + "authorize", json={
            "plan": plan.json(), "decision_id": "offline-pin-approval",
            "expires_at": (NOW + timedelta(minutes=1)).isoformat(),
        }, headers=metrics.headers("human"))
        assert approval.status_code == 200, approval.text
        body = {"plan": plan.json(), "authorization": approval.json()}
        applied = client.post(base + "apply", json=body, headers=metrics.headers("human"))
        assert applied.status_code == 200, applied.text
        assert client.post(base + "reconcile", json=body, headers=metrics.headers("human")).status_code == 200
        verified = client.post(base + "verify", json=body, headers=metrics.headers("human"))
        assert verified.status_code == 200, verified.text
    assert len(table.submissions) == submissions + 1
    after = store.read_inventory()
    replaced = {installation_record(service.old)[0], *protection_keys(service.old)}
    assert all(after.rows[key] == value for key, value in before.rows.items() if key not in replaced)
    assert all(after.receipts[key] == value for key, value in before.receipts.items())
    assert installed_origin(store, service.new) == original
    for lane, key in zip(("sources", "backups"), protection_keys(service.new)):
        assert after.rows[key].payload["data"] == getattr(service.new, lane).protection.model_dump(mode="json")


def test_valid_storage_observation_cannot_reactivate_an_expired_current_review(pin_channel):
    from shared_context.errors import Problem
    service, store, table, _, _ = pin_channel
    service.clock = lambda: NOW + timedelta(minutes=3)
    before = store.read_inventory()
    with pytest.raises(Problem) as denied:
        service.plan(principal_for(service), transition_id="expired-current-claim")
    assert denied.value.body["error"]["code"] == "MEMORY_CLAIM_EXPIRED"
    assert store.read_inventory().entity_sha256 == before.entity_sha256


def approved(service):
    principal = principal_for(service)
    plan = service.plan(principal, transition_id="approved-pin-once")
    approval = service.authorize(principal, plan, decision_id="fixed-human-pin-approval",
                                 expires_at=NOW + timedelta(seconds=60))
    return principal, plan, approval


def restarted(service, table):
    return ChannelPinRefresh(MigrationStore(table, service.old.table.scope), service.old, service.new,
        service.evidence, service.operator_code, clock=service.clock, allow_apply=True)


def test_lost_ack_restart_reconciles_original_request_without_second_egt(pin_channel):
    service, store, table, _, _ = pin_channel
    principal, plan, approval = approved(service)
    before = len(table.submissions)
    table.failure = "after"
    with pytest.raises(Problem) as unknown:
        service.apply(principal, plan, approval)
    assert unknown.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    table.failure = None
    resumed = restarted(service, table)
    assert resumed.reconcile(principal, plan, approval).payload["request"] == service.request(plan, approval).model_dump(mode="json")
    resumed.verify(principal, plan, approval)
    with pytest.raises(Problem):
        restarted(service, table).apply(principal, plan, approval)
    assert len(table.submissions) == before + 1
    resumed.clock = lambda: NOW + timedelta(hours=5)
    assert resumed.reconcile(principal, plan, approval).payload["result"]["activation_performed"] is False


@pytest.mark.parametrize("field", (
    "core_code_sha256", "operator_code_sha256", "inventory_sha256", "counter_sha256",
    "preserved_sha256", "reviewed_source_sha256", "old-pin-etag", "old-pin-preimage", "witness",
))
def test_authorization_refuses_changed_plan_material(pin_channel, field):
    service, store, table, _, metrics = pin_channel
    client = TestClient(create_channels_app(service, metrics.verifier, allow_apply=True))
    route = "/operator/v2/channels/fixed-plan/"
    plan = client.post(route + "plan", json={}, headers=metrics.headers("human")).json()
    if field == "old-pin-etag":
        plan["old_pins"][0]["etag"] = 'W/"not-observed"'
    elif field == "old-pin-preimage":
        data = json.loads(plan["old_pins"][0]["preimage"])
        data["data"]["evidence_sha256"] = "f" * 64
        plan["old_pins"][0]["preimage"] = json.dumps(data)
    elif field == "witness":
        plan["witness_sha256"]["previous_sources"] = "f" * 64
    else:
        plan[field] = "f" * 64
    before = store.read_inventory()
    response = client.post(route + "authorize", json={
        "plan": plan, "decision_id": "not-approved", "expires_at": (NOW + timedelta(seconds=50)).isoformat(),
    }, headers=metrics.headers("human"))
    assert response.status_code == 409, response.text
    assert store.read_inventory().entity_sha256 == before.entity_sha256


@pytest.mark.parametrize("field", ("backend", "source", "policy", "target", "route"))
def test_new_configuration_cannot_expand_the_approved_delta(pin_channel, field):
    service, store, table, _, _ = pin_channel
    raw = service.new.model_dump(mode="json")
    if field == "backend":
        raw["sources"]["protection"]["configuration_version"] = "different-retention"
    elif field == "source":
        raw["assistant"]["profiles"][0]["source"]["sha256"] = "f" * 64
    elif field == "policy":
        raw["policies"][0]["policy"]["valid_until"] = (NOW + timedelta(hours=4)).isoformat()
    elif field == "target":
        raw["targets"][0]["agent_version"] = str(int(raw["targets"][0]["agent_version"]) + 1)
    else:
        raw["channels"]["route_id"] = "not-the-captured-route"
    before = store.read_inventory()
    with pytest.raises((Problem, ValueError)):
        changed = ServerConfigurationV2.model_validate_json(json.dumps(raw))
        alternate = ChannelPinRefresh(MigrationStore(table, service.old.table.scope), service.old, changed,
            service.evidence, service.operator_code, clock=service.clock)
        alternate.plan(principal_for(service), transition_id="different-scope")
    assert store.read_inventory().entity_sha256 == before.entity_sha256


def withdraw_current_source(service, table):
    from shared_context.source_materials import (
        SourceMaterials, SourceReference, SourceWithdrawalInput, AuthenticatedSourceContext,
    )
    b, policy, profile = service.old.bindings[0], service.old.policies[0].policy, service.old.assistant.profiles[0]
    principal = principal_for(service)
    context = AuthenticatedSourceContext(b.tenant_id, b.team_id, principal.source_actor_id,
                                         b.ingress_id, policy.allowed_purposes)
    reference = SourceReference(scope=context.scope(policy.source_visibility), **profile.source.model_dump())
    sources = SourceMaterials(MigrationStore(table, service.old.table.scope), clock=lambda: NOW)
    sources.withdraw_source(principal, context, SourceWithdrawalInput(
        withdrawal_id="withdraw-before-channel-pin", reference=reference, expected_access_revision=1,
        reason="Offline explicit source revocation."))


def test_revoked_source_cannot_receive_a_new_pin_plan(pin_channel):
    service, store, table, _, _ = pin_channel
    withdraw_current_source(service, table)
    before = store.read_inventory()
    with pytest.raises(Problem):
        service.plan(principal_for(service), transition_id="revoked-source")
    assert store.read_inventory().entity_sha256 == before.entity_sha256


@pytest.mark.parametrize("change", ("expiry", "source", "owner", "operator-code", "core-code", "witness", "scope"))
def test_final_send_checks_and_same_guard_egt_refuse_races(pin_channel, monkeypatch, change):
    service, store, table, _, _ = pin_channel
    principal, plan, approval = approved(service)
    original_submit = table.submit_transaction
    fired, restore = [], []
    def submit(operations, **kwargs):
        if not fired:
            fired.append(True)
            if change == "expiry":
                service.clock = lambda: NOW + timedelta(minutes=3)
            elif change == "source":
                withdraw_current_source(service, table)
            elif change == "owner":
                from test_behavior_channels_migration import update
                writer = MigrationStore(table, service.old.table.scope)
                key = control_key("owner", service.old.bindings[0].owner.owner_record_id)
                data = writer.read_snapshot((key,))[key].payload["data"]
                update(writer, key, {**data, "active": False}, "offline-owner-revocation")
            elif change in {"operator-code", "core-code"}:
                import shared_context.behavior_channels_pin_refresh as code
                path = (service.operator_code.directory / "offline_entrypoint.py"
                        if change == "operator-code" else Path(code.__file__))
                raw = path.read_bytes()
                restore.append((path, raw))
                path.write_bytes(raw + b"\n# OFFLINE code-pin mutation; restored by this test.\n")
            elif change == "witness":
                path = service.evidence.previous_sources
                raw = path.read_bytes()
                restore.append((path, raw))
                path.write_bytes(raw + b"\n")
            else:
                def expired_scope(_historical):
                    raise Problem(409, "PHASE_SCOPE_EXPIRED", "offline final scope check")
                service.scope_check = expired_scope
        return original_submit(operations, **kwargs)
    monkeypatch.setattr(table, "submit_transaction", submit)
    try:
        with pytest.raises(Problem):
            service.apply(principal, plan, approval)
    finally:
        for path, raw in restore:
            path.write_bytes(raw)
    assert fired
    inventory = store.read_inventory()
    assert service.request(plan, approval).request_id not in inventory.receipts
    assert inventory.rows[installation_record(service.old)[0]].payload["data"] == installation_record(service.old)[1]
    assert channel_key("current", service.old.table.scope.authority_domain) not in inventory.rows


@pytest.mark.parametrize("tamper", ("preimage", "history-hash", "pin", "old-receipt", "missing-old-receipt"))
def test_installed_reader_rejects_tampered_preimages_pins_and_original_receipts(pin_channel, tamper):
    service, store, table, _, _ = pin_channel
    principal, plan, approval = approved(service)
    before = store.read_inventory()
    service.apply(principal, plan, approval)
    history_key = channel_key("history", plan.transition_id)
    prefix, row_id = "behavior:v1:row:", history_key
    if tamper in {"old-receipt", "missing-old-receipt"}:
        prefix, row_id = "behavior:v1:receipt:", next(iter(before.receipts))
    elif tamper == "pin":
        row_id = protection_keys(service.old)[0]
    key = table.table_name, store._scope.partition_key, prefix + row_id
    if tamper == "missing-old-receipt":
        del table.backing.rows[key]
    else:
        entity = table.backing.rows[key]
        value = json.loads(entity["Payload"])
        if tamper == "preimage":
            value["data"]["plan"]["old_pins"][0]["etag"] = 'W/"wrong-preimage"'
        elif tamper == "history-hash":
            value["data"]["physical_guard_sha256"] = "f" * 64
        elif tamper == "pin":
            value["data"]["evidence_sha256"] = "f" * 64
        else:
            value["request"]["request_digest"] = "f" * 64
        entity["Payload"] = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    with pytest.raises(Problem):
        installed_origin(MigrationStore(table, service.new.table.scope), service.new)


def test_unknown_original_control_row_and_receipt_are_not_projected_away(pin_channel):
    from test_behavior_channels_migration import update
    service, store, table, _, _ = pin_channel
    update(store, "unclassified-current-row", {"new_unknown_responsibility": True}, "unknown-current-request")
    before = store.read_inventory()
    with pytest.raises(Problem):
        service.plan(principal_for(service), transition_id="cannot-ignore-unknown")
    assert store.read_inventory().entity_sha256 == before.entity_sha256


@pytest.mark.parametrize("identity", ("reader", "agent", "wrong-client", "wrong-tenant"))
def test_pin_operator_requires_the_original_delegated_human(pin_channel, signing_key, identity):
    from test_m3_auth import token
    service, store, table, _, metrics = pin_channel
    config = service.old.identity
    claims = {"tid": config.tenant_id, "iss": f"https://sts.windows.net/{config.tenant_id}/",
              "aud": config.audience, "oid": config.human_oid, "appid": config.admin_client_id,
              "scp": "Behavior.Read Behavior.Approve"}
    if identity == "reader":
        claims["scp"] = "Behavior.Read"
    elif identity == "agent":
        claims["oid"] = service.old.bindings[0].agent_oid
    elif identity == "wrong-client":
        claims["appid"] = "00000000-0000-0000-0000-000000000001"
    else:
        claims["tid"] = "00000000-0000-0000-0000-000000000001"
    before = store.read_inventory()
    with TestClient(create_channels_app(service, metrics.verifier, allow_apply=True)) as client:
        response = client.post("/operator/v2/channels/no-spoof/plan", json={},
                               headers={"Authorization": "Bearer " + token(signing_key, claims)})
    assert response.status_code in {401, 403}
    assert store.read_inventory().entity_sha256 == before.entity_sha256


@pytest.mark.parametrize("competitor_wins", (False, True))
def test_two_approved_pin_transitions_share_one_original_guard(pin_channel, competitor_wins):
    service, store, table, original, _ = pin_channel
    principal, first, first_approval = approved(service)
    competitor = restarted(service, table)
    second = competitor.plan(principal, transition_id="competing-pin")
    second_approval = competitor.authorize(principal, second, decision_id="competing-human-approval",
                                           expires_at=NOW + timedelta(seconds=55))
    before = store.read_inventory()
    if competitor_wins:
        table.before_submit = lambda: competitor.apply(principal, second, second_approval)
        with pytest.raises(Problem):
            service.apply(principal, first, first_approval)
        winner, loser = second, first
    else:
        service.apply(principal, first, first_approval)
        with pytest.raises(Problem):
            competitor.apply(principal, second, second_approval)
        winner, loser = first, second
    after = store.read_inventory()
    assert channel_key("apply", winner.transition_id) in after.receipts
    assert channel_key("apply", loser.transition_id) not in after.receipts
    assert len(after.receipts) == len(before.receipts) + 1
    assert installed_origin(MigrationStore(table, service.new.table.scope), service.new) == original


def test_cli_derives_local_config_before_retirement_without_dummy_facts(pin_channel, tmp_path):
    import subprocess
    import sys
    service, store, table, _, _ = pin_channel
    old, channels, output = (tmp_path / name for name in ("current-v1.json", "channels.json", "new-v2.json"))
    old.write_text(service.old.model_dump_json(), encoding="utf-8")
    channels.write_text(service.new.channels.model_dump_json(), encoding="utf-8")
    before = store.read_inventory()
    command = [sys.executable, "-B", "-m", "shared_context.behavior_channels_api", "propose",
               "--old-config", str(old), "--channels", str(channels), "--output", str(output), "--pin-refresh"]
    for name in ("sources", "backups", "previous_sources", "previous_backups"):
        command.extend(("--" + name.replace("_", "-"), str(getattr(service.evidence, name))))
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert ServerConfigurationV2.model_validate_json(output.read_bytes()) == service.new
    status = json.loads(result.stdout)
    assert status["ready"] is False and status["applied"] is False and status["needs_fresh_retirement"] is True
    assert store.read_inventory().entity_sha256 == before.entity_sha256


@pytest.mark.parametrize("bad", ("missing-history", "forbidden-observation", "unknown-inventory"))
def test_witness_read_failures_and_unknown_inventory_never_mean_absence(pin_channel, bad):
    service, store, table, _, metrics = pin_channel
    path = service.evidence.previous_sources if bad == "missing-history" else service.evidence.sources
    if bad == "missing-history":
        path.rename(path.with_suffix(".retained-original"))
    else:
        raw = json.loads(path.read_bytes())
        if bad == "forbidden-observation":
            raw["management_policy"]["status_code"] = 403
            raw["management_policy"]["kind"] = "absence"
            raw["management_policy"]["error_code"] = "AuthorizationFailed"
        else:
            raw["inventory_version"] = None
        path.write_text(json.dumps(raw), encoding="utf-8")
    before = store.read_inventory()
    with TestClient(create_channels_app(service, metrics.verifier, allow_apply=True)) as client:
        response = client.post("/operator/v2/channels/missing-is-not-absent/plan",
                               json={}, headers=metrics.headers("human"))
    assert response.status_code in {409, 503}, response.text
    assert "error" in response.json()
    assert store.read_inventory().entity_sha256 == before.entity_sha256


def test_original_signed_jwt_is_reverified_at_final_table_send(pin_channel, signing_key, monkeypatch):
    from datetime import datetime, timezone
    import jwt.api_jwt
    from test_m3_auth import token
    service, store, table, _, metrics = pin_channel
    identity = service.old.identity
    instant = datetime.now(timezone.utc)
    headers = {"Authorization": "Bearer " + token(signing_key, {
        "tid": identity.tenant_id, "iss": f"https://sts.windows.net/{identity.tenant_id}/",
        "aud": identity.audience, "oid": identity.human_oid, "appid": identity.admin_client_id,
        "scp": "Behavior.Read Behavior.Approve", "exp": int((instant + timedelta(minutes=10)).timestamp()),
    })}
    route = "/operator/v2/channels/expiring-original-token/"
    client = TestClient(create_channels_app(service, metrics.verifier, allow_apply=True))
    plan = client.post(route + "plan", json={}, headers=headers)
    assert plan.status_code == 200, plan.text
    approval = client.post(route + "authorize", json={
        "plan": plan.json(), "decision_id": "original-token-only", "expires_at": (NOW + timedelta(seconds=50)).isoformat(),
    }, headers=headers)
    assert approval.status_code == 200, approval.text
    original_submit = table.submit_transaction
    class ExpiredJwtClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant + timedelta(minutes=20)
    def submit(operations, **kwargs):
        with monkeypatch.context() as clock:
            clock.setattr(jwt.api_jwt, "datetime", ExpiredJwtClock)
            return original_submit(operations, **kwargs)
    monkeypatch.setattr(table, "submit_transaction", submit)
    before = store.read_inventory()
    response = client.post(route + "apply", json={"plan": plan.json(), "authorization": approval.json()}, headers=headers)
    assert response.status_code == 401, response.text
    assert store.read_inventory().entity_sha256 == before.entity_sha256
