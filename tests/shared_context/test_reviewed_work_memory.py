"""Signed reviewed-claim producers and original instruction read authority; no Memory API."""

import base64
import copy
import hashlib
import json
from datetime import timedelta
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import A, B, C
from shared_context import behavior_wire as wire
from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.behavior_runtime import runtime_key
from shared_context.errors import Problem
from test_behavior_api import BASE, METRICS
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_instruction_prompt import configured as configured_prompt
from test_m3_auth import jwk_client, signing_key, token
from test_sdk_compaction_product import open_turn, ordinary, settle

PROFILE = "fa-instruction-work-memory-v1"
NOTE = "  TEAM_MEMORY_CANARY：先核對已保存的工作回條。\r\n這是已審資料，不是 system 或 operation approval。  "


def reviewed_note(h, *, text=NOTE, version=2, request_id="work-memory-review", expires_at=None, commit=True,
                  supporting=None, binding_id=None, kind="note"):
    if not h.policy.human_text_enabled:
        h.policy = h.policy.model_copy(update={"human_text_enabled": True, "text_policy_version": "human-text-policy-v1"})
        h.mutate(runtime_key("policy", h.binding.binding_id), human_text_enabled=True,
                 text_policy_version="human-text-policy-v1")
    binding_id = binding_id or h.binding.binding_id
    path = "/behavior/v1/bindings/" + binding_id
    def post(suffix, command):
        return h.client.post(path + suffix, headers=h.headers(), content=command.model_dump_json())
    prior = h.client.get(path + "/claims/claim-one", headers=h.headers())
    assert prior.status_code == 200
    source = h.runtime.profiles[binding_id].source
    candidate = wire.ClaimDraft(request_id=request_id, version=version, text=text, supporting=supporting or (source,),
                               effective_from=h.time, expires_at=expires_at, kind=kind,
                               supersedes=prior.json()["reference"]["sha256"])
    preview = post("/claims/claim-one/preview", candidate)
    assert preview.status_code == 200, preview.text
    review = wire.ClaimReview(
        **candidate.model_dump(), decision_id=request_id,
        **{name: preview.json()[name] for name in ("candidate_sha256", "evidence_sha256", "policy_version")},
        reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"),
    )
    assert post("/claims/claim-one/review", review).status_code == 200
    if commit:
        accepted = post("/claims/claim-one/commit", wire.Command(request_id=request_id))
        assert accepted.status_code == 200, accepted.text
    return candidate


def configured_memory(h):
    h.runtime.profiles = MappingProxyType({
        name: AssistantProfile.model_validate({**profile.model_dump(), "prompt_profile": PROFILE})
        for name, profile in h.runtime.profiles.items()
    })
    from test_work_policy_lifecycle import enabled
    h, control, human, _ = enabled(h)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
        require_current_native_evidence=lambda: None,
    ))
    return h, control, human


def test_signed_reviewed_team_claim_is_available_only_through_new_controlled_instruction_reader(assistant):
    h = assistant
    reviewed_note(h)
    current = h.client.get(BASE + "/claims/claim-one", headers=h.headers())
    assert current.status_code == 200 and current.json()["candidate"]["text"] == NOTE
    raw = h.post("/context", wire.ContextRequest(request_id="prove-producer", required_claim_ids=("claim-one",)))
    assert raw.status_code == 200
    assert METRICS in [base64.b64decode(value) for value in raw.json()["originals"].values()]
    h, control, human = configured_memory(h)
    turn = open_turn(h, control, human, "work-memory-first")
    try:
        catalog = turn.client.work_memory_catalog()
        assert catalog.schema_version == "fa-work-memory-catalog-v1"
        assert catalog.source.claim_id == "claim-one" and catalog.source.claim_version == 2
        assert catalog.source.visibility == "team"
        assert NOTE not in catalog.model_dump_json() and "TEAM_MEMORY_CANARY" not in catalog.model_dump_json()
        before = h.store._guard()
        loaded = turn.client.read_work_memory("claim-one")
        assert loaded.text == NOTE
        assert loaded.source == catalog.source
        assert loaded.source.text_sha256 == hashlib.sha256(NOTE.encode("utf-8")).hexdigest()
        assert loaded.source.text_utf8_bytes == len(NOTE.encode("utf-8"))
        assert [value.text.encode("utf-8") for value in loaded.originals] == [METRICS]
        assert "reviewer_id" not in loaded.model_dump_json() and "reviewed_checks" not in loaded.model_dump_json()
        assert h.store._guard() == before and h.endpoint.requests == []
    finally:
        turn.client.close()


@pytest.mark.parametrize("kind", ["profile", "episode"])
def test_assigned_memory_reader_keeps_reviewed_type_and_exact_validity(assistant, kind):
    expiry = assistant.time + timedelta(minutes=10)
    reviewed_note(assistant, kind=kind, expires_at=expiry)
    h, control, human = configured_memory(assistant)
    turn = open_turn(h, control, human, "assigned-typed-reader")
    try:
        catalog = turn.client.work_memory_catalog()
        result = turn.client.read_work_memory("claim-one")
        assert result.source == catalog.source and result.text == NOTE
        assert result.applicability.kind == kind and result.applicability.expires_at == expiry
        assert result.applicability.effective_from == h.time
        assert h.endpoint.requests == []
    finally:
        turn.client.close()


