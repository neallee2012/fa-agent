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
def installed_assistant(tmp_path, jwk_client):
    """An assistant configuration installed by the ordinary bootstrap writer, on its own offline Table."""
    h = AssistantHarness(tmp_path, jwk_client)
    h.legacy()
    h.activate()
    identity = h.config.identity
    principal = BehaviorPrincipal(identity.tenant_id, identity.human_oid, identity.team_id, "human", None,
                                  frozenset({"read", "submit", "approve", "acknowledge"}))
    try:
        yield SimpleNamespace(harness=h, table=h.table, config=h.config, principal=principal, clock=[NOW])
    finally:
        h.composition.close()


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


@pytest.mark.parametrize("race", ["egt", "predicate"])
def test_one_no_effect_conflict_does_not_strand_the_original_acquire(native_acquire, monkeypatch, race):
    c = native_acquire
    original = c.transport.send
    raced = []
    acquire_id = key("acquire", c.assignment.session_id, "sdk-start")
    def send(request, **kwargs):
        match = (request.method == "GET" and urlsplit(request.url).path.rstrip("()") == "/Behavior"
                 if race == "inventory" else request.method == "POST"
                 and acquire_id.encode() in request.body)
        if race == "predicate":
            match = request.method == "GET" and key("session", c.assignment.session_id) in request.url
        if match and not raced:
            raced.append(race)
            from concurrent.futures import ThreadPoolExecutor
            # A competing controller is not inside the reader's pure scope.
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(c.tick).result()
        return original(request, **kwargs)
    monkeypatch.setattr(c.transport, "send", send)
    try:
        result = c.hosted.acquire()
    except Problem as error:
        assert raced == [race], "expected contention was not reached"
        assert error.body["error"]["upstream_status"] == 412
        assert receipt(c) is None and session(c).state == "accepted"
        raise
    assert result.state == "running"
    assert raced == [race] and len(c.starts) == 1
    assert receipt(c) is not None and session(c).model_requests == 0
    assert len(c.requests) == 1
    with pytest.raises(Problem):
        c.hosted.acquire()


@pytest.mark.parametrize("race,phase", [("egt", "acquire_authority")])
def test_exhausted_conflicts_keep_first_safe_category_and_do_not_spawn_sdk(native_acquire, monkeypatch, tmp_path, race, phase):
    c = native_acquire
    seen = inject_race(c, monkeypatch, race, times=3)
    supervisor = AssistantSupervisor(tmp_path / "original-host", runtime_path=tmp_path / "must-not-run")
    supervisor._execute(SimpleNamespace(launch_id="original-launch"), c.hosted)
    assert len(seen) == 3 and c.count == [3] and len(c.requests) == 1
    assert session(c).state == "accepted" and session(c).model_requests == 0 and receipt(c) is None
    with supervisor.journal.connect() as db:
        raw, = db.execute("SELECT body FROM assistant_failures WHERE launch_id='original-launch'").fetchone()
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
    saved = json.loads(raw)
    assert saved["upstream_status"] == 412
    assert saved["upstream_error_code"] == "BEHAVIOR_CONFLICT"
    assert saved["upstream_error_phase"] == phase
    assert not supervisor.record_failure("original-launch", "acquire", Problem(503, "later-error", "private"))
    with supervisor.journal.connect() as db:
        assert db.execute("SELECT body FROM assistant_failures WHERE launch_id='original-launch'").fetchone()[0] == raw


