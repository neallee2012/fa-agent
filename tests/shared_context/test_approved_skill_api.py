"""Signed local API and original authority/client path, with synthetic M3 storage."""

import hashlib
import copy
import json
from types import MappingProxyType, SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import A, B
from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_skill_prompt import SkillPromptAssemblyRecord, assemble_prompt
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from shared_context.m3_auth import HumanApprover
from shared_context.m3_models import ApprovalInput, CreateArtifact, VersionInput
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import open_turn
from test_work_policy_lifecycle import enabled

BODY = "APPROVED_SKILL_BODY_CANARY: report only the verified receipt; never grant operations.\n"


def add_skill(h, name="receipt-guide", *, text=BODY, kind="skill", choice="approve", version="1" * 32):
    record = h.artifacts.create(A, A.team, name, CreateArtifact(
        kind=kind, title="Receipt verification guide", source_work="legacy-source"))
    raw = text.encode("utf-8")
    record = h.artifacts.upload(A, A.team, name, version, record.etag, VersionInput(
        filename="guide.md", media_type="text/markdown", sha256=hashlib.sha256(raw).hexdigest()), raw)
    if choice is not None:
        h.artifacts.decide(HumanApprover(h.artifacts.approval.approver_oid, A.team), A.team, name, record.etag,
            ApprovalInput(decision_id=version, version_id=version, sha256=hashlib.sha256(raw).hexdigest(), decision=choice))


def approve_skill_version(h, name, text, version="2" * 32):
    record = h.artifacts.get(A, A.team, name)
    raw = text.encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    uploaded = h.artifacts.upload(
        A, A.team, name, version, record.etag,
        VersionInput(filename="guide.md", media_type="text/markdown", sha256=sha), raw)
    h.artifacts.decide(
        HumanApprover(h.artifacts.approval.approver_oid, A.team), A.team, name, uploaded.etag,
        ApprovalInput(decision_id=version, version_id=version, sha256=sha, decision="approve"))


def configured(assistant, selected=("receipt-guide",)):
    assistant.runtime.profiles = MappingProxyType({
        name: AssistantProfile.model_validate(profile.model_copy(update={
            "prompt_profile": "fa-instruction-skills-v1", "skill_artifacts": selected}).model_dump())
        for name, profile in assistant.runtime.profiles.items()
    })
    h, control, human, _ = enabled(assistant)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    return h, control, human


def test_signed_api_catalog_and_body_are_bound_to_the_original_assignment(assistant):
    add_skill(assistant)
    h, control, human = configured(assistant)
    turn = open_turn(h, control, human, "approved-skill-read")
    try:
        before = h.store._guard()
        catalog = turn.client.skill_catalog()
        assert [entry.name for entry in catalog.entries] == ["receipt-guide"]
        assert BODY not in catalog.model_dump_json()
        result = turn.client.read_skill("receipt-guide")
        assert result.skill == catalog.entries[0] and result.text == BODY
        assert h.store._guard() == before
        assert h.endpoint.requests == []
    finally:
        turn.client.close()


def post_skill(turn, body, *, action="read-skill", identity="agent", ticket=True, query=""):
    headers = turn.h.headers(identity)
    if ticket:
        headers["x-behavior-assistant-ticket"] = turn.client.ticket.get_secret_value()
    path = (f"/behavior/v1/bindings/{turn.binding.binding_id}/assistant"
            f"/sessions/{turn.assignment.session_id}/{action}")
    return turn.h.client.post(path + query, json=body, headers=headers)


def change_artifact(h, change, name="receipt-guide"):
    record = h.artifacts.get(A, A.team, name)
    payload = copy.deepcopy(record.payload)
    change(payload)
    partition = "m3-" + h.artifacts.context.auth.tenant_id + "-" + A.team
    h.artifacts.repository.replace(partition, "artifact:" + name, payload, record.etag)


def test_api_catalog_omits_pending_rejected_sop_and_unconfigured_bodies(assistant):
    for name, options in (
        ("receipt-guide", {}), ("pending", {"choice": None}), ("rejected", {"choice": "reject"}),
        ("sop-only", {"kind": "sop"}), ("not-configured", {}),
    ):
        add_skill(assistant, name, **options)
    h, control, human = configured(assistant, ("receipt-guide", "pending", "rejected", "sop-only"))
    turn = open_turn(h, control, human, "catalog-filter")
    try:
        catalog = turn.client.skill_catalog()
        assert [entry.name for entry in catalog.entries] == ["receipt-guide"]
        assert catalog.unavailable_count == 3
        for name in ("pending", "rejected", "sop-only", "not-configured"):
            assert name not in catalog.model_dump_json()
            with pytest.raises(Problem) as denied:
                turn.client.read_skill(name)
            assert denied.value.body["error"]["code"] == "SKILL_SOURCE_DENIED"
    finally:
        turn.client.close()


