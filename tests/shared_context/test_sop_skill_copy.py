"""One approved Team SOP, original instruction ticket, one unapproved exact-copy draft."""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
from types import MappingProxyType, SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import A, B, C
from legacy_m2_m4.api import create_app
from shared_context.auth import JwtVerifier
from shared_context.behavior_assistant import AssistantProfile, key
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.m3_auth import M3JwtVerifier
from shared_context.m3_models import SkillPurpose
from shared_context.behavior_operations import control_key
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_inventory, instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key, token
from test_sdk_compaction_product import open_turn, ordinary, settle
from test_work_policy_lifecycle import enabled
from test_work_cost_policy import standing
from shared_context.behavior_work_policy import WorkPolicyControl, work_policy_key

PROFILE = "fa-instruction-sop-copy-v1"
SOP = "copy-source-sop"
LEGACY_WORK = "copy-source-work"
SOURCE_VERSION = "c" * 32
BODY = "APPROVED_TEAM_SOP_CANARY\r\n核對原始回條，不批准操作。\t\n".encode("utf-8")
PURPOSE = "  用於核對已保存回條；僅為草稿，不是批准。  "


def approved_sop(h, signing_key, jwk_client, *, body=BODY, version=SOURCE_VERSION, approve=True, kind="sop"):
    http = TestClient(create_app(h.artifacts.context, JwtVerifier(h.artifacts.context.auth, jwk_client),
        artifact_service=h.artifacts,
        verified_artifact_identity_dependency=M3JwtVerifier(h.artifacts.context.auth, h.artifacts.approval, jwk_client)))
    agent = {"Authorization": "Bearer " + token(signing_key, {"oid": A.oid})}
    human = {"Authorization": "Bearer " + token(signing_key)}
    original = http.post("/teams/" + A.team + "/works/" + LEGACY_WORK, headers=agent,
                         json={"goal": "Synthetic existing Team work for an approved procedure."})
    assert original.status_code == 201, original.text
    path = "/teams/" + A.team + "/artifacts/" + SOP
    created = http.post(path, headers=agent, json={
        "kind": kind, "title": "Approved shared Team procedure", "source_work": LEGACY_WORK,
    })
    assert created.status_code == 201, created.text
    uploaded = http.put(path + "/versions/" + version, content=body, headers={
        **agent, "If-Match": created.headers["etag"], "Content-Type": "application/octet-stream",
        "X-Artifact-Filename": "procedure.md", "X-Artifact-Media-Type": "text/markdown",
        "X-Artifact-SHA256": hashlib.sha256(body).hexdigest(),
    })
    assert uploaded.status_code == 200, uploaded.text
    if approve:
        approved = http.post(path + "/decisions", headers={**human, "If-Match": uploaded.headers["etag"]}, json={
            "decision_id": version, "version_id": version, "sha256": hashlib.sha256(body).hexdigest(), "decision": "approve",
        })
        assert approved.status_code == 200, approved.text
    source = http.get(path + "/versions/" + version, headers=agent)
    assert source.status_code == 200
    return http, agent, human, source.json()


def copy_scope(version):
    return {
        "schema_version": "fa-team-sop-skill-copy-v1",
        "authorization": "one-approved-sop-byte-copy-per-work",
        "visibility": "team", "source_visibility": "team", "team_id": A.team,
        "source_artifact_id": SOP, "source_version_id": version["version_id"],
        "source_sha256": version["sha256"], "source_approval_sha256": digest(version["approval"]),
        "purpose": SkillPurpose(schema_version="fa-skill-purpose-v1", description=PURPOSE).model_dump(),
    }


def configured_copy(h, scope):
    h.runtime.profiles = MappingProxyType({
        name: AssistantProfile.model_validate({
            **profile.model_dump(), "prompt_profile": PROFILE, "sop_copy": scope, "legacy_source_work": LEGACY_WORK,
        }) for name, profile in h.runtime.profiles.items()
    })
    _, policy, observation, _, _ = standing(h)
    human = h.verifier(h.client.build_request("GET", "/", headers=h.headers()))
    control = WorkPolicyControl(h.runtime, policy, lambda: observation)
    control.install_offline(human, "signed-copy-scope-installation", h.time + timedelta(minutes=10))
    readiness = h.readiness()
    h.delivery.registration = replace(h.delivery.registration, verify_readiness=lambda: readiness.model_copy(update={
        "observed_at": h.time, "expires_at": h.time + timedelta(minutes=5)}))
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    return h, control, human


