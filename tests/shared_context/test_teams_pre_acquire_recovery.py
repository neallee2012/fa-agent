"""Current Window2/claim4/signed activity/native claim producers, offline only."""
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import json
import os
import copy

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("test_channel_window", reason="run with the pinned offline Window2 operator fixture bundle")
from test_channel_window import (
    next_window, next_review, original_version, bounded_initial_control_quota, closed_v2,
    original_completed_nonempty_page, installed_empty, operator, exact_source_initialization,
    fixed_original_high_water, expired_claim, source_contract, closed_case, offline_socket_guard,
    original_captured_route_producer, closed_finished, sufficient_initial_budget, durable_result,
    native_acquire, current_rotation, two_rotations, current_cancelled, rotated, jwk_client, signing_key,
    observer_packet,
)
from shared_context.behavior_migration import MigrationStore, installed_origin, installation_record
from shared_context.behavior_runtime_rotation_history import receipt_catalog
from shared_context.behavior_operations import control_key
from shared_context.behavior_foundry import native_key
from shared_context.behavior_recovery import TeamsPreAcquireRecoveryScope, PlatformStopObservation, stop_endpoint
from shared_context.behavior_teams_recovery import TeamsPreAcquireRecovery
from shared_context.errors import Problem

pytestmark = pytest.mark.parametrize("source_contract", ["claim"], indirect=True)


@pytest.fixture(autouse=True)
def original_finite_worker_contract(monkeypatch):
    import test_behavior_runtime_transition as transition
    original = transition.synthetic_configuration
    def configured():
        config = original()
        return config.model_copy(update={"workers": config.workers.model_copy(update={
            "queries_per_generation": 16, "control_units": 8})})
    monkeypatch.setattr(transition, "synthetic_configuration", configured)