@pytest.mark.parametrize("change", [
    lambda value: value.update(effective_version_id=None),
    lambda value: value.update(kind="sop"),
    lambda value: value["versions"][0].update(source_work="different-source"),
    lambda value: value["versions"][0].update(source_work_revision=2),
    lambda value: value["versions"][0]["approval"].update(approver_oid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
])
def test_current_source_or_approval_change_denies_the_original_catalog_and_read(assistant, change):
    add_skill(assistant)
    h, control, human = configured(assistant)
    turn = open_turn(h, control, human, "source-change")
    try:
        assert turn.client.read_skill("receipt-guide").text == BODY
        change_artifact(h, change)
        guard = h.store._guard()
        for read in (turn.client.skill_catalog, lambda: turn.client.read_skill("receipt-guide")):
            with pytest.raises(Problem) as denied:
                read()
            assert denied.value.status == 403
            assert denied.value.body["error"]["code"] in {"SKILL_SOURCE_CHANGED", "SKILL_APPROVAL_DENIED"}
        assert h.store._guard() == guard and h.endpoint.requests == []
    finally:
        turn.client.close()


def test_new_approved_version_is_not_substituted_into_an_existing_assignment(assistant):
    add_skill(assistant)
    h, control, human = configured(assistant)
    turn = open_turn(h, control, human, "new-effective-version")
    try:
        assert turn.client.read_skill("receipt-guide").text == BODY
        before = h.artifacts.get(A, A.team, "receipt-guide")
        data = b"NEW_APPROVED_BODY_CANARY"
        sha = hashlib.sha256(data).hexdigest()
        uploaded = h.artifacts.upload(A, A.team, "receipt-guide", "2" * 32, before.etag,
            VersionInput(filename="guide.md", media_type="text/markdown", sha256=sha), data)
        h.artifacts.decide(HumanApprover(h.artifacts.approval.approver_oid, A.team), A.team, "receipt-guide", uploaded.etag,
            ApprovalInput(decision_id="2" * 32, version_id="2" * 32, sha256=sha, decision="approve"))
        response = post_skill(turn, {"request_id": "read-skill", "name": "receipt-guide"})
        assert response.status_code == 403
        assert response.headers["x-behavior-assistant-error-code"] == "SKILL_SOURCE_CHANGED"
        assert "CANARY" not in response.text
    finally:
        turn.client.close()


@pytest.mark.parametrize("fault", ["bytes", "blob-error", "lookup-error", "actor-removed", "actor-rebound",
                                 "configuration-removed", "during-read"])
def test_source_failures_do_not_return_a_prior_body_or_mutate_authority(assistant, monkeypatch, fault):
    add_skill(assistant)
    h, control, human = configured(assistant)
    turn = open_turn(h, control, human, "source-failure")
    try:
        assert turn.client.read_skill("receipt-guide").text == BODY
        def fail(*args, **kwargs):
            raise RuntimeError("PRIVATE_SOURCE_ERROR_CANARY")
        if fault == "bytes":
            monkeypatch.setattr(h.artifacts.store, "read", lambda *args: b"CORRUPTED_BODY_CANARY")
        elif fault == "blob-error":
            monkeypatch.setattr(h.artifacts.store, "read", fail)
        elif fault == "lookup-error":
            monkeypatch.setattr(h.artifacts.repository, "get", fail)
        elif fault == "actor-removed":
            h.assistant.actors = {}
        elif fault == "actor-rebound":
            h.assistant.actors[A.oid] = B
        elif fault == "during-read":
            read = h.artifacts.store.read
            def revoke_during_read(*args):
                data = read(*args)
                change_artifact(h, lambda value: value.update(effective_version_id=None))
                return data
            monkeypatch.setattr(h.artifacts.store, "read", revoke_during_read)
        else:
            control.policy = control.policy.model_copy(update={
                "profile": control.policy.profile.model_copy(update={"skill_artifacts": ()})})
        guard = h.store._guard()
        response = post_skill(turn, {"request_id": "read-skill", "name": "receipt-guide"})
        assert response.status_code in {403, 502, 503}
        assert "CANARY" not in response.text and "text" not in response.json()
        assert h.store._guard() == guard and h.endpoint.requests == []
    finally:
        turn.client.close()


@pytest.mark.parametrize("field,value", [
    ("path", "../SKILL.md"), ("url", "https://private.invalid/skill"), ("team", "team-two"),
    ("principal", "human"), ("actor", "agent-b"), ("purpose", "external-operation"),
    ("version_id", "2" * 32), ("sha256", "0" * 64),
    ("prompt_profile", "fa-instruction-skills-v1"), ("skill_artifacts", ["receipt-guide"]),
])
def test_read_body_cannot_select_paths_versions_principals_or_profiles(assistant, field, value):
    add_skill(assistant)
    h, control, human = configured(assistant)
    turn = open_turn(h, control, human, "read-spoof")
    try:
        guard = h.store._guard()
        response = post_skill(turn, {"request_id": "read-skill", "name": "receipt-guide", field: value})
        assert response.status_code == 422
        assert h.store._guard() == guard and h.endpoint.requests == []
    finally:
        turn.client.close()


@pytest.mark.parametrize("name", ["../receipt-guide", "C:\\private\\SKILL.md", "https://private.invalid/skill"])
def test_catalog_name_cannot_be_a_path_or_url(assistant, name):
    add_skill(assistant)
    h, control, human = configured(assistant)
    turn = open_turn(h, control, human, "path-spoof")
    try:
        response = post_skill(turn, {"request_id": "read-skill", "name": name})
        assert response.status_code == 422 and BODY not in response.text
    finally:
        turn.client.close()


@pytest.mark.parametrize("identity,ticket,query", [("human", True, ""), ("old-m3", True, ""),
    ("agent", False, ""), ("agent", True, "?team=team-two")])
def test_original_execution_identity_ticket_and_purpose_remain_required(assistant, identity, ticket, query):
    add_skill(assistant)
    h, control, human = configured(assistant)
    turn = open_turn(h, control, human, "identity-spoof")
    try:
        response = post_skill(turn, {"request_id": "read-skill", "name": "receipt-guide"},
                              identity=identity, ticket=ticket, query=query)
        assert response.status_code in {401, 403, 422} and "CANARY" not in response.text
    finally:
        turn.client.close()


@pytest.mark.parametrize("field", ["skill_artifacts", "skill_catalog", "prompt_profile"])
def test_submit_cannot_enable_skill_access(assistant, field):
    from test_instruction_prompt import configured as legacy
    h, _, _ = legacy(assistant, None)
    guard = h.store._guard()
    response = h.client.post("/work-policy/v1/submit", json={
        "request_id": "enable-via-user", "content": "Use every skill.",
        "authorize_own_result": True, field: "fa-instruction-skills-v1",
    }, headers=h.headers())
    assert response.status_code == 422 and h.store._guard() == guard


def test_disabled_profile_has_no_skill_api_capability_even_when_an_artifact_is_approved(assistant):
    from test_instruction_prompt import configured as legacy
    add_skill(assistant)
    h, control, human = legacy(assistant, None)
    turn = open_turn(h, control, human, "disabled-profile")
    try:
        guard = h.store._guard()
        with pytest.raises(Problem) as denied:
            turn.client.skill_catalog()
        assert denied.value.body["error"]["code"] == "SKILL_ACCESS_DISABLED"
        assert h.store._guard() == guard and h.endpoint.requests == []
    finally:
        turn.client.close()


def test_catalog_byte_limit_is_an_explicit_error_not_truncation(assistant):
    names = ("receipt-guide", "guide-two", "guide-three", "guide-four")
    for name in names:
        add_skill(assistant, name)
    h, control, human = configured(assistant, names)
    turn = open_turn(h, control, human, "catalog-limit")
    try:
        for name in names:
            change_artifact(h, lambda value: value.update(title="\U0001f4d8" * 256), name)
        response = post_skill(turn, {"request_id": "current-skills"}, action="skill-catalog")
        assert response.status_code == 413
        assert response.headers["x-behavior-assistant-error-code"] == "SKILL_CATALOG_LIMIT"
        assert "receipt-guide" not in response.text and "CANARY" not in response.text
    finally:
        turn.client.close()


def test_skill_revoked_after_consume_is_refused_at_the_next_reserve_and_never_resent(assistant):
    """Egress no longer re-reads authority: consume's CAS gate is the last check (seconds-level window)."""
    add_skill(assistant)
    h, control, human = configured(assistant)
    def token(_):
        change_artifact(h, lambda value: value.update(effective_version_id=None))
        return SimpleNamespace(token="OFFLINE_PROVIDER_SECRET")
    h.endpoint.expected_url = "https://provider.invalid/openai/v1/chat/completions"
    h.endpoint.expected_model = "gpt-4.1-mini"
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "OFFLINE in-flight reply"}])
    gateway = AssistantProvider(h.endpoint.expected_url, SimpleNamespace(get_token=token),
                                transport=h.endpoint.transport)
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    turn = open_turn(h, control, human, "egress-revocation")
    try:
        result = turn.client.read_skill("receipt-guide")

        def request(label):
            body = {"model": "gpt-4.1-mini", "stream": True,
                    "messages": [{"role": "user", "content": label},
                                 {"role": "tool", "tool_call_id": "read", "content": result.model_dump_json()}]}
            return body, hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        body, digest = request("Use receipt-guide")
        turn.client.reserve_model(digest)
        sent = turn.client.model(httpx.Request("POST", "https://context.invalid/model/v1/chat/completions", json=body))
        assert sent.status_code == 200 and len(h.endpoint.requests) == 1
        with pytest.raises(Problem) as later:
            turn.client.reserve_model(request("A later model step.")[1])
        assert later.value.body["error"]["upstream_status"] == 403
        # Restore only the synthetic source state, not the consumed model reservation.
        change_artifact(h, lambda value: value.update(effective_version_id="1" * 32))
        response = post_skill(turn, {"request_id": digest, "body": body}, action="model")
        assert response.status_code == 409 and len(h.endpoint.requests) == 1
        assert "CANARY" not in response.text and "SECRET" not in response.text
    finally:
        turn.client.close()


