"""Real operator/parser, JWT verifier and Table SDK with local transports only."""

import base64
from copy import deepcopy
from datetime import timedelta
import json
import os
import socket
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

from azure.data.tables import TableTransactionError
from fastapi.testclient import TestClient
import pytest

from shared_context.behavior_agent_registry import (
    read_agent_registry, validate_agent_registry_history,
)
from shared_context.behavior_fresh_installation import (
    FreshInitializer, FreshInstallationAuthorization, FreshInstallationInputs,
    fresh_key, installed_fresh_origin, validate_fresh_inventory,
)
from shared_context.behavior_migration import MigrationStore, installation_record, installed_origin, migration_key
from shared_context.behavior_operations import control_key
from shared_context.behavior_server import application, bootstrap, build_server
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite, GUARD_KEY
from shared_context.behavior_work_policy import CostLedger, SteadyWorkPolicy, WorkRegistry, work_policy_key
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_behavior_assistant_server import AssistantHarness
from test_behavior_store import WireTransport, batch_conflict_response, json_response
from test_m3_auth import jwk_client, signing_key


class FreshTableTransport(WireTransport):
    def __init__(self):
        super().__init__()
        self.fail_list = None
        self.empty_first_page = False
        self.before_post = None

    def send(self, request, **kwargs):
        if request.method == "GET" and urlsplit(request.url).path.rstrip("()") == "/Behavior":
            self.requests.append(request)
            query = parse_qs(urlsplit(request.url).query)
            if self.fail_list is not None:
                return json_response(request, self.fail_list, {"odata.error": {
                    "code": "SyntheticReadFailed", "message": {"value": "synthetic list failure"},
                }}, {})
            if self.empty_first_page and "NextPartitionKey" not in query:
                return json_response(request, 200, {"value": []},
                                     {"x-ms-continuation-NextPartitionKey": "other"})
            filtered = query.get("$filter")
            assert filtered in (None, ["PartitionKey eq 'authority'"])
            items = [{**entity, "Payload": base64.b64encode(entity.get("Payload", b"unknown")).decode(),
                      "Payload@odata.type": "Edm.Binary", "odata.etag": entity.metadata["etag"]}
                     for (table, partition, _), entity in sorted(self.backing.rows.items())
                     if table == "Behavior" and (filtered is None or partition == "authority")]
            return json_response(request, 200, {"value": items}, {})
        if request.method == "POST" and self.before_post is not None:
            callback, self.before_post = self.before_post, None
            callback()
        try:
            return super().send(request, **kwargs)
        except TableTransactionError as error:
            return batch_conflict_response(request, error.status_code)


def fresh_inputs(config):
    inputs = FreshInstallationInputs(
        policy=SteadyWorkPolicy(
            policy_id="fresh-native-policy", revision=1, configuration_sha256=config.sha256,
            evidence_mode="native", authority_domain=config.table.scope.authority_domain,
            control_binding=config.workers.control_binding, active=True, read_enabled=True,
            template=config.bindings[0], profile=config.assistant.profiles[0],
            legacy_template_policy=config.policies[0].policy,
            cost_scope_sha256=digest(config.resource_bindings()), cost_observer_oid=config.evidence.observer_oid,
            minimum_observed_microusd=0, work_estimate_microusd=1_000_000, cost_max_age_seconds=300,
            queue_seconds=min(600, config.delivery.max_queue_seconds), max_model_requests=1,
        ),
        peer_agent_id=config.targets[1].agent_id, peer_registration_id="fresh-b-registration",
        initial_accounted_microusd=0, budget_enforced=False,
    )
    return FreshInstallationInputs.model_validate_json(inputs.model_dump_json())


@pytest.fixture
def fresh_case(tmp_path, jwk_client, monkeypatch):
    connect = socket.socket.connect
    def external(sock, address):
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return connect(sock, address)
        pytest.fail("fresh offline initialization must not open an external connection")
    monkeypatch.setattr(socket.socket, "connect", external)
    monkeypatch.setattr("test_behavior_server.WireTransport", FreshTableTransport)
    monkeypatch.setenv("FA_NATIVE_TURNS", "foundry-native-v1")
    monkeypatch.delenv("FA_WORK_COST_OBSERVATION_FILE", raising=False)
    h = AssistantHarness(tmp_path, jwk_client)
    config = h.config
    h.inputs = fresh_inputs(config)
    h.initializer = FreshInitializer(h.composition)
    h.plan = h.initializer.plan(h.inputs, request_id="fresh-original-request", installation_id="fresh-original-install")
    h.authorization = FreshInstallationAuthorization(
        decision_id="fresh-operator-file-approval", authorizing_human_oid=config.identity.human_oid,
        configuration_sha256=config.sha256, plan_sha256=h.plan.sha256,
        not_before=h.time, expires_at=h.time + timedelta(minutes=5),
    )
    yield h
    h.composition.close()


