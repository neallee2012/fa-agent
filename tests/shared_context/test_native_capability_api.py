"""Native C2 (approved skills) and C3 (mcp-api) scope: the verified Agent's own running RID capture only.

Offline: the Agent's native receipt comes from the original T5 relay test transport; MCP list/call are fakes.
"""

import asyncio
import hashlib
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shared_context import mcp_client
from shared_context.behavior_agent_template import TemplateSkill
from shared_context.behavior_api import _install_error_handlers
from shared_context.behavior_capabilities import CapabilityCapture, available_grants
from shared_context.behavior_native_capability_wire import (
    NATIVE_CAPABILITY_PREFIX, NativeCapabilityCall, NativeSkillRead, NativeTurnScope,
)
from shared_context.behavior_native_turn_wire import NativeReceipt, NativeSubmit, NativeTurnCapture
from shared_context.errors import Problem
from shared_context.m3_skills import PurposeSkillCatalog, PurposeSkillEntry, PurposeSkillReadResult
from test_agent_runtime import peer_runtime  # noqa: F401
from test_agent_runtime_routing import agent_headers
from test_capability_calls import CATALOG, KEY, listed, result
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_agent_api import relay_case  # noqa: F401

BODY = "APPROVED_NATIVE_SKILL_CANARY: verify the receipt; guidance never grants an operation.\n"
SKILLS = available_grants(None, ("fa-skills-v1",))["fa-skills-v1"]
WEB = available_grants(CATALOG)["WebIQ-MCP"]


class ApprovedSkills:
    """The ApprovedSkillSource contract (tested in its own module): only the current approved version."""

    def __init__(self):
        self.current = ("1" * 32, BODY)

    def approve(self, version, text):
        self.current = (version, text)

    def _entry(self, team):
        version, text = self.current
        raw = text.encode("utf-8")
        return PurposeSkillEntry(name="receipt-guide", title="Native receipt guide", version_id=version,
                                 sha256=hashlib.sha256(raw).hexdigest(), byte_length=len(raw), team=team,
                                 source_work="legacy-source", source_work_revision=1)

    def catalog(self, actor, team, selected, *, schema_version):
        assert schema_version == "fa-approved-skill-catalog-v2" and actor is not None
        entries = tuple(self._entry(team) for name in selected if name == "receipt-guide")
        return PurposeSkillCatalog(schema_version=schema_version, entries=entries,
                                   unavailable_count=len(selected) - len(entries))

    def read(self, actor, team, selected, expected, *, include_resources=False):
        if include_resources or expected not in self.catalog(
                actor, team, selected, schema_version="fa-approved-skill-catalog-v2").entries:
            raise Problem(403, "SKILL_SOURCE_CHANGED", "original configured approved skill is no longer current")
        return PurposeSkillReadResult(schema_version="fa-approved-skill-read-v2", skill=expected, text=self.current[1])


def started(case, grants, *, skills=(), rid="native-capability-rid", status="started", cancel=False):
    base = case.sender.capture
    refs = tuple(sorted((grant.ref for grant in grants), key=lambda ref: ref.entry_id))
    template = base.template.model_copy(update={
        "skills": skills, "settings": base.template.settings.model_copy(update={"capabilities": refs})})
    capture = NativeTurnCapture.model_validate_json(json.dumps({
        **base.model_dump(mode="json"), "template": template.model_dump(mode="json"),
        "capabilities": None if not grants else CapabilityCapture(
            head_selection_id=None, grants=tuple(sorted(grants, key=lambda grant: grant.entry_id))).model_dump(mode="json")}))
    command = case.sender.command.model_copy(update={"request_id": rid})
    record = NativeReceipt(
        agent_id=case.sender.agent_id, hosted_session_id=case.sender.hosted_session_id, request_id=rid,
        conversation_id=case.sender.conversation_id, command=command, capture=capture,
        submit_digest=NativeSubmit(command=command, capture=capture).sha256, capture_digest=capture.sha256,
        status=status, turn_number=1 if status != "accepted" else None, cancel_requested=cancel)
    case.remotes[0].records[rid] = record
    return NativeTurnScope(conversation_id=record.conversation_id, request_id=rid)