@pytest.mark.parametrize("selected,profile", [
    (("receipt-guide",), None), (("receipt-guide",), "fa-instruction-v1"),
    (("receipt-guide", "receipt-guide"), "fa-instruction-skills-v1"),
    (("one", "two", "three", "four", "five"), "fa-instruction-skills-v1"),
    (("../private",), "fa-instruction-skills-v1"),
])
def test_server_configuration_rejects_ambiguous_unbounded_or_disabled_selectors(assistant, selected, profile):
    original = next(iter(assistant.runtime.profiles.values())).model_dump()
    with pytest.raises(ValueError):
        AssistantProfile.model_validate({**original, "prompt_profile": profile, "skill_artifacts": selected})


def test_factory_configuration_pins_skill_selectors_without_new_actor_or_storage_scope():
    from shared_context.behavior_server_config import ServerConfiguration
    from test_behavior_assistant_server import assistant_configuration
    raw = assistant_configuration()
    before = ServerConfiguration.model_validate_json(json.dumps(raw))
    raw["assistant"]["profiles"][0].update(
        prompt_profile="fa-instruction-skills-v1", skill_artifacts=["receipt-guide"])
    after = ServerConfiguration.model_validate_json(json.dumps(raw))
    assert before.sha256 != after.sha256
    assert before.resource_bindings()["assistant_sha256"] != after.resource_bindings()["assistant_sha256"]
    assert before.identity == after.identity and before.table == after.table and before.sources == after.sources
    assert after.assistant.profiles[0].skill_artifacts == ("receipt-guide",)


