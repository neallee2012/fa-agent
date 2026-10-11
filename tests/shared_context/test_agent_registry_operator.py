"""One-off peer setup uses the original signed operator and registry contract."""

import base64
import json
from urllib.parse import urlsplit

from azure.core.exceptions import ServiceRequestError
import pytest

from shared_context.errors import Problem
import test_conversation_transfer_startup as startup
from test_behavior_store import WireResponse
from test_conversation_transfer_startup import read_startup, synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key, token


class OperatorTableTransport(startup.StartupTableTransport):
    lose_create_response = False

    def send(self, request, **kwargs):
        if request.method == "POST" and urlsplit(request.url).path == "/Behavior":
            self.requests.append(request)
            entity = json.loads(request.body)
            assert entity.pop("Payload@odata.type") == "Edm.Binary"
            entity["Payload"] = base64.b64decode(entity["Payload"])
            result = self.backing.apply("Behavior", [("create", entity)])
            if self.lose_create_response:
                raise ServiceRequestError("Synthetic original B source-work response loss")
            return WireResponse(request, 204, b"", {
                "Content-Type": "application/json", "etag": result[0]["etag"],
            })
        return super().send(request, **kwargs)


@pytest.fixture
def operator_case(request, monkeypatch):
    monkeypatch.setattr(startup, "StartupTableTransport", OperatorTableTransport)
    return request.getfixturevalue("synthetic_standing_v1")


def test_operator_registers_b_with_the_original_approver_and_preserves_a_ledger(
        operator_case, signing_key):
    from shared_context.behavior_agent_registry_operator import operate

    case = operator_case
    composition = case.h.composition
    agent_id = case.h.config.targets[1].agent_id
    authorization = case.h.headers(signing_key, "Behavior.Read Behavior.Approve")["Authorization"]
    original = case.control.accounting(case.human)
    missing = operate(composition, "show", agent_id, authorization)
    assert missing["state"] == "missing"

    registered = operate(composition, "register", agent_id, authorization,
                         request_id="original-b-operator")
    assert registered["state"] == "registered"
    assert registered["agent_id"] == agent_id
    assert registered["registry_id"] == "original-b-operator"
    assert registered["budget_reset"] is False
    assert registered["runtime_activated"] is False
    assert operate(composition, "show", agent_id, authorization) == registered
    assert operate(composition, "reconcile", agent_id, authorization,
                   request_id="original-b-operator") == registered
    assert operate(composition, "register", agent_id, authorization,
                   request_id="original-b-operator") == registered
    with pytest.raises(Problem) as changed:
        operate(composition, "register", agent_id, authorization, request_id="replacement-registration")
    assert changed.value.body["error"]["code"] == "AGENT_SCOPE_CONFLICT"
    assert operate(composition, "show", agent_id, authorization) == registered

    peer = composition.for_agent(agent_id)
    assert peer.runtime.work_policy.accounting(case.human)["version"]["ledger_etag"] == (
        original["version"]["ledger_etag"])
    after = case.control.accounting(case.human)
    assert {key: value for key, value in after.items() if key != "version"} == {
        key: value for key, value in original.items() if key != "version"}
    for field in ("ledger_etag", "ledger_data_sha256", "policy_sha256"):
        assert after["version"][field] == original["version"][field]
    assert peer.runtime.work_policy.policy.template.agent_oid == case.h.config.targets[1].agent_oid
    assert peer.runtime.work_policy.policy.profile.memory_scope is None
    assert case.sessions.calls == []
    read_startup(case)


def test_operator_requires_original_human_approval_before_any_storage_write(operator_case, signing_key):
    from shared_context.behavior_agent_registry_operator import operate

    case = operator_case
    agent_id = case.h.config.targets[1].agent_id
    readonly = case.h.headers(signing_key, "Behavior.Read")["Authorization"]
    agent = "Bearer " + token(signing_key, {
        "oid": case.h.config.targets[1].agent_oid, "idtyp": "app", "roles": ["Behavior.Agent"],
    }, remove=("scp",))
    before = sum(request.method == "POST" for request in case.h.table.requests)
    for authorization, status in (("", 401), (readonly, 403), (agent, 403)):
        with pytest.raises(Problem) as denied:
            operate(case.h.composition, "register", agent_id, authorization, request_id="not-authorized")
        assert denied.value.status == status
    assert operate(case.h.composition, "show", agent_id, readonly)["state"] == "missing"
    with pytest.raises(Problem) as unknown:
        operate(case.h.composition, "reconcile", agent_id, readonly, request_id="not-authorized")
    assert unknown.value.body["error"]["code"] == "AGENT_SCOPE_UNKNOWN"
    assert sum(request.method == "POST" for request in case.h.table.requests) == before