@pytest.fixture
def original_teams_gap(next_window, monkeypatch, jwk_client, tmp_path):
    from azure.core.credentials import AccessToken
    from shared_context.behavior_server import build_server, application
    from shared_context.behavior_server_config import NativeEvidenceBundle
    from shared_context.behavior_assistant_hosted import AssistantSupervisor, AssistantHostedRuntime
    from shared_context.behavior_hosted import HostedInvocation, BehaviorExecutorClient
    from shared_context.behavior_assistant import _LocalHttp
    from shared_context.teams_native import TeamsNativeEvidence
    from shared_context.teams_identity import PublicCloudTransport
    from shared_context.source_materials import digest
    from shared_context import behavior_wire as wire
    from test_behavior_runtime_rotation_sources import QueryTransport
    from test_behavior_store import Credential
    from test_finished_teams_gates import parent_v2_observation
    from test_teams_identity import identity, token, activity

    r, migration, scope = next_window
    h = r.h
    plan = migration.plan(h.who, transition_id=scope.window_id)
    approved = migration.authorize(h.who, plan, decision_id="offline-window-for-original-gap",
                                  expires_at=h.clock[0]+timedelta(minutes=4))
    migration.apply(h.who, plan, approved)
    migration.verify(h.who, plan, approved)
    config = migration.new
    values = migration.evidence.read()
    bundle = parent_v2_observation(h.c, config).model_copy(update={lane: values[lane] for lane in ("sources", "backups")})
    worker = bundle.worker.model_copy(update={"evidence_id": "offline-gap-worker"})
    bundle = bundle.model_copy(update={"worker": worker, "delivery": None, "inbox": None})
    Path(config.evidence.path).write_text(bundle.model_dump_json(), encoding="utf-8")
    h.clock[0] = bundle.captured_at
    transport = QueryTransport()
    transport.backing = h.table.backing
    denied = httpx.MockTransport(lambda _: pytest.fail("no Foundry/model external request"))
    sends = []
    def network(_, request):
        if request.url.host == "login.botframework.com":
            return identity().jwks_transport.handle_request(request)
        sends.append("original-Teams-result")
        return httpx.Response(200, json={"id": "offline-original-result-once"})
    class BotCredential:
        def __init__(self, **_):
            pass
        def get_token(self, _):
            return AccessToken("OFFLINE-ONLY", int(h.clock[0].timestamp())+300)
        def close(self):
            pass
    monkeypatch.setattr(PublicCloudTransport, "handle_request", network)
    monkeypatch.setattr("shared_context.teams_personal.ManagedIdentityCredential", BotCredential)
    with build_server(config, credential=Credential(), table_transport=transport, key_client=jwk_client,
            native_transports={t.executor_id: denied for t in config.active_targets},
            assistant_transport=denied, clock=lambda: h.clock[0]) as parent:
        parent.assistant.artifacts = h.parent.assistant.artifacts
        parent.assistant.start()
        parent.installation()
        packet, _ = observer_packet(parent, h, scope, tmp_path/"gap-observer", lambda: h.clock[0])
        bundle = NativeEvidenceBundle.model_validate_json(json.dumps(packet["parent-evidence.json"]))
        Path(config.evidence.path).write_text(bundle.model_dump_json(), encoding="utf-8")
        runtime, store = parent.runtime, parent.runtime.store
        runtime.dep.workers.publish_readiness(bundle.worker.evidence_id)
        runtime.dep.inbox.publish_readiness(bundle.inbox.evidence_id)
        capture_key = control_key("teams-capture-route-binding", config.channels.personal.configuration_digest)
        capture = store.read_snapshot((capture_key,))[capture_key].payload["data"]
        ikey, installed = installation_record(config)
        native_proof = TeamsNativeEvidence(
            evidence_id=config.channels.native_evidence_id, configuration_digest=runtime.dep.delivery.configuration_digest(),
            server_configuration_sha256=config.sha256, control_binding=config.delivery.control_binding,
            personal_configuration_digest=config.channels.personal.configuration_digest,
            settings_digest=digest(parent.teams.channel.adapter.settings.model_dump(mode="json")),
            installation_row_id=ikey, installation_data_sha256=digest(installed),
            route_witness_sha256=capture["route_material_sha256"], capture_binding_sha256=digest(capture),
            bot_registration_sha256="b"*64, personal_installation_sha256="c"*64, package_sha256="d"*64,
            callback_url=config.channels.personal.endpoint, bot_app_id=config.channels.personal.uami_client_id,
            bot_principal_id=config.channels.personal.uami_principal_id, readiness=bundle.delivery,
            observed_at=h.clock[0], expires_at=config.channels.valid_until)
        Path(config.channels.native_evidence_path).write_text(native_proof.model_dump_json(), encoding="utf-8")
        parent.teams.channel.adapter.publish_installation()
        api = TestClient(application(parent), raise_server_exceptions=False)
        encoded = token(nbf=int(h.clock[0].timestamp())-60, exp=int(h.clock[0].timestamp())+300)
        incoming = api.post("/behavior/v1/teams/activities", json=activity(text="FA metrics"),
                            headers={"Authorization": "Bearer "+encoded})
        assert incoming.status_code == 200, incoming.text
        work = parent.teams.for_obligation(incoming.json()["obligation_id"])
        parent.teams.ensure_enqueued(work.binding)
        native, workers = runtime.dep.native_launches, runtime.dep.workers
        starts, reservations = [], []
        def reserve(start):
            starts.append(start)
            reservations.append(native.prepare(start))
        workers.adapter = replace(workers.adapter, start=reserve)
        workers.start(work.binding.job_id, work.binding.owner)
        launch, permit = reservations[0]
        from test_m3_auth import token as agent_token
        claims = {"tid": config.identity.tenant_id, "iss": f"https://sts.windows.net/{config.identity.tenant_id}/",
                  "aud": config.identity.audience, "oid": work.binding.agent_oid, "appid": work.binding.agent_oid,
                  "roles": ["Behavior.Agent"]}
        bearer = agent_token(h.c.signing_key, claims, remove=("scp",))
        local = _LocalHttp(api)
        acquired = []
        def http(request):
            if request.url.path.endswith("/acquire"):
                acquired.append(True)
                return httpx.Response(412, json={"error": {"code": "BEHAVIOR_CONFLICT"}})
            return local.handle_request(request)
        client = BehaviorExecutorClient("https://context.invalid", config.identity.audience,
            SimpleNamespace(get_token=lambda _: SimpleNamespace(token=bearer)), transport=httpx.MockTransport(http))
        supervisor = AssistantSupervisor(tmp_path/"original-hosted", runtime_path=Path(os.environ["M2_TEST_RUNTIME"]))
        target = config.active_targets[0]
        host = AssistantHostedRuntime(wire.HostedIdentity(**{k: getattr(target, k) for k in (
            "agent_id", "agent_name", "agent_version", "session_id")}), supervisor, client, clock=lambda: h.clock[0])
        payload = HostedInvocation(binding_id=work.binding.binding_id, launch_id=launch.launch_id, launch_permit=permit)
        host.start(payload, "offline-original-teams-invocation", target.session_id)
        supervisor.join(timeout=30)
        def query(binding):
            probe = native.probe(binding)
            return host.observe("offline-original-teams-invocation", target.session_id, probe.probe_id).evidence_id
        workers.adapter = replace(workers.adapter, query=query)
        # Original independent query/control lanes: never replenish live counters.
        for n in range(9):
            if n == 8:
                with pytest.raises(Problem) as exhausted:
                    workers.reconcile(work.binding.job_id, work.binding.owner)
                assert exhausted.value.body["error"]["code"] == "WORKER_CONTROL_EXHAUSTED"
            else:
                workers.reconcile(work.binding.job_id, work.binding.owner)
        job = workers.inspect(work.binding.job_id)
        assert job.control_remaining == 0 and job.queries_reserved == 9
        assert job.cancel_credit == job.stop_credit == job.completion_credit == 1
        assert acquired == [True] and sends == []
        before = store.read_inventory()
        installed_origin(MigrationStore(parent.clients[0], config.table.scope), config)
        receipt_catalog(before)
        try:
            yield SimpleNamespace(parent=parent, config=config, runtime=runtime, store=store, h=h, work=work,
                native=native, workers=workers, launch_id=launch.launch_id, principal=h.who, clock=h.clock,
                client=api, supervisor=supervisor, sends=sends, before=before, transport=transport, headers=r.headers,
                agent_headers={"Authorization": "Bearer "+bearer,
                               "x-behavior-assistant-ticket": supervisor.clients[launch.launch_id].ticket.get_secret_value()})
        finally:
            client.close()
            api.close()