@pytest.mark.parametrize("failure", ["table-response-lost", "http-response-lost", "network-no-response"])
def test_unknown_response_never_replays_acquire_or_grants_second_sdk(native_acquire, monkeypatch, failure):
    c = native_acquire
    original = c.hosted.http._transport.handle_request
    acquire_id = key("acquire", c.assignment.session_id, "sdk-start").encode()
    if failure == "table-response-lost":
        c.transport.after_apply = "lost"
    else:
        def send(request):
            if failure == "http-response-lost":
                response = original(request)
                assert response.status_code == 200
                response.close()
            raise httpx.ReadTimeout("SYNTHETIC_PRIVATE_FAILURE", request=request)
        c.hosted.http._transport = httpx.MockTransport(send)
    with pytest.raises(Problem):
        c.hosted.acquire()
    c.transport.after_apply = None
    posts = [r for r in c.transport.requests if r.method == "POST" and acquire_id in r.body]
    assert len(posts) == (0 if failure == "network-no-response" else 1)
    assert (receipt(c) is None) == (failure == "network-no-response")
    assert session(c).model_requests == 0
    if failure != "network-no-response":
        c.hosted.http._transport = httpx.MockTransport(original)
        with pytest.raises(Problem) as denied:
            c.hosted.acquire()
        assert denied.value.body["error"]["upstream_error_code"] == "ASSISTANT_START_UNKNOWN"
        assert len([r for r in c.transport.requests if r.method == "POST" and acquire_id in r.body]) == 1


@pytest.mark.parametrize("change", ["owner", "expired", "job", "request", "binding", "ticket", "assignment"])
def test_same_request_retry_rechecks_current_scope_and_denies_revoked_authority(native_acquire, monkeypatch, change):
    from test_behavior_settlement import write
    c = native_acquire
    b = c.binding
    def mutate():
        if change == "expired":
            c.clock[0] = c.config.policies[0].policy.valid_until + timedelta(seconds=1)
        elif change in {"owner", "job"}:
            from shared_context.behavior_workers import worker_key
            row_id = control_key("owner", b.owner.owner_record_id) if change == "owner" else worker_key("job", b.job_id)
            row = c.competitor.read_snapshot((row_id,))[row_id]
            write(c.competitor, row_id, {**row.payload["data"], **(
                {"active": False} if change == "owner" else {"cancel_requested": True})})
        elif change in {"ticket", "assignment"}:
            row_id = key("session", c.assignment.session_id)
            row = c.competitor.read_snapshot((row_id,))[row_id]
            update = ({"ticket_digest": "f" * 64} if change == "ticket" else
                      {"assignment": {**row.payload["data"]["assignment"], "sdk_session_id": "changed-sdk-session"}})
            write(c.competitor, row_id, {**row.payload["data"], **update})
    if change in {"owner", "expired", "job", "ticket", "assignment"}:
        seen = inject_race(c, monkeypatch, "egt", change=mutate)
        with pytest.raises(Problem):
            c.hosted.acquire()
        assert len(seen) == 1 and receipt(c) is None
    else:
        url = (f"/behavior/v1/bindings/{b.binding_id if change != 'binding' else 'foreign-binding'}"
               f"/assistant/sessions/{c.assignment.session_id}/acquire")
        result = c.client.post(url, json={"request_id": "changed-request" if change == "request" else "sdk-start"},
            headers={"Authorization": "Bearer " + c.hosted.token_provider(),
                     "x-behavior-assistant-ticket": c.ticket.get_secret_value()})
        assert result.status_code in {403, 422} and receipt(c) is None
    assert session(c).state == "accepted" and session(c).model_requests == 0


@pytest.mark.parametrize("status,code", [(409, "BEHAVIOR_CONFLICT"), (412, "MIGRATION_HISTORY_INVALID"),
                                       (500, "BEHAVIOR_CONFLICT"), (503, "BEHAVIOR_READ_FAILED")])
def test_only_exact_known_no_effect_conflict_is_reentered(native_acquire, monkeypatch, status, code):
    c = native_acquire
    calls = []
    def fail(*args):
        calls.append(code)
        raise Problem(status, code, "SYNTHETIC_PRIVATE_CANARY")
    monkeypatch.setattr(c.authority, "acquire", fail)
    with pytest.raises(Problem) as error:
        c.hosted.acquire()
    assert calls == [code] and len(c.requests) == 1
    assert error.value.body["error"]["upstream_error_code"] == code
    assert receipt(c) is None