def post_copy(turn, **extra):
    return turn.h.client.post(
        f"/behavior/v1/bindings/{turn.binding.binding_id}/assistant/sessions/{turn.assignment.session_id}/copy-sop-draft",
        headers={**turn.h.headers("agent"), "x-behavior-assistant-ticket": turn.client.ticket.get_secret_value()},
        json={"request_id": "copy-sop-draft", **extra},
    )


def change_source(http, agent, human, *, body=BODY, approve=True):
    path = "/teams/" + A.team + "/artifacts/" + SOP
    current = http.get(path, headers=agent)
    uploaded = http.put(path + "/versions/" + "d" * 32, content=body, headers={
        **agent, "If-Match": current.headers["etag"], "Content-Type": "application/octet-stream",
        "X-Artifact-Filename": "procedure.md", "X-Artifact-Media-Type": "text/markdown",
        "X-Artifact-SHA256": hashlib.sha256(body).hexdigest(),
    })
    assert uploaded.status_code == 200
    result = http.post(path + "/decisions", headers={**human, "If-Match": uploaded.headers["etag"]}, json={
        "decision_id": "d" * 32, "version_id": "d" * 32,
        "sha256": hashlib.sha256(body).hexdigest(), "decision": "approve" if approve else "reject"})
    assert result.status_code == 200


def test_current_ticket_copies_only_approved_sop_to_its_unapproved_skill_slot(assistant, signing_key, jwk_client):
    h = assistant
    _, _, _, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    turn = open_turn(h, control, human, "exact-sop-copy")
    try:
        reply = post_copy(turn)
        assert reply.status_code == 200, reply.text
        result = reply.json()
        assert result["schema_version"] == "fa-sop-skill-draft-receipt-v1"
        assert result["approved"] is False and result["effective_version_id"] is None
        assert result["visibility"] == "team" and result["job_id"] == turn.binding.job_id
        assert result["artifact_id"] == turn.assignment.task.artifact_id
        assert result["version_id"] == turn.assignment.task.artifact_version
        assert "APPROVED_TEAM_SOP_CANARY" not in reply.text and "approver_oid" not in reply.text
        saved = h.artifacts.get(A, A.team, result["artifact_id"])
        assert saved.payload["kind"] == "skill" and saved.payload["effective_version_id"] is None
        version = h.artifacts.version(A, A.team, result["artifact_id"], result["version_id"])
        assert version.approval is None and version.skill_purpose.description == PURPOSE
        assert version.source_work == LEGACY_WORK and version.sha256 == source["sha256"]
        assert h.artifacts.download(A, A.team, result["artifact_id"], result["version_id"], version.blob.etag)[1] == BODY
        assert h.assistant.skills.catalog(A, A.team, (result["artifact_id"],)).entries == ()
        before = h.store._guard()
        assert post_copy(turn).json() == result and h.store._guard() == before
        assert h.artifacts.get(A, A.team, result["artifact_id"]) == saved
        assert h.endpoint.requests == []
        settle(turn, ordinary(turn.assignment.sdk_session_id))
        archived = h.store.read_snapshot((key("text-archive", turn.binding.job_id),))[key("text-archive", turn.binding.job_id)]
        assert archived.payload["data"]["sop_copy_receipt"] == result
        assert h.assistant.result(turn.start.binding).known_effects == (result["receipt_id"],)
    finally:
        turn.client.close()


