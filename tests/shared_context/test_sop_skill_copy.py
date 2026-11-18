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
from shared_context.behavior_assistant import AssistantProfile, key
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.m3_auth import HumanApprover
from shared_context.m3_models import ApprovalInput, CreateArtifact, SkillPurpose, VersionInput
from shared_context.models import CreateWork
from shared_context.behavior_operations import control_key
from shared_context.errors import Problem
from shared_context.source_materials import digest
from assistant_api_helpers import assistant_api
from instruction_flow_helpers import instruction_inventory, instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key, token
from sdk_compaction_product_helpers import ordinary, settle
from work_policy_lifecycle_helpers import enabled
from work_cost_policy_helpers import standing, install_offline
from shared_context.behavior_work_policy import WorkPolicyControl, work_policy_key
from sop_skill_copy_helpers import (
    copy_scope,
    PURPOSE,
    SOP,
    approved_sop,
    procedure,
    BODY,
    SOURCE_VERSION,
    LEGACY_WORK,
)

PROFILE = "fa-instruction-sop-copy-v1"


def configured_copy(h, scope):
    h.runtime.profiles = MappingProxyType({
        name: AssistantProfile.model_validate({
            **profile.model_dump(), "prompt_profile": PROFILE, "sop_copy": scope, "legacy_source_work": LEGACY_WORK,
        }) for name, profile in h.runtime.profiles.items()
    })
    _, policy, observation, _, _ = standing(h)
    human = h.verifier(h.client.build_request("GET", "/", headers=h.headers()))
    control = WorkPolicyControl(h.runtime, policy, lambda: observation)
    install_offline(control, human, "signed-copy-scope-installation", h.time + timedelta(minutes=10))
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


def change_source(artifacts, agent, human, *, body=BODY, approve=True):
    current = artifacts.get(agent, A.team, SOP)
    uploaded = artifacts.upload(agent, A.team, SOP, "d" * 32, current.etag, procedure(body), body)
    artifacts.decide(human, A.team, SOP, uploaded.etag, ApprovalInput(
        decision_id="d" * 32, version_id="d" * 32,
        sha256=hashlib.sha256(body).hexdigest(), decision="approve" if approve else "reject"))


def test_old_installation_cannot_be_reinterpreted_as_team_copy_write_authority(assistant_api, signing_key, jwk_client):
    h = assistant_api
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