def service_for(c, *, before_stop=None, unknown=False):
    scope = TeamsPreAcquireRecoveryScope(recovery_id="original-teams-gap", configuration_sha256=c.config.sha256,
        job_id=c.work.binding.job_id, launch_id=c.launch_id, event_id=c.work.event_id,
        operator_oid=c.config.identity.human_oid, expires_at=c.clock[0]+timedelta(minutes=5),
        recovery_phase="teams_before_sdk_acquire")
    stops = []
    def stop(target, deadline, *, before_send):
        assert c.clock[0] < deadline
        if before_stop is not None:
            before_stop()
        before_send()
        stops.append(target)
        if unknown:
            raise Problem(503, "RECOVERY_STOP_UNKNOWN", "synthetic original provider response unavailable")
        return PlatformStopObservation(target=target, endpoint=stop_endpoint(target),
            credential_client_id=c.config.identity.managed_identity_client_id, status_code=204, observed_at=c.clock[0])
    service = TeamsPreAcquireRecovery(c.parent, scope, stop)
    return service, stops


def test_original_dynamic_teams_gap_requires_a_supported_recovery(original_teams_gap, monkeypatch):
    c = original_teams_gap
    service, stops = service_for(c)
    scope = service.scope
    plan = service.plan(c.principal)
    assert plan.binding.job_id == c.work.binding.job_id and stops == []
    original = c.store.transact
    original_entity, original_send = c.store._entity, c.transport.send
    sizes, inline, multipart = [], [], []
    def entity(*args, **kwargs):
        value = original_entity(*args, **kwargs)
        sizes.append(len(value["Payload"]))
        if getattr(args[1], "material", None) is not None:
            inline.append(len(args[1].material.data))
        return value
    def send(request, **kwargs):
        if request.method == "POST":
            multipart.append(len(request.body))
        return original_send(request, **kwargs)
    monkeypatch.setattr(c.store, "_entity", entity)
    monkeypatch.setattr(c.transport, "send", send)
    phases = []
    def committed(request, decide, **kwargs):
        result = original(request, decide, **kwargs)
        installed_origin(MigrationStore(c.parent.clients[0], c.config.table.scope), c.config)
        receipt_catalog(c.store.read_inventory())
        phases.append(result.payload)
        return result
    monkeypatch.setattr(c.store, "transact", committed)
    approval = service.authorize(c.principal, plan, "offline-original-close")
    done = service.execute(c.principal, approval)
    assert done["state"] == "cancelled" and done["model_outcome"] == "not_requested" and len(stops) == 1
    service.deliver(c.principal, approval)
    assert c.sends == ["original-Teams-result"]
    assert c.workers.inspect(c.work.binding.job_id).control_remaining == 0
    assert c.workers.inspect(c.work.binding.job_id).queries_reserved == 9
    final = c.store.read_inventory()
    assert c.workers.inspect(c.work.binding.job_id).result_forwarded
    assert final.rows[control_key("budget", c.work.binding.owner.root_budget_id)].payload["data"]["remaining_units"] == (
        c.before.rows[control_key("budget", c.work.binding.owner.root_budget_id)].payload["data"]["remaining_units"]-1)
    assert all(final.receipts[rid] == record for rid, record in c.before.receipts.items())
    assert service.execute(c.principal, approval)["stop_resent"] is False and len(stops) == 1
    assert service.deliver(c.principal, approval) and c.sends == ["original-Teams-result"]
    assert max(sizes) <= 49152 and max(inline) <= 32768 and max(multipart) <= 4*1024*1024
    print(f"max_entity={max(sizes)} max_inline={max(inline)} max_multipart={max(multipart)} "
          f"provider_stop=1 Teams_send=1 queries=9 control=0 prior_receipts_preserved={len(c.before.receipts)}")
    if output := os.environ.get("TEAMS_CLOSURE_OUTPUT"):
        Path(output).write_text(json.dumps({
            "scope": scope.model_dump(mode="json"), "plan": plan.model_dump(mode="json"),
            "authorization": approval.model_dump(mode="json"), "done": done,
            "phases": phases,
            "sizes": {"entity": max(sizes), "inline": max(inline), "multipart": max(multipart)},
            "before_rows": {rid: row.payload["data"].get("kind", row.payload["data"].get("schema_version", sorted(row.payload["data"])))
                            for rid, row in c.before.rows.items()},
            "after": {rid: row.payload for rid, row in c.store.read_inventory().rows.items() if rid not in c.before.rows},
        }, indent=2), encoding="utf-8")