def writes(h):
    return sum(request.method == "POST" for request in h.table.requests)


def initialize(h):
    return h.initializer.execute(h.plan, h.authorization)


def test_complete_atomic_installation_without_legacy_history_or_readiness(fresh_case):
    h = fresh_case
    assert not h.table.backing.rows and writes(h) == 0
    receipt = initialize(h)
    assert writes(h) == 1 and receipt.payload["guard_revision"] == 1
    assert all(part[0] == "POST" for part in h.table.parts[0])
    assert len(h.table.parts[0]) == len(receipt.payload["writes"]) + 2
    store = h.runtime.store
    assert installed_origin(store, h.config) == h.config
    assert store._verified_fresh_initialization == h.config.sha256
    inventory = MigrationStore(h.composition.clients[0], h.config.table.scope).read_inventory()
    initial = validate_fresh_inventory(inventory, h.config)
    assert initial.authorization.approval_source == "operator-approved-file"
    assert len(inventory.receipts) == 1
    values = [row.payload["data"] for row in inventory.rows.values()]
    assert sum(value.get("kind") == "steady_cumulative_cost_ledger" for value in values) == 1
    assert sum(value.get("kind") == "steady_work_registry" for value in values) == 2
    assert not any(value.get("kind") in {
        "worker_readiness", "inbox_deployment_evidence", "native_execution_grant", "steady_work_registration",
    } for value in values)
    assert not any("handoff" in key or "retirement" in key for key in inventory.rows)
    assert validate_agent_registry_history(inventory, h.config)
    assert h.calls == [] and h.source.requests == h.backup.requests == []


@pytest.mark.parametrize("loss", [None, "lost", "incomplete"])
def test_original_command_and_reconcile_only_read_original_receipt_after_success(fresh_case, loss):
    h = fresh_case
    h.table.after_apply = loss
    if loss is None:
        original = initialize(h)
    else:
        with pytest.raises(Problem) as error:
            initialize(h)
        assert error.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
        assert error.value.body["error"]["request_id"] == h.plan.request_id
        original = None
    before = deepcopy(h.table.backing.rows)
    h.table.after_apply = None
    h.time += timedelta(days=1)
    recovered = h.initializer.execute(h.plan, h.authorization, reconcile=True)
    assert original is None or recovered == original
    assert initialize(h) == recovered
    assert h.table.backing.rows == before and writes(h) == 1
    replacement = h.plan.model_copy(update={"request_id": "new-request"})
    approval = h.authorization.model_copy(update={"plan_sha256": replacement.sha256,
        "not_before": h.time, "expires_at": h.time + timedelta(minutes=5)})
    with pytest.raises(Problem) as denied:
        h.initializer.execute(replacement, approval)
    assert denied.value.body["error"]["code"] == "FRESH_TABLE_NOT_EMPTY"
    assert h.table.backing.rows == before


@pytest.mark.parametrize("row_kind", ["installation", "migration", "standing", "random", "other-partition", "guard"])
def test_any_existing_entity_refuses_without_writing(fresh_case, row_kind):
    h = fresh_case
    config = h.config
    partition = config.table.scope.partition_key
    keys = {
        "installation": "behavior:v1:row:" + installation_record(config)[0],
        "migration": "behavior:v1:row:" + migration_key(config.table.scope.authority_domain, "current"),
        "standing": "behavior:v1:row:" + work_policy_key("policy", config.table.scope.authority_domain),
        "guard": GUARD_KEY,
    }
    if row_kind == "other-partition":
        partition = "unrelated-team"
    h.table.backing.apply("Behavior", [("create", {
        "PartitionKey": partition, "RowKey": keys.get(row_kind, "arbitrary-unknown-schema"),
        "Payload": b'{"not_an_FA_record":true}',
    })])
    before = deepcopy(h.table.backing.rows)
    for action in (lambda: h.initializer.plan(h.inputs, request_id="different", installation_id="different"),
                   lambda: initialize(h)):
        with pytest.raises(Problem) as denied:
            action()
        assert denied.value.body["error"]["code"] == "FRESH_TABLE_NOT_EMPTY"
    assert writes(h) == 0 and h.table.backing.rows == before