@pytest.fixture
def skilled(relay_case, monkeypatch):
    case = relay_case
    a = case.base.a
    monkeypatch.setattr(a.runtime, "capability_grants", {SKILLS.entry_id: SKILLS, WEB.entry_id: WEB}, raising=False)
    monkeypatch.setattr(a.runtime, "capability_catalog", CATALOG, raising=False)
    authority, binding = a.assistant.authority, a.runtime.work_policy.policy.template
    skills = ApprovedSkills()
    monkeypatch.setattr(authority, "skills", skills)
    catalog = skills.catalog(authority.actors.get(binding.agent_oid), binding.team_id, ("receipt-guide",),
                             schema_version="fa-approved-skill-catalog-v2")
    refs = tuple(TemplateSkill(name=item.name, version_id=item.version_id, sha256=item.sha256,
                              metadata_sha256=item.metadata_sha256) for item in catalog.entries)
    case.skills = skills
    return case, refs


def gateway(case):
    from shared_context.native_capability_api import NativeCapabilityGateway
    return NativeCapabilityGateway(case.base.a, enabled=True)


def code(error):
    return error.value.body["error"]["code"]


def test_native_turn_reads_only_its_captured_approved_skill_version(skilled):
    case, refs = skilled
    native = gateway(case)
    scope = started(case, (SKILLS,), skills=refs)
    before = case.base.a.runtime.store._guard()
    catalog = native.skill_catalog(case.principal, scope)
    assert [entry.name for entry in catalog.entries] == ["receipt-guide"] and BODY not in catalog.model_dump_json()
    read = native.read_skill(case.principal, NativeSkillRead(**scope.model_dump(), name="receipt-guide"))
    assert read.text == BODY and read.skill == catalog.entries[0]
    with pytest.raises(Problem) as missing:
        native.read_skill(case.principal, NativeSkillRead(**scope.model_dump(), name="not-selected"))
    assert code(missing) == "SKILL_SOURCE_DENIED"
    # A new approved version is never substituted for the captured one, also after a cold resume.
    case.skills.approve("2" * 32, "A later approved body.\n")
    for call in (lambda: native.skill_catalog(case.principal, scope),
                 lambda: native.read_skill(case.principal, NativeSkillRead(**scope.model_dump(), name="receipt-guide"))):
        with pytest.raises(Problem) as changed:
            call()
        assert code(changed) == "SKILL_SOURCE_CHANGED"
    assert case.base.a.runtime.store._guard() == before
    assert case.base.a.runtime.work_policy.records == {}


def test_skills_need_the_captured_grant_and_the_original_running_turn(skilled):
    case, refs = skilled
    native = gateway(case)
    with pytest.raises(Problem) as disabled:
        native.skill_catalog(case.principal, started(case, (), skills=refs, rid="no-skill-grant"))
    assert code(disabled) == "SKILL_ACCESS_DISABLED"
    for rid, values in (("finished-rid", {"status": "unknown"}), ("cancelled-rid", {"cancel": True})):
        with pytest.raises(Problem) as stopped:
            native.skill_catalog(case.principal, started(case, (SKILLS,), skills=refs, rid=rid, **values))
        assert code(stopped) == "NATIVE_CAPABILITY_SCOPE"
    scope = started(case, (SKILLS,), skills=refs)
    for wrong in (scope.model_copy(update={"conversation_id": "another-cid"}),
                  scope.model_copy(update={"request_id": "missing-rid"})):
        with pytest.raises(Problem) as denied:
            native.skill_catalog(case.principal, wrong)
        assert code(denied) == "NATIVE_CAPABILITY_SCOPE"


def call(scope, tool_call_id="call-1", tool="WebIQ-MCP-web", query="fa"):
    return NativeCapabilityCall(**scope.model_dump(), tool_call_id=tool_call_id, tool=tool,
                                arguments={"query": query}, input_schema_sha256="1" * 64)