def request(turn, action="read-work-memory", **fields):
    return turn.h.client.post(
        f"/behavior/v1/bindings/{turn.binding.binding_id}/assistant/sessions/{turn.assignment.session_id}/{action}",
        headers={**turn.h.headers("agent"), "x-behavior-assistant-ticket": turn.client.ticket.get_secret_value()},
        json={"request_id": "current-work-memory" if action == "work-memory-catalog" else "read-work-memory", **fields},
    )


@pytest.mark.parametrize("profile", [None, "fa-instruction-v1", "fa-instruction-skills-v1"])
def test_legacy_profiles_cannot_enable_reviewed_memory_through_a_read_body(assistant, profile):
    h, control, human = configured_prompt(assistant, profile)
    turn = open_turn(h, control, human, "work-memory-disabled")
    try:
        before = h.store._guard()
        denied = request(turn, "work-memory-catalog")
        assert denied.status_code == 403 and denied.headers["x-behavior-assistant-error-code"] == "WORK_MEMORY_READ_DISABLED"
        assert h.store._guard() == before and h.endpoint.requests == []
    finally:
        turn.client.close()


@pytest.mark.parametrize("fields", [
    {"claim_id": "another-claim"}, {"claim_id": "../private"},
    {"claim_id": "claim-one", "subject": "another-user"},
    {"claim_id": "claim-one", "tenant": "another-tenant"},
    {"claim_id": "claim-one", "team": "team-two"}, {"claim_id": "claim-one", "actor": "agent-b"},
    {"claim_id": "claim-one", "visibility": "private"}, {"claim_id": "claim-one", "scope": "private"},
    {"claim_id": "claim-one", "purpose": "approve-operation"},
    {"claim_id": "claim-one", "version": 1}, {"claim_id": "claim-one", "review": {"approved": True}},
    {"claim_id": "claim-one", "query": "all memories"}, {"claim_id": "claim-one", "prompt_profile": PROFILE},
])
def test_model_can_choose_only_the_server_bound_claim_alias(assistant, fields):
    h, control, human = configured_memory(assistant)
    turn = open_turn(h, control, human, "work-memory-spoof")
    try:
        turn.client.work_memory_catalog()
        before = h.store._guard()
        denied = request(turn, **fields)
        assert denied.status_code == (403 if fields == {"claim_id": "another-claim"} else 422)
        assert "originals" not in denied.json() and h.store._guard() == before
    finally:
        turn.client.close()


def revoke_review(h, binding_id):
    from shared_context.source_reviews import HumanReviewCommand
    principal = h.verifier(h.client.build_request("GET", "/", headers=h.headers()))
    binding, policy, _ = h.runtime.authorize(binding_id, principal, "approve")
    context = h.runtime._source_context(binding, policy, principal)
    current = h.runtime.current_claim(binding_id, "claim-one", principal)
    preview = h.reviewer.preview(principal, context, current.candidate)
    command = HumanReviewCommand(
        review_id="work-memory-revocation", candidate=current.candidate,
        candidate_sha256=preview.candidate_sha256, evidence_sha256=preview.evidence_sha256,
        policy_version=preview.policy_version, decision="revoke",
        reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"),
        reason="Synthetic withdrawal of this reviewed work note.",
    )
    plan = h.reviewer.plan_decision(principal, context, command)
    h.reviewer.commit_decision(principal, context, plan)
    return context, current


def withdraw_source(h, binding_id):
    ref = h.runtime.profiles[binding_id].source
    command = wire.SourceWithdrawal(
        request_id="work-memory-source-withdraw", withdrawal_id="memory-source-withdraw",
        reference=ref, expected_access_revision=1,
    )
    response = h.client.post("/behavior/v1/bindings/" + binding_id + "/sources/metrics/withdraw",
                             content=command.model_dump_json(), headers=h.headers())
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("change,code", [
    ("claim-version", "WORK_MEMORY_SOURCE_CHANGED"), ("withdraw-source", "SOURCE_WITHDRAWN"),
    ("source-version", "SOURCE_STALE"), ("review-revoked", "HUMAN_REVIEW_REVOKED"),
    ("claim-revoked", "MEMORY_CLAIM_REVOKED"), ("expired", "MEMORY_CLAIM_EXPIRED"),
    ("policy-lost", "BEHAVIOR_ACCESS_DENIED"),
])
def test_loaded_note_cannot_be_reused_after_current_authority_or_exact_version_changes(assistant, change, code):
    reviewed_note(assistant, expires_at=assistant.time + timedelta(minutes=1))
    h, control, human = configured_memory(assistant)
    turn = open_turn(h, control, human, "work-memory-change")
    try:
        catalog = turn.client.work_memory_catalog()
        assert turn.client.read_work_memory("claim-one").text == NOTE
        if change == "claim-version":
            reviewed_note(h, version=3, request_id="same-text-new-version", expires_at=h.time + timedelta(minutes=1),
                          binding_id=turn.binding.binding_id)
        elif change == "withdraw-source":
            withdraw_source(h, turn.binding.binding_id)
        elif change == "source-version":
            base = "/behavior/v1/bindings/" + turn.binding.binding_id
            prepared = h.client.post(base + "/sources/metrics/prepare", headers=h.headers(), content=wire.SourcePrepare(
                request_id="same-bytes-new-source", version=2, original=METRICS).model_dump_json())
            assert prepared.status_code == 200
            assert h.client.post(base + "/sources/metrics/commit", headers=h.headers(),
                content=wire.Command(request_id="same-bytes-new-source").model_dump_json()).status_code == 200
        elif change in {"review-revoked", "claim-revoked"}:
            context, claim = revoke_review(h, turn.binding.binding_id)
            if change == "claim-revoked":
                h.memory.revoke(context, claim.reference, reason="Synthetic withdrawal of this reviewed work note.")
        elif change == "expired":
            h.time += timedelta(minutes=2)
        else:
            h.mutate(runtime_key("policy", turn.binding.binding_id), active=False)
        before = h.store._guard()
        denied = request(turn, claim_id="claim-one")
        assert denied.status_code in {403, 409}
        assert denied.headers.get("x-behavior-assistant-error-code", denied.json().get("error", {}).get("code")) == code
        assert NOTE not in denied.text and "originals" not in denied.json()
        assert h.store._guard() == before and h.endpoint.requests == []
        if change == "claim-version":
            principal = h.verifier(h.client.build_request("GET", "/", headers=h.headers("agent")))
            current = h.runtime.current_claim(turn.binding.binding_id, "claim-one", principal)
            assert current.reference.version == 3 and current.candidate.text == NOTE
            assert current.reference.sha256 != catalog.source.claim_sha256
    finally:
        turn.client.close()


