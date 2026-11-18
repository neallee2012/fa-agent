"""Real native-mode authority/HTTP producers on offline Table transports only."""

from dataclasses import replace
from datetime import timedelta
import base64
import json
import os
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from shared_context import behavior_assistant_wire as aw, behavior_wire as wire
from shared_context import behavior_delivery as delivery, behavior_inbox as inbox_module
from shared_context.behavior_assistant import AssistantAuthority, _LocalHttp, _Session, _row, key
from shared_context.behavior_assistant_hosted import AssistantClient, AssistantSupervisor
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_migration import MigrationStore, installed_origin
from shared_context.behavior_operations import control_key
from shared_context.behavior_server import application, build_server
from shared_context.behavior_store import ControlRequest
from shared_context.source_materials import digest
from shared_context.errors import Problem
from shared_context.m3_artifacts import ArtifactService
from shared_context.m3_auth import ApprovalSettings
from shared_context.m3_store import InMemoryArtifactStore
from shared_context.models import CreateWork
from shared_context.service import ContextService
from test_behavior_assistant_server import AssistantHarness
from test_behavior_server import NOW
from test_behavior_store import Credential, WireTransport, json_response
from test_m3_auth import jwk_client, signing_key, token
from conftest import InMemoryRepository, FakeMemory
from behavior_assistant_acquire_conflicts_helpers import installed_assistant


class QueryTransport(WireTransport):
    """Actual Table SDK serialization, exclusively local HTTP responses."""
    def send(self, request, **kwargs):
        if request.method == "GET" and urlsplit(request.url).path.rstrip("()") == "/Behavior":
            self.requests.append(request)
            assert parse_qs(urlsplit(request.url).query)["$filter"] == ["PartitionKey eq 'authority'"]
            items = [{**entity, "Payload": base64.b64encode(entity["Payload"]).decode(),
                      "Payload@odata.type": "Edm.Binary", "odata.etag": entity.metadata["etag"]}
                     for (table, partition, _), entity in sorted(self.backing.rows.items())
                     if table == "Behavior" and partition == "authority"]
            return json_response(request, 200, {"value": items}, {})
        return super().send(request, **kwargs)


@pytest.fixture
def native_acquire(installed_assistant, jwk_client, signing_key, request):
    c, config = installed_assistant, installed_assistant.config
    clock = [c.clock[0] + timedelta(seconds=1)]
    transport = QueryTransport()
    transport.backing = c.table.backing
    def forbidden(*args, **kwargs):
        pytest.fail("no external model, Blob or Foundry call is allowed")
    no_io = WireTransport()
    no_io.send = forbidden
    with build_server(config, credential=Credential(), table_transport=transport, source_transport=no_io,
                      backup_transport=no_io, assistant_transport=httpx.MockTransport(forbidden),
                      native_transports={t.executor_id: httpx.MockTransport(forbidden) for t in config.active_targets},
                      key_client=jwk_client, clock=lambda: clock[0]) as composition:
        runtime = composition.runtime
        store = runtime.store
        installed_origin(store, config)
        b, profile = config.bindings[0], config.assistant.profiles[0]
        auth = config.identity.auth()
        actor = auth.actors[b.agent_oid]
        context = ContextService(InMemoryRepository(), FakeMemory(), auth)
        context.create(actor, b.team_id, profile.legacy_source_work, CreateWork(goal="Offline original source"))
        artifacts = ArtifactService(context, InMemoryArtifactStore(auth.tenant_id),
                                    ApprovalSettings(config.identity.human_oid, config.identity.admin_client_id, b.team_id))
        authority = AssistantAuthority(runtime, artifacts, (actor,))
        composition.assistant.authority = authority
        principal = replace(c.principal, actions=c.principal.actions | {"submit"})
        runtime.submit(b.binding_id, aw.AssistantSubmit(request_id="one-original-admission"), principal)
        starts = []
        reservations = []
        def reserve(start):
            starts.append(start)
            reservations.append(runtime.dep.native_launches.prepare(start))
            raise RuntimeError("offline reservation; provider must not be called")
        runtime.dep.workers.adapter = replace(runtime.dep.workers.adapter, start=reserve)
        with pytest.raises(Problem):
            runtime.dep.workers.start(b.job_id, b.owner)
        launch, permit = reservations[0]
        identity = wire.HostedIdentity(**{field: getattr(config.active_targets[0], field) for field in (
            "agent_id", "agent_name", "agent_version", "session_id")})
        agent = BehaviorPrincipal(b.tenant_id, b.agent_oid, b.team_id, "agent", actor.actor,
                                  frozenset({"read", "consume", "execute"}))
        before_native = getattr(request, "param", None) == "before-native"
        claim = None if before_native else runtime.dep.native_launches.claim(b.binding_id, launch.launch_id, wire.HostedClaimRequest(
            request_id="one-native-claim", invocation_id="offline-native-invocation", identity=identity,
            process_id="offline-host-process", launch_permit=permit), agent)
        claim_command = None if before_native else wire.HostedProbeRequest(
            request_id="one-assistant-claim", invocation_id=claim.assignment.invocation_id,
            identity=identity, process_id=claim.assignment.process_id, claim_id=claim.assignment.claim_id,
            report_token=claim.report_token)
        assignment, ticket = (None, None) if getattr(request, "param", None) in {"before-assignment", "before-native"} else (
            authority.native_claim(b.binding_id, launch.launch_id, claim_command, agent))
        claims = {"tid": config.identity.tenant_id, "iss": f"https://sts.windows.net/{config.identity.tenant_id}/",
                  "aud": config.identity.audience, "oid": b.agent_oid, "appid": b.agent_oid,
                  "roles": ["Behavior.Agent"]}
        headers = {"Authorization": "Bearer " + token(signing_key, claims, remove=("scp",))}
        client = TestClient(application(composition), raise_server_exceptions=False)
        requests = []
        statuses = []
        local = _LocalHttp(client)
        def send(request):
            requests.append(request)
            response = local.handle_request(request)
            statuses.append(response.status_code)
            return response
        hosted = None if assignment is None else AssistantClient(
            "https://context.invalid", assignment, ticket,
            token_provider=lambda: headers["Authorization"].removeprefix("Bearer "),
            transport=httpx.MockTransport(send))
        competitor = MigrationStore(composition.clients[0], store._scope)
        other_inbox = inbox_module.PullInbox(competitor, runtime.dep.inbox.registration)
        other_core = delivery.DeliveryCore(
            competitor, clock=lambda: clock[0],
            registration=replace(runtime.dep.delivery.registration, channel_adapter=other_inbox))
        other_inbox.attach(other_core)
        count = [0]
        def tick():
            count[0] += 1
            if count[0] == 1:
                delivery.DeliveryConsumer(other_core).drain(
                    b.owner.root_budget_id, lambda _: b.owner, "concurrent-controller", limit=1)
            else:
                runtime.dep.native_launches.probe(starts[0].binding)
        try:
            yield SimpleNamespace(
                h=c, config=config, clock=clock, composition=composition, authority=authority, store=store,
                transport=transport, hosted=hosted, requests=requests, client=client,
                assignment=assignment, ticket=ticket, binding=b, tick=tick, count=count, starts=starts,
                statuses=statuses, competitor=competitor, native=runtime.dep.native_launches,
                native_claim=claim, claim_command=claim_command, agent=agent, principal=principal, launch_id=launch.launch_id,
                permit=permit, identity=identity, api_transport=httpx.MockTransport(send), token=headers["Authorization"].removeprefix("Bearer "))
        finally:
            if hosted is not None:
                hosted.close()
            client.close()