@pytest.mark.parametrize("field,value", [
    ("system_message", "PRIVATE_SYSTEM_CANARY"), ("prefix_tokens", 0),
    ("prefix_sha256", "0" * 64), ("prefix_utf8_bytes", 1), ("source", "model-attested"),
    ("declared_capabilities", ["read_skill", "shell"]), ("omissions", ["PRIVATE_REASONING_CANARY"]),
])
def test_skill_prompt_metadata_rejects_private_fields_false_counts_and_attestation(field, value):
    record = assemble_prompt("fa-instruction-skills-v1").record.model_dump(mode="json")
    record[field] = value
    with pytest.raises(ValueError):
        SkillPromptAssemblyRecord.model_validate_json(json.dumps(record))


@pytest.mark.parametrize("unavailable", ["revoked", "missing"])
def test_cold_rebuild_omits_unavailable_skill_without_erasing_the_completed_archive(assistant, monkeypatch, unavailable):
    from test_sdk_compaction_product import ordinary, settle
    add_skill(assistant)
    h, control, human = configured(assistant)
    first = open_turn(h, control, human, "skill-rebuild-first")
    try:
        assert first.client.read_skill("receipt-guide").text == BODY
        state = ordinary(first.assignment.sdk_session_id)
        settle(first, state)
        from shared_context.behavior_assistant import AssistantAuthority, key
        archive_key = key("text-archive", first.binding.job_id)
        archive = h.store.read_snapshot((archive_key,))[archive_key]
        if unavailable == "revoked":
            change_artifact(h, lambda value: value.update(effective_version_id=None))
        else:
            get = h.artifacts.repository.get
            def missing(partition, row):
                if row == "artifact:receipt-guide":
                    raise Problem(404, "NOT_FOUND", "synthetic missing artifact")
                return get(partition, row)
            monkeypatch.setattr(h.artifacts.repository, "get", missing)
        h.assistant = AssistantAuthority(h.runtime, h.artifacts, (A,))
        h.app = create_assistant_app(h.assistant, h.verifier)
        install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
            installation=lambda: None, require_current_native_evidence=lambda: None))
        h.client = TestClient(h.app)
        h.executor.authority, h.executor.http_client = h.assistant, h.client
        second = open_turn(h, control, human, "skill-rebuild-second", first.assignment.task.instruction.conversation_id)
        try:
            guard = h.store._guard()
            catalog = second.client.skill_catalog()
            assert catalog.entries == () and catalog.unavailable_count == 1
            assert "receipt-guide" not in catalog.model_dump_json()
            assert second.client.restore() == state
            with pytest.raises(Problem) as denied:
                second.client.read_skill("receipt-guide")
            assert denied.value.body["error"]["code"] == "SKILL_SOURCE_DENIED"
            assert h.store._guard() == guard
            settle(second, state)
            from test_instruction_flow import check_history
            check_history(h)
        finally:
            second.client.close()
        assert h.store.read_snapshot((archive_key,))[archive_key] == archive
        assert h.endpoint.requests == []
    finally:
        first.client.close()


def test_completed_turn_can_continue_with_the_new_approved_skill_version(assistant):
    from shared_context.behavior_assistant import key
    from test_sdk_compaction_product import ordinary, settle

    add_skill(assistant)
    h, control, human = configured(assistant)
    first = open_turn(h, control, human, "catalog-continuation-first")
    try:
        old_catalog = first.client.skill_catalog()
        assert first.client.read_skill("receipt-guide").text == BODY
        state = ordinary(first.assignment.sdk_session_id)
        settle(first, state)
        archive_key = key("text-archive", first.binding.job_id)
        archive = h.store.read_snapshot((archive_key,))[archive_key]

        text = "CURRENT_SKILL_V2: verify the current receipt without granting operations."
        approve_skill_version(h, "receipt-guide", text)

        second = open_turn(
            h, control, human, "catalog-continuation-second",
            first.assignment.task.instruction.conversation_id)
        try:
            catalog = second.client.skill_catalog()
            assert catalog.entries[0].version_id == "2" * 32
            assert catalog != old_catalog
            assert second.assignment.sdk_session_id == first.assignment.sdk_session_id
            assert second.client.restore() == state
            assert second.client.read_skill("receipt-guide").text == text
            with pytest.raises(Problem) as stale:
                h.assistant.skills.read(A, A.team, ("receipt-guide",), old_catalog.entries[0])
            assert stale.value.body["error"]["code"] == "SKILL_SOURCE_CHANGED"
            settle(second, state)
            from test_instruction_flow import check_history
            check_history(h)
            assert h.store.read_snapshot((archive_key,))[archive_key] == archive
            assert h.endpoint.requests == []
        finally:
            second.client.close()
    finally:
        first.client.close()