def test_pending_review_is_not_the_committed_current_memory(assistant):
    reviewed_note(assistant, commit=False)
    h, control, human = configured_memory(assistant)
    turn = open_turn(h, control, human, "work-memory-pending")
    try:
        catalog = turn.client.work_memory_catalog()
        assert catalog.source.claim_version == 1
        assert turn.client.read_work_memory("claim-one").text == "The accepted metrics source has been reviewed."
        assert NOTE not in turn.client.read_work_memory("claim-one").model_dump_json()
    finally:
        turn.client.close()


def test_missing_committed_claim_is_explicit_not_empty_or_summary(assistant, monkeypatch):
    from shared_context.source_materials import SourceScope
    from shared_context.behavior_operations import control_key
    scope = SourceScope(tenant_id=assistant.binding.tenant_id, team_id=assistant.binding.team_id, visibility="team")
    missing = control_key("memory-claim", scope.key, assistant.binding.subject, "claim-one", "1")
    h, control, human = configured_memory(assistant)
    turn = open_turn(h, control, human, "work-memory-missing")
    original = h.memory.read_snapshot
    def absent(keys):
        rows = dict(original(keys))
        if missing in rows:
            rows[missing] = None
        return rows
    monkeypatch.setattr(h.memory, "read_snapshot", absent)
    try:
        before = h.store._guard()
        denied = request(turn, "work-memory-catalog")
        assert denied.status_code == 409 and denied.headers["x-behavior-assistant-error-code"] == "SOURCE_REQUIRED_MISSING"
        assert "source" not in denied.json() and h.store._guard() == before
    finally:
        turn.client.close()


def test_private_source_policy_is_not_mapped_to_the_human_or_another_agent(assistant):
    assistant.policy = assistant.policy.model_copy(update={"source_visibility": "private"})
    assistant.mutate(runtime_key("policy", assistant.binding.binding_id), source_visibility="private")
    h, control, human = configured_memory(assistant)
    turn = open_turn(h, control, human, "work-memory-private")
    try:
        denied = request(turn, "work-memory-catalog")
        assert denied.status_code == 403 and denied.headers["x-behavior-assistant-error-code"] == "WORK_MEMORY_SCOPE_DENIED"
        assert "source" not in denied.json() and h.endpoint.requests == []
    finally:
        turn.client.close()


def test_source_context_uses_verified_agent_mapping_and_public_routes_deny_other_identities(assistant, monkeypatch, signing_key):
    h, control, human = configured_memory(assistant)
    turn = open_turn(h, control, human, "work-memory-identity")
    seen = []
    original = h.runtime._source_context
    def actual(binding, policy, principal):
        context = original(binding, policy, principal)
        seen.append((principal.kind, principal.source_actor_id, context.actor_id, context.tenant_id, context.team_id))
        return context
    monkeypatch.setattr(h.runtime, "_source_context", actual)
    try:
        turn.client.work_memory_catalog()
        turn.client.read_work_memory("claim-one")
        assert seen and all(value == ("agent", A.actor, A.actor, turn.binding.tenant_id, A.team) for value in seen)
        path = f"/behavior/v1/bindings/{turn.binding.binding_id}/assistant/sessions/{turn.assignment.session_id}/read-work-memory"
        for headers in (h.headers(), h.headers("old-m3"), {
            "Authorization": "Bearer " + token(signing_key, {"oid": B.oid, "idtyp": "app", "roles": ["Behavior.Agent"]}, remove=("scp",)),
        }, {
            "Authorization": "Bearer " + token(signing_key, {"oid": C.oid, "idtyp": "app", "roles": ["Behavior.Agent"]}, remove=("scp",)),
        }):
            denied = h.client.post(path, headers={**headers, "x-behavior-assistant-ticket": turn.client.ticket.get_secret_value()},
                                   json={"request_id": "read-work-memory", "claim_id": "claim-one"})
            assert denied.status_code == 403 and "originals" not in denied.json()
    finally:
        turn.client.close()