def receipt(case):
    request = ControlRequest(request_id=key("acquire", case.assignment.session_id, "sdk-start"),
        request_digest=digest({"request_id": "sdk-start"}),
        read_set=tuple(dict.fromkeys(case.authority._keys(session(case)))))
    return case.store._receipt(request)


def session(case):
    sid = case.assignment.session_id
    return _row(case.store.read_snapshot((key("session", sid),)), key("session", sid), _Session)


def inject_race(case, monkeypatch, kind, *, times=1, change=None):
    original = case.transport.send
    requests = []
    acquire_id = key("acquire", case.assignment.session_id, "sdk-start")
    def send(request, **kwargs):
        match = request.method == "POST" and acquire_id.encode() in request.body
        if match:
            requests.append(request)
            if len(requests) <= times:
                case.tick()
                if change is not None:
                    change()
        return original(request, **kwargs)
    monkeypatch.setattr(case.transport, "send", send)
    return requests


@pytest.mark.parametrize("field", ["code", "phase", "duplicate"])
def test_only_allowlisted_acquire_headers_can_enter_first_failure(tmp_path, caplog, field):
    private = "SYNTHETIC_PRIVATE_CANARY"
    headers = [("x-behavior-assistant-error-code", "BEHAVIOR_CONFLICT"),
               ("x-behavior-assistant-error-phase", "native_evidence")]
    if field == "code":
        headers[0] = (headers[0][0], private)
    elif field == "phase":
        headers[1] = (headers[1][0], private)
    else:
        headers.append(headers[0])
    client = AssistantClient(
        "https://context.invalid", SimpleNamespace(binding_id="binding", session_id="a" * 64, resume=False),
        SecretStr("b" * 64), token_provider=lambda: private,
        transport=httpx.MockTransport(lambda _: httpx.Response(412, headers=headers, text=private)))
    supervisor = AssistantSupervisor(tmp_path, runtime_path=tmp_path / "must-not-start")
    try:
        supervisor._execute(SimpleNamespace(launch_id="original"), client)
    finally:
        client.close()
    with supervisor.journal.connect() as db:
        raw, = db.execute("SELECT body FROM assistant_failures WHERE launch_id='original'").fetchone()
    assert private not in raw and private not in caplog.text
    saved = json.loads(raw)
    if field in {"code", "duplicate"}:
        assert "upstream_error_code" not in saved
    if field == "phase":
        assert "upstream_error_phase" not in saved


def test_server_and_hosted_diagnostic_allowlists_match():
    from shared_context.behavior_assistant_api import _ACQUIRE_CODES as published
    from shared_context.behavior_assistant_hosted import _ACQUIRE_CODES as accepted
    assert published == accepted