def test_unknown_original_copy_cannot_be_hidden_by_a_successful_sdk_reply(assistant, signing_key, jwk_client, monkeypatch):
    from shared_context.errors import Problem
    h = assistant
    _, _, _, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    turn = open_turn(h, control, human, "unknown-copy-turn")
    original = h.artifacts.create
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError("SYNTHETIC_UNKNOWN")
    monkeypatch.setattr(h.artifacts, "create", lost)
    try:
        response = post_copy(turn)
        assert response.status_code == 503
        reference = turn.client.save_sdk_state(ordinary(turn.assignment.sdk_session_id), turn.stop)
        with pytest.raises(Problem):
            turn.client.save_text_reply("This cannot silently complete unknown copy work.", reference, turn.stop)
        assert turn.client.status().state == "running"
        assert h.store.read_snapshot((key("result", turn.binding.work.work_id),))[key("result", turn.binding.work.work_id)] is None
    finally:
        turn.client.close()


@pytest.mark.parametrize("fields", [
    {"body": "PRIVATE_USER_CANARY"}, {"purpose": {"description": "unassigned"}},
    {"team": "team-two"}, {"actor": "another"}, {"source_work": "runtime-is-not-legacy"},
    {"source_artifact_id": "private"}, {"version_id": "f" * 32}, {"approved": True},
])
def test_model_or_client_cannot_supply_copy_content_identity_or_approval(assistant, signing_key, jwk_client, fields):
    h = assistant
    _, _, _, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    turn = open_turn(h, control, human, "copy-no-arguments")
    try:
        before = h.store._guard()
        with patch.object(h.artifacts, "create", wraps=h.artifacts.create) as create:
            response = post_copy(turn, **fields)
        assert response.status_code == 422 and create.call_count == 0
        assert h.store._guard() == before and "PRIVATE_USER_CANARY" not in response.text
    finally:
        turn.client.close()


@pytest.mark.parametrize("identity", ["human", "peer", "other-team", "expired", "wrong-ticket"])
def test_copy_route_requires_its_actual_agent_and_original_ticket(assistant, signing_key, jwk_client, identity):
    h = assistant
    _, _, _, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    turn = open_turn(h, control, human, "copy-actual-identity")
    try:
        headers = h.headers() if identity == "human" else h.headers("agent")
        if identity in {"peer", "other-team", "expired"}:
            changes = {"oid": B.oid if identity == "peer" else C.oid if identity == "other-team" else A.oid,
                       "idtyp": "app", "roles": ["Behavior.Agent"]}
            if identity == "expired":
                changes["exp"] = 1
            headers = {"Authorization": "Bearer " + token(signing_key, changes, remove=("scp",))}
        headers["x-behavior-assistant-ticket"] = "f" * 64 if identity == "wrong-ticket" else turn.client.ticket.get_secret_value()
        before = h.store._guard()
        response = h.client.post(
            f"/behavior/v1/bindings/{turn.binding.binding_id}/assistant/sessions/{turn.assignment.session_id}/copy-sop-draft",
            headers=headers, json={"request_id": "copy-sop-draft"})
        assert response.status_code == (401 if identity == "expired" else 403)
        assert h.store._guard() == before
    finally:
        turn.client.close()


@pytest.mark.parametrize("fault", ["unapproved", "wrong-kind", "wrong-version", "wrong-hash", "wrong-approval", "wrong-team"])
def test_only_the_original_current_approved_team_sop_can_be_copied(assistant, signing_key, jwk_client, fault):
    h = assistant
    _, _, _, source = approved_sop(h, signing_key, jwk_client,
                                  approve=fault != "unapproved", kind="skill" if fault == "wrong-kind" else "sop")
    scope = copy_scope(source)
    if fault in {"wrong-version", "wrong-hash", "wrong-approval", "wrong-team"}:
        field, value = {"wrong-version": ("source_version_id", "e" * 32),
            "wrong-hash": ("source_sha256", "f" * 64),
            "wrong-approval": ("source_approval_sha256", "f" * 64),
            "wrong-team": ("team_id", "team-two")}[fault]
        scope[field] = value
    h, control, human = configured_copy(h, scope)
    turn = open_turn(h, control, human, "copy-source-denied")
    try:
        before = h.store._guard()
        with patch.object(h.artifacts, "create", wraps=h.artifacts.create) as create:
            response = post_copy(turn)
        assert response.status_code == 403 and create.call_count == 0, response.text
        assert h.store._guard() == before
    finally:
        turn.client.close()