@pytest.mark.parametrize("change", ["source-withdrawal", "text-policy-revoked"])
def test_read_race_denies_bytes_before_return_and_does_not_change_the_captured_reference(assistant, monkeypatch, change):
    reviewed_note(assistant)
    h, control, human = configured_memory(assistant)
    turn = open_turn(h, control, human, "work-memory-race")
    try:
        catalog = turn.client.work_memory_catalog()
        original = h.runtime.context
        calls = []
        def revoke_after_assembled(*args):
            result = original(*args)
            calls.append(True)
            if len(calls) == 3:
                if change == "source-withdrawal":
                    withdraw_source(h, turn.binding.binding_id)
                else:
                    h.mutate(runtime_key("policy", turn.binding.binding_id), human_text_enabled=False, text_policy_version=None)
            return result
        monkeypatch.setattr(h.runtime, "context", revoke_after_assembled)
        denied = request(turn, claim_id="claim-one")
        assert denied.status_code == 409
        assert "originals" not in denied.json() and "TEAM_MEMORY_CANARY" not in denied.text
        from shared_context.behavior_assistant import key
        saved = h.store.read_snapshot((key("session", turn.assignment.session_id),))
        assert saved[key("session", turn.assignment.session_id)].payload["data"]["work_memory_source"] == catalog.source.model_dump(mode="json")
        assert h.endpoint.requests == []
    finally:
        turn.client.close()


@pytest.mark.parametrize("change", ["withdrawal", "same-text-new-claim"])
def test_same_conversation_restores_original_memory_provenance_but_not_revoked_history(assistant, change):
    reviewed_note(assistant)
    h, control, human = configured_memory(assistant)
    first = open_turn(h, control, human, "work-memory-saved")
    second = None
    try:
        reference = first.client.work_memory_catalog().source
        assert first.client.read_work_memory("claim-one").text == NOTE
        state = ordinary(first.assignment.sdk_session_id)
        settle(first, state)
        from shared_context.behavior_assistant import key
        akey = key("text-archive", first.binding.job_id)
        old = h.store.read_snapshot((akey,))[akey]
        second = open_turn(h, control, human, "work-memory-follow", first.assignment.task.instruction.conversation_id)
        assert second.client.restore() == state
        assert second.client.work_memory_catalog().source == reference
        if change == "withdrawal":
            withdraw_source(h, second.binding.binding_id)
        else:
            reviewed_note(h, version=3, request_id="new-claim-after-archive", binding_id=second.binding.binding_id)
        with pytest.raises(Problem) as denied:
            second.client.restore()
        assert denied.value.body["error"]["code"] == (
            "SOURCE_WITHDRAWN" if change == "withdrawal" else "WORK_MEMORY_SOURCE_CHANGED")
        assert h.store.read_snapshot((akey,))[akey] == old and h.endpoint.requests == []
    finally:
        if second is not None:
            second.client.close()
        first.client.close()


@pytest.mark.parametrize("stage", ["reserve", "consume", "egress"])
def test_revoked_memory_never_reaches_provider_or_replays_a_consumed_request(assistant, stage):
    """A reserve after the withdrawal is refused. A dispatch reserved before it is inside the seconds-level
    window: sent once, never resent, and the next reserve is refused."""
    reviewed_note(assistant)
    h, control, human = configured_memory(assistant)
    sent, turn = [], None
    def credential(_):
        assert stage != "reserve"
        if stage == "egress":
            withdraw_source(h, turn.binding.binding_id)
        return SimpleNamespace(token="OFFLINE_MEMORY_PROVIDER")
    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=credential), transport=httpx.MockTransport(lambda req: sent.append(True) or httpx.Response(
            200, content=b'data: {"choices":[{"delta":{"content":"in-flight"}}]}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"})))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    turn = open_turn(h, control, human, "work-memory-egress")
    try:
        turn.client.work_memory_catalog()
        loaded = turn.client.read_work_memory("claim-one")
        def request(label):
            body = {"model": "gpt-4.1-mini", "stream": True, "messages": [
                {"role": "user", "content": label},
                {"role": "tool", "tool_call_id": "memory-read", "content": loaded.model_dump_json()},
            ]}
            return body, hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                                   ensure_ascii=False).encode()).hexdigest()
        body, digest = request("Use the reviewed note.")
        if stage == "reserve":
            withdraw_source(h, turn.binding.binding_id)
            refused = digest
        else:
            turn.client.reserve_model(digest)
            if stage == "consume":
                withdraw_source(h, turn.binding.binding_id)
            response = turn.client.model(httpx.Request(
                "POST", "https://context.invalid/model/v1/chat/completions", json=body))
            assert response.status_code == 200 and sent == [True]
            refused = request("A later model step.")[1]
        before = h.store._guard()
        with pytest.raises(Problem) as denied:
            turn.client.reserve_model(refused)
        assert denied.value.body["error"]["upstream_status"] == 403
        assert denied.value.body["error"]["upstream_error_code"] == "SOURCE_WITHDRAWN"
        assert h.store._guard() == before and len(sent) == (stage != "reserve")
        from shared_context.behavior_assistant import key
        state = h.store.read_snapshot((key("session", turn.assignment.session_id),))[key("session", turn.assignment.session_id)]
        assert state.payload["data"]["model_requests"] == (0 if stage == "reserve" else 1)
        intent_key = key("model-intent", turn.assignment.session_id, digest)
        intent = h.store.read_snapshot((intent_key,))[intent_key]
        assert (intent is None) == (stage == "reserve")
        if intent is not None:
            assert intent.payload["data"]["consumed"] is True
            with pytest.raises(Problem):
                turn.client.model(httpx.Request("POST", "https://context.invalid/model/v1/chat/completions", json=body))
            assert len(sent) == 1
        assert turn.client.status().state == "running"
    finally:
        turn.client.close()