@pytest.mark.parametrize("lost_response", ["source-work", "registry"])
def test_operator_retains_the_original_id_and_recovers_only_its_saved_rows(
        operator_case, signing_key, lost_response):
    from shared_context.behavior_agent_registry_operator import operate

    case = operator_case
    composition = case.h.composition
    agent_id = case.h.config.targets[1].agent_id
    authorization = case.h.headers(signing_key, "Behavior.Read Behavior.Approve")["Authorization"]
    before = sum(request.method == "POST" for request in case.h.table.requests)
    if lost_response == "source-work":
        case.h.table.lose_create_response = True
    else:
        case.h.table.after_apply = "lost"
    with pytest.raises(Problem) as unknown:
        operate(composition, "register", agent_id, authorization, request_id="original-response-loss")
    assert unknown.value.status >= 500
    written = sum(request.method == "POST" for request in case.h.table.requests)
    assert written - before == (1 if lost_response == "source-work" else 2)
    case.h.table.lose_create_response = False
    case.h.table.after_apply = None

    observed = operate(composition, "show", agent_id, authorization)
    if lost_response == "source-work":
        assert observed["state"] == "missing"
        with pytest.raises(Problem) as absent:
            operate(composition, "reconcile", agent_id, authorization, request_id="original-response-loss")
        assert absent.value.body["error"]["code"] == "AGENT_SCOPE_UNKNOWN"
        registered = operate(composition, "register", agent_id, authorization,
                             request_id="original-response-loss")
        assert sum(request.method == "POST" for request in case.h.table.requests) == written + 1
    else:
        assert observed["state"] == "registered"
        registered = operate(composition, "reconcile", agent_id, authorization,
                             request_id="original-response-loss")
        assert registered == observed
        assert sum(request.method == "POST" for request in case.h.table.requests) == written
    assert registered["registry_id"] == "original-response-loss"
    assert operate(composition, "show", agent_id, authorization) == registered
    assert case.sessions.calls == []
    read_startup(case)


def test_fresh_operator_verifies_installation_before_preparing_any_b_work(operator_case, signing_key):
    from shared_context.behavior_agent_registry_operator import operate
    from shared_context.behavior_server import build_server

    case = operator_case
    agent_id = case.h.config.targets[1].agent_id
    authorization = case.h.headers(signing_key, "Behavior.Read Behavior.Approve")["Authorization"]
    before = sum(request.method == "POST" for request in case.h.table.requests)
    with build_server(case.h.config, **case.h.options) as fresh:
        assert fresh.runtime.work_policy is None
        # This fixture deliberately lacks the complete Window2/WebHandoff
        # installation lineage. A fresh operator must reject it before writing.
        with pytest.raises(Problem) as invalid:
            operate(fresh, "register", agent_id, authorization, request_id="fresh-unverified-b")
        assert "standing policy is not exact original approved history" in str(invalid.value)
        assert sum(request.method == "POST" for request in case.h.table.requests) == before
        assert operate(fresh, "show", agent_id, authorization)["state"] == "missing"
        with pytest.raises(Problem) as absent:
            operate(fresh, "reconcile", agent_id, authorization, request_id="fresh-unverified-b")
        assert absent.value.body["error"]["code"] == "AGENT_SCOPE_UNKNOWN"
        assert sum(request.method == "POST" for request in case.h.table.requests) == before


def test_original_migration_reader_enables_the_same_fenced_writer_without_changing_its_epoch():
    from shared_context.behavior_migration import MigrationStore, installation_record, installed_origin
    from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
    from test_behavior_migration_release_fences import migrated

    _, original, table, _, config, _, _, _ = migrated()
    writer = MigrationStore(table, original._scope,
                            expected_installation=(installation_record(config)[0], config.sha256))
    request = ControlRequest(request_id="operator-after-original-installation", request_digest="e" * 64)

    def prepare(_):
        return ControlChanges(writes=(ControlWrite(
            row_id="operator-new-row", mode="create", data={"original_installation_checked": True},
        ),), result={"saved": True})

    with pytest.raises(Problem) as unverified:
        writer.transact(request, prepare)
    assert unverified.value.body["error"]["code"] == "MIGRATION_HISTORY_UNVERIFIED"
    installed_origin(writer, config)
    assert writer.transact(request, prepare).payload["result"] == {"saved": True}
    assert writer.reconcile(request).payload["result"] == {"saved": True}