def test_empty_page_with_continuation_is_not_an_empty_table(fresh_case):
    h = fresh_case
    h.table.empty_first_page = True
    h.table.backing.apply("Behavior", [("create", {
        "PartitionKey": "other", "RowKey": "unknown", "Payload": b"not even JSON",
    })])
    with pytest.raises(Problem) as denied:
        initialize(h)
    assert denied.value.body["error"]["code"] == "FRESH_TABLE_NOT_EMPTY"
    assert writes(h) == 0


@pytest.mark.parametrize("status", [403, 404, 503])
def test_failed_table_read_is_never_empty(fresh_case, status):
    h = fresh_case
    h.table.fail_list = status
    with pytest.raises(Problem) as error:
        initialize(h)
    assert error.value.body["error"]["code"] == "FRESH_TABLE_READ_FAILED"
    assert writes(h) == 0 and not h.table.backing.rows


def test_unknown_without_commit_stays_unknown_and_never_serves(fresh_case):
    h = fresh_case
    h.table.fail_post = True
    with pytest.raises(Problem) as error:
        initialize(h)
    assert error.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    assert writes(h) == 1 and not h.table.backing.rows
    h.table.fail_post = False
    with pytest.raises(Problem) as unknown:
        h.initializer.execute(h.plan, h.authorization, reconcile=True)
    assert unknown.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    with pytest.raises(Problem):
        application(h.composition)
    assert writes(h) == 1 and not h.table.backing.rows


def test_two_clients_racing_commit_one_whole_initialization(fresh_case):
    h = fresh_case
    other = build_server(h.config, **h.options)
    try:
        winner = FreshInitializer(other)
        h.table.before_post = lambda: winner.execute(h.plan, h.authorization)
        with pytest.raises(Problem) as conflict:
            initialize(h)
        assert conflict.value.body["error"]["code"] == "BEHAVIOR_CONFLICT"
        receipt = h.initializer.execute(h.plan, h.authorization, reconcile=True)
        assert receipt.payload["guard_revision"] == 1
        assert len(h.table.backing.rows) == len(receipt.payload["writes"]) + 2
        assert installed_fresh_origin(h.runtime.store, h.config) == h.config
    finally:
        other.close()

def test_late_other_partition_or_expiry_stops_before_the_first_write(fresh_case, monkeypatch):
    h = fresh_case
    original = h.initializer.store._submit

    def raced(operations, request, **kwargs):
        h.table.backing.apply("Behavior", [("create", {
            "PartitionKey": "other", "RowKey": "late-writer", "Payload": b"unknown",
        })])
        return original(operations, request, **kwargs)

    monkeypatch.setattr(h.initializer.store, "_submit", raced)
    with pytest.raises(Problem) as refused:
        initialize(h)
    assert refused.value.body["error"]["code"] == "FRESH_TABLE_NOT_EMPTY"
    assert writes(h) == 0 and len(h.table.backing.rows) == 1


def test_expiry_during_serialization_stops_before_dispatch(fresh_case, monkeypatch):
    h = fresh_case
    original = h.initializer.store._submit

    def expired(operations, request, **kwargs):
        h.time = h.authorization.expires_at
        return original(operations, request, **kwargs)

    monkeypatch.setattr(h.initializer.store, "_submit", expired)
    with pytest.raises(Problem) as refused:
        initialize(h)
    assert refused.value.status == 403
    assert writes(h) == 0 and not h.table.backing.rows


def test_same_request_changed_approval_does_not_rebind_an_installed_result(fresh_case):
    h = fresh_case
    initialize(h)
    before = deepcopy(h.table.backing.rows)
    changed = h.authorization.model_copy(update={"decision_id": "another-approval"})
    with pytest.raises(Problem) as conflict:
        h.initializer.execute(h.plan, changed)
    assert conflict.value.body["error"]["code"] == "BEHAVIOR_REQUEST_CONFLICT"
    assert writes(h) == 1 and h.table.backing.rows == before