@pytest.mark.parametrize("prior_state", ["missing", "pending", "rejected"])
def test_completed_turn_can_gain_a_newly_approved_configured_skill(assistant, prior_state):
    from shared_context.behavior_assistant import key
    from test_instruction_flow import check_history
    from test_sdk_compaction_product import ordinary, settle

    if prior_state != "missing":
        add_skill(assistant, choice=None if prior_state == "pending" else "reject")
    h, control, human = configured(assistant)
    first = open_turn(h, control, human, "unavailable-first")
    try:
        assert first.client.skill_catalog().entries == ()
        state = ordinary(first.assignment.sdk_session_id)
        settle(first, state)
        archive_key = key("text-archive", first.binding.job_id)
        archive = h.store.read_snapshot((archive_key,))[archive_key]
        if prior_state == "missing":
            add_skill(h, version="2" * 32)
        else:
            approve_skill_version(h, "receipt-guide", BODY)
        second = open_turn(h, control, human, "available-second", first.assignment.task.instruction.conversation_id)
        try:
            assert second.client.restore() == state
            assert second.client.skill_catalog().entries[0].version_id == "2" * 32
            assert second.client.read_skill("receipt-guide").text == BODY
            settle(second, state)
            check_history(h)
            assert h.store.read_snapshot((archive_key,))[archive_key] == archive
            assert h.endpoint.requests == []
        finally:
            second.client.close()
    finally:
        first.client.close()


def test_admitted_turn_cannot_recapture_a_new_catalog_during_restore(assistant, monkeypatch):
    from shared_context.behavior_assistant import key
    from test_sdk_compaction_product import ordinary, settle

    add_skill(assistant)
    h, control, human = configured(assistant)
    first = open_turn(h, control, human, "restore-race-first")
    second = None
    try:
        settle(first, ordinary(first.assignment.sdk_session_id))
        approve_skill_version(h, "receipt-guide", "Approved v2.")
        second = open_turn(h, control, human, "restore-race-second", first.assignment.task.instruction.conversation_id)
        assert second.client.skill_catalog().entries[0].version_id == "2" * 32
        approve_skill_version(h, "receipt-guide", "Approved v3.", "3" * 32)
        retained_keys = (key("text-archive", first.binding.job_id), key("session", second.assignment.session_id))
        retained, guard = h.store.read_snapshot(retained_keys), h.store._guard()
        monkeypatch.setattr(h.artifacts.store, "read", lambda *args: pytest.fail("stale turn must not load archive or skill bytes"))
        with pytest.raises(Problem) as denied:
            second.client.restore()
        assert denied.value.status == 403 and denied.value.body["error"]["code"] == "SKILL_SOURCE_CHANGED"
        assert h.store.read_snapshot(retained_keys) == retained and h.store._guard() == guard
        assert h.endpoint.requests == []
    finally:
        if second is not None:
            second.client.close()
        first.client.close()


@pytest.mark.parametrize("stage", ["reserve", "consume"])
def test_unused_entry_change_fences_the_running_turn_before_model_dispatch(assistant, stage):
    """A reserve after the change is refused; a dispatch reserved before it is sent once (seconds-level window)."""
    add_skill(assistant)
    add_skill(assistant, "unused-guide")
    h, control, human = configured(assistant, ("receipt-guide", "unused-guide"))
    h.endpoint.expected_url = "https://provider.invalid/openai/v1/chat/completions"
    h.endpoint.expected_model = "gpt-4.1-mini"
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "OFFLINE in-flight reply"}])
    gateway = AssistantProvider(h.endpoint.expected_url,
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE-not-a-credential")),
        transport=h.endpoint.transport)
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    turn = open_turn(h, control, human, "unused-entry-race")
    try:
        assert turn.client.read_skill("receipt-guide").text == BODY

        def request(label):
            body = {"model": "gpt-4.1-mini", "stream": True, "messages": [{"role": "user", "content": label}]}
            return body, hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        body, digest = request("Use receipt-guide.")
        if stage == "consume":
            turn.client.reserve_model(digest)
        approve_skill_version(h, "unused-guide", "Unused updated guidance.")
        if stage == "consume":
            sent = turn.client.model(httpx.Request("POST", "https://context.invalid/model/v1/chat/completions", json=body))
            assert sent.status_code == 200 and len(h.endpoint.requests) == 1
            _, digest = request("A later model step.")
        guard = h.store._guard()
        with pytest.raises(Problem) as denied:
            turn.client.reserve_model(digest)
        detail = denied.value.body["error"]
        assert detail["code"] == "ASSISTANT_API_UNKNOWN"
        assert detail["upstream_status"] == 403 and detail["upstream_error_code"] == "SKILL_SOURCE_CHANGED"
        assert h.store._guard() == guard and len(h.endpoint.requests) == (stage == "consume")
    finally:
        turn.client.close()