def test_metadata_capture_ack_loss_is_not_silent_success_or_another_write(assistant, monkeypatch):
    h, control, human = configured_memory(assistant)
    turn = open_turn(h, control, human, "work-memory-capture-unknown")
    original = h.assistant._command
    captures = []
    def lose_ack(session, name, *args, **kwargs):
        result = original(session, name, *args, **kwargs)
        if name == "work-memory":
            captures.append(True)
            raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "synthetic acknowledgement loss")
        return result
    monkeypatch.setattr(h.assistant, "_command", lose_ack)
    try:
        with pytest.raises(Problem):
            turn.client.work_memory_catalog()
        assert captures == [True]
        before = h.store._guard()
        current = turn.client.work_memory_catalog()
        assert current.source.claim_version == 1 and captures == [True] and h.store._guard() == before
        assert turn.client.read_work_memory("claim-one").source == current.source
    finally:
        turn.client.close()


def test_all_necessary_sources_are_returned_with_exact_bytes_and_no_projection(assistant):
    h = assistant
    dependency = b'{ "schema_version": "fa-artifact-metrics-v1", "item_count": 5, "completed_count": 1 }\r\n'
    command = wire.SourcePrepare(request_id="memory-dependency", version=1, original=dependency)
    prepared = h.post("/sources/dependency/prepare", command)
    assert prepared.status_code == 200
    assert h.post("/sources/dependency/commit", wire.Command(request_id="memory-dependency")).status_code == 200
    ref = wire.ExactSourceRef(source_id="dependency", version=1, sha256=hashlib.sha256(dependency).hexdigest(),
                              byte_length=len(dependency))
    reviewed_note(h, supporting=(h.runtime.profiles[h.binding.binding_id].source, ref))
    h, control, human = configured_memory(h)
    turn = open_turn(h, control, human, "work-memory-graph")
    try:
        metadata = turn.client.work_memory_catalog().source
        result = turn.client.read_work_memory("claim-one")
        assert [(value.source.source_id, value.text.encode()) for value in result.originals] == [
            ("dependency", dependency), ("metrics", METRICS)]
        assert result.source == metadata and result.source.tokens is None
        assert "summary" not in result.model_dump()
    finally:
        turn.client.close()


@pytest.mark.parametrize("missing", ["tokens", "budget", "source-receipt", "review-receipt"])
def test_incomplete_evidence_or_unknown_tokens_has_no_success_shaped_memory_fallback(assistant, monkeypatch, missing):
    h, control, human = configured_memory(assistant)
    turn = open_turn(h, control, human, "work-memory-unavailable")
    try:
        if missing == "tokens":
            monkeypatch.setattr(h.memory, "_count", lambda _: None)
        elif missing == "budget":
            monkeypatch.setattr(h.memory, "_max_bytes", 1)
        else:
            original = h.sources.verify_receipt
            def unavailable(record, request, target):
                data = record.payload["data"]
                if ((missing == "review-receipt" and "decision_key" in data)
                        or (missing == "source-receipt" and "schema_id" in data)):
                    raise Problem(502, "SOURCE_RECEIPT_MISMATCH", "synthetic original receipt missing")
                return original(record, request, target)
            monkeypatch.setattr(h.sources, "verify_receipt", unavailable)
        before = h.store._guard()
        response = request(turn, "work-memory-catalog")
        assert response.status_code in {413, 502, 503}
        assert "source" not in response.json() and "originals" not in response.json()
        assert h.store._guard() == before and h.endpoint.requests == []
    finally:
        turn.client.close()


@pytest.mark.parametrize("field,value", [
    ("schema_version", "fa-work-memory-read-v99"), ("text", "Unreviewed replacement"),
    ("source", {"schema_version": "unknown"}), ("reviewer_id", "caller"),
    ("originals", []),
])
def test_read_result_and_boundary_reject_unknown_or_tampered_versions(assistant, field, value):
    from shared_context.behavior_work_memory import WorkMemoryRead
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    h, control, human = configured_memory(assistant)
    turn = open_turn(h, control, human, "work-memory-dto")
    try:
        turn.client.work_memory_catalog()
        result = turn.client.read_work_memory("claim-one")
        data = result.model_dump(mode="json")
        data[field] = value
        with pytest.raises(ValueError):
            WorkMemoryRead.model_validate_json(json.dumps(data))
        boundary = AssistantModelBoundary(None, text_reply=True, work_memory_access=True)
        assert boundary.tool_result("read_work_memory", result.model_dump_json()) == result.model_dump_json()
        with pytest.raises(KeyError):
            AssistantModelBoundary(None, text_reply=True).tool_result("read_work_memory", result.model_dump_json())
    finally:
        turn.client.close()