@pytest.mark.parametrize("field", ["operator", "config", "plan-digest", "targets", "job-input", "expired", "future"])
def test_wrong_scope_input_or_approval_refuses_without_writes(fresh_case, field):
    h = fresh_case
    plan, approval = h.plan, h.authorization
    if field == "operator":
        approval = approval.model_copy(update={"authorizing_human_oid": h.config.targets[0].agent_oid})
    elif field == "config":
        plan = plan.model_copy(update={"configuration_sha256": "f" * 64})
    elif field == "plan-digest":
        approval = approval.model_copy(update={"plan_sha256": "f" * 64})
    elif field == "targets":
        inputs = h.inputs.model_copy(update={"peer_agent_id": h.config.targets[2].agent_id})
        plan = plan.model_copy(update={"inputs": inputs, "input_sha256": digest(inputs.model_dump(mode="json"))})
    elif field == "job-input":
        plan = plan.model_copy(update={"job_input_sha256": "f" * 64})
    elif field == "expired":
        h.time += timedelta(minutes=6)
    else:
        approval = approval.model_copy(update={"not_before": h.time + timedelta(minutes=1)})
    if field in {"config", "targets", "job-input"}:
        approval = approval.model_copy(update={"plan_sha256": plan.sha256})
    with pytest.raises(Problem) as denied:
        h.initializer.execute(plan, approval)
    assert denied.value.status in {403, 409}
    assert writes(h) == 0 and not h.table.backing.rows


def test_fresh_startup_and_signed_a_b_routes_do_not_require_a_fake_bundle(fresh_case, signing_key, monkeypatch):
    h = fresh_case
    initialize(h)
    import shared_context.behavior_server as server
    from azure.core.exceptions import ResourceNotFoundError
    from test_native_session_prepare_api import project_client
    platform_reads = []
    def missing_platform_session(name, sid):
        platform_reads.append(name)
        raise ResourceNotFoundError("no platform session was prepared")
    project_client(monkeypatch, get_session=missing_platform_session, list_sessions=lambda *_a, **_k: iter(()))
    monkeypatch.setattr(server, "load_configuration", lambda: h.config)
    monkeypatch.setattr(server, "build_server", lambda config: build_server(config, **h.options))
    app = server.create_production_app()
    before = deepcopy(h.table.backing.rows)
    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["operational_readiness"] == "NOT_EVALUATED"
        headers = h.headers(signing_key)
        for prefix, target in zip(("", "/agents/" + h.inputs.peer_agent_id), h.config.targets[:2]):
            account = client.get(prefix + "/work-policy/v1/accounting", headers=headers)
            assert account.status_code == 200, account.text
            assert client.get(prefix + "/work-policy/v1/accounting").status_code == 401
            platform_reads.clear()
            state = client.get(prefix + "/work-policy/v1/native-turns/preparation", headers=headers)
            assert state.status_code == 200, state.text
            assert state.json()["state"] == "needs_follow" and state.json()["preparation_verified"] is False
            assert state.json()["observed_sessions"] == []
            assert set(platform_reads) == {target.agent_name}
        owner = app.state.composition
        assert owner.runtime.work_policy.policy == h.inputs.policy
        peer = owner.for_agent(h.inputs.peer_agent_id)
        assert peer.runtime.work_policy.policy == h.plan.peer_policy
        assert peer.runtime.work_policy.cost_key == owner.runtime.work_policy.cost_key
        assert peer.runtime.work_policy.registry_key != owner.runtime.work_policy.registry_key
        assert any(route.path == "/work-policy/v1/native-turns/prepare" for route in app.routes)
    assert h.table.backing.rows == before and h.calls == []
    assert not os.path.exists(h.config.evidence.path)
    assert h.source.requests == h.backup.requests == []