@pytest.mark.parametrize("fault,code", [
    ("removed-actor", "SKILL_IDENTITY_DENIED"),
    ("rebound-actor", "SKILL_IDENTITY_DENIED"),
    ("current-acl", "SKILL_IDENTITY_DENIED"),
    ("team-acl", "SKILL_IDENTITY_DENIED"),
])
def test_benign_catalog_update_does_not_restore_history_after_real_access_loss(assistant, fault, code):
    from dataclasses import replace
    from shared_context.behavior_assistant import key
    from test_sdk_compaction_product import ordinary, settle

    add_skill(assistant)
    h, control, human = configured(assistant)
    first = open_turn(h, control, human, "acl-first")
    second = None
    try:
        settle(first, ordinary(first.assignment.sdk_session_id))
        archive_key = key("text-archive", first.binding.job_id)
        archive = h.store.read_snapshot((archive_key,))[archive_key]
        approve_skill_version(h, "receipt-guide", "New approved version.")
        second = open_turn(h, control, human, "acl-second", first.assignment.task.instruction.conversation_id)
        if fault == "removed-actor":
            h.assistant.actors = {}
        elif fault == "rebound-actor":
            h.assistant.actors[A.oid] = B
        elif fault == "current-acl":
            auth = h.artifacts.context.auth
            h.artifacts.context.auth = replace(auth, actors={oid: actor for oid, actor in auth.actors.items() if oid != A.oid})
        else:
            h.artifacts.approval = replace(h.artifacts.approval, team="team-two")
        guard = h.store._guard()
        for read in (second.client.restore, second.client.skill_catalog, lambda: second.client.read_skill("receipt-guide")):
            with pytest.raises(Problem) as denied:
                read()
            assert denied.value.status == 403 and denied.value.body["error"]["code"] == code
            assert "CANARY" not in json.dumps(denied.value.body)
        assert h.store._guard() == guard
        assert h.store.read_snapshot((archive_key,))[archive_key] == archive
        assert h.endpoint.requests == []
    finally:
        if second is not None:
            second.client.close()
        first.client.close()


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_new_user_turn_can_continue_after_a_catalog_fence_is_terminally_saved(assistant, terminal):
    from shared_context.behavior_assistant import key
    from test_instruction_flow import check_history
    from test_instruction_prompt import saved_turn
    from test_sdk_compaction_product import ordinary, settle

    add_skill(assistant)
    h, control, human = configured(assistant)
    first = open_turn(h, control, human, "terminal-catalog-first")
    second = third = None
    try:
        conversation = first.assignment.task.instruction.conversation_id
        state = ordinary(first.assignment.sdk_session_id)
        settle(first, state)
        prior_keys = (key("text-archive", first.binding.job_id), key("result", first.binding.work.work_id))
        retained = h.store.read_snapshot(prior_keys)
        approve_skill_version(h, "receipt-guide", "Approved v2.")
        second = open_turn(h, control, human, "terminal-catalog-second", conversation)
        assert second.client.restore() == state
        assert second.client.read_skill("receipt-guide").text == "Approved v2."
        approve_skill_version(h, "receipt-guide", "Approved v3.", "3" * 32)
        guard = h.store._guard()
        with pytest.raises(Problem) as fenced:
            second.client.reserve_model("1" * 64)
        assert fenced.value.body["error"]["upstream_status"] == 403
        assert fenced.value.body["error"]["upstream_error_code"] == "SKILL_SOURCE_CHANGED"
        assert h.store._guard() == guard and h.endpoint.requests == []

        def refused_followup(identifier):
            response = h.client.post("/work-policy/v1/submit", json={
                "request_id": identifier, "content": "A new user turn.",
                "conversation_id": conversation,
            }, headers=h.headers())
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "CONVERSATION_TURN_UNRESOLVED"

        refused_followup("still-running")
        reference = second.client.save_sdk_state(state, second.stop)
        if terminal == "cancelled":
            cancelled = h.client.post(f"/work-policy/v1/bindings/{second.binding.binding_id}/cancel",
                json={"request_id": "cancel-original"}, headers=h.headers())
            assert cancelled.status_code == 200 and h.workers.inspect(second.binding.job_id).cancel_requested
        failure = "cancelled" if terminal == "cancelled" else "sdk_failed"
        result = second.client.finish_failed(reference, second.stop, failure)
        refused_followup("stop-not-reconciled")
        h.executor._complete(second.start, result.model_dump(mode="json"))
        h.workers.reconcile(second.binding.job_id, second.binding.owner)
        job = h.workers.inspect(second.binding.job_id)
        assert job.state == terminal and not job.result_forwarded
        refused_followup("result-not-forwarded")
        outcome = h.runtime.controller.step(second.binding.binding_id)
        assert outcome.state == terminal and outcome.result_forwarded
        saved = saved_turn(h, second.binding.job_id)
        assert saved.failure == failure and saved.sdk_stop.graceful
        assert saved.skill_catalog.entries[0].version_id == "2" * 32
        assert h.store.read_snapshot(prior_keys) == retained
        failed_keys = (key("text-archive", second.binding.job_id), key("result", second.binding.work.work_id))
        retained.update(h.store.read_snapshot(failed_keys))

        third = open_turn(h, control, human, "terminal-catalog-third", conversation)
        assert third.binding.job_id not in {first.binding.job_id, second.binding.job_id}
        assert third.assignment.sdk_session_id == first.assignment.sdk_session_id
        assert third.client.restore() == state
        assert third.client.skill_catalog().entries[0].version_id == "3" * 32
        assert third.client.read_skill("receipt-guide").text == "Approved v3."
        settle(third, state)
        check_history(h)
        assert h.workers.inspect(second.binding.job_id).state == terminal
        assert h.store.read_snapshot(tuple(retained)) == retained
        assert h.endpoint.requests == []
    finally:
        if third is not None:
            third.client.close()
        if second is not None:
            second.client.close()
        first.client.close()