def test_late_changes_and_expiry_are_not_new_stop_permissions(original_teams_gap, monkeypatch):
    from test_behavior_settlement import write
    from shared_context.behavior_assistant import key
    from shared_context.behavior_workers import worker_key
    from shared_context.behavior_channels_migration import channel_key
    from shared_context.behavior_migration import installed_origin
    c = original_teams_gap
    backing = c.transport.backing
    baseline, serial, instant = copy.deepcopy(backing.rows), backing.sequence, c.clock[0]
    checked = []
    # Restore only the isolated synthetic scenario between independent cases.
    # No original campaign record or live budget is ever reset.
    for fault in ("late-acquire", "orphan-model", "artifact", "result", "owner", "window",
                  "history", "claim", "native-report", "expiry", "unknown-row"):
        backing.rows, backing.sequence, c.clock[0] = copy.deepcopy(baseline), serial, instant
        service, stops = service_for(c)
        approval = service.authorize(c.principal, service.plan(c.principal), "offline-fault-approved")
        if fault == "late-acquire":
            sid = c.supervisor.clients[c.launch_id].assignment.session_id
            response = c.client.post(
                f"/behavior/v1/bindings/{c.work.binding.binding_id}/assistant/sessions/{sid}/acquire",
                json={"request_id": "sdk-start"}, headers=c.agent_headers)
            assert response.status_code == 200, response.text
        elif fault == "expiry":
            c.clock[0] = c.config.channels.valid_until
        else:
            sid = c.supervisor.clients[c.launch_id].assignment.session_id
            rid = {"orphan-model": key("model-intent", sid, "f"*64),
                   "artifact": key("artifact", c.work.binding.work.work_id),
                   "result": key("result", c.work.binding.work.work_id),
                   "owner": control_key("owner", c.work.binding.owner.owner_record_id),
                   "window": channel_key("current", c.config.table.scope.authority_domain),
                   "history": c.parent.teams.pointer().history_id,
                   "native-report": native_key("launch", c.launch_id),
                   "claim": native_key("launch", c.launch_id),
                   "unknown-row": "unrecognized-current-row"}[fault]
            data = c.store.read_snapshot((rid,))[rid]
            body = ({"request_sha256": "f"*64, "consumed": False} if fault == "orphan-model"
                    else {"synthetic_unexpected_effect": True} if data is None
                    else {**data.payload["data"], **({"active": False} if fault == "owner" else
                          {"last_report_sequence": 99} if fault == "native-report" else
                          {"claim_id": "f"*64} if fault == "claim" else {"revision": 99})})
            if fault in {"window", "history"}:
                # Corrupt only the synthetic backing; real normal writers
                # cannot replace the protected current installation pointer.
                entity = backing.rows[("Behavior", c.store._scope.partition_key, "behavior:v1:row:"+rid)]
                envelope = json.loads(entity["Payload"])
                envelope["data"] = body
                entity["Payload"] = json.dumps(envelope).encode()
            else:
                write(c.store, rid, body)
        with pytest.raises(Problem):
            service.execute(c.principal, approval)
        assert stops == []
        assert c.workers.inspect(c.work.binding.job_id).completion_credit == 1
        checked.append(fault)
    backing.rows, backing.sequence, c.clock[0] = copy.deepcopy(baseline), serial, instant
    def late_before_send():
        write(c.store, control_key("owner", c.work.binding.owner.owner_record_id),
              {**c.store.read_snapshot((control_key("owner", c.work.binding.owner.owner_record_id),))[
                  control_key("owner", c.work.binding.owner.owner_record_id)].payload["data"], "epoch": 2})
    service, stops = service_for(c, before_stop=late_before_send)
    approval = service.authorize(c.principal, service.plan(c.principal), "offline-last-wire-gate")
    with pytest.raises(Problem):
        service.execute(c.principal, approval)
    assert stops == [] and c.workers.inspect(c.work.binding.job_id).stop_credit == 0
    backing.rows, backing.sequence, c.clock[0] = copy.deepcopy(baseline), serial, instant
    service, stops = service_for(c)
    approval = service.authorize(c.principal, service.plan(c.principal), "offline-last-read-expiry")
    read_inventory, final_reads = c.store.read_inventory, []
    def expire_after_last_read(*args, **kwargs):
        import sys
        caller = sys._getframe(1).f_code.co_name
        value = read_inventory(*args, **kwargs)
        if caller == "final_gate":
            final_reads.append(True)
            if len(final_reads) == 2:
                c.clock[0] = c.config.channels.valid_until
        return value
    monkeypatch.setattr(c.store, "read_inventory", expire_after_last_read)
    with pytest.raises(Problem):
        service.execute(c.principal, approval)
    assert final_reads == [True, True] and stops == []
    monkeypatch.setattr(c.store, "read_inventory", read_inventory)
    print("rejected without provider stop: "+",".join(checked)+
          ",late-owner-before-provider-send,expiry-after-final-provider-read")