def test_cosmos_memory_routes_reach_peers_and_share_one_warmed_client(fresh_case, signing_key, monkeypatch, caplog):
    # ADR-0011 phase 3: installed in every app built by application(); one client, warmed and closed by the owner.
    from shared_context import memory_relay
    from test_memory_relay import FakeContainer
    h = fresh_case
    initialize(h)
    import shared_context.behavior_server as server
    from test_native_session_prepare_api import project_client
    project_client(monkeypatch, get_session=lambda *_a: None, list_sessions=lambda *_a, **_k: iter(()))
    monkeypatch.setattr(server, "load_configuration", lambda: h.config)
    monkeypatch.setattr(server, "build_server", lambda config: build_server(config, **h.options))
    monkeypatch.setenv("FA_MEMORY_BACKEND", "cosmos")
    monkeypatch.setenv("FA_COSMOS_ENDPOINT", "https://fa-memory-test.documents.azure.com:443/")
    monkeypatch.setattr(memory_relay, "_SHARED", {})
    fake, built, closed = FakeContainer(), [], []
    monkeypatch.setattr(memory_relay.SharedCosmos, "container",
                        lambda self: built.append(self) or memory_relay.CosmosMemoryContainer(fake))
    monkeypatch.setattr(memory_relay.SharedCosmos, "close", lambda self: closed.append(self))
    app = server.create_production_app()
    with TestClient(app) as client:
        assert fake.calls == [("read",)] and "FA_MEMORY_RELAY_WARMUP status=ready" in caplog.text
        headers = h.headers(signing_key)
        for prefix in ("", "/agents/" + h.inputs.peer_agent_id):
            # A human token reaches the route (not 404) and is refused before any read.
            human = client.post(prefix + memory_relay.PREFIX + "/search", headers=headers,
                                json={"scope": "fap1-" + "0" * 48, "mode": "static", "top": 5})
            assert human.status_code == 403 and human.json()["error"]["code"] == "MEMORY_AGENT_ONLY", human.text
    assert fake.calls == [("read",)]
    assert len(memory_relay._SHARED) == 1 and set(built) == set(closed) == set(memory_relay._SHARED.values())


def test_missing_standing_policy_and_legacy_bootstrap_do_not_bypass_startup(fresh_case):
    h = fresh_case
    initialize(h)
    with pytest.raises(Problem) as old:
        bootstrap(h.composition, h.approval)
    assert old.value.body["error"]["code"] == "MIGRATION_BOOTSTRAP_DISABLED"
    key = work_policy_key("policy", h.config.table.scope.authority_domain)
    h.table.backing.rows.pop(("Behavior", "authority", "behavior:v1:row:" + key))
    with pytest.raises(Problem):
        application(h.composition)


@pytest.mark.parametrize("profile", ["fa-instruction-v1", "fa-instruction-v2"])
def test_fresh_native_peer_requires_its_own_explicit_template_selection(
    fresh_case, signing_key, monkeypatch, profile,
):
    from shared_context.behavior_agent_template import AgentTemplate, with_native_default_checkpoint
    from shared_context.behavior_native_capture import new_conversation_template
    from shared_context.behavior_profile_selection import current_selection
    from shared_context.behavior_work_policy import configured_prompt_override
    import shared_context.behavior_server as server

    h = fresh_case
    initialize(h)
    monkeypatch.setenv("FA_INSTRUCTION_DEFAULT_PROMPT_PROFILE", profile)
    monkeypatch.setattr("shared_context.behavior_sdk_state.local_platform", lambda: "linux-x64")
    monkeypatch.setattr(server, "load_configuration", lambda: h.config)
    monkeypatch.setattr(server, "build_server", lambda config: build_server(config, **h.options))
    before = deepcopy(h.table.backing.rows)
    with TestClient(server.create_production_app()) as client:
        owner = client.app.state.composition
        peer = owner.for_agent(h.inputs.peer_agent_id)
        policy = peer.runtime.work_policy.policy
        assert policy == h.plan.peer_policy and policy.profile.prompt_profile is None
        assert configured_prompt_override(policy) is None
        primary = client.get("/work-policy/v1/agent-template", headers=h.headers(signing_key))
        assert primary.status_code == 200, primary.text
        assert AgentTemplate.model_validate_json(primary.content).role.profile == profile
        denied = client.get(
            "/agents/" + h.inputs.peer_agent_id + "/work-policy/v1/agent-template",
            headers=h.headers(signing_key),
        )
        assert denied.status_code == 409
        assert denied.json()["error"]["code"] == "AGENT_TEMPLATE_PROFILE_UNSUPPORTED"
        with pytest.raises(Problem) as refused:
            new_conversation_template(peer.assistant.authority, policy, None)
        assert refused.value.body["error"]["code"] == "AGENT_TEMPLATE_PROFILE_UNSUPPORTED"
        assert peer.runtime.work_policy.policy == h.plan.peer_policy
        monkeypatch.delenv("FA_NATIVE_TURNS")
        denied = client.get(
            "/agents/" + h.inputs.peer_agent_id + "/work-policy/v1/agent-template",
            headers=h.headers(signing_key),
        )
        assert denied.status_code == 409
        assert denied.json()["error"]["code"] == "AGENT_TEMPLATE_PROFILE_UNSUPPORTED"
        base = "/agents/" + h.inputs.peer_agent_id + "/work-policy/v1/agent-template"
        wanted = with_native_default_checkpoint(AgentTemplate.model_validate_json(primary.content))
        assert wanted.settings.checkpoint_profile.name == "auto-no-custom-v1"
        assert wanted.settings.checkpoint_profile.platform == "linux-x64"
        command = {
            "request_id": "fresh-peer-own-template-selection",
            "expected_selection_id": None,
            "template": wanted.model_dump(mode="json"),
        }
        refused = client.post(
            base + "/select", json=command, headers=h.headers(signing_key, "Behavior.Read"),
        )
        assert refused.status_code == 403
        assert h.table.backing.rows == before
        monkeypatch.setenv("FA_NATIVE_TURNS", "foundry-native-v1")
        selected = client.post(
            base + "/select", json=command,
            headers=h.headers(signing_key, "Behavior.Read Behavior.Approve"),
        )
        assert selected.status_code == 200, selected.text
        assert selected.json()["result"]["permissions_changed"] is False
        assert selected.json()["result"]["existing_conversations_changed"] is False
        current = client.get(base + "/selection", headers=h.headers(signing_key))
        assert current.status_code == 200
        assert current.json()["selection_id"] == selected.json()["result"]["selection_id"]
        exported = client.get(base, headers=h.headers(signing_key))
        assert exported.status_code == 200, exported.text
        template = AgentTemplate.model_validate_json(exported.content)
        assert template == wanted
        choice = current_selection(peer.runtime.work_policy.store, policy)
        assert choice.selected_by == h.config.identity.human_oid
        assert choice.agent_id == h.inputs.peer_agent_id
        assert new_conversation_template(peer.assistant.authority, policy, choice) == template
        assert peer.runtime.work_policy.policy == h.plan.peer_policy
    assert h.calls == []
    assert h.source.requests == h.backup.requests == []


