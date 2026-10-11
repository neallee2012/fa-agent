"""Authenticated accounting projection; local storage/JWT fakes, no SDK or Azure."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context
import copy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from conftest import A, C
from shared_context.behavior_api import create_behavior_app
from shared_context.behavior_assistant import AssistantProfile, AssistantRuntime
from shared_context.behavior_assistant_wire import CHECKS
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite, GUARD_KEY
from shared_context.behavior_work_policy import CostObservation, SteadyWorkPolicy, WorkPolicyControl
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_behavior_api import RuntimeHarness
from test_m3_auth import UNKNOWN_OID, jwk_client, signing_key, token

PATH = "/work-policy/v1/accounting"


@pytest.fixture
def accounting(auth, jwk_client, signing_key):
    h = RuntimeHarness(auth, jwk_client, signing_key, work_checks=CHECKS)
    profile = AssistantProfile(
        binding_id=h.binding.binding_id, source=h.source(), claim_id="claim-one",
        legacy_source_work="legacy-source", artifact_id="accounting-fixture",
        artifact_version="a" * 32, policy_version="assistant-own-result-v1",
    )
    h.runtime = AssistantRuntime(
        h.dep, (h.binding,), profiles=(profile,), clock=lambda: h.time,
        mode="offline_contract", controller_id="internal-controller",
    )
    policy = SteadyWorkPolicy(
        policy_id="synthetic-standing-policy", revision=1, configuration_sha256="a" * 64,
        evidence_mode=h.runtime.mode, authority_domain=h.store._scope.authority_domain,
        control_binding=h.store._binding, active=True, read_enabled=True,
        template=h.binding, profile=profile, legacy_template_policy=h.policy,
        cost_scope_sha256="c" * 64, cost_observer_oid="synthetic-cost-observer",
        minimum_observed_microusd=20_000_000, work_estimate_microusd=2_000_000,
        cost_max_age_seconds=300, queue_seconds=min(600, h.config.max_queue_seconds), max_model_requests=6,
    )
    observation = CostObservation(
        evidence_id="synthetic-observation", evidence_mode=h.runtime.mode,
        observer_oid=policy.cost_observer_oid, authority_domain=policy.authority_domain,
        control_binding=policy.control_binding, cost_scope_sha256=policy.cost_scope_sha256,
        observed_cumulative_microusd=20_000_000, legacy_unresolved_reserve_microusd=1_000_000,
        legacy_liability_sha256="d" * 64, evidence_sha256="e" * 64, observed_at=h.time,
    )
    h.human = BehaviorPrincipal(
        h.binding.tenant_id, h.binding.human_oid, h.binding.team_id, "human", None,
        frozenset({"read", "approve"}),
    )
    h.control = WorkPolicyControl(h.runtime, policy, lambda: observation)
    h.control.install_offline(h.human, "synthetic-install", h.time + timedelta(minutes=10))
    h.mutate(
        h.control.cost_key, work_reservations_microusd=2_000_000, reservation_count=1,
        model_pending_microusd=750_000, model_metered_microusd=125_000, budget_enforced=False,
    )
    h.app = create_behavior_app(h.runtime, h.verifier)
    h.starts = []
    install_work_policy_routes(
        h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier), start_work=h.starts.append,
    )
    with TestClient(h.app) as client:
        h.client = client
        yield h


def test_current_human_reads_original_accounting_components_without_spending_permission(accounting):
    h = accounting
    response = h.client.get(PATH, headers=h.headers("read-only"))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["schema_version"] == "fa-work-accounting-read-v1"
    assert result["unit"] == "USD-micro"
    assert result["components"] == {
        "observed_high_water_microusd": 20_000_000,
        "legacy_unresolved_reserve_microusd": 1_000_000,
        "work_reservations_microusd": 2_000_000,
        "model_pending_microusd": 750_000,
        "model_metered_microusd": 125_000,
    }
    assert result["reservation_count"] == 1 and result["budget_enforced"] is False
    assert result["all_fields_present"] and all(result["field_presence"].values())
    assert result["authorization"] is False and result["provider_hard_cap"] is False
    assert result["headroom_resolved"] is False
    assert "remaining_microusd" not in result and "cumulative_ceiling_microusd" not in result
    assert response.headers["cache-control"] == "no-store"
    assert set(result) == {
        "schema_version", "read_at", "unit", "components", "reservation_count", "budget_enforced",
        "field_presence", "all_fields_present", "source", "version",
        "authorization", "provider_hard_cap", "headroom_resolved",
    }


@pytest.mark.parametrize("claims,remove,status", [
    ({"oid": A.oid, "idtyp": "app", "roles": ["Behavior.Agent"]}, ("scp",), 403),
    ({"oid": C.oid, "idtyp": "app", "roles": ["Behavior.Agent"]}, ("scp",), 403),
    ({"oid": UNKNOWN_OID}, (), 403),
    ({"tid": UNKNOWN_OID}, (), 401),
    ({"aud": "api://wrong-audience"}, (), 401),
    ({"appid": UNKNOWN_OID}, (), 403),
    ({"exp": 1}, (), 401),
    ({"scp": "Artifacts.Approve"}, (), 403),
    ({"scp": "Behavior.Approve"}, (), 403),
    ({}, ("scp",), 403),
])
def test_accounting_http_rejects_noncurrent_identity_or_read_consent(accounting, claims, remove, status):
    h = accounting
    encoded = token(h.signing, {"scp": "Behavior.Read", **claims}, remove=remove)
    h.transport.requests.clear()
    response = h.client.get(PATH, headers={"Authorization": "Bearer " + encoded})
    assert response.status_code == status, response.text
    assert h.transport.requests == []


@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer malformed"},
    [("Authorization", "Bearer first"), ("Authorization", "Bearer second")],
])
def test_authentication_is_required_before_body_or_storage(accounting, headers):
    h = accounting
    h.transport.requests.clear()
    response = h.client.request("GET", PATH, headers=headers, content=b"not-accounting-input")
    assert response.status_code == 401
    assert h.transport.requests == []


@pytest.mark.parametrize("changes", [
    {"tenant_id": UNKNOWN_OID}, {"oid": UNKNOWN_OID}, {"team": "team-two"},
    {"kind": "agent"}, {"actor_id": "agent-a"}, {"actions": frozenset({"approve"})},
])
def test_control_also_denies_wrong_human_team_tenant_actor_and_read_action(accounting, changes):
    h = accounting
    h.transport.requests.clear()
    with pytest.raises(Problem) as denied:
        h.control.accounting(replace(h.human, **changes))
    assert denied.value.status == 403
    assert h.transport.requests == []


def test_a_previous_read_is_not_authority_after_explicit_revocation(accounting):
    h = accounting
    assert h.client.get(PATH, headers=h.headers()).status_code == 200
    h.control.revoke(h.human, "synthetic-revoke")
    h.transport.requests.clear()
    response = h.client.get(PATH, headers=h.headers())
    assert response.status_code == 403 and response.json()["error"]["code"] == "BEHAVIOR_ACCESS_DENIED"
    assert all(request.method == "GET" for request in h.transport.requests)


@pytest.mark.parametrize("change", [
    {"active": False}, {"read_enabled": False}, {"revision": 2},
    {"configuration_sha256": "f" * 64}, {"cost_scope_sha256": "f" * 64},
    {"control_binding": "f" * 64}, {"authority_domain": "different-authority"},
])
def test_current_policy_changes_do_not_inherit_cached_read_permission(accounting, change):
    h = accounting
    assert h.client.get(PATH, headers=h.headers()).status_code == 200
    current = h.control.policy.model_dump(mode="json")
    h.mutate(h.control.policy_key, policy={**current, **change})
    response = h.client.get(PATH, headers=h.headers())
    assert response.status_code == 403, response.text


def replace_ledger(h, data):
    request = ControlRequest(
        request_id="synthetic-ledger-shape", request_digest=digest(data), read_set=(h.control.cost_key,),
    )
    h.store.transact(request, lambda _: ControlChanges(
        writes=(ControlWrite(row_id=h.control.cost_key, mode="replace", data=data),),
        result={"synthetic": True},
    ))


@pytest.mark.parametrize("field", [
    "work_reservations_microusd", "model_pending_microusd", "model_metered_microusd",
    "reservation_count", "budget_enforced", "unit",
])
def test_missing_legacy_fields_are_unknown_not_dto_defaults(accounting, field):
    h = accounting
    raw = h.store.read_snapshot((h.control.cost_key,))[h.control.cost_key].payload["data"]
    del raw[field]
    replace_ledger(h, raw)
    response = h.client.get(PATH, headers=h.headers())
    assert response.status_code == 200, response.text
    result = response.json()
    values = {**result["components"], **result}
    assert values[field] is None and result["field_presence"][field] is False
    assert result["all_fields_present"] is False
    assert result["version"]["ledger_data_sha256"] == digest(raw)


def test_explicit_zero_and_true_fields_stay_distinct_from_absence(accounting):
    h = accounting
    h.mutate(h.control.cost_key, work_reservations_microusd=0, model_pending_microusd=0,
             model_metered_microusd=0, reservation_count=0, budget_enforced=True)
    result = h.client.get(PATH, headers=h.headers()).json()
    for field in ("work_reservations_microusd", "model_pending_microusd", "model_metered_microusd"):
        assert result["components"][field] == 0 and result["field_presence"][field] is True
    assert result["reservation_count"] == 0 and result["budget_enforced"] is True
    assert result["all_fields_present"] is True


def test_source_age_and_versions_describe_the_retained_row_not_a_new_cost_observation(accounting):
    h = accounting
    keys = (h.control.cost_key, h.control.policy_key)
    rows = h.store.read_snapshot(keys)
    guard, guard_etag = h.store._guard()
    original_time = h.time
    h.time += timedelta(days=2)
    response = h.client.get(PATH, headers=h.headers())
    assert response.status_code == 200, response.text
    result = response.json()
    assert datetime.fromisoformat(result["read_at"]) == h.time
    assert datetime.fromisoformat(result["source"]["latest_observed_at"]) == original_time
    assert result["source"]["age_seconds"] == 172800
    assert result["source"]["latest_observation_sha256"] == rows[h.control.cost_key].payload["data"]["latest_observation_sha256"]
    assert result["version"] == {
        "ledger_etag": rows[h.control.cost_key].etag,
        "ledger_data_sha256": digest(rows[h.control.cost_key].payload["data"]),
        "policy_etag": rows[h.control.policy_key].etag,
        "policy_sha256": h.control.policy.sha256, "policy_revision": 1,
        "guard_revision": guard.revision, "guard_etag": guard_etag,
    }
    assert h.time > h.control.policy.legacy_template_policy.valid_until


def test_read_only_snapshot_keeps_unknown_model_cost_and_every_original_record(accounting, monkeypatch):
    from shared_context.behavior_instruction_cost import ModelCost, cost_key
    h = accounting
    pending = ModelCost(
        session_id="synthetic-unknown-session", job_id="synthetic-unknown-job", request_sha256="f" * 64,
        model="gpt-4.1-mini", input_microusd_per_million=1_000_000, output_microusd_per_million=1_000_000,
        request_token_estimate=10, input_token_upper_bound=10, output_token_upper_bound=10,
        pending_microusd=750_000,
    )
    h.seed(((cost_key(pending.session_id, pending.request_sha256), pending),))
    before = copy.deepcopy(h.transport.backing.rows)
    h.transport.requests.clear()
    def forbidden(*args, **kwargs):
        pytest.fail("An accounting read must not observe, refresh, admit, dispatch or write.")
    for target, name in (
        (h.control, "cost_reader"), (h.control, "observe_cost"), (h.control, "refresh"),
        (h.control, "register"), (h.runtime, "submit"), (h.store, "transact"),
    ):
        monkeypatch.setattr(target, name, forbidden)
    h.workers.adapter = replace(h.workers.adapter, start=forbidden, query=forbidden, stop=forbidden)
    response = h.client.get(PATH, headers=h.headers())
    assert response.status_code == 200, response.text
    assert h.transport.backing.rows == before
    assert h.starts == [] and h.executor.starts == [] and h.control.records == {}
    assert all(request.method == "GET" for request in h.transport.requests)
    keys = Counter(unquote(request.url).split("RowKey='")[1].split("'")[0] for request in h.transport.requests)
    assert keys == {
        GUARD_KEY: 2, "behavior:v1:row:" + h.control.policy_key: 1,
        "behavior:v1:row:" + h.control.revocation_key: 1, "behavior:v1:row:" + h.control.cost_key: 1,
    }


@pytest.mark.parametrize("race", ["ledger-update", "revocation", "continuous"])
def test_consistent_read_never_returns_a_mixed_or_revoked_snapshot(accounting, monkeypatch, race):
    h = accounting
    send = h.transport.send
    injected = []
    writing = False
    def change():
        nonlocal writing
        writing = True
        try:
            if race == "revocation":
                h.control.revoke(h.human, "synthetic-racing-revocation")
            else:
                h.mutate(h.control.cost_key, model_pending_microusd=800_000 + len(injected))
        finally:
            writing = False
    with ThreadPoolExecutor(max_workers=1) as writer:
        def racing(request, **kwargs):
            result = send(request, **kwargs)
            if (not writing and request.method == "GET"
                    and "RowKey='behavior:v1:row:" + h.control.cost_key + "'" in unquote(request.url)
                    and (race == "continuous" or not injected)):
                injected.append(True)
                writer.submit(Context().run, change).result(timeout=10)
            return result
        monkeypatch.setattr(h.transport, "send", racing)
        response = h.client.get(PATH, headers=h.headers())
    if race == "ledger-update":
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["components"]["model_pending_microusd"] == 800_001
        monkeypatch.setattr(h.transport, "send", send)
        current = h.store.read_snapshot((h.control.cost_key,))[h.control.cost_key]
        assert result["version"]["ledger_etag"] == current.etag
        assert result["version"]["ledger_data_sha256"] == digest(current.payload["data"])
    else:
        assert response.status_code == (403 if race == "revocation" else 412), response.text
        assert "components" not in response.json()
        assert response.json()["error"]["automatic_retry"] is False
    # Each continuous attempt also changes the ledger during its row re-check.
    assert len(injected) == (6 if race == "continuous" else 1)


@pytest.mark.parametrize("row", ["ledger", "policy", "guard"])
def test_missing_accounting_authority_is_an_error_not_an_empty_balance(accounting, row):
    h = accounting
    key = {"ledger": "behavior:v1:row:" + h.control.cost_key,
           "policy": "behavior:v1:row:" + h.control.policy_key, "guard": GUARD_KEY}[row]
    del h.transport.backing.rows[(h.store._scope.table_name, h.store._scope.partition_key, key)]
    response = h.client.get(PATH, headers=h.headers())
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "BEHAVIOR_CONFLICT"
    assert "components" not in response.json()


@pytest.mark.parametrize("field,value,status", [
    ("authority_domain", "wrong-authority", 409),
    ("cost_scope_sha256", "f" * 64, 409),
    ("unit", "USD", 502),
    ("model_pending_microusd", -1, 502),
    ("model_pending_microusd", True, 502),
    ("model_pending_microusd", "750000", 502),
    ("model_pending_microusd", None, 502),
    ("model_body", "SYNTHETIC_PRIVATE_BODY", 502),
])
def test_incoherent_or_invalid_ledger_is_not_projected(accounting, field, value, status):
    h = accounting
    h.mutate(h.control.cost_key, **{field: value})
    response = h.client.get(PATH, headers=h.headers())
    assert response.status_code == status, response.text
    assert "components" not in response.json() and "SYNTHETIC_PRIVATE_BODY" not in response.text
    assert response.json()["error"]["automatic_retry"] is False


def test_required_accounting_values_are_not_replaced_by_success_shaped_unknowns(accounting):
    h = accounting
    raw = h.store.read_snapshot((h.control.cost_key,))[h.control.cost_key].payload["data"]
    del raw["observed_high_water_microusd"]
    replace_ledger(h, raw)
    response = h.client.get(PATH, headers=h.headers())
    assert response.status_code == 502 and "components" not in response.json()


def test_future_source_timestamp_is_not_reported_as_current(accounting):
    h = accounting
    h.mutate(h.control.cost_key, latest_observed_at=(h.time + timedelta(seconds=1)).isoformat())
    response = h.client.get(PATH, headers=h.headers())
    assert response.status_code == 409 and "components" not in response.json()


@pytest.mark.parametrize("unexpected", [False, True])
def test_unreadable_storage_errors_are_sanitized_and_do_not_reset_anything(accounting, monkeypatch, unexpected):
    from azure.core.exceptions import ServiceRequestError
    h = accounting
    before = copy.deepcopy(h.transport.backing.rows)
    def fail(request, **kwargs):
        assert request.method == "GET"
        error = RuntimeError if unexpected else ServiceRequestError
        raise error("SYNTHETIC_STORAGE_SECRET credential body and private row")
    monkeypatch.setattr(h.transport, "send", fail)
    with TestClient(h.app, raise_server_exceptions=False) as client:
        response = client.get(PATH, headers=h.headers())
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "BEHAVIOR_DEPENDENCY_UNAVAILABLE"
    assert "SYNTHETIC_STORAGE_SECRET" not in response.text and "private row" not in response.text
    assert h.transport.backing.rows == before and h.starts == []


@pytest.mark.parametrize("query,body", [
    ("?table=untrusted", b""), ("?row_id=untrusted", b""), ("?tenant_id=untrusted", b""),
    ("?team_id=untrusted", b""), ("?human_oid=untrusted", b""), ("?budget_enforced=false", b""),
    ("", b'{"table":"SYNTHETIC_PRIVATE_INPUT","budget_enforced":false}'),
])
def test_clients_cannot_select_accounting_scope_or_flags(accounting, query, body):
    h = accounting
    h.transport.requests.clear()
    response = h.client.request("GET", PATH + query, headers=h.headers(), content=body)
    assert response.status_code == 422
    assert "SYNTHETIC_PRIVATE_INPUT" not in response.text and h.transport.requests == []


def test_absent_standing_control_does_not_trigger_installation_or_fallback(accounting):
    h = accounting
    h.runtime.work_policy = None
    h.transport.requests.clear()
    response = h.client.get(PATH, headers=h.headers())
    assert response.status_code == 503 and h.transport.requests == []


def test_accounting_route_and_production_factory_import_without_hosted_sdk():
    source = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"
    program = r"""
import importlib.abc, sys
class NoHostedSdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "copilot" or fullname.startswith("copilot."):
            raise ModuleNotFoundError("Hosted SDK forbidden in API", name=fullname)
sys.meta_path.insert(0, NoHostedSdk())
sys.path.insert(0, sys.argv[1])
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.behavior_server import create_production_app
assert callable(install_work_policy_routes) and callable(create_production_app)
assert not any(name == "copilot" or name.startswith("copilot.") for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-c", program, str(source)],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