def test_original_stop_ackloss_and_restart_never_resend(original_teams_gap, monkeypatch):
    from azure.core.exceptions import ServiceRequestError
    from test_behavior_store import parse_batch
    from shared_context.behavior_recovery import recovery_key
    c = original_teams_gap
    baseline, serial = copy.deepcopy(c.transport.backing.rows), c.transport.backing.sequence
    transport = c.transport.send
    for stage in ("stop-intent", "stop-observation"):
        c.transport.backing.rows, c.transport.backing.sequence = copy.deepcopy(baseline), serial
        service, stops = service_for(c)
        approval = service.authorize(c.principal, service.plan(c.principal), "offline-stop-loss")
        wanted = recovery_key(stage, service.scope.recovery_id)
        lost, submits = [], []
        def send(request, **kwargs):
            response = transport(request, **kwargs)
            if request.method == "POST":
                operations, _ = parse_batch(request)
                receipt = next(json.loads(op[1]["Payload"]) for op in operations if "behavior:v1:receipt:" in op[1]["RowKey"])
                if receipt["request"]["request_id"] == wanted:
                    submits.append(True)
                    if not lost:
                        lost.append(receipt["request"])
                        raise ServiceRequestError("synthetic committed ACK loss")
            return response
        monkeypatch.setattr(c.transport, "send", send)
        with pytest.raises(Problem) as unknown:
            service.execute(c.principal, approval)
        assert unknown.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
        from shared_context.behavior_store import ControlRequest
        c.store.reconcile(ControlRequest.model_validate_json(json.dumps(lost[0])))
        installed_origin(MigrationStore(c.parent.clients[0], c.config.table.scope), c.config)
        restarted, again = service_for(c)
        assert restarted.execute(c.principal, approval)["stop_resent"] is False
        assert again == [] and len(stops) == (0 if stage == "stop-intent" else 1)
        if stage == "stop-observation":
            assert restarted.finish(c.principal, approval)["state"] == "cancelled"
            restarted.deliver(c.principal, approval)
            assert len(c.sends) == 1
        else:
            assert c.workers.inspect(c.work.binding.job_id).completion_credit == 1
        assert submits == [True]
        monkeypatch.setattr(c.transport, "send", transport)


