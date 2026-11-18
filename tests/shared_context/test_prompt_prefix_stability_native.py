"""Measure FA-owned prefix bytes on native requests, not provider cache or model quality."""

from datetime import timedelta
import hashlib
import importlib.metadata
import itertools
import json
import os
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory
from types import MappingProxyType, SimpleNamespace
import uuid

import httpx
import pytest
from copilot import CopilotClient
from copilot.session import CopilotSession
from fastapi.testclient import TestClient

from shared_context import m3_store
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_assistant_hosted import AssistantClient
from shared_context.behavior_sdk_state import CheckpointProfile, DIAG_ARCHIVE_CODEC
from shared_context.behavior_skill_copy import SopCopyReceipt, TeamSopCopy
from shared_context.behavior_user_turn import UserTurnRead
from shared_context.behavior_work_memory import WorkMemoryRead
from shared_context.behavior_work_policy import SteadyWorkPolicy, WorkPolicyControl
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from shared_context.m3_skills import PurposeSkillReadResult, ResourceSkillReadResult
from shared_context.m3_models import SkillResourceRead, SkillResources
from shared_context.sdk_pin import RUNTIME_VERSION, SDK_VERSION
from web_test_console.freeform import ConversationProjection
from sdk_checkpoint_local import sse
from legacy_submit_rows_helpers import load_store
from test_approved_skill_api import add_skill, approve_skill_version, configured as configured_skills
from test_behavior_api import METRICS
from test_behavior_assistant import assistant
from test_conversation_routing import conversation_view
from test_instruction_flow import instruction_models
from test_instruction_prompt import configured, saved_turn, submit, submit_legacy
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_reviewed_work_memory import NOTE as MEMORY_NOTE, configured_memory, reviewed_note
from test_skill_purpose import BODY as PURPOSE_BODY
from test_skill_purpose_api import add_purpose_skill, configured_purpose
from test_sop_skill_copy import BODY as COPY_BODY, PURPOSE as COPY_PURPOSE, LEGACY_WORK as COPY_WORK
from test_sop_skill_copy import approved_sop, configured_copy, copy_scope
from test_skill_resources import resource_fixture


def deterministic_ids(monkeypatch):
    """The stored selector tables were written under these ids, so the artifacts built around them must match."""
    counter = itertools.count()
    monkeypatch.setattr(secrets, "token_hex", lambda k=32: ("%064x" % (10**9 + next(counter)))[: 2 * k])
    monkeypatch.setattr(m3_store, "uuid4", lambda: uuid.UUID(int=next(counter)))


@pytest.mark.parametrize("original,replacement", [
    (None, "fa-instruction-v1"),
    ("fa-instruction-v1", "fa-instruction-skills-v1"),
    ("fa-instruction-skills-v1", None),
    ("fa-instruction-v1", "fa-instruction-user-turns-v1"),
    ("fa-instruction-user-turns-v1", None),
    ("fa-instruction-skills-v1", "fa-instruction-skill-purpose-v1"),
    ("fa-instruction-skill-purpose-v1", None),
    ("fa-instruction-v1", "fa-instruction-work-memory-v1"),
    ("fa-instruction-work-memory-v1", None),
    ("fa-instruction-v1", "fa-instruction-sop-copy-v1"),
])
def test_new_server_selector_cannot_silently_attach_an_old_installation(
        assistant, original, replacement, signing_key, jwk_client, request, monkeypatch):
    deterministic_ids(monkeypatch)
    add_skill(assistant)
    sop_copy = None
    if "fa-instruction-sop-copy-v1" in (original, replacement):
        _, _, _, version = approved_sop(assistant, signing_key, jwk_client)
        sop_copy = TeamSopCopy.model_validate(copy_scope(version))
    if original == "fa-instruction-sop-copy-v1":
        h, control, _ = configured_copy(assistant, sop_copy)
    elif original == "fa-instruction-work-memory-v1":
        reviewed_note(assistant)
        h, control, _ = configured_memory(assistant)
    elif original == "fa-instruction-skill-purpose-v1":
        h, control, _ = configured_purpose(assistant)
    elif original == "fa-instruction-skills-v1":
        h, control, _ = configured_skills(assistant)
    else:
        h, control, _ = configured(assistant, original)
    if original == "fa-instruction-sop-copy-v1":
        # Retired profile: its approved-copy artifact is not reproducible, so no stored table can match it.
        receipt, _ = submit(h, "original-profile-request", "Keep the original configured instruction version.")
    else:
        # The one conversation the retiring submit opened for "original-profile-request" under this original profile.
        load_store(h.transport.backing,
                   "selector-" + (original or "none").replace("fa-instruction-", "").replace("-v1", ""))
        control.refresh()
        receipt = {"conversation_id": "07bcd196e1783fd7dc1c09f218cf0a1da37d4b69f0214e8959324a8f91cb932f"}
    before = conversation_view(h, receipt)
    profile = control.policy.profile.model_copy(update={
        "prompt_profile": replacement,
        "skill_artifacts": ("receipt-guide",) if replacement in {
            "fa-instruction-skills-v1", "fa-instruction-skill-purpose-v1",
        } else (),
        "sop_copy": sop_copy if replacement == "fa-instruction-sop-copy-v1" else None,
        "legacy_source_work": COPY_WORK if replacement == "fa-instruction-sop-copy-v1"
                              else control.policy.profile.legacy_source_work,
    })
    desired = SteadyWorkPolicy.model_validate_json(
        control.policy.model_copy(update={"profile": profile}).model_dump_json(),
    )
    restarted = WorkPolicyControl(h.runtime, desired, control.cost_reader)
    with pytest.raises(Problem) as denied:
        restarted.attach()
    assert denied.value.body["error"]["code"] == "WORK_POLICY_SCOPE_CHANGED"
    assert conversation_view(h, receipt) == before
    assert h.endpoint.requests == []
