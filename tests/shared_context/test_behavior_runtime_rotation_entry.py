"""Canonical operator integration: offline schemas, dispatch and API-MI binding.

No listener, Azure authentication, provider/model call or deployment is started.
The inherited rotation fixture remains explicitly synthetic, not native proof.
"""
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import behavior_server_main as entry
from shared_context import behavior_runtime_rotation as rotation
from shared_context import behavior_runtime_rotation_api as api
from shared_context.behavior_migration import installed_origin
from shared_context.behavior_store import ControlChanges, ControlRequest
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_behavior_runtime_rotation import approved, rotated


def operator_args():
    return [
        "--old-config", "old.json", "--new-config", "new.json", "--sources", "sources.json",
        "--backups", "backups.json", "--retirement", "retirement.json", "--native-grants", "grants.json",
    ]


@pytest.mark.parametrize("kind", api.SCHEMA_KINDS)
def test_canonical_schema_exports_do_not_read_files_open_clients_or_serve(kind, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["behavior_server_main.py", "schema", kind])
    forbidden = Mock(side_effect=AssertionError("schema export must not access runtime resources"))
    monkeypatch.setattr(api, "operator_application", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr("shared_context.behavior_server.build_server", forbidden)
    entry.main()
    value = json.loads(capsys.readouterr().out)
    assert value == api.input_schema(kind)
    assert not forbidden.called
    if kind == "rotation-native-grants":
        assert value["type"] == "array" and value["minItems"] == value["maxItems"] == 1
    else:
        assert value["type"] == "object" and value["additionalProperties"] is False


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("canonical", [False, True])
def test_canonical_and_module_entries_share_exact_isolated_factory_args(enabled, canonical, monkeypatch):
    args = operator_args() + ["--host", "127.0.0.1", "--port", "8091"]
    if enabled:
        args.append("--allow-apply")
    argv = ["behavior_server_main.py", "runtime-rotation", *args] if canonical else ["rotation-module", *args]
    monkeypatch.setattr("sys.argv", argv)
    application = object()
    factory = Mock(return_value=application)
    listener = Mock()
    monkeypatch.setattr(api, "operator_application", factory)
    monkeypatch.setattr("uvicorn.run", listener)
    monkeypatch.setattr("shared_context.behavior_server.build_server",
                        Mock(side_effect=AssertionError("rotation must not build the business runtime")))
    (entry.main if canonical else api.main)()
    factory.assert_called_once_with(
        Path("old.json"), Path("new.json"), Path("sources.json"), Path("backups.json"),
        Path("retirement.json"), Path("grants.json"), allow_apply=enabled,
    )
    listener.assert_called_once_with(application, host="127.0.0.1", port=8091, access_log=False)


@pytest.mark.parametrize("extra", [["--config", "ambiguous.json"], ["--evidence", "ambiguous.json"]])
def test_canonical_entry_rejects_general_runtime_overrides(extra, monkeypatch):
    monkeypatch.setattr("sys.argv", ["behavior_server_main.py", "runtime-rotation", *operator_args(), *extra])
    forbidden = Mock(side_effect=AssertionError("invalid scope must not construct an operator"))
    monkeypatch.setattr(api, "operator_application", forbidden)
    with pytest.raises(SystemExit, match="BEHAVIOR_SERVER_CONFIGURATION_OR_DEPENDENCY_UNAVAILABLE"):
        entry.main()
    assert not forbidden.called


@pytest.mark.parametrize("field", ["credential_client_id", "credential_principal_oid", "credential_tenant_id"])
def test_identity_observation_cannot_rebind_the_original_api_mi(rotated, field):
    h = rotated
    value = json.loads(h.paths.retirement.read_bytes())
    value["current_targets"][0][field] = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    h.paths.retirement.write_text(json.dumps(value), encoding="utf-8")
    before = h.store.read_inventory().entity_sha256
    with pytest.raises(Problem):
        h.service.plan(h.principal, rotation_id="wrong-api-identity")
    assert h.store.read_inventory().entity_sha256 == before


@pytest.mark.parametrize("owner", ["hosted", "human"])
def test_hosted_or_human_owner_cannot_be_relabelled_as_api_session_owner(rotated, owner):
    h = rotated
    value = json.loads(h.paths.retirement.read_bytes())
    oid = h.new.targets[0].agent_oid if owner == "hosted" else h.new.identity.human_oid
    value["current_targets"][0].update(session_owner_oid=oid, credential_principal_oid=oid)
    h.paths.retirement.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(Problem, match="original API MI"):
        h.service.plan(h.principal, rotation_id="wrong-owner")


def test_api_owned_candidate_session_and_receipt_only_history_preserve_unknown_model(rotated):
    h = rotated
    original_rows = h.store.read_inventory().rows
    for action in ("create", "observe"):
        body = {
            "kind": "offline_candidate_session_" + action,
            "target": h.new.targets[0].model_dump(mode="json"),
            "credential_client_id": h.old.identity.managed_identity_client_id,
            "old_configuration_sha256": h.old.sha256, "model_posts": 0, "business_job": False,
        }
        h.store.transact(ControlRequest(request_id="candidate-session-" + action, request_digest=digest(body)),
                         lambda _, body=body: ControlChanges(result=body))
    assert h.store.read_inventory().rows == original_rows
    observed = json.loads(h.paths.retirement.read_bytes())["current_targets"][0]
    assert observed["session_owner_oid"] == observed["credential_principal_oid"]
    assert observed["session_owner_oid"] != h.new.targets[0].agent_oid
    before = h.store.read_inventory()
    plan, authorization = approved(h)
    h.service.apply(h.principal, plan, authorization)
    verified = h.service.verify(h.principal, plan, authorization)
    after = h.store.read_inventory()
    assert verified["liability"]["model_outcome"] == "unknown"
    assert verified["liability"]["model_request_sha256"] == h.h.service.scope.model_request_sha256
    assert all(after.receipts[key] == record for key, record in before.receipts.items())
    assert installed_origin(h.store, h.new) == h.original


def test_both_provider_image_and_api_identity_observations_are_approval_pinned(rotated):
    h = rotated
    plan, auth = approved(h)
    original = h.paths.retirement.read_bytes()
    count = len(h.table.submissions)
    for field in ("agent_image_sha256", "identity_observation_sha256", "provider_observation_sha256"):
        value = json.loads(original)
        value["current_targets"][0][field] = "f" * 64
        h.paths.retirement.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(Problem):
            h.service.apply(h.principal, plan, auth)
    assert len(h.table.submissions) == count


def test_retired_configuration_cannot_overwrite_the_current_rotation(rotated):
    h = rotated
    plan, auth = approved(h)
    h.service.apply(h.principal, plan, auth)
    before = h.store.read_inventory()
    with pytest.raises(Problem) as denied:
        h.service.plan(h.principal, rotation_id="nested-sdk")
    assert denied.value.body["error"]["code"] == "BEHAVIOR_CONFIGURATION"
    assert h.store.read_inventory().entity_sha256 == before.entity_sha256


def test_rotation_fingerprint_includes_canonical_entry_without_changing_legacy_hash():
    legacy = rotation.m.code_sha256()
    before = rotation.rotation_code_sha256()
    read_bytes = Path.read_bytes
    def altered(path):
        data = read_bytes(path)
        return data + b"\n# offline changed canonical entry\n" if path.name == "behavior_server_main.py" else data
    with patch.object(Path, "read_bytes", altered):
        assert rotation.m.code_sha256() == legacy
        assert rotation.rotation_code_sha256() != before


@pytest.mark.parametrize("body", ["[]", "[{}, {}]", '[{"grant_id":"one","grant_id":"two"}]'])
def test_grant_file_fails_closed_before_constructing_azure_clients(rotated, tmp_path, monkeypatch, body):
    h = rotated
    old, new, grants = tmp_path / "old.json", tmp_path / "new.json", tmp_path / "bad-grants.json"
    old.write_text(h.old.model_dump_json(), encoding="utf-8")
    new.write_text(h.new.model_dump_json(), encoding="utf-8")
    grants.write_text(body, encoding="utf-8")
    forbidden = Mock(side_effect=AssertionError("invalid input must not construct Azure credentials"))
    monkeypatch.setattr("azure.identity.ManagedIdentityCredential", forbidden)
    with pytest.raises(ValueError):
        api.operator_application(old, new, h.paths.sources, h.paths.backups, h.paths.retirement, grants)
    assert not forbidden.called


def test_exact_single_grant_document_roundtrips_without_client_creation(rotated, tmp_path):
    from shared_context.behavior_server_config import read_document
    grants = tmp_path / "native-grants.json"
    grants.write_text(json.dumps([grant.model_dump(mode="json") for grant in rotated.service.grants]), encoding="utf-8")
    assert read_document(grants, api._NativeGrantsDocument).root == rotated.service.grants