def test_signed_operator_and_expiry_do_not_expand_original_scope(original_teams_gap):
    from shared_context.behavior_recovery_api import create_recovery_app
    from test_m3_auth import token
    c = original_teams_gap
    service, stops = service_for(c)
    api = TestClient(create_recovery_app(c.parent, service.scope, stop=service._platform_stop))
    prefix = "/operator/v1/sdk-recovery"
    identity = c.config.identity
    claims = {"tid": identity.tenant_id, "iss": f"https://sts.windows.net/{identity.tenant_id}/",
              "aud": identity.audience, "oid": identity.human_oid, "appid": identity.admin_client_id,
              "scp": "Behavior.Read"}
    reader = {"Authorization": "Bearer "+token(c.h.c.signing_key, claims)}
    denied = api.post(prefix+"/plan", json={}, headers=reader)
    assert denied.status_code == 403
    for changed in ({"oid": "00000000-0000-0000-0000-000000000001"},
                    {"appid": "00000000-0000-0000-0000-000000000002"},
                    {"tid": "00000000-0000-0000-0000-000000000003"}):
        forbidden = {"Authorization": "Bearer "+token(c.h.c.signing_key, {
            **claims, "scp": "Behavior.Read Behavior.Approve", **changed})}
        assert api.post(prefix+"/plan", json={}, headers=forbidden).status_code in (401, 403)
    planned = api.post(prefix+"/plan", json={}, headers=c.headers)
    assert planned.status_code == 200, planned.text
    granted = api.post(prefix+"/authorize", json={"plan": planned.json(), "decision_id": "offline-jwt-original"},
                       headers=c.headers)
    assert granted.status_code == 200, granted.text
    assert granted.json()["schema_version"] == "fa-teams-pre-acquire-recovery-authorization-v1"
    assert api.post(prefix+"/execute", json={**granted.json(), "sdk_stop": {"graceful": True}},
                    headers=c.headers).status_code == 422
    done = api.post(prefix+"/execute", json=granted.json(), headers=c.headers)
    assert done.status_code == 200, done.text
    assert done.json()["model_outcome"] == "not_requested" and not done.json()["sdk_graceful_exit_claim"]
    c.clock[0] = c.config.channels.valid_until
    for phase in ("execute", "finish", "deliver"):
        refused = api.post(prefix+"/"+phase, json=granted.json(), headers=c.headers)
        assert refused.status_code == 403, (phase, refused.text)
    observed = api.post(prefix+"/reconcile", json=granted.json(), headers=reader)
    assert observed.status_code == 200 and observed.json()["stop_resent"] is False
    assert len(stops) == 1 and c.sends == []