@pytest.mark.parametrize("archive_status", ["unavailable", "upload-unknown", "ungraceful"])
def test_catalog_change_does_not_admit_a_turn_without_a_lawful_stopped_archive(assistant, monkeypatch, archive_status):
    from shared_context.behavior_assistant import key
    from shared_context.behavior_assistant_wire import ArchiveUnavailable
    from shared_context.behavior_sdk_checkpoint import checkpoint_key
    from test_sdk_compaction_product import ordinary, settle

    add_skill(assistant)
    h, control, human = configured(assistant)
    first = open_turn(h, control, human, "incomplete-catalog-first")
    second = None
    try:
        conversation = first.assignment.task.instruction.conversation_id
        state = ordinary(first.assignment.sdk_session_id)
        settle(first, state)
        first_key = key("text-archive", first.binding.job_id)
        retained = h.store.read_snapshot((first_key,))
        approve_skill_version(h, "receipt-guide", "Approved v2.")
        second = open_turn(h, control, human, "incomplete-catalog-second", conversation)
        assert second.client.restore() == state
        approve_skill_version(h, "receipt-guide", "Approved v3.", "3" * 32)
        with pytest.raises(Problem) as fenced:
            second.client.reserve_model("1" * 64)
        assert fenced.value.body["error"]["upstream_error_code"] == "SKILL_SOURCE_CHANGED"
        uploads = []
        archive = ArchiveUnavailable(error="SDK_ARCHIVE_UNAVAILABLE")
        if archive_status == "upload-unknown":
            put = h.artifacts.store.put
            def lost_receipt(*args):
                uploads.append(True)
                put(*args)
                raise Problem(503, "ARTIFACT_UNKNOWN", "synthetic upload receipt unknown")
            monkeypatch.setattr(h.artifacts.store, "put", lost_receipt)
            with pytest.raises(Problem) as unknown:
                second.client.save_sdk_state(state, second.stop)
            assert unknown.value.status == 503
            assert unknown.value.body["error"]["code"] == "ASSISTANT_API_UNKNOWN"
            with pytest.raises(Problem) as retained_unknown:
                second.client.save_sdk_state(state, second.stop)
            assert retained_unknown.value.body["error"]["code"] == "SDK_STATE_UPLOAD_UNKNOWN"
            assert uploads == [True]
            upload_key = checkpoint_key(second.binding.job_id)
            retained.update(h.store.read_snapshot((upload_key,)))
            assert retained[upload_key].payload["data"]["reference"] is None
            archive = ArchiveUnavailable(error="SDK_STATE_UPLOAD_UNKNOWN")
        if archive_status == "ungraceful":
            before = h.store._guard()
            response = post_skill(second, {
                "request_id": "save-text-reply", "reply": None, "archive": archive.model_dump(mode="json"),
                "failure": "sdk_failed", "sdk_stop": {**second.stop.model_dump(mode="json"), "graceful": False},
            }, action="save-text-reply")
            assert response.status_code == 422 and h.store._guard() == before
        else:
            result = second.client.finish_failed(archive, second.stop, "sdk_failed")
            h.executor._complete(second.start, result.model_dump(mode="json"))
            done = h.runtime.controller.step(second.binding.binding_id)
            assert done.state == "failed" and done.result_forwarded
            retained.update(h.store.read_snapshot((key("text-archive", second.binding.job_id),
                                                   key("result", second.binding.work.work_id))))
        guard = h.store._guard()
        followup = h.client.post("/work-policy/v1/submit", json={
            "request_id": "blocked-followup", "content": "Continue with current guidance.",
            "conversation_id": conversation,
        }, headers=h.headers())
        assert followup.status_code == 409
        assert followup.json()["error"]["code"] == (
            "CONVERSATION_TURN_UNRESOLVED" if archive_status == "ungraceful" else "CONVERSATION_CONTEXT_MISSING")
        assert h.store.read_snapshot(tuple(retained)) == retained
        assert h.store._guard() == guard and len(uploads) == (1 if archive_status == "upload-unknown" else 0)
        assert h.endpoint.requests == []
    finally:
        if second is not None:
            second.client.close()
        first.client.close()