def test_original_b_operator_reads_fresh_registration_without_a_second_ledger(fresh_case, signing_key):
    from shared_context.behavior_agent_registry_operator import operate
    h = fresh_case
    initialize(h)
    original = deepcopy(h.table.backing.rows)
    authorization = h.headers(signing_key, "Behavior.Read Behavior.Approve")["Authorization"]
    result = operate(h.composition, "reconcile", h.inputs.peer_agent_id, authorization,
                     request_id=h.inputs.peer_registration_id)
    assert result["state"] == "registered"
    assert result["control_request_id"] == h.plan.request_id
    assert result["budget_reset"] is False and h.table.backing.rows == original
    assert read_agent_registry(h.runtime.store, h.config, h.inputs.peer_agent_id).initialization_request is not None


def test_later_unknown_usage_is_not_erased_by_reconcile_or_restart(fresh_case):
    from shared_context.behavior_instruction_cost import record_agent_turn_usage
    h = fresh_case
    original = initialize(h)
    h.composition.installation()
    control = h.runtime.work_policy
    record_agent_turn_usage(control, rid="synthetic-unreported-turn", usage=None)
    key = work_policy_key("cost", h.config.table.scope.authority_domain)
    ledger = h.runtime.store.read_record(key)
    assert CostLedger.model_validate_json(json.dumps(ledger.payload["data"])).model_unknown_turns == 1
    before = deepcopy(h.table.backing.rows)
    assert initialize(h) == original
    with build_server(h.config, **h.options) as restarted:
        restarted.installation()
        assert restarted.runtime.store.read_record(key) == ledger
    assert h.table.backing.rows == before


def test_nonfresh_authority_cannot_opt_out_of_legacy_owner_check(fresh_case):
    from shared_context.behavior_assistant import AssistantAuthority
    h = fresh_case
    with pytest.raises(Problem) as refused:
        AssistantAuthority(h.runtime, h.composition.assistant.artifacts, h.composition.assistant.actors,
                           require_legacy_sources=False)
    assert refused.value.body["error"]["code"] == "ASSISTANT_CONFIGURATION"
    with pytest.raises(Problem) as missing:
        h.composition.assistant.start()
    assert missing.value.status == 404
    assert writes(h) == 0