def test_committed_receipt_cannot_bypass_later_owner_revocation(native_acquire):
    from test_behavior_settlement import write
    c = native_acquire
    assert c.hosted.acquire().state == "running"
    saved = receipt(c)
    owner_key = control_key("owner", c.binding.owner.owner_record_id)
    row = c.competitor.read_snapshot((owner_key,))[owner_key]
    write(c.competitor, owner_key, {**row.payload["data"], "active": False})
    with pytest.raises(Problem) as denied:
        c.hosted.acquire()
    assert denied.value.body["error"]["upstream_error_code"] == "BEHAVIOR_CURRENT_POLICY_DENIED"
    assert receipt(c) == saved and session(c).model_requests == 0


def test_positive_receipt_after_exception_cannot_become_a_fresh_start_permission(native_acquire, monkeypatch):
    c = native_acquire
    original = c.authority.acquire
    calls = []
    def committed_then_failed(session, command):
        calls.append(command)
        original(session, command)
        raise Problem(412, "BEHAVIOR_CONFLICT", "post-commit observer failed")
    monkeypatch.setattr(c.authority, "acquire", committed_then_failed)
    with pytest.raises(Problem) as denied:
        c.hosted.acquire()
    assert denied.value.body["error"]["upstream_error_code"] == "ASSISTANT_START_UNKNOWN"
    assert len(calls) == 1 and receipt(c) is not None and session(c).state == "running"
    assert session(c).model_requests == 0


def test_two_simultaneous_acquires_get_only_one_fresh_permission(native_acquire, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    c = native_acquire
    barrier = Barrier(2)
    original = c.transport.send
    acquire_id = key("acquire", c.assignment.session_id, "sdk-start").encode()
    posts = []
    def send(request, **kwargs):
        if request.method == "POST" and acquire_id in request.body:
            posts.append(request)
            barrier.wait(timeout=10)
        return original(request, **kwargs)
    monkeypatch.setattr(c.transport, "send", send)
    def acquire():
        try:
            return c.hosted.acquire().state
        except Problem as error:
            return error.body["error"].get("upstream_error_code")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(acquire), pool.submit(acquire)
        results = [first.result(timeout=20), second.result(timeout=20)]
    assert sorted(results) == ["ASSISTANT_START_UNKNOWN", "running"]
    assert len(posts) == 2 and len(c.requests) == 2 and receipt(c) is not None
    assert session(c).state == "running" and session(c).model_requests == 0


def test_bounded_reentry_retains_original_assignment_and_request_bytes(native_acquire, monkeypatch):
    c = native_acquire
    before = c.assignment.model_dump_json()
    seen = inject_race(c, monkeypatch, "egt", times=2)
    assert c.hosted.acquire().state == "running"
    assert len(seen) == 3 and c.count == [2]
    assert c.assignment.model_dump_json() == before
    assert len(c.requests) == 1 and c.requests[0].content == wire.Command(request_id="sdk-start").model_dump_json().encode()
    saved = receipt(c)
    assert saved.payload["request"]["request_digest"] == digest({"request_id": "sdk-start"})
    assert len(saved.payload["writes"]) == 1


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


def test_other_412_from_native_evidence_is_not_a_conflict_retry(native_acquire, monkeypatch):
    c = native_acquire
    calls = []
    def reject():
        calls.append("native-evidence")
        raise Problem(412, "MIGRATION_HISTORY_INVALID", "SYNTHETIC_PRIVATE_CANARY")
    monkeypatch.setattr(c.composition, "require_current_native_evidence", reject)
    with pytest.raises(Problem) as error:
        c.hosted.acquire()
    assert calls == ["native-evidence"] and len(c.requests) == 1
    assert error.value.body["error"]["upstream_error_code"] == "MIGRATION_HISTORY_INVALID"
    assert error.value.body["error"]["upstream_error_phase"] == "native_evidence"
    assert receipt(c) is None