def test_unfinished_job_resume_keeps_its_catalog_and_original_checkpoint(assistant):
    import zlib
    from dataclasses import replace
    from shared_context.behavior_assistant import key
    from shared_context.behavior_assistant_wire import SdkArchive
    from test_sdk_compaction_product import ordinary

    add_skill(assistant)
    h, control, human = configured(assistant)
    first = open_turn(h, control, human, "unfinished-catalog")
    try:
        assert first.client.read_skill("receipt-guide").text == BODY
        raw = json.dumps(ordinary(first.assignment.sdk_session_id).files, sort_keys=True).encode("utf-8")
        archive = SdkArchive(sdk_session_id=first.assignment.sdk_session_id,
                            data=zlib.compress(raw), sha256=hashlib.sha256(raw).hexdigest())
        first.client.stopped("finish_missing")
        checkpoint = first.client.checkpoint(archive)
        h.executor._complete(first.start, {"state": "waiting", **checkpoint.model_dump(mode="json")})
        parked = h.runtime.controller.step(first.binding.binding_id)
        assert parked.state == "waiting_dependency"
        prior_keys = (key("session", first.assignment.session_id), key("archive", first.assignment.session_id))
        retained = h.store.read_snapshot(prior_keys)
        approve_skill_version(h, "receipt-guide", "Updated guidance is not resume permission.")
        starts = []
        h.workers.adapter = replace(h.workers.adapter, start=starts.append)
        guard = h.store._guard()
        with pytest.raises(Problem) as dispatch_denied:
            h.workers.start(first.binding.job_id, first.binding.owner)
        assert dispatch_denied.value.body["error"]["code"] == "WORK_POLICY_START_FENCED"
        assert starts == []
        # The retained checkpoint guard also rejects a changed catalog before any issue write.
        resume = first.start.model_copy(update={"checkpoint": checkpoint.checkpoint.model_dump_json().encode()})
        with pytest.raises(Problem) as denied:
            h.assistant.issue(resume)
        assert denied.value.body["error"]["code"] == "SKILL_SOURCE_CHANGED"
        assert h.store.read_snapshot(prior_keys) == retained
        assert h.store._guard() == guard and h.endpoint.requests == []
        response = h.client.post("/work-policy/v1/submit", json={
            "request_id": "not-a-new-turn", "content": "Continue.",
            "conversation_id": first.assignment.task.instruction.conversation_id,
        }, headers=h.headers())
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CONVERSATION_TURN_UNRESOLVED"
    finally:
        first.client.close()


def test_reader_preserves_the_exact_8k_body_even_when_json_escaping_doubles_transport_size(assistant):
    text = '"\\' * 4096
    add_skill(assistant, text=text)
    h, control, human = configured(assistant)
    turn = open_turn(h, control, human, "maximum-skill-body")
    try:
        result = turn.client.read_skill("receipt-guide")
        assert len(result.text.encode("utf-8")) == 8192 and result.text == text
        assert len(result.model_dump_json().encode("utf-8")) > 16384
    finally:
        turn.client.close()


def test_8193_byte_body_cannot_become_an_approved_skill(assistant):
    with pytest.raises(Problem) as denied:
        add_skill(assistant, text="x" * 8193)
    assert denied.value.body["error"]["code"] == "M3_POLICY_FORMAT"
    h, control, human = configured(assistant)
    turn = open_turn(h, control, human, "oversize-skill-body")
    try:
        assert turn.client.skill_catalog().entries == ()
        with pytest.raises(Problem):
            turn.client.read_skill("receipt-guide")
    finally:
        turn.client.close()


@pytest.mark.parametrize("spoof", ["tools", "skill_access", "prompt_profile", "skill_artifacts"])
def test_model_body_cannot_enable_or_select_the_skill_capability(assistant, spoof):
    from test_instruction_prompt import configured as legacy
    h, control, human = legacy(assistant, None)
    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: pytest.fail("invalid model flags must not reach provider credentials")),
        transport=httpx.MockTransport(lambda _: pytest.fail("invalid model flags must not leave the API")))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    turn = open_turn(h, control, human, "model-flags-spoof")
    try:
        body = {"model": "gpt-4.1-mini", "stream": True, "messages": [{"role": "user", "content": "Test."}]}
        body[spoof] = ([{"type": "function", "function": {"name": "read_skill"}}] if spoof == "tools"
                       else "fa-instruction-skills-v1")
        guard = h.store._guard()
        response = post_skill(turn, {"request_id": "untrusted-model-flags", "body": body}, action="model")
        assert response.status_code == (403 if spoof == "tools" else 422)
        assert response.headers["x-behavior-model-upstream-error-code"] == (
            "ASSISTANT_TOOLS_DENIED" if spoof == "tools" else "ASSISTANT_MODEL_REQUEST_INVALID")
        assert h.store._guard() == guard
    finally:
        turn.client.close()
