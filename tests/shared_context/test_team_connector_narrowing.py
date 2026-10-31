"""Team connectors captured per conversation and narrowed per Agent; a fake Team connector service, no cloud I/O."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shared_context.behavior_agent_registry import register_agent_scope
from shared_context.behavior_api import _install_error_handlers
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_native_capture import resolve_capture, system_prompt_inputs
from shared_context.behavior_native_turn_wire import NativeConversation, NativeTurnCapture
from shared_context.behavior_work_policy import WorkPolicyControl
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.connector_registry import TEAMS
from shared_context.native_turns import NativeTurnRuntime
from shared_context.team_connector_narrowing import save_narrowing
from shared_context.team_connector_wire import (
    TEAM_CONNECTORS_ENABLED_ENV, SelectAgentConnectors, connector_entry_id,
)
from shared_context.team_mcp_wire import TeamMcpServer, toolbox_grant
from test_agent_scope_registry import peer_policy
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401

ROUTE = "/work-policy/v1/agent-connectors"
ON_V1 = {"connector-teams_read": "v1", "connector-teams_write": "v1"}


class FakeTeamConnectors:
    """The Team connector service as turn captures read it: Teams at one toolbox version, or not enabled."""

    def __init__(self):
        self.version = None

    def team_grants(self, tenant, team):
        if self.version is None:
            return ()
        return tuple(toolbox_grant(connector_entry_id(entry.server_label), "fa-connector-team-one-teams", TeamMcpServer(
            server_label=entry.server_label, server_url=TEAMS.server_url, allowed_tools=entry.allowed_tools,
            project_connection_id="fa-team-one-teams", require_approval=entry.default_approval, version=self.version))
            for entry in TEAMS.entries)


@pytest.fixture
def team(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    service = FakeTeamConnectors()
    monkeypatch.setattr(c.h.runtime, "team_connectors", service, raising=False)
    policy = peer_policy(c.h.config, c.control.policy)
    # A peer Agent has no server default prompt; give it the one Agent A starts new conversations with.
    policy = policy.model_copy(update={"profile": policy.profile.model_copy(update={"prompt_profile": "fa-instruction-v2"})})
    register_agent_scope(c.h.runtime.store, c.h.config, policy, c.human, now=c.h.time)
    return SimpleNamespace(c=c, service=service, a=c.control, b=WorkPolicyControl(c.h.runtime, policy, c.control.cost_reader))


def first_turn(team, control, rid):
    return resolve_capture(control, team.c.authority, team.c.human, InstructionSubmit(request_id=rid, content="Hello"))


def next_turn(team, control, first, rid):
    conversation = NativeConversation(
        agent_id="agent", hosted_session_id="session", conversation_id="cid-" + rid, last_turn=1,
        last_request_id="first-" + rid, capture=first, capture_digest=first.sha256)
    return resolve_capture(control, team.c.authority, team.c.human, InstructionSubmit(
        request_id=rid, conversation_id=conversation.conversation_id, content="Next"), conversation=conversation)


def connectors(capture):
    return None if capture.connectors is None else {
        grant.entry_id: grant.toolbox.version for grant in capture.connectors.grants}


def narrow(team, control, rid, disabled, expected):
    return save_narrowing(control, team.c.human, SelectAgentConnectors(
        schema_version="fa-agent-connectors-command-v1", request_id=rid, expected_revision=expected,
        disabled_connector_ids=disabled))


def test_a_team_connector_is_on_for_every_agent_without_a_capability_slot(team):
    team.service.version = "v1"
    a, b = first_turn(team, team.a, "a-new"), first_turn(team, team.b, "b-new")
    assert connectors(a) == connectors(b) == ON_V1
    assert a.capabilities is None
    assert [grant.entry_id for grant in a.tools.grants] == ["connector-teams_read", "connector-teams_write"]
    assert a.tools.grants[0].tools[:2] == ("connector-teams_read-fetch", "connector-teams_read-fetch_blob")
    _, _, preview, _ = system_prompt_inputs(team.a, team.c.authority, team.c.human)
    assert [grant.entry_id for grant in preview.grants] == ["connector-teams_read", "connector-teams_write"]


def test_turning_a_connector_off_for_one_agent_changes_only_its_new_conversations(team):
    team.service.version = "v1"
    before = first_turn(team, team.a, "a-before")
    assert narrow(team, team.a, "a-off", ("teams",), 0) == {
        "schema_version": "fa-agent-connectors-v1", "revision": 1, "disabled_connector_ids": ["teams"],
        "applies_to": "new-conversations-only"}
    assert connectors(first_turn(team, team.a, "a-after")) is None
    assert connectors(first_turn(team, team.b, "b-after")) == ON_V1
    assert connectors(next_turn(team, team.a, before, "a-before-2")) == ON_V1
    assert narrow(team, team.a, "a-on", (), 1)["revision"] == 2
    assert connectors(first_turn(team, team.a, "a-again")) == ON_V1


def test_a_team_disable_reaches_every_conversation_on_its_next_turn_and_reenable_returns_within_it(team):
    team.service.version = "v1"
    a, b = first_turn(team, team.a, "a-1"), first_turn(team, team.b, "b-1")
    team.service.version = None
    assert connectors(next_turn(team, team.a, a, "a-2")) is None
    assert connectors(next_turn(team, team.b, b, "b-2")) is None
    team.service.version = "v3"
    assert connectors(next_turn(team, team.a, a, "a-3")) == {
        "connector-teams_read": "v3", "connector-teams_write": "v3"}
    assert connectors(first_turn(team, team.b, "b-new")) == {
        "connector-teams_read": "v3", "connector-teams_write": "v3"}


@pytest.fixture
def loop():
    # Created before the startup fixture forbids sockets; the Windows event loop opens a socket pair.
    value = asyncio.new_event_loop()
    yield value
    value.close()


def test_a_conversation_never_gains_a_connector_the_team_adds_later(loop, team):
    old = first_turn(team, team.a, "a-old")
    assert connectors(old) is None
    team.service.version = "v1"
    assert connectors(next_turn(team, team.a, old, "a-old-2")) is None
    new = first_turn(team, team.a, "a-new")
    assert connectors(new) == ON_V1

    def fits(conversation, turn):
        return loop.run_until_complete(NativeTurnRuntime._capture_fits(
            None, SimpleNamespace(capture=conversation, base_ckpt=None, rid=None), turn))

    assert fits(old, new) is False, "Hosted refuses a connector its conversation did not start with"
    assert fits(new, old) is True


def test_a_captured_connector_survives_the_history_reader_and_old_captures_keep_their_digest(team):
    old = first_turn(team, team.a, "a-plain")
    team.service.version = "v1"
    new = first_turn(team, team.a, "a-connected")
    assert "connectors" not in json.loads(old.model_dump_json())
    rebuilt = NativeTurnCapture.model_validate_json(new.model_dump_json())
    assert (connectors(rebuilt), rebuilt.sha256) == (ON_V1, new.sha256)


@pytest.fixture
def api(loop, team, signing_key, monkeypatch):
    monkeypatch.setenv(TEAM_CONNECTORS_ENABLED_ENV, "1")
    app = FastAPI()
    _install_error_handlers(app)
    install_work_policy_routes(app, team.c.h.composition)
    with TestClient(app, backend_options={"loop_factory": lambda: loop}) as client:
        yield SimpleNamespace(client=client, owner=team.c.h.headers(signing_key, "Behavior.Read Behavior.Approve"),
                              reader=team.c.h.headers(signing_key, "Behavior.Read"), store=team.c.h.runtime.store)


def body(rid, disabled, expected):
    return {"schema_version": "fa-agent-connectors-command-v1", "request_id": rid,
            "expected_revision": expected, "disabled_connector_ids": list(disabled)}


def test_the_agent_settings_api_saves_one_owner_choice_per_request_with_compare_and_set(api):
    def post(payload, headers=api.owner):
        response = api.client.post(ROUTE, json=payload, headers=headers)
        return response.status_code, response.json()

    def current():
        response = api.client.get(ROUTE, headers=api.reader)
        assert response.status_code == 200, response.text
        return response.json()

    assert current() == {"schema_version": "fa-agent-connectors-v1", "revision": 0, "disabled_connector_ids": [],
                         "applies_to": "new-conversations-only"}
    before = api.store._guard()
    assert post(body("read-only", ("teams",), 0), api.reader)[0] == 403
    status, refused = post(body("unknown", ("github",), 0))
    assert (status, refused["error"]["code"]) == (422, "AGENT_CONNECTOR_UNKNOWN")
    assert api.store._guard() == before

    saved = post(body("off", ("teams",), 0))
    assert saved == (200, {"schema_version": "fa-agent-connectors-v1", "revision": 1,
                           "disabled_connector_ids": ["teams"], "applies_to": "new-conversations-only"})
    after = api.store._guard()
    assert post(body("off", ("teams",), 0)) == saved
    assert api.store._guard() == after
    status, stale = post(body("off-again", ("teams",), 0))
    assert (status, stale["error"]["code"]) == (409, "AGENT_CONNECTORS_CHANGED")
    status, reused = post(body("off", (), 1))
    assert (status, reused["error"]["code"]) == (409, "AGENT_CONNECTORS_CONFLICT")
    assert current()["revision"] == 1 and current()["disabled_connector_ids"] == ["teams"]


def test_saved_choices_pass_the_real_startup_and_migration_readers(team):
    from shared_context.behavior_migration import MigrationStore, installed_origin
    from test_conversation_transfer_startup import read_startup
    from test_resident_worker_poc import outcome_of

    c, config = team.c, team.c.h.config

    def readers():
        store = MigrationStore(c.h.composition.clients[0], config.table.scope)
        return (outcome_of(lambda: read_startup(c)), outcome_of(lambda: installed_origin(store, config)))

    baseline = readers()
    assert baseline[0] == "ok"
    narrow(team, team.a, "startup-off", ("teams",), 0)
    narrow(team, team.a, "startup-on", (), 1)
    narrow(team, team.b, "startup-b-off", ("teams",), 0)
    assert readers() == baseline


def test_a_turn_with_twelve_selected_capabilities_still_carries_its_connectors_to_hosted():
    from shared_context.behavior_assistant_wire import InstructionTask
    from shared_context.behavior_capabilities import MAX_CAPABILITIES, CapabilityCapture, turn_grants

    def grant(entry_id, toolbox, label):
        server = TeamMcpServer(server_label=label, server_url="https://example.test/mcp", allowed_tools=("read",),
                               require_approval="never", version="1")
        return toolbox_grant(entry_id, toolbox, server)

    selected = CapabilityCapture(head_selection_id=None, grants=tuple(
        grant(f"team-s{index:02d}", "fa-team-team-one", f"s{index:02d}") for index in range(MAX_CAPABILITIES)))
    connector = tuple(grant(connector_entry_id(label), "fa-connector-team-one-teams", label)
                      for label in ("teams_read", "teams_write"))
    tools = turn_grants(selected, connector)
    task = InstructionTask(content="hi", conversation_id="c1", turn_number=1, capabilities=tools,
                           prompt_profile="fa-instruction-v4")
    assert len(task.capabilities.grants) == MAX_CAPABILITIES + 2
    assert [item.entry_id for item in task.capabilities.grants][:2] == ["connector-teams_read", "connector-teams_write"]
    assert type(task.capabilities).__name__ == "TurnGrants"
    assert InstructionTask.model_validate_json(task.model_dump_json()) == task


def test_a_selected_capture_reads_back_as_the_same_capture_and_an_in_memory_turn_grants_stays_one():
    from shared_context.behavior_assistant_wire import InstructionTask
    from shared_context.behavior_capabilities import CapabilityCapture, turn_grants

    def grant(entry_id, toolbox, label):
        server = TeamMcpServer(server_label=label, server_url="https://example.test/mcp", allowed_tools=("read",),
                               require_approval="never", version="1")
        return toolbox_grant(entry_id, toolbox, server)

    selected = CapabilityCapture(head_selection_id="sel-1", grants=(grant("team-s00", "fa-team-team-one", "s00"),))
    task = InstructionTask(content="hi", conversation_id="c1", turn_number=1, capabilities=selected,
                           prompt_profile="fa-instruction-v4")
    again = InstructionTask.model_validate_json(task.model_dump_json())
    assert again == task and type(again.capabilities).__name__ == "CapabilityCapture"
    both = turn_grants(selected, (grant(connector_entry_id("teams_read"), "fa-connector-team-one-teams", "teams_read"),))
    kept = InstructionTask(content="hi", conversation_id="c1", turn_number=1, capabilities=both,
                           prompt_profile="fa-instruction-v4")
    assert kept.capabilities is both and type(both).__name__ == "TurnGrants"