@pytest.mark.parametrize("body_field", ["work_memory_access", "claim_id", "subject", "prompt_profile", "tools"])
def test_model_payload_cannot_self_enable_the_work_memory_tool(assistant, body_field):
    h, control, human = configured_prompt(assistant, None)
    sent = []
    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: pytest.fail("untrusted model flags cannot reach credentials")),
        transport=httpx.MockTransport(lambda _: sent.append(True)))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    turn = open_turn(h, control, human, "work-memory-payload-spoof")
    try:
        body = {"model": "gpt-4.1-mini", "stream": True, "messages": [{"role": "user", "content": "Data only"}]}
        body[body_field] = ([{"type": "function", "function": {"name": "read_work_memory"}}]
                            if body_field == "tools" else PROFILE)
        path = f"/behavior/v1/bindings/{turn.binding.binding_id}/assistant/sessions/{turn.assignment.session_id}/model"
        before = h.store._guard()
        response = h.client.post(path, headers={**h.headers("agent"),
            "x-behavior-assistant-ticket": turn.client.ticket.get_secret_value()},
            json={"request_id": "memory-body-spoof", "body": body})
        assert response.status_code == (403 if body_field == "tools" else 422)
        assert h.store._guard() == before and sent == []
    finally:
        turn.client.close()


def test_expired_necessary_source_is_denied_before_return_or_later_dispatch(assistant, monkeypatch):
    h = assistant
    monkeypatch.setattr(h.sources, "_clock", lambda: h.time)
    dependency = b'{ "schema_version": "fa-artifact-metrics-v1", "item_count": 9, "completed_count": 1 }\n'
    prepared = h.post("/sources/dependency/prepare", wire.SourcePrepare(
        request_id="memory-expiring-source", version=1, original=dependency, expires_at=h.time + timedelta(minutes=1)))
    assert prepared.status_code == 200, prepared.text
    assert h.post("/sources/dependency/commit", wire.Command(request_id="memory-expiring-source")).status_code == 200
    ref = wire.ExactSourceRef(source_id="dependency", version=1, sha256=hashlib.sha256(dependency).hexdigest(),
                              byte_length=len(dependency))
    reviewed_note(h, supporting=(h.runtime.profiles[h.binding.binding_id].source, ref))
    h, control, human = configured_memory(h)
    turn = open_turn(h, control, human, "work-memory-expiry")
    try:
        turn.client.work_memory_catalog()
        assert len(turn.client.read_work_memory("claim-one").originals) == 2
        h.time += timedelta(minutes=2)
        for read in (turn.client.work_memory_catalog, lambda: turn.client.read_work_memory("claim-one")):
            with pytest.raises(Problem) as denied:
                read()
            assert denied.value.body["error"]["code"] == "SOURCE_EXPIRED"
        assert h.endpoint.requests == []
    finally:
        turn.client.close()


def test_unfinished_work_keeps_original_memory_and_does_not_admit_another_user_turn(assistant):
    import zlib
    from dataclasses import replace
    from shared_context.behavior_assistant import key
    from shared_context.behavior_assistant_wire import SdkArchive
    reviewed_note(assistant)
    h, control, human = configured_memory(assistant)
    turn = open_turn(h, control, human, "work-memory-unfinished")
    try:
        captured = turn.client.work_memory_catalog().source
        assert turn.client.read_work_memory("claim-one").text == NOTE
        raw = json.dumps(ordinary(turn.assignment.sdk_session_id).files, sort_keys=True).encode()
        archive = SdkArchive(sdk_session_id=turn.assignment.sdk_session_id,
                             data=zlib.compress(raw), sha256=hashlib.sha256(raw).hexdigest())
        turn.client.stopped("finish_missing")
        checkpoint = turn.client.checkpoint(archive)
        h.executor._complete(turn.start, {"state": "waiting", **checkpoint.model_dump(mode="json")})
        assert h.runtime.controller.step(turn.binding.binding_id).state == "waiting_dependency"
        rows = (key("session", turn.assignment.session_id), key("archive", turn.assignment.session_id))
        before = h.store.read_snapshot(rows)
        starts = []
        h.workers.adapter = replace(h.workers.adapter, start=starts.append)
        with pytest.raises(Problem) as denied:
            h.workers.start(turn.binding.job_id, turn.binding.owner)
        assert denied.value.body["error"]["code"] == "WORK_POLICY_START_FENCED" and starts == []
        follow = h.client.post("/work-policy/v1/submit", headers=h.headers(), json={
            "request_id": "no-memory-retry", "content": "Continue",
            "conversation_id": turn.assignment.task.instruction.conversation_id,
        })
        assert follow.status_code == 409 and follow.json()["error"]["code"] == "CONVERSATION_TURN_UNRESOLVED"
        assert h.store.read_snapshot(rows) == before and h.endpoint.requests == []
        assert before[rows[0]].payload["data"]["work_memory_source"] == captured.model_dump(mode="json")
    finally:
        turn.client.close()