def test_absent_copy_opt_in_does_not_inherit_own_result_or_source_approval(assistant, signing_key, jwk_client):
    h = assistant
    approved_sop(h, signing_key, jwk_client)
    h, control, human, _ = enabled(h)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    turn = open_turn(h, control, human, "copy-default-off")
    try:
        before = h.store._guard()
        response = post_copy(turn)
        assert response.status_code == 403 and response.headers["x-behavior-assistant-error-code"] == "SOP_COPY_DISABLED"
        assert h.store._guard() == before
    finally:
        turn.client.close()


@pytest.mark.parametrize("stage", ["create", "upload", "blob", "missing-blob", "intent-ack", "upload-intent-ack"])
def test_copy_ack_loss_reconciles_original_slot_without_repeating_effects(
        assistant, signing_key, jwk_client, monkeypatch, stage):
    h = assistant
    _, _, _, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    turn = open_turn(h, control, human, "copy-original-unknown")
    native_create, native_upload, native_put = h.artifacts.create, h.artifacts.upload, h.artifacts.store.put
    command = h.assistant._command
    counts = {"create": 0, "upload": 0, "blob": 0}
    def create(*args, **kwargs):
        counts["create"] += 1
        result = native_create(*args, **kwargs)
        if stage == "create" and counts["create"] == 1:
            raise TimeoutError("SYNTHETIC_CREATE_ACK")
        return result
    def upload(*args, **kwargs):
        counts["upload"] += 1
        result = native_upload(*args, **kwargs)
        if stage == "upload" and counts["upload"] == 1:
            raise TimeoutError("SYNTHETIC_UPLOAD_ACK")
        return result
    def put(*args, **kwargs):
        counts["blob"] += 1
        if stage == "missing-blob":
            raise TimeoutError("SYNTHETIC_BLOB_UNKNOWN")
        result = native_put(*args, **kwargs)
        if stage == "blob" and counts["blob"] == 1:
            raise TimeoutError("SYNTHETIC_BLOB_ACK")
        return result
    def lost_intent(session, name, *args, **kwargs):
        result = command(session, name, *args, **kwargs)
        if (stage == "intent-ack" and name == "sop-copy-intent"
                or stage == "upload-intent-ack" and name == "sop-copy-upload_unknown"):
            raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "Synthetic original intent acknowledgement lost.")
        return result
    monkeypatch.setattr(h.artifacts, "create", create)
    monkeypatch.setattr(h.artifacts, "upload", upload)
    monkeypatch.setattr(h.artifacts.store, "put", put)
    monkeypatch.setattr(h.assistant, "_command", lost_intent)
    try:
        response = post_copy(turn)
        assert response.status_code == 503
        original_counts = dict(counts)
        monkeypatch.setattr(h.assistant, "_command", command)
        again = post_copy(turn)
        if stage in {"missing-blob", "intent-ack", "upload-intent-ack"}:
            assert again.status_code == 503 and counts == original_counts
            state = h.store.read_snapshot((key("artifact", turn.binding.work.work_id),))[key("artifact", turn.binding.work.work_id)]
            assert state.payload["data"]["stage"] != "saved"
        else:
            assert again.status_code == 200, again.text
            assert counts == {"create": 1, "upload": 1, "blob": 1}
            before = h.store._guard()
            assert post_copy(turn).json() == again.json() and h.store._guard() == before
            assert counts == {"create": 1, "upload": 1, "blob": 1}
    finally:
        turn.client.close()