def test_unknown_stop_and_tampered_terminal_history_remain_unresolved(original_teams_gap):
    from test_behavior_settlement import write
    from shared_context.behavior_recovery import recovery_key
    c = original_teams_gap
    baseline, serial = copy.deepcopy(c.transport.backing.rows), c.transport.backing.sequence
    service, stops = service_for(c, unknown=True)
    approval = service.authorize(c.principal, service.plan(c.principal), "offline-stop-unknown")
    with pytest.raises(Problem) as unknown:
        service.execute(c.principal, approval)
    assert unknown.value.body["error"]["code"] == "RECOVERY_STOP_UNKNOWN"
    assert service.execute(c.principal, approval)["stop_resent"] is False and len(stops) == 1
    assert c.workers.inspect(c.work.binding.job_id).completion_credit == 1
    assert c.store.read_snapshot((recovery_key("stop-observation", service.scope.recovery_id),))[
        recovery_key("stop-observation", service.scope.recovery_id)] is None
    c.transport.backing.rows, c.transport.backing.sequence = copy.deepcopy(baseline), serial
    service, stops = service_for(c)
    approval = service.authorize(c.principal, service.plan(c.principal), "offline-proof-original")
    service.execute(c.principal, approval)
    closed, sequence = copy.deepcopy(c.transport.backing.rows), c.transport.backing.sequence
    for fault in ("unknown-row", "budget-refund", "query-reset", "proof"):
        c.transport.backing.rows, c.transport.backing.sequence = copy.deepcopy(closed), sequence
        if fault == "proof":
            rid = native_key("platform-stop", c.launch_id)
            item = c.transport.backing.rows[("Behavior", c.store._scope.partition_key, "behavior:v1:row:"+rid)]
            payload = json.loads(item["Payload"])
            payload["data"]["observation"]["credential_client_id"] = "foreign"
            item["Payload"] = json.dumps(payload).encode()
        else:
            from shared_context.behavior_workers import worker_key
            rid = ("unknown-after-seal" if fault == "unknown-row" else
                   control_key("budget", c.work.binding.owner.root_budget_id) if fault == "budget-refund" else
                   worker_key("job", c.work.binding.job_id))
            row = c.store.read_snapshot((rid,))[rid]
            value = ({"unknown": True} if row is None else {**row.payload["data"],
                **({"remaining_units": row.payload["data"]["remaining_units"]+1} if fault == "budget-refund"
                   else {"queries_reserved": 0})})
            write(c.store, rid, value)
        with pytest.raises(Problem):
            installed_origin(MigrationStore(c.parent.clients[0], c.config.table.scope), c.config)
        with pytest.raises(Problem):
            service.deliver(c.principal, approval)
    assert c.sends == [] and len(stops) == 1


def test_lost_original_teams_receipt_reconciles_without_second_post(original_teams_gap, monkeypatch):
    from azure.core.exceptions import ServiceRequestError
    from test_behavior_store import parse_batch
    from shared_context.behavior_store import ControlRequest
    c = original_teams_gap
    service, stops = service_for(c)
    approval = service.authorize(c.principal, service.plan(c.principal), "offline-once-report")
    service.execute(c.principal, approval)
    did = control_key("delivery-final-id", c.work.binding.obligation_id)
    wanted = control_key("teams-provider-receipt", did)
    transport, lost, count = c.transport.send, [], []
    def send(request, **kwargs):
        response = transport(request, **kwargs)
        if request.method == "POST":
            operations, _ = parse_batch(request)
            receipt = next(json.loads(op[1]["Payload"]) for op in operations if "behavior:v1:receipt:" in op[1]["RowKey"])
            if receipt["request"]["request_id"] == wanted:
                count.append(True)
                if not lost:
                    lost.append(receipt["request"])
                    raise ServiceRequestError("synthetic original provider receipt committed ACK lost")
        return response
    monkeypatch.setattr(c.transport, "send", send)
    with pytest.raises(Problem):
        service.deliver(c.principal, approval)
    assert c.sends == ["original-Teams-result"] and len(stops) == 1
    c.store.reconcile(ControlRequest.model_validate_json(json.dumps(lost[0])))
    installed_origin(MigrationStore(c.parent.clients[0], c.config.table.scope), c.config)
    restarted, new_stops = service_for(c)
    receipt = restarted.deliver(c.principal, approval)
    assert receipt.payload["result"]["fulfillment"] == "fulfilled"
    assert c.sends == ["original-Teams-result"] and new_stops == [] and count == [True]