def test_zero_floor_is_exclusive_to_the_empty_table_installer(fresh_case):
    from shared_context.behavior_auth import BehaviorPrincipal
    from shared_context.behavior_work_policy import WorkPolicyControl
    h = fresh_case
    human = BehaviorPrincipal(h.config.identity.tenant_id, h.config.identity.human_oid,
                             h.config.identity.team_id, "human", None, frozenset({"read", "approve"}))
    control = WorkPolicyControl(h.runtime, h.inputs.policy, lambda: None)
    with pytest.raises(Problem) as refused:
        control.initial_records(human, "not-a-fresh-approval", h.time + timedelta(minutes=5))
    assert refused.value.body["error"]["code"] == "WORK_POLICY_FRESH_INSTALLER_REQUIRED"
    assert writes(h) == 0


def test_real_operator_cli_plan_apply_reconcile(fresh_case, tmp_path, monkeypatch, capsys):
    from shared_context import behavior_fresh_operator as cli
    h = fresh_case
    config = tmp_path / "config.json"
    inputs = tmp_path / "inputs.json"
    plan_file = tmp_path / "plan.json"
    approval_file = tmp_path / "authorization.json"
    config.write_text(h.config.model_dump_json(), encoding="utf-8")
    inputs.write_text(h.inputs.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(cli, "build_server", lambda config: build_server(config, **h.options))
    assert cli.main(["plan", "--config", str(config), "--inputs", str(inputs),
                     "--request-id", h.plan.request_id, "--installation-id", h.plan.installation_id,
                     "--output", str(plan_file)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["table_writes"] == writes(h) == 0
    approval = h.authorization.model_copy(update={"plan_sha256": result["plan_sha256"]})
    approval_file.write_text(approval.model_dump_json(), encoding="utf-8")
    args = ["--config", str(config), "--plan", str(plan_file), "--authorization", str(approval_file)]
    outputs = []
    for action in ("apply", "reconcile", "apply"):
        assert cli.main([action, *args]) == 0
        output = capsys.readouterr()
        assert len(output.out.splitlines()) == 1 and not output.err
        outputs.append(json.loads(output.out))
    assert outputs[0] == outputs[1] == outputs[2] and writes(h) == 1
    assert "policy" not in outputs[0] and "authorization" not in outputs[0]
    assert "sources" not in outputs[0] and "identity" not in outputs[0]


def test_fresh_api_only_cold_import():
    script = """
import importlib.abc
import sys
class RejectHosted(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'copilot' or fullname.startswith(('copilot.', 'azure.ai.agentserver')):
            raise AssertionError('Hosted SDK import: ' + fullname)
sys.meta_path.insert(0, RejectHosted())
from shared_context.behavior_fresh_operator import main
from shared_context.behavior_server import create_production_app
main(['schema', 'authorization'])
"""
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", script],
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["title"] == "FreshInstallationAuthorization"


def test_offline_export_uses_existing_console_models_without_claiming_installation(fresh_case, tmp_path, monkeypatch, capsys):
    from web_test_console.fresh_export import main
    from web_test_console.agent_connections import AgentConnections, read_connection
    h = fresh_case
    config_path, plan_path, output = tmp_path / "config.json", tmp_path / "plan.json", tmp_path / "console"
    config_path.write_text(h.config.model_dump_json(), encoding="utf-8")
    plan_path.write_text(h.plan.model_dump_json(), encoding="utf-8")
    before = deepcopy(h.table.backing.rows)
    args = ["--config", str(config_path), "--plan", str(plan_path),
            "--backend-origin", "https://synthetic.example", "--client-id", h.config.identity.admin_client_id,
            "--output-directory", str(output)]
    assert main(args) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary == {"state": "connections-exported", "agents": 2,
                       "installation_verified": False, "table_writes": 0}
    manifest = AgentConnections.model_validate_json((output / "agents.json").read_bytes())
    assert manifest.default_agent_id == h.config.targets[0].agent_id
    for entry, policy in zip(manifest.agents, (h.inputs.policy, h.plan.peer_policy)):
        connection, instruction = read_connection(entry)
        assert connection.standing_policy == policy
        assert instruction.state_profile == "foundry-native-v1"
        assert instruction.configuration_sha256 == connection.sha256
        assert connection.backend_core_sha256 == h.plan.code_sha256
        assert connection.cli is None
    assert main(args) == 0
    capsys.readouterr()
    first = output / manifest.default_agent_id / "config.json"
    first.write_text("unrelated local data", encoding="utf-8")
    assert main(args) == 1
    assert first.read_text(encoding="utf-8") == "unrelated local data"
    assert h.table.backing.rows == before and writes(h) == 0