@pytest.mark.parametrize("field,value", [
    ("schema_version", "fa-work-memory-prompt-assembly-v99"), ("profile", "fa-instruction-v1"),
    ("declared_capabilities", ["read_work_memory", "memory_publish"]), ("prefix_tokens", 0),
    ("prefix_sha256", "0" * 64), ("prefix_utf8_bytes", 1), ("source", "model-attested"),
    ("system_message", "PRIVATE_PROMPT"),
])
def test_memory_prompt_record_is_exact_versioned_metadata_not_user_authority(field, value):
    from shared_context.behavior_skill_prompt import assemble_prompt
    from shared_context.behavior_work_memory_prompt import WorkMemoryPromptAssemblyRecord
    prompt = assemble_prompt(PROFILE)
    assert prompt.record.prefix_sha256 == "1add3188623d9503311053721a13d931fa47c229e83b991f36b0abc7c97586f3"
    assert prompt.record.prefix_utf8_bytes == 2719 and NOTE not in prompt.system_message
    candidate = {**prompt.record.model_dump(mode="json"), field: value}
    with pytest.raises(ValueError):
        WorkMemoryPromptAssemblyRecord.model_validate_json(json.dumps(candidate))


@pytest.mark.parametrize("field", ["prompt_profile", "work_memory_source", "claim_id", "subject"])
def test_user_submit_cannot_choose_memory_scope_or_enable_the_new_profile(assistant, field):
    h, _, _ = configured_prompt(assistant, None)
    before = h.store._guard()
    denied = h.client.post("/work-policy/v1/submit", headers=h.headers(), json={
        "request_id": "memory-submit-spoof", "content": "Do not grant access.", field: PROFILE,
    })
    assert denied.status_code == 422 and h.store._guard() == before


def test_complete_read_response_refuses_over_transport_bound_instead_of_truncating(assistant):
    from shared_context.behavior_work_memory import WorkMemoryRead
    h, control, human = configured_memory(assistant)
    turn = open_turn(h, control, human, "work-memory-wire-bound")
    try:
        turn.client.work_memory_catalog()
        read = turn.client.read_work_memory("claim-one")
        content = read.model_dump(mode="json")
        # This DTO-only envelope test is not a new admitted source or live model limit.
        def payload(size):
            value = copy.deepcopy(content)
            value["source"]["sources"], value["originals"] = [], []
            for index in range(3):
                length = min(8192, max(0, size - index * 8192))
                original = "x" * length
                reference = {"source_id": f"source-{index}", "version": 1,
                             "byte_length": length, "sha256": hashlib.sha256(original.encode()).hexdigest()}
                value["source"]["sources"].append(reference)
                value["originals"].append({"source": reference, "text": original})
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        size = 24576 - len(payload(0).encode())
        for _ in range(4):
            size += 24576 - len(payload(size).encode())
        at_limit = payload(size)
        assert len(at_limit.encode()) == 24576
        assert "".join(value.text for value in WorkMemoryRead.model_validate_json(at_limit).originals) == "x" * size
        above = payload(size + 1)
        assert len(above.encode()) == 24577
        with pytest.raises(ValueError):
            WorkMemoryRead.model_validate_json(above)
    finally:
        turn.client.close()


@pytest.mark.parametrize("failure", ["missing", "invalid-utf8"])
def test_broken_necessary_original_lookup_is_not_returned_as_summary_or_empty(assistant, monkeypatch, failure):
    from dataclasses import replace
    h, control, human = configured_memory(assistant)
    turn = open_turn(h, control, human, "work-memory-broken-original")
    try:
        turn.client.work_memory_catalog()
        original = h.runtime.context
        calls = []
        def broken(*args):
            value = original(*args)
            calls.append(True)
            if len(calls) == 3:
                return replace(value, originals=(
                    {} if failure == "missing" else {key: b"\xff" for key in value.originals}))
            return value
        monkeypatch.setattr(h.runtime, "context", broken)
        denied = request(turn, claim_id="claim-one")
        assert denied.status_code == 502
        assert denied.headers["x-behavior-assistant-error-code"] == "WORK_MEMORY_SOURCE_INVALID"
        assert "text" not in denied.json() and "originals" not in denied.json()
    finally:
        turn.client.close()


def test_memory_profile_does_not_apply_the_instruction_tool_to_structured_work(assistant):
    from dataclasses import replace
    from shared_context.behavior_assistant import _LocalHttp
    from shared_context.behavior_assistant_hosted import AssistantClient
    from shared_context.behavior_assistant_wire import AssistantSubmit
    h = assistant
    h.runtime.profiles = MappingProxyType({
        name: AssistantProfile.model_validate({**profile.model_dump(), "prompt_profile": PROFILE})
        for name, profile in h.runtime.profiles.items()
    })
    submitted = h.post("/assistant/submit", AssistantSubmit(request_id="structured-memory-off"))
    assert submitted.status_code == 200
    starts = []
    h.workers.adapter = replace(h.workers.adapter, start=starts.append)
    h.workers.start(h.binding.job_id, h.binding.owner)
    assignment, ticket = h.assistant.issue(starts[0])
    assert assignment.task.instruction is None and "work_memory_source" not in assignment.model_dump_json()
    client = AssistantClient("https://context.invalid", assignment, ticket,
        token_provider=lambda: h.headers("agent")["Authorization"].removeprefix("Bearer "),
        transport=_LocalHttp(h.client))
    try:
        client.acquire()
        result = client.context()
        assert result.item_count == 3 and result.completed_count == 2
        with pytest.raises(Problem) as disabled:
            client.work_memory_catalog()
        assert disabled.value.body["error"]["code"] == "WORK_MEMORY_READ_DISABLED"
        assert h.endpoint.requests == []
    finally:
        client.close()