@pytest.mark.parametrize("when", ["before-copy", "after-copy", "during-read", "after-create", "after-upload"])
def test_source_effective_version_changes_never_retarget_or_repeat_copy(
        assistant, signing_key, jwk_client, monkeypatch, when):
    h = assistant
    http, agent, human_headers, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    turn = open_turn(h, control, human, "copy-source-race")
    changed, calls = [], {"create": 0, "upload": 0}
    def update():
        changed.append(True)
        change_source(http, agent, human_headers)
    before = None
    if when == "after-copy":
        result = post_copy(turn)
        assert result.status_code == 200
        before = h.artifacts.get(A, A.team, turn.assignment.task.artifact_id)
    if when in {"before-copy", "after-copy"}:
        update()
    for name in ("create", "upload"):
        original = getattr(h.artifacts, name)
        def invoked(*args, _name=name, _original=original, **kwargs):
            target_call = args[2] == turn.assignment.task.artifact_id
            if target_call:
                calls[_name] += 1
            value = _original(*args, **kwargs)
            if target_call and when == "after-" + _name and not changed:
                update()
            return value
        monkeypatch.setattr(h.artifacts, name, invoked)
    if when == "during-read":
        original_context = h.artifacts.policy_context
        def read_then_change(*args, **kwargs):
            result = original_context(*args, **kwargs)
            update()
            return result
        monkeypatch.setattr(h.artifacts, "policy_context", read_then_change)
    try:
        response = post_copy(turn)
        historical = when in {"after-copy", "after-upload"}
        assert response.status_code == (200 if historical else 403), response.text
        if historical:
            assert response.json()["source_version_id"] == SOURCE_VERSION
            assert response.json()["state_scope"] == "at-original-copy-write"
        if before is not None:
            assert h.artifacts.get(A, A.team, turn.assignment.task.artifact_id) == before
        assert calls["create"] == (1 if when in {"after-create", "after-upload"} else 0)
        assert calls["upload"] == (1 if when == "after-upload" else 0)
        counts = dict(calls)
        assert post_copy(turn).status_code == (200 if historical else 403) and calls == counts
    finally:
        turn.client.close()


@pytest.mark.parametrize("change", ["standing-policy", "read-permission", "actor-map", "source-owner"])
def test_current_write_authority_is_not_resurrected_by_a_saved_source_approval(
        assistant, signing_key, jwk_client, monkeypatch, change):
    from shared_context.models import Handoff
    h = assistant
    _, _, _, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    turn = open_turn(h, control, human, "copy-authority-loss")
    if change == "standing-policy":
        control.revoke(human, "copy-scope-revoked")
    elif change == "read-permission":
        installed = h.store.read_snapshot((control.policy_key,))[control.policy_key]
        h.mutate(control.policy_key, policy={**installed.payload["data"]["policy"], "read_enabled": False})
    elif change == "actor-map":
        monkeypatch.setattr(h.assistant, "actors", {A.oid: B})
    else:
        source_work = h.artifacts.context.get(A, A.team, LEGACY_WORK)
        h.artifacts.context.handoff(A, A.team, LEGACY_WORK, source_work.etag, Handoff(to_actor=B.actor))
    try:
        with patch.object(h.artifacts, "create", wraps=h.artifacts.create) as created:
            denied = post_copy(turn)
        assert denied.status_code == 403 and not created.called
    finally:
        turn.client.close()


def test_saved_copy_is_team_visible_but_draft_and_later_rejection_never_activates_it(
        assistant, signing_key, jwk_client):
    h = assistant
    http, agent, human_headers, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    turn = open_turn(h, control, human, "copy-team-not-private")
    try:
        copied = post_copy(turn)
        assert copied.status_code == 200
        receipt = copied.json()
        version = h.artifacts.version(B, A.team, receipt["artifact_id"], receipt["version_id"])
        assert h.artifacts.download(B, A.team, receipt["artifact_id"], receipt["version_id"], version.blob.etag)[1] == BODY
        with pytest.raises(Problem):
            h.artifacts.get(C, A.team, receipt["artifact_id"])
        path = "/teams/" + A.team + "/artifacts/" + receipt["artifact_id"]
        snapshot = http.get(path, headers=agent)
        decision = {"version_id": receipt["version_id"], "decision_id": "a" * 32, "decision": "reject",
                    "sha256": receipt["sha256"], "metadata_sha256": receipt["metadata_sha256"]}
        assert http.post(path + "/decisions", headers={**agent, "If-Match": snapshot.headers["etag"]},
                         json=decision).status_code == 403
        rejected = http.post(path + "/decisions", headers={**human_headers, "If-Match": snapshot.headers["etag"]}, json=decision)
        assert rejected.status_code == 200 and rejected.json()["effective_version_id"] is None
        assert h.assistant.skills.catalog(A, A.team, (receipt["artifact_id"],)).entries == ()
        before = h.store._guard()
        assert post_copy(turn).json() == receipt and h.store._guard() == before
        assert receipt["state_scope"] == "at-original-copy-write"
    finally:
        turn.client.close()


