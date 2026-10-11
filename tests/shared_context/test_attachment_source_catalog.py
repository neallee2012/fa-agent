"""A human sees only original Team source metadata, never content or permission claims."""

from datetime import timedelta
from contextvars import Context
from types import MappingProxyType, SimpleNamespace

import pytest

from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_attachment_catalog import AttachmentSourceCatalog
from shared_context.behavior_attachment_catalog import read_attachment_catalog
from shared_context.behavior_work_policy_api import install_work_policy_routes
from test_behavior_assistant import assistant
from test_instruction_attachments import BODY, configured_attachments, withdraw
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_work_policy_lifecycle import enabled

PATH = "/work-policy/v1/attachment-sources"


def test_catalogue_lists_exact_metadata_without_reading_attachment_body(assistant, monkeypatch):
    h, control, _, attachment = configured_attachments(assistant)
    for method in ("read_exact", "read_graph"):
        monkeypatch.setattr(h.sources, method, lambda *args, **kwargs: pytest.fail("Metadata must not read source contents"))
    before = h.store._guard()
    principal = h.verifier(h.client.build_request("GET", PATH, headers=h.headers("read-only")))
    direct = read_attachment_catalog(control, principal)
    response = h.client.get(PATH, headers=h.headers("read-only"))
    assert response.status_code == 200, response.text
    parsed = AttachmentSourceCatalog.model_validate_json(response.content)
    assert parsed == direct
    expected = next(item for item in parsed.entries if item.source_id == attachment.source.source_id)
    assert parsed.state == "metadata" and expected.state == "recorded" and expected.reference == attachment.source
    assert parsed.authorization is parsed.content_reverified is parsed.dependencies_reverified is False
    assert BODY not in response.text and "ATTACHED_SOURCE_CANARY" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert h.store._guard() == before and not control.records and not h.endpoint.requests


@pytest.mark.parametrize("active,visibility,state", [
    (False, "team", "disabled"), (True, "private", "unsupported-scope"),
])
def test_unavailable_capability_does_not_reveal_source_references(assistant, monkeypatch, active, visibility, state):
    h, _, _, _ = configured_attachments(assistant, active=active, visibility=visibility)
    monkeypatch.setattr(h.sources, "read_exact", lambda *args, **kwargs: pytest.fail("No source access"))
    response = h.client.get(PATH, headers=h.headers("read-only"))
    assert response.status_code == 200, response.text
    assert response.json()["state"] == state and response.json()["entries"] == []


@pytest.mark.parametrize("change,state", [("withdraw", "withdrawn"), ("expire", "expired")])
def test_catalogue_explicitly_marks_unavailable_versions_without_suggesting_a_reference(assistant, change, state):
    h, control, human, attachment = configured_attachments(assistant, expires=change == "expire")
    if change == "withdraw":
        withdraw(h, control, human, attachment)
    else:
        h.time += timedelta(seconds=2)
    before = h.store._guard()
    response = h.client.get(PATH, headers=h.headers("read-only"))
    assert response.status_code == 200, response.text
    selected = next(item for item in response.json()["entries"] if item["source_id"] == attachment.source.source_id)
    assert selected["state"] == state and selected["reference"] is None
    assert h.store._guard() == before


def test_missing_allowed_source_is_not_reported_as_an_existing_file(assistant):
    h = assistant
    h.runtime.profiles = MappingProxyType({key: AssistantProfile.model_validate({
        **value.model_dump(), "prompt_profile": "fa-instruction-v1",
        "text_attachment_profile": "fa-source-text-attachments-v1",
    }) for key, value in h.runtime.profiles.items()})
    h, _, _, _ = enabled(h)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    response = h.client.get(PATH, headers=h.headers("read-only"))
    assert response.status_code == 200, response.text
    entries = {item["source_id"]: item for item in response.json()["entries"]}
    assert entries["dependency"]["state"] == "missing" and entries["dependency"]["reference"] is None
    assert entries["metrics"]["state"] == "recorded"


def test_new_human_profile_selection_controls_catalogue_without_reinstalling_policy(assistant):
    from test_conversation_profile_selection import configured, select, template
    h, control, _ = configured(assistant)
    before = control.policy.model_dump_json()
    assert h.client.get(PATH, headers=h.headers()).json()["state"] == "disabled"
    selected, _ = select(h, value=template(text_attachment_profile="fa-source-text-attachments-v1"))
    assert selected.status_code == 200, selected.text
    response = h.client.get(PATH, headers=h.headers("read-only"))
    assert response.status_code == 200 and response.json()["state"] == "metadata"
    assert control.policy.model_dump_json() == before


@pytest.mark.parametrize("identity", ["agent", "old-m3"])
def test_catalogue_does_not_accept_other_principals_or_old_scopes(assistant, identity):
    h, _, _, _ = configured_attachments(assistant)
    before = h.store._guard()
    response = h.client.get(PATH, headers=h.headers(identity))
    assert response.status_code == 403
    assert h.store._guard() == before


def test_catalogue_requires_current_standing_read_authority(assistant):
    h, control, human, _ = configured_attachments(assistant)
    control.revoke(human, "revoke-source-catalogue")
    response = h.client.get(PATH, headers=h.headers("read-only"))
    assert response.status_code == 403


def test_late_standing_revocation_does_not_release_catalogue_metadata(assistant, monkeypatch):
    h, control, human, _ = configured_attachments(assistant)
    original = h.sources.verify_receipt
    changed = False
    def revoke_after_read(*args):
        nonlocal changed
        value = original(*args)
        if not changed:
            changed = True
            Context().run(control.revoke, human, "revoke-during-catalogue")
        return value
    monkeypatch.setattr(h.sources, "verify_receipt", revoke_after_read)
    response = h.client.get(PATH, headers=h.headers("read-only"))
    assert response.status_code in {403, 409, 412}
    assert "entries" not in response.json() and "ATTACHED_SOURCE_CANARY" not in response.text
    assert changed and not h.endpoint.requests


@pytest.mark.parametrize("writer", ["read-key", "unrelated"])
def test_one_read_conflict_rechecks_metadata_without_changing_the_original_scope(assistant, monkeypatch, writer):
    from shared_context.behavior_store import ControlChanges, ControlRequest
    from test_behavior_store import rewrite_read_rows
    h, control, _, _ = configured_attachments(assistant)
    verify = h.sources.verify_receipt
    calls = []
    def concurrent_guard(*args):
        result = verify(*args)
        calls.append(True)
        if len(calls) == 1:
            Context().run(h.store.transact, ControlRequest(
                request_id="other-reader-bookkeeping", request_digest="a" * 64),
                lambda _: ControlChanges(result={"unrelated": True}))
            if writer == "read-key":
                rewrite_read_rows(h.transport.backing)
        return result
    monkeypatch.setattr(h.sources, "verify_receipt", concurrent_guard)
    response = h.client.get(PATH, headers=h.headers("read-only"))
    assert response.status_code == 200, response.text
    # #197: only a change to a key the catalogue read is a conflict that re-checks; an unrelated writer is accepted.
    assert response.json()["state"] == "metadata" and (len(calls) > 2 if writer == "read-key" else len(calls) == 2)
    assert not control.records and not h.endpoint.requests


@pytest.mark.parametrize("variant", ["query", "body"])
def test_catalogue_does_not_take_user_supplied_scope(assistant, variant):
    h, _, _, _ = configured_attachments(assistant)
    response = h.client.request("GET", PATH + ("?team=other" if variant == "query" else ""),
                                content=b'{"source_id":"other"}' if variant == "body" else None,
                                headers=h.headers("read-only"))
    assert response.status_code == 422