@pytest.mark.parametrize("corrected", [False, True], ids=["same-source", "changed-source"])
def test_concurrent_catalog_capture_never_returns_a_source_different_from_the_original_receipt(
    assistant, monkeypatch, corrected,
):
    from shared_context import behavior_work_memory_source as source_module
    from shared_context.behavior_assistant import key
    reviewed_note(assistant)
    h, control, human = configured_memory(assistant)
    turn = open_turn(h, control, human, "work-memory-capture-race")
    original = source_module.load
    entered, captured = [], []
    def other_capture_wins(authority, session, principal):
        if not entered:
            entered.append(True)
            captured.append(turn.client.work_memory_catalog().source)
            if corrected:
                reviewed_note(h, version=3, request_id="correction-after-capture", binding_id=turn.binding.binding_id)
        return original(authority, session, principal)
    monkeypatch.setattr(source_module, "load", other_capture_wins)
    try:
        result = request(turn, "work-memory-catalog")
        assert result.status_code == (409 if corrected else 200), result.text
        if corrected:
            assert result.headers["x-behavior-assistant-error-code"] == "BEHAVIOR_REQUEST_CONFLICT"
            assert "source" not in result.json()
        else:
            assert result.json()["source"] == captured[0].model_dump(mode="json")
        skey = key("session", turn.assignment.session_id)
        row = h.store.read_snapshot((skey,))[skey]
        assert row.payload["data"]["work_memory_source"] == captured[0].model_dump(mode="json")
        assert captured[0].claim_version == 2 and h.endpoint.requests == []
    finally:
        turn.client.close()


@pytest.mark.parametrize("refusal,code", [
    ("unassigned", "WORK_MEMORY_SCOPE_DENIED"),
    ("changed", "WORK_MEMORY_SOURCE_CHANGED"),
    ("withdrawn", "SOURCE_WITHDRAWN"),
])
def test_hosted_diagnostics_preserve_real_memory_client_refusal_without_secrets_or_stop(
    assistant, tmp_path, caplog, refusal, code,
):
    from shared_context.behavior_assistant import key
    from shared_context.behavior_assistant_hosted import AssistantSupervisor
    reviewed_note(assistant)
    h, control, human = configured_memory(assistant)
    turn = open_turn(h, control, human, "memory-refusal-diagnostic")
    private = "PRIVATE_MEMORY_REFUSAL_CANARY"
    supervisor = AssistantSupervisor(tmp_path / "diagnostics", runtime_path=tmp_path / "never-started")
    try:
        captured = turn.client.work_memory_catalog().source
        if refusal == "changed":
            reviewed_note(h, version=3, request_id="new-diagnostic-claim", binding_id=turn.binding.binding_id)
        elif refusal == "withdrawn":
            withdraw_source(h, turn.binding.binding_id)
        transport = turn.client.http._transport
        responses = []
        def private_error_response(request):
            response = transport.handle_request(request)
            responses.append((response.status_code, response.headers.get("x-behavior-assistant-error-code")))
            assert request.url.path.endswith("/read-work-memory") and response.status_code == 403
            return httpx.Response(response.status_code, content=private.encode(), headers={
                **{name: value for name, value in response.headers.items() if name.lower() != "content-length"},
                "x-private-error": private,
            })
        turn.client.http._transport = httpx.MockTransport(private_error_response)
        before = h.store._guard()
        with pytest.raises(Problem) as denied:
            turn.client.read_work_memory("another-claim" if refusal == "unassigned" else "claim-one")
        assert responses == [(403, code)]
        detail = denied.value.body["error"]
        assert detail["code"] == detail["upstream_error_code"] == code
        assert supervisor.record_failure("original-launch", "sdk_run", denied.value)
        assert not supervisor.record_failure("original-launch", "sdk_run", RuntimeError(private))
        with supervisor.journal.connect() as db:
            raw, = db.execute("SELECT body FROM assistant_failures WHERE launch_id='original-launch'").fetchone()
            assert db.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
        saved = json.loads(raw)
        assert saved["code"] == saved["upstream_error_code"] == code
        assert saved["phase"] == "sdk_run" and saved["upstream_status"] == 403
        assert saved["upstream_phase"] == "response_status"
        assert saved["upstream_error_phase"] == "assistant_authority"
        assert saved["exception_type"] == "Problem"
        assert not {"sdk_stop", "child_pid", "entire_tree_idle", "sdk_no_start"} & saved.keys()
        assert private not in raw + caplog.text + json.dumps(detail)
        skey = key("session", turn.assignment.session_id)
        session = h.store.read_snapshot((skey,))[skey]
        assert session.payload["data"]["state"] == "running"
        assert session.payload["data"]["work_memory_source"] == captured.model_dump(mode="json")
        assert session.payload["data"]["model_requests"] == 0
        assert h.store._guard() == before and h.endpoint.requests == []
    finally:
        turn.client.close()