def test_one_slot_does_not_overwrite_an_existing_approved_target(assistant, signing_key, jwk_client):
    from shared_context.m3_models import CreateArtifact, VersionInput, ApprovalInput
    from shared_context.m3_auth import HumanApprover
    h = assistant
    _, _, _, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    turn = open_turn(h, control, human, "copy-existing-target")
    try:
        target = turn.assignment.task.artifact_id
        created = h.artifacts.create(A, A.team, target, CreateArtifact(kind="skill", title="Original approved skill", source_work=LEGACY_WORK))
        prior = h.artifacts.upload(A, A.team, target, "f" * 32, created.etag,
            VersionInput(filename="original.md", media_type="text/markdown", sha256=hashlib.sha256(b"original").hexdigest()),
            b"original")
        principal = HumanApprover(h.artifacts.approval.approver_oid, A.team)
        h.artifacts.decide(principal, A.team, target, prior.etag,
            ApprovalInput(version_id="f" * 32, decision_id="f" * 32,
                          sha256=hashlib.sha256(b"original").hexdigest(), decision="approve"))
        old = h.artifacts.get(A, A.team, target)
        assert post_copy(turn).status_code == 409
        assert h.artifacts.get(A, A.team, target) == old and old.payload["effective_version_id"] == "f" * 32
    finally:
        turn.client.close()


def test_old_installation_cannot_be_reinterpreted_as_team_copy_write_authority(assistant, signing_key, jwk_client):
    h = assistant
    _, _, _, source = approved_sop(h, signing_key, jwk_client)
    h, control, human, _ = enabled(h)
    profile = AssistantProfile.model_validate({
        **control.policy.profile.model_dump(), "prompt_profile": PROFILE, "sop_copy": copy_scope(source),
    })
    control.policy = control.policy.model_copy(update={"profile": profile})
    before = h.store._guard()
    with pytest.raises(Problem) as denied:
        control.installed()
    assert denied.value.body["error"]["code"] == "WORK_POLICY_SCOPE_CHANGED"
    assert h.store._guard() == before


def test_copy_only_profile_cannot_reuse_metrics_save_to_fill_its_slot(assistant, signing_key, jwk_client):
    h = assistant
    _, _, _, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    turn = open_turn(h, control, human, "copy-not-metrics-writer")
    try:
        before = h.store._guard()
        response = h.client.post(
            f"/behavior/v1/bindings/{turn.binding.binding_id}/assistant/sessions/{turn.assignment.session_id}/save",
            headers={**h.headers("agent"), "x-behavior-assistant-ticket": turn.client.ticket.get_secret_value()},
            json={"request_id": "own-result", "answer": {"item_count": 3, "completed_count": 2, "remaining_count": 1}})
        assert response.status_code == 403 and h.store._guard() == before
        assert post_copy(turn).status_code == 200
    finally:
        turn.client.close()


@pytest.mark.parametrize("field,value", [
    ("job_id", "another-job"), ("artifact_id", "another-artifact"),
    ("version_id", "f" * 32), ("approved", 0),
    ("schema_version", "fa-sop-skill-draft-future"),
    ("state_scope", "current-target-state"),
])
def test_hosted_receipt_rejects_wrong_slot_or_untyped_claims_without_recopy(
        assistant, signing_key, jwk_client, monkeypatch, field, value):
    import httpx
    h = assistant
    _, _, _, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    turn = open_turn(h, control, human, "copy-receipt-mismatch")
    try:
        transport = turn.client.http._transport
        def corrupt(request):
            response = transport.handle_request(request)
            if response.status_code != 200:
                return response
            packet = json.loads(response.content)
            packet[field] = value
            return httpx.Response(200, json=packet)
        monkeypatch.setattr(turn.client.http, "_transport", httpx.MockTransport(corrupt))
        with patch.object(h.artifacts, "create", wraps=h.artifacts.create) as created, \
                patch.object(h.artifacts, "upload", wraps=h.artifacts.upload) as uploaded:
            with pytest.raises(Problem):
                turn.client.copy_sop_draft()
            monkeypatch.setattr(turn.client.http, "_transport", transport)
            result = turn.client.copy_sop_draft()
        assert result.job_id == turn.binding.job_id
        assert created.call_count == uploaded.call_count == 1
    finally:
        turn.client.close()