def test_native_mcp_api_uses_the_existing_broker_once_per_agent_rid_and_tool_call(skilled, monkeypatch, caplog):
    case, _ = skilled
    native = gateway(case)
    calls, lookups = [], []

    async def fake_list(entry, lookup, **_):
        lookups.append(lookup("webiq-api-key"))
        return listed()

    async def fake_call(entry, lookup, tool, arguments, *, expected_schema_sha256=None, **_):
        calls.append((tool, arguments, expected_schema_sha256))
        return result()

    def run_without_loop(async_call):
        # The startup fixture forbids sockets, and anyio.run opens a socket pair; the fakes never await.
        coroutine = async_call()
        try:
            coroutine.send(None)
        except StopIteration as done:
            return done.value
        raise AssertionError("the offline MCP fake must not await")
    from shared_context import behavior_capability_calls
    monkeypatch.setattr(behavior_capability_calls, "_run", run_without_loop)
    monkeypatch.setenv("FA_CAPABILITY_SECRET_WEBIQ_API_KEY", KEY)
    monkeypatch.setattr(mcp_client, "list_tools", fake_list)
    monkeypatch.setattr(mcp_client, "call_tool", fake_call)
    scope = started(case, (WEB, SKILLS))
    assert [item.entry_id for item in native.capability_catalog(case.principal, scope).entries] == ["WebIQ-MCP"]
    assert lookups == [KEY]
    with caplog.at_level(logging.WARNING, logger="shared_context.behavior_capability_calls"):
        first = native.capability_call(case.principal, call(scope))
        again = native.capability_call(case.principal, call(scope))
    assert first == again == result() and calls == [("web", {"query": "fa"}, "1" * 64)]
    with pytest.raises(Problem) as conflict:
        native.capability_call(case.principal, call(scope, query="other"))
    assert code(conflict) == "CAPABILITY_CALL_CONFLICT" and len(calls) == 1
    other = started(case, (WEB,), rid="another-native-rid")
    native.capability_call(case.principal, call(other))
    assert len(calls) == 2
    with pytest.raises(Problem) as denied:
        native.capability_call(case.principal, call(scope, "call-2", tool="WebIQ-MCP-images"))
    assert code(denied) == "CAPABILITY_TOOL_DENIED"
    without = started(case, (SKILLS,), rid="no-web-grant")
    assert native.capability_catalog(case.principal, without).entries == ()
    with pytest.raises(Problem) as missing:
        native.capability_call(case.principal, call(without))
    assert code(missing) == "CAPABILITY_TOOL_DENIED"
    with pytest.raises(Problem) as stopped:
        native.capability_call(case.principal, call(started(case, (WEB,), rid="cancelled-web", cancel=True)))
    assert code(stopped) == "NATIVE_CAPABILITY_SCOPE" and len(calls) == 2
    logged = [json.loads(r.getMessage().split(" ", 1)[1]) for r in caplog.records if "FA_CAPABILITY_CALL" in r.getMessage()]
    assert logged[0] == {"agent_id": case.sender.agent_id, "capability": "WebIQ-MCP",
                         "counts_against_shared_budget": False, "outcome": "ok", "request_id": scope.request_id,
                         "tool": "web", "tool_call_id": "call-1"}
    assert KEY not in caplog.text and KEY not in first.model_dump_json()


@pytest.fixture
def skilled_http(request, signing_key):
    # Event loops first: the startup fixture forbids the socket pair a new loop opens.
    loops = [asyncio.new_event_loop() for _ in range(2)]
    try:
        yield request.getfixturevalue("skilled"), loops
    finally:
        for loop in loops:
            if not loop.is_closed():
                loop.close()


def test_signed_routes_accept_only_the_verified_agent_and_strict_bodies(skilled_http, signing_key):
    from shared_context.native_capability_api import install_native_capability_routes
    (case, refs), (loop, other_loop) = skilled_http
    scope = started(case, (SKILLS,), skills=refs)
    app = FastAPI()
    _install_error_handlers(app)
    install_native_capability_routes(app, case.base.a, enabled=True)
    agent, human = agent_headers(case.base.c, signing_key, 0), case.base.c.h.headers(signing_key)
    body = scope.model_dump(mode="json")
    with TestClient(app, backend_options={"loop_factory": lambda: loop}) as client:
        read = client.post(NATIVE_CAPABILITY_PREFIX + "/read-skill", json={**body, "name": "receipt-guide"},
                           headers=agent)
        assert read.status_code == 200, read.text
        assert read.json()["text"] == BODY and read.headers["cache-control"] == "no-store"
        assert client.post(NATIVE_CAPABILITY_PREFIX + "/skill-catalog", json=body, headers=human).status_code == 403
        assert client.post(NATIVE_CAPABILITY_PREFIX + "/skill-catalog",
                           json={**body, "agent_oid": "self-asserted"}, headers=agent).status_code == 422
    disabled = FastAPI()
    _install_error_handlers(disabled)
    install_native_capability_routes(disabled, case.base.a, enabled=False)
    with TestClient(disabled, backend_options={"loop_factory": lambda: other_loop}) as client:
        assert client.post(NATIVE_CAPABILITY_PREFIX + "/skill-catalog", json=body, headers=agent).status_code == 503