def test_copy_progress_contract_is_readable_and_has_no_body_or_permission(assistant, signing_key, jwk_client):
    from shared_context.behavior_skill_prompt import assemble_prompt
    from web_test_console.freeform import ConversationProjection
    h = assistant
    _, _, _, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    turn = open_turn(h, control, human, "copy-bridge-contract")
    try:
        assert post_copy(turn).status_code == 200
        settle(turn, ordinary(turn.assignment.sdk_session_id))
        raw = h.client.get("/work-policy/v1/conversations/" + turn.assignment.task.instruction.conversation_id, headers=h.headers())
        projection = ConversationProjection.model_validate_json(raw.content)
        assert projection.can_follow_up and projection.turns[0].prompt_assembly == assemble_prompt(PROFILE).record
        assert "APPROVED_TEAM_SOP_CANARY" not in raw.text
        bad = raw.json()
        bad["turns"][0]["prompt_assembly"]["prefix_sha256"] = "0" * 64
        with pytest.raises(ValueError):
            ConversationProjection.model_validate_json(json.dumps(bad))
    finally:
        turn.client.close()


@pytest.mark.parametrize("stage", ["reserve", "consume", "egress"])
def test_before_copy_source_change_fences_model_send_and_keeps_original_intent(
        assistant, signing_key, jwk_client, stage):
    import httpx
    from shared_context.behavior_assistant_api import create_assistant_app
    from shared_context.behavior_assistant_gateway import AssistantProvider
    h = assistant
    source_http, agent, human_headers, source = approved_sop(h, signing_key, jwk_client)
    h, control, human = configured_copy(h, copy_scope(source))
    sent = []
    def credential(_):
        if stage == "egress":
            change_source(source_http, agent, human_headers)
        return SimpleNamespace(token="OFFLINE_COPY_EGRESS")
    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=credential), transport=httpx.MockTransport(lambda request: sent.append(True) or httpx.Response(
            200, content=b'data: {"choices":[{"delta":{"content":"in-flight"}}]}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"})))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    turn = open_turn(h, control, human, "copy-model-source-change")
    try:
        # A reserve after the change is refused; a dispatch reserved before it is inside the seconds-level
        # window: sent once, never resent, and the next reserve is refused.
        def request(label):
            body = {"model": "gpt-4.1-mini", "stream": True, "messages": [{"role": "user", "content": label}]}
            return body, digest(body)
        body, request_sha = request("Copy only the currently approved original SOP.")
        if stage == "reserve":
            change_source(source_http, agent, human_headers)
            refused = request_sha
        else:
            turn.client.reserve_model(request_sha)
            if stage == "consume":
                change_source(source_http, agent, human_headers)
            response = turn.client.model(httpx.Request(
                "POST", "https://context.invalid/model/v1/chat/completions", json=body))
            assert response.status_code == 200 and sent == [True]
            refused = request("A later model step.")[1]
        with pytest.raises(Problem) as denied:
            turn.client.reserve_model(refused)
        assert denied.value.body["error"]["upstream_error_code"] == "SOP_COPY_SOURCE_CHANGED"
        intent_key = key("model-intent", turn.assignment.session_id, request_sha)
        intent = h.store.read_snapshot((intent_key,))[intent_key]
        assert (intent is None) == (stage == "reserve")
        if intent is not None:
            assert intent.payload["data"]["consumed"] is True
            before = h.store._guard()
            with pytest.raises(Problem):
                turn.client.model(httpx.Request("POST", "https://context.invalid/model/v1/chat/completions", json=body))
            assert h.store._guard() == before and h.store.read_snapshot((intent_key,))[intent_key] == intent
        assert len(sent) == (stage != "reserve")
    finally:
        turn.client.close()

