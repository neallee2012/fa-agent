"""CAP-06 PR4: an active Team owner's POST creates one Hosted agent and registers it, resumable by request ID."""

import asyncio
import json
import logging
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import shared_context.behavior_agent_onboarding as onboarding
from shared_context.behavior_agent_message_api import public_agents
from shared_context.behavior_agent_onboarding import (
    CreateTeamAgent, FoundryCallError, TeamAgentOnboarding, console_connection, derived_names, name_key, onboard,
    onboarding_key, onboarding_status, pending_key,
)
from shared_context.behavior_agent_role_prompt import ROLE_PROMPT_MAX_BYTES
from shared_context.behavior_agent_role_prompt_versions import current_role_prompt
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_code_identity import code_sha256
from shared_context.behavior_team_registry import TeamRegistry, registered_agent_ids
from shared_context.behavior_work_target import instruction_hosted_version
import shared_context.behavior_agent_versions as onboarding_versions
from shared_context.errors import Problem
import test_conversation_transfer_startup as startup
from test_agent_registry_operator import OperatorTableTransport
from test_conversation_transfer_startup import read_startup, synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_team_registry import OTHER_AGENT_OID

PROBES = {"FA_MODEL_DIRECT_PROBE_URL": "https://probe", "FA_MODEL_DIRECT_PROBE_MODEL": "probe-model"}


class FakeFoundry:
    """In-memory agent versions. ``creating`` versions become active after ``ready_after`` reads."""

    def __init__(self, a_target, *, ready_after=0):
        self.a_name = a_target.agent_name
        self.versions = {a_target.agent_name: [{
            "name": a_target.agent_name, "version": a_target.agent_version, "status": "active",
            "definition": {"kind": "hosted", "image": "fa-a:build", "environment_variables": {
                "KEEP": "1", **PROBES}}, "metadata": {},
        }]}
        self.ready_after, self.calls, self.fail_create, self.fail_list = ready_after, [], [], []
        self.final_status = "active"

    def _reads(self, name):
        for item in self.versions.get(name, []):
            if item["status"] == "creating":
                item["_reads"] = item.get("_reads", 0) + 1
                if item["_reads"] > self.ready_after:
                    item["status"] = self.final_status
        return [{k: v for k, v in item.items() if k != "_reads"} for item in self.versions.get(name, [])]

    def get_version(self, name, version):
        self.calls.append(("get", name, version))
        found = [item for item in self._reads(name) if item["version"] == version]
        if not found and name == self.a_name:
            return {**self.versions[name][0], "version": version}
        return found[0]

    def list_versions(self, name):
        self.calls.append(("list", name))
        if self.fail_list:
            raise FoundryCallError(self.fail_list.pop(0))
        if name not in self.versions:
            raise FoundryCallError(404)
        return self._reads(name)

    def create_version(self, name, *, definition, metadata):
        self.calls.append(("create", name, json.loads(json.dumps(definition)), dict(metadata)))
        mode = self.fail_create.pop(0) if self.fail_create else None
        if isinstance(mode, int):
            raise FoundryCallError(mode)
        items = self.versions.setdefault(name, [])
        item = {"name": name, "version": str(len(items) + 1), "status": "creating", "definition": definition,
                "metadata": dict(metadata), "agent_guid": str(uuid.uuid4()).upper(),
                "instance_identity": {"principal_id": str(uuid.uuid4()), "client_id": str(uuid.uuid4())}}
        items.append(item)
        if mode == "unknown-after-commit":
            raise FoundryCallError(None)
        return dict(item)

    def creates(self):
        return [call for call in self.calls if call[0] == "create"]

    def get_agent(self, name):
        self.calls.append(("get_agent", name))
        return {"name": name, "agent_endpoint": {"protocol_configuration": dict(
            getattr(self, "endpoints", {}).get(name, {"responses": {}}))}}

    def update_route(self, name, endpoint):
        self.calls.append(("patch", name, json.loads(json.dumps(endpoint))))
        self.endpoints = {**getattr(self, "endpoints", {}), name: endpoint["protocol_configuration"]}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def wall(self):
        return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=self.now)


@pytest.fixture
def team(request, monkeypatch, signing_key):
    monkeypatch.setattr(startup, "StartupTableTransport", OperatorTableTransport)
    monkeypatch.delenv("FA_INSTRUCTION_HOSTED_VERSIONS", raising=False)
    monkeypatch.delenv("FA_INSTRUCTION_HOSTED_VERSION", raising=False)
    monkeypatch.delenv("FA_ONBOARDING_ENDPOINT_PROTOCOLS", raising=False)
    monkeypatch.delenv("FA_ONBOARDING_ROLE_SCOPE", raising=False)
    loop = asyncio.new_event_loop()
    try:
        c = request.getfixturevalue("synthetic_standing_v1")
        a, config = c.h.composition, c.h.config
        ids = config.identity
        teams = TeamRegistry(a.runtime.store, tenant_id=ids.tenant_id, operator_oid=ids.human_oid,
                             legacy_team_id=ids.team_id, legacy_owner_oid=ids.human_oid,
                             agent_oids=tuple(agent.oid for agent in ids.agents), targets=config.targets)
        if getattr(a.verifier, "teams", None) is None:
            monkeypatch.setattr(a.verifier, "teams", teams, raising=False)
        transports = next(cell.cell_contents for cell in a._peer_factory.__closure__
                          if isinstance(cell.cell_contents, dict) and config.targets[0].executor_id in cell.cell_contents)
        foundry = FakeFoundry(config.targets[0])
        clock = Clock()
        case = SimpleNamespace(c=c, a=a, config=config, teams=a.verifier.teams, foundry=foundry, clock=clock,
                               transports=transports, loop=loop, signing_key=signing_key)

        def run(rid="onboard-one", name="Researcher", text="You research things.", principal=None, selection=None,
                **kw):
            names = derived_names(ids.tenant_id, ids.team_id, rid)
            transports[names["executor_id"]] = transports[config.targets[0].executor_id]
            chosen = {} if selection is None else {"capabilities": selection[0], "skills": selection[1]}
            body = CreateTeamAgent(request_id=rid, display_name=name, role_prompt=text, **chosen)
            return onboard(a, principal or c.human, body, foundry=kw.pop("foundry", foundry),
                           sleep=clock.sleep, clock=clock, now=clock.wall, **kw)
        case.run = run
        yield case
    finally:
        if not loop.is_closed():
            loop.close()


def _code(error):
    return error.value.status, error.value.body["error"]["code"]


def _entry(case, agent_id):
    return next(item for item in public_agents(case.a, case.c.human).agents if item.agent_id == agent_id)


def test_full_onboarding_creates_registers_and_lists_the_agent_ready(team):
    finished, value = team.run()
    assert finished and value["state"] == "ready" and value["reason"] is None
    agent_id = value["agent_id"]
    create = team.foundry.creates()
    assert len(create) == 1
    _, name, definition, metadata = create[0]
    assert name == value["agent_name"] == derived_names(team.config.identity.tenant_id,
                                                         team.config.identity.team_id, "onboard-one")["agent_name"]
    assert definition["image"] == "fa-a:build" and definition["environment_variables"] == {"KEEP": "1"}
    original = team.config.targets[0]
    deployed = onboarding_versions.current_hosted_version(team.a.runtime, original)
    assert metadata == {"fa_onboarding_request_id": "onboard-one", "fa_team_id": team.config.identity.team_id,
                        "fa_build": code_sha256(), "fa_source": f"{original.agent_name}:{deployed}"}
    assert registered_agent_ids(team.a.runtime.store, team.config) == (agent_id,)
    entry = _entry(team, agent_id)
    assert entry.state == "ready"
    peer = team.a.for_agent(agent_id)
    policy = peer.runtime.work_policy.policy
    assert policy.profile.prompt_profile == "fa-instruction-agent-role-v4"
    assert current_role_prompt(team.a.runtime.store, policy).text == "You research things."
    stored = team.a.runtime.store.read_record(pending_key(team.config.identity.tenant_id,
                                                          team.config.identity.team_id)).payload["data"]
    assert stored["request_ids"] == []
    assert instruction_hosted_version(peer.runtime.work_policy, None, existing=False) == "1"
    assert onboarding_status(team.a, team.c.human, "onboard-one") == value


def test_onboarded_agent_composition_gets_the_upgrade_snapshot_container_url(team):
    # ADR-0009 phase 5: build_server sets it on the owner; a per-agent composition must carry the same value.
    from shared_context.behavior_server import ServerComposition
    assert ServerComposition(None, None, None, None, (), None).upgrade_snapshot_container_url is None
    url = "https://stsnaptok3n.blob.core.windows.net/upgrade-snapshots"
    team.a.upgrade_snapshot_container_url = url
    finished, value = team.run()
    assert finished
    peer = team.a.for_agent(value["agent_id"])
    assert peer is not team.a and peer.upgrade_snapshot_container_url == url


def test_same_request_resend_never_creates_twice_or_takes_another_slot(team):
    team.foundry.ready_after = 10**6
    finished, first = team.run(deadline_seconds=3)
    assert not finished and first["state"] == "creating"
    inventory = team.a.runtime.store.read_inventory()
    finished, again = team.run(deadline_seconds=3)
    assert not finished and again == first
    assert len(team.foundry.creates()) == 1
    assert team.a.runtime.store.read_inventory().rows == inventory.rows
    team.foundry.ready_after = 0
    finished, done = team.run()
    assert finished and done["state"] == "ready" and len(team.foundry.creates()) == 1
    assert team.run() == (True, done)
    with pytest.raises(Problem) as changed:
        team.run(text="Different role.")
    assert _code(changed) == (409, "AGENT_ONBOARDING_CONFLICT")


class SetupSpy:
    """#468 setup that answers each call with the next reason (None = done)."""

    def __init__(self, *reasons):
        self.reasons, self.calls = list(reasons), []

    def pending(self, foundry, name, principal_id):
        self.calls.append((name, principal_id))
        return self.reasons.pop(0) if self.reasons else None


def test_new_agent_setup_runs_before_registration_and_holds_it_pending_until_done(team, caplog):
    spy = SetupSpy("step=roles reason=refused role=r status=403 code=AuthorizationFailed")
    with caplog.at_level(logging.WARNING):
        finished, value = team.run(setup=spy)
    assert not finished and value["state"] == "creating" and value["reason"] == "AGENT_SETUP_PENDING"
    assert registered_agent_ids(team.a.runtime.store, team.config) == ()
    principal = team.foundry.versions[value["agent_name"]][0]["instance_identity"]["principal_id"]
    assert spy.calls == [(value["agent_name"], principal.lower())]
    assert ("FA_AGENT_SETUP_PENDING request_id=onboard-one step=roles reason=refused role=r status=403 "
            "code=AuthorizationFailed") in caplog.text
    # The reason is reply-only: the stored record and its status read stay as before.
    assert onboarding_status(team.a, team.c.human, "onboard-one")["reason"] is None
    finished, done = team.run(setup=spy)
    assert finished and done["state"] == "ready" and done["reason"] is None
    assert len(team.foundry.creates()) == 1 and len(spy.calls) == 2
    assert registered_agent_ids(team.a.runtime.store, team.config) == (done["agent_id"],)


def test_endpoint_setup_adds_the_fa_protocols_to_the_new_agent_before_it_is_ready(team):
    from shared_context.behavior_agent_setup import NewAgentSetup
    finished, value = team.run(setup=NewAgentSetup(endpoint=True))
    assert finished and value["state"] == "ready"
    patches = [call for call in team.foundry.calls if call[0] == "patch"]
    assert patches == [("patch", value["agent_name"], {"protocol_configuration": {
        "responses": {}, "invocations": {}, "invocations_ws": {}}})]
    # A's own endpoint is never touched.
    assert all(call[1] != team.foundry.a_name for call in team.foundry.calls if call[0] in {"patch", "get_agent"})


def test_without_setup_switches_onboarding_never_reads_or_patches_the_endpoint(team):
    finished, _ = team.run()
    assert finished and not [call for call in team.foundry.calls if call[0] in {"patch", "get_agent"}]


def test_unknown_create_result_reads_back_and_adopts_the_original_version(team):
    team.foundry.fail_create.append("unknown-after-commit")
    finished, value = team.run()
    assert not finished and value["state"] == "creating"
    assert len(team.foundry.creates()) == 1
    finished, value = team.run()
    assert finished and value["state"] == "ready"
    assert len(team.foundry.creates()) == 1
    assert team.foundry.calls[-1][0] != "create"


def test_name_owned_by_another_request_is_refused_without_creating(team):
    names = derived_names(team.config.identity.tenant_id, team.config.identity.team_id, "onboard-one")
    team.foundry.versions[names["agent_name"]] = [{"name": names["agent_name"], "version": "1", "status": "active",
                                                   "metadata": {"fa_onboarding_request_id": "someone-else"}}]
    with pytest.raises(Problem) as refused:
        team.run()
    assert _code(refused) == (409, "AGENT_ONBOARDING_NAME_CONFLICT")
    assert team.foundry.creates() == []


def test_quota_full_is_refused_with_its_limit_and_no_table_change(team, monkeypatch):
    limit = len(team.config.targets) + 1
    monkeypatch.setattr(onboarding, "MAX_TEAM_AGENTS", limit)
    team.foundry.ready_after = 10**6
    team.run(deadline_seconds=1)
    before = team.a.runtime.store.read_inventory()
    with pytest.raises(Problem) as full:
        team.run(rid="onboard-two", name="Writer")
    assert _code(full) == (409, "AGENT_QUOTA_REACHED") and full.value.body["error"]["limit"] == limit
    after = team.a.runtime.store.read_inventory()
    assert (after.rows, after.receipts) == (before.rows, before.receipts)
    assert len(team.foundry.creates()) == 1


def test_concurrent_admissions_for_the_last_slot_admit_exactly_one(team, monkeypatch):
    monkeypatch.setattr(onboarding, "MAX_TEAM_AGENTS", len(team.config.targets) + 1)
    store = team.a.runtime.store
    original = store.transact
    raced = []
    ids = team.config.identity
    first = onboarding.control_key("team-agent-onboarding-step", ids.tenant_id, ids.team_id, "onboard-one", "admitted")

    def racing(request, decide, **kw):
        def interleaved(rows):
            if request.request_id == first and not raced:
                raced.append(True)
                monkeypatch.setattr(store, "transact", original)
                team.run(rid="onboard-two", name="Writer", deadline_seconds=0)
            return decide(rows)
        return original(request, interleaved, **kw)

    monkeypatch.setattr(store, "transact", racing)
    team.foundry.ready_after = 10**6
    with pytest.raises(Problem) as lost:
        team.run(deadline_seconds=0)
    assert _code(lost) == (409, "AGENT_QUOTA_REACHED")
    monkeypatch.setattr(store, "transact", original)
    ids = team.config.identity
    pending = store.read_record(pending_key(ids.tenant_id, ids.team_id)).payload["data"]["request_ids"]
    assert pending == ["onboard-two"]
    assert store.read_record(onboarding_key(ids.tenant_id, ids.team_id, "onboard-one")) is None


def test_explicit_rejection_releases_the_slot_and_the_name(team, monkeypatch):
    monkeypatch.setattr(onboarding, "MAX_TEAM_AGENTS", len(team.config.targets) + 1)
    team.foundry.fail_create.append(400)
    finished, value = team.run()
    assert finished and (value["state"], value["reason"]) == ("failed", "FOUNDRY_CREATE_REJECTED")
    ids = team.config.identity
    store = team.a.runtime.store
    assert store.read_record(pending_key(ids.tenant_id, ids.team_id)).payload["data"]["request_ids"] == []
    assert store.read_record(name_key(ids.tenant_id, ids.team_id, "Researcher")).payload["data"]["active"] is False
    assert team.run() == (True, value)
    finished, other = team.run(rid="onboard-two", name="researcher")
    assert finished and other["state"] == "ready"


def test_failed_version_status_releases_the_slot(team):
    team.foundry.final_status = "failed"
    finished, value = team.run()
    assert finished and (value["state"], value["reason"]) == ("failed", "FOUNDRY_VERSION_FAILED")


def test_unknown_foundry_outcomes_keep_the_slot(team):
    team.foundry.fail_create.append(503)
    finished, value = team.run()
    assert not finished and value["state"] == "creating"
    team.foundry.fail_list.append(None)
    finished, value = team.run()
    assert not finished and value["state"] == "creating"
    ids = team.config.identity
    pending = team.a.runtime.store.read_record(pending_key(ids.tenant_id, ids.team_id)).payload["data"]
    assert pending["request_ids"] == ["onboard-one"]
    finished, value = team.run(deadline_seconds=3)
    assert not finished and value["state"] == "creating" and len(team.foundry.creates()) == 1
    team.clock.now += onboarding.CREATE_RETRY_SECONDS
    assert team.run()[1]["state"] == "ready"
    assert len(team.foundry.creates()) == 2


def test_concurrent_same_request_posts_create_once(team, monkeypatch):
    store = team.a.runtime.store
    original = store.transact
    ids = team.config.identity
    first = onboarding.control_key("team-agent-create-attempt-step", ids.tenant_id, ids.team_id, "onboard-one", "1")
    raced = []

    def racing(request, decide, **kw):
        def interleaved(rows):
            if request.request_id == first and not raced:
                raced.append(True)
                team.run(deadline_seconds=0)
            return decide(rows)
        return original(request, interleaved, **kw)

    monkeypatch.setattr(store, "transact", racing)
    team.foundry.ready_after = 10**6
    finished, value = team.run(deadline_seconds=1)
    assert raced and not finished and value["state"] == "creating"
    assert len(team.foundry.creates()) == 1
    monkeypatch.setattr(store, "transact", original)
    team.foundry.ready_after = 0
    assert team.run()[1]["state"] == "ready" and len(team.foundry.creates()) == 1


def test_real_foundry_client_retries_only_reads_and_bounds_each_call(monkeypatch):
    import azure.ai.projects
    from shared_context.behavior_foundry import PLATFORM_NO_RETRY, PLATFORM_READ_RETRY
    seen = []

    def result(value):
        return lambda *a, **k: SimpleNamespace(as_dict=lambda: value)

    class Client:
        def __init__(self, **kw):
            seen.append(kw)
            self.agents = SimpleNamespace(
                get_version=result({"ok": True}), get=result({"agent": True}),
                list_versions=lambda *a, **k: [SimpleNamespace(as_dict=lambda: {"v": 1})],
                create_version=result({"created": True}), update_details=result({"routed": True}))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(azure.ai.projects, "AIProjectClient", Client)
    foundry = onboarding.ProjectAgentFoundry("https://p", object())
    assert foundry.get_version("n", "1") == {"ok": True}
    assert foundry.get_agent("n") == {"agent": True}
    assert foundry.list_versions("n") == [{"v": 1}]
    assert foundry.create_version("n", definition={}, metadata={}) == {"created": True}
    assert foundry.update_route("n", {}) == {"routed": True}
    keys = ("retry_total", "retry_connect", "retry_read", "retry_status")
    # An idempotent read retries one stalled or dropped connection; a write never retries.
    assert PLATFORM_READ_RETRY["retry_total"] == 1 and PLATFORM_READ_RETRY["retry_status"] == 0
    assert [{k: kw[k] for k in keys} for kw in seen] == [
        {k: PLATFORM_READ_RETRY[k] for k in keys}] * 3 + [PLATFORM_NO_RETRY] * 2
    assert seen[3]["connection_timeout"] + seen[3]["read_timeout"] < onboarding.CREATE_RETRY_SECONDS


def test_duplicate_display_name_is_refused(team):
    team.foundry.ready_after = 10**6
    team.run(deadline_seconds=0)
    with pytest.raises(Problem) as taken:
        team.run(rid="onboard-two", name="researcher")
    assert _code(taken) == (409, "AGENT_NAME_TAKEN")


def _human(case, oid, team_id=None):
    p = case.c.human
    return BehaviorPrincipal(tenant_id=p.tenant_id, oid=oid, team=team_id or p.team, kind="human", actor_id=None,
                             actions=p.actions)


def test_non_owner_inactive_owner_and_other_team_are_refused(team):
    stranger = "12121212-1212-1212-1212-121212121212"
    with pytest.raises(Problem) as denied:
        team.run(principal=_human(team, stranger))
    assert _code(denied) == (403, "TEAM_OWNER_DENIED")
    teams = team.teams
    other_owner = "34343434-3434-3434-3434-343434343434"
    teams.register_owner(team.c.human, "owner-two", "team-two", other_owner)
    with pytest.raises(Problem) as other:
        team.run(principal=_human(team, other_owner, "team-two"))
    assert _code(other) == (409, "TEAM_RUNTIME_NOT_SERVED")
    teams.deactivate_owner(team.c.human, "owner-two-off", other_owner)
    with pytest.raises(Problem) as inactive:
        team.run(principal=_human(team, other_owner, "team-two"))
    assert _code(inactive) == (403, "TEAM_OWNER_DENIED")
    assert team.foundry.creates() == []
    with pytest.raises(Problem) as unknown:
        onboarding_status(team.a, team.c.human, "never-started")
    assert _code(unknown) == (404, "AGENT_ONBOARDING_NOT_FOUND")


def test_oversized_role_prompt_is_a_422_with_its_limit(team):
    with pytest.raises(Problem) as large:
        team.run(text="x" * (64 * 1024 + 1))
    assert _code(large) == (422, "AGENT_ROLE_PROMPT_TOO_LARGE")
    assert large.value.body["error"]["max_bytes"] == ROLE_PROMPT_MAX_BYTES
    assert team.foundry.calls == []


def test_routes_return_202_until_ready_and_keep_refusal_details(team, monkeypatch):
    from shared_context.behavior_api import _install_error_handlers
    from shared_context.behavior_work_policy_api import install_work_policy_routes
    names = derived_names(team.config.identity.tenant_id, team.config.identity.team_id, "route-one")
    team.transports[names["executor_id"]] = team.transports[team.config.targets[0].executor_id]
    real = onboarding.onboard

    def fake(composition, principal, body, **kw):
        return real(composition, principal, body, foundry=team.foundry, sleep=team.clock.sleep, clock=team.clock,
                    now=team.clock.wall, deadline_seconds=1)
    monkeypatch.setattr(onboarding, "onboard", fake)
    app = FastAPI()
    _install_error_handlers(app)
    install_work_policy_routes(app, team.a)
    headers = team.c.h.headers(team.signing_key, "Behavior.Read Behavior.Approve")
    body = {"request_id": "route-one", "display_name": "Router", "role_prompt": "Route things."}
    with TestClient(app, backend_options={"loop_factory": lambda: team.loop}) as client:
        large = client.post("/work-policy/v1/agents", headers=headers,
                            json={**body, "role_prompt": "y" * (64 * 1024 + 10)})
        assert large.status_code == 422 and large.json()["error"]["code"] == "AGENT_ROLE_PROMPT_TOO_LARGE"
        assert large.json()["error"]["max_bytes"] == ROLE_PROMPT_MAX_BYTES
        bad = client.post("/work-policy/v1/agents", headers=headers, json={**body, "extra": 1})
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "AGENT_ONBOARDING_INVALID"
        team.foundry.ready_after = 10**6
        pending = client.post("/work-policy/v1/agents", headers=headers, json=body)
        assert pending.status_code == 202 and pending.json()["state"] == "creating"
        status = client.get("/work-policy/v1/agents/onboarding/route-one", headers=headers)
        assert status.status_code == 200 and status.json()["state"] == "creating"
        team.foundry.ready_after = 0
        done = client.post("/work-policy/v1/agents", headers=headers, json=body)
        assert done.status_code == 200 and done.json()["state"] == "ready"
        monkeypatch.setattr(onboarding, "MAX_TEAM_AGENTS", len(team.config.targets) + 1)
        full = client.post("/work-policy/v1/agents", headers=headers,
                           json={**body, "request_id": "route-two", "display_name": "Other"})
        assert full.status_code == 409 and full.json()["error"] == {
            "code": "AGENT_QUOTA_REACHED", "message": "this Team has no remaining agent capacity",
            "continue_same_request": False, "automatic_retry": False, "limit": len(team.config.targets) + 1}
        missing = client.get("/work-policy/v1/agents/onboarding/nope", headers=headers)
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "AGENT_ONBOARDING_NOT_FOUND"


def test_onboarding_passes_the_real_startup_readers_and_a_tampered_row_is_refused(team):
    from shared_context.behavior_migration import MigrationStore, installed_origin
    from test_resident_worker_poc import outcome_of
    config = team.config

    def readers():
        store = MigrationStore(team.c.h.composition.clients[0], config.table.scope)
        return (outcome_of(lambda: read_startup(team.c)), outcome_of(lambda: installed_origin(store, config)))
    baseline = readers()
    assert baseline[0] == "ok"
    team.run()
    team.foundry.fail_create.append(422)
    team.run(rid="onboard-failed", name="Failed")
    team.foundry.ready_after = 10**6
    team.run(rid="onboard-open", name="Open", deadline_seconds=0)
    assert readers() == baseline


def _stop_before_role(team, monkeypatch):
    real, calls = onboarding._save_role, []

    def unavailable_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise Problem(503, "TEST_ROLE_UNAVAILABLE", "role prompt store unavailable")
        return real(*args, **kwargs)
    monkeypatch.setattr(onboarding, "_save_role", unavailable_once)
    with pytest.raises(Problem):
        team.run()
    value = onboarding_status(team.a, team.c.human, "onboard-one")
    assert value["state"] == "registered"
    return value["agent_id"]


def test_directory_uses_the_onboarding_name_and_stays_unavailable_until_ready(team, monkeypatch):
    agent_id = _stop_before_role(team, monkeypatch)
    entry = _entry(team, agent_id)
    assert (entry.display_name, entry.state, entry.reason) == ("Researcher", "unavailable",
                                                               "AGENT_ONBOARDING_IN_PROGRESS")
    finished, value = team.run()
    assert finished and value["state"] == "ready"
    entry = _entry(team, agent_id)
    assert (entry.display_name, entry.state, entry.reason) == ("Researcher", "ready", None)


def test_console_connection_is_owner_only_ready_only_and_never_writes(team, monkeypatch):
    from shared_context.behavior_agent_console_wire import AgentConsoleConnection
    from shared_context.behavior_team_registry import agent_key
    agent_id = _stop_before_role(team, monkeypatch)
    with pytest.raises(Problem) as early:
        console_connection(team.a, team.c.human, agent_id)
    assert _code(early) == (409, "AGENT_ONBOARDING_IN_PROGRESS")
    team.run()
    store = team.a.runtime.store
    before = store.read_inventory()
    value = console_connection(team.a, team.c.human, agent_id)
    assert type(value) is AgentConsoleConnection and value.display_name == "Researcher"
    installed = team.a.for_agent(agent_id).runtime.work_policy.installed()
    assert value.standing_policy == installed and value.standing_policy_sha256 == installed.sha256
    assert value.standing_policy.agent_id == agent_id
    AgentConsoleConnection.model_validate_json(value.model_dump_json())
    after = store.read_inventory()
    assert (after.rows, after.receipts) == (before.rows, before.receipts)
    with pytest.raises(Problem) as stranger:
        console_connection(team.a, _human(team, "12121212-1212-1212-1212-121212121212"), agent_id)
    assert _code(stranger) == (403, "TEAM_OWNER_DENIED")
    for unknown in (team.config.targets[0].agent_id, "never-onboarded", "../x"):
        with pytest.raises(Problem) as missing:
            console_connection(team.a, team.c.human, unknown)
        assert _code(missing) == (404, "AGENT_CONSOLE_NOT_FOUND")
    key = agent_key(team.config.identity.tenant_id, agent_id)
    original = store.read_record

    def elsewhere(name, *args, **kwargs):
        row = original(name, *args, **kwargs)
        if name != key:
            return row
        return replace(row, payload={**row.payload, "data": {**row.payload["data"], "team_id": "team-two"}})
    monkeypatch.setattr(store, "read_record", elsewhere)
    with pytest.raises(Problem) as other:
        console_connection(team.a, team.c.human, agent_id)
    assert _code(other) == (403, "AGENT_CONSOLE_DENIED")


def test_console_connection_route_returns_the_exact_connection(team):
    from shared_context.behavior_agent_console_wire import AgentConsoleConnection
    from shared_context.behavior_api import _install_error_handlers
    from shared_context.behavior_work_policy_api import install_work_policy_routes
    finished, value = team.run()
    assert finished
    app = FastAPI()
    _install_error_handlers(app)
    install_work_policy_routes(app, team.a)
    headers = team.c.h.headers(team.signing_key, "Behavior.Read Behavior.Approve")
    path = "/work-policy/v1/agents/" + value["agent_id"] + "/console-connection"
    with TestClient(app, backend_options={"loop_factory": lambda: team.loop}) as client:
        read = client.get(path, headers=headers)
        assert read.status_code == 200 and read.headers["cache-control"] == "no-store"
        assert AgentConsoleConnection.model_validate_json(read.content) == console_connection(
            team.a, team.c.human, value["agent_id"])
        query = client.get(path + "?x=1", headers=headers)
        assert query.status_code == 422
        missing = client.get("/work-policy/v1/agents/nobody/console-connection", headers=headers)
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "AGENT_CONSOLE_NOT_FOUND"


# ----- PR6: owner-chosen Team-approved tools and skills -------------------------------------------------------

from shared_context.behavior_agent_template import TemplateSkill  # noqa: E402
from shared_context.behavior_capabilities import (  # noqa: E402
    SKILLIFY_ENTRY_ID, SKILLS_ENTRY_ID, CapabilityRef, available_grants,
)
from shared_context.m3_skills import ApprovedSkillSource  # noqa: E402

SKILL = TemplateSkill(name="receipt-guide", version_id="1" * 32, sha256="a" * 64, metadata_sha256="b" * 64)


def _grants(team, *builtins):
    from shared_context.tool_catalog import build_catalog, parse_entry_file
    entry = parse_entry_file("lookup.json", json.dumps({"mcpServers": {"lookup": {
        "type": "stdio", "command": "/opt/fa-tools/bin/lookup", "tools": ["find"]}}}).encode())
    team.a.runtime.capability_grants = available_grants(build_catalog([entry]), builtins)
    return team.a.runtime.capability_grants


def _refs(grants, *ids):
    return tuple(CapabilityRef(entry_id=i, entry_sha256=grants[i].entry_sha256) for i in sorted(ids))


def _approved(monkeypatch, *skills, unavailable=0):
    reads = []

    def catalog(self, actor, team, selected, *, schema_version="fa-approved-skill-catalog-v1"):
        reads.append((actor.oid, team, tuple(selected), schema_version))
        return SimpleNamespace(entries=tuple(item for item in skills if item.name in selected),
                               unavailable_count=unavailable)
    monkeypatch.setattr(ApprovedSkillSource, "catalog", catalog)
    return reads


def _selected(team, agent_id):
    from shared_context.behavior_profile_selection import current_selection, profile_from_template
    control = team.a.for_agent(agent_id).runtime.work_policy
    head = current_selection(control.runtime.store, control.policy)
    return control, head, None if head is None else profile_from_template(control.policy, head.command.template)


def test_owner_choice_becomes_the_new_agents_own_selection_not_a_copy_of_a(team, monkeypatch):
    grants = _grants(team, SKILLS_ENTRY_ID)
    mcp = next(i for i, g in grants.items() if g.kind != "fa-builtin")
    reads = _approved(monkeypatch, SKILL)
    chosen = _refs(grants, mcp, SKILLS_ENTRY_ID)
    finished, value = team.run(selection=(chosen, (SKILL,)))
    assert finished and value["state"] == "ready"
    control, head, profile = _selected(team, value["agent_id"])
    assert control.policy.profile.capabilities == () and control.policy.profile.skill_artifacts == ()
    assert head is not None and head.command.template.settings.capabilities == chosen and head.command.template.skills == (SKILL,)
    assert profile.capabilities == chosen and profile.skill_artifacts == ("receipt-guide",)
    assert profile.prompt_profile == "fa-instruction-agent-role-v4"
    assert reads and all(read[2] == ("receipt-guide",) for read in reads)
    stored = onboarding._read(team.a.runtime.store, onboarding_key(
        team.config.identity.tenant_id, team.config.identity.team_id, "onboard-one"))
    assert stored.selection.capabilities == chosen and stored.selection.skills == (SKILL,)
    assert _entry(team, value["agent_id"]).state == "ready"


def test_owner_may_choose_no_tools_and_a_tools_only_selection_reads_no_skills(team, monkeypatch):
    grants = _grants(team, SKILLS_ENTRY_ID)
    reads = _approved(monkeypatch, SKILL)
    finished, value = team.run(selection=((), ()))
    assert finished and value["state"] == "ready" and all(read[2] == () for read in reads)
    _, head, profile = _selected(team, value["agent_id"])
    assert head.command.template.settings.capabilities == () and profile.capabilities == () and profile.skill_artifacts == ()
    finished, other = team.run(rid="onboard-two", name="Writer", selection=(_refs(grants, SKILLS_ENTRY_ID), ()))
    assert finished and all(read[2] == () for read in reads)
    assert _selected(team, other["agent_id"])[2].capabilities == _refs(grants, SKILLS_ENTRY_ID)


# agent-role v2 composes skillify, so a new Agent may choose it (test_agent_role_prompt_v2.py).
@pytest.mark.parametrize("case", ["not-granted", "wrong-sha", "skills-without-tool", "unknown-skill",
                                  "wrong-skill-sha", "unavailable-skill", "unordered-skills"])
def test_choices_outside_the_approved_catalog_are_refused_before_any_write(team, monkeypatch, case):
    grants = _grants(team, SKILLS_ENTRY_ID, SKILLIFY_ENTRY_ID)
    other = SKILL.model_copy(update={"name": "alpha-guide"})
    _approved(monkeypatch, SKILL, other, unavailable=1 if case == "unavailable-skill" else 0)
    tools, skills = _refs(grants, SKILLS_ENTRY_ID), (SKILL,)
    if case == "not-granted":
        tools = (*tools, CapabilityRef(entry_id="fa-unknown-v1", entry_sha256="c" * 64))
    elif case == "wrong-sha":
        tools = (CapabilityRef(entry_id=SKILLS_ENTRY_ID, entry_sha256="d" * 64),)
    elif case == "skills-without-tool":
        tools = ()
    elif case == "unknown-skill":
        skills = (SKILL.model_copy(update={"name": "zeta-guide"}),)
    elif case == "wrong-skill-sha":
        skills = (SKILL.model_copy(update={"sha256": "e" * 64}),)
    elif case == "unordered-skills":
        skills = (SKILL, other)
    expected = {
        "not-granted": (409, "CAPABILITY_SOURCES_CHANGED"), "wrong-sha": (409, "CAPABILITY_SOURCES_CHANGED"),
        "skills-without-tool": (422, "AGENT_ONBOARDING_SKILLS_TOOL_REQUIRED"),
        "unknown-skill": (409, "PROFILE_SELECTION_SOURCES_CHANGED"),
        "wrong-skill-sha": (409, "PROFILE_SELECTION_SOURCES_CHANGED"),
        "unavailable-skill": (409, "PROFILE_SELECTION_SOURCES_CHANGED"),
        "unordered-skills": (422, "AGENT_ONBOARDING_INVALID"),
    }[case]
    before = team.a.runtime.store.read_inventory()
    with pytest.raises(Problem) as refused:
        team.run(selection=(tools, skills))
    assert _code(refused) == expected
    after = team.a.runtime.store.read_inventory()
    assert (after.rows, after.receipts) == (before.rows, before.receipts)
    assert team.foundry.calls == []


def test_a_different_choice_on_the_same_request_is_a_conflict(team, monkeypatch):
    grants = _grants(team, SKILLS_ENTRY_ID)
    _approved(monkeypatch, SKILL)
    team.foundry.ready_after = 10**6
    team.run(selection=(_refs(grants, SKILLS_ENTRY_ID), (SKILL,)), deadline_seconds=1)
    for changed in (((), ()), None):
        with pytest.raises(Problem) as conflict:
            team.run(selection=changed, deadline_seconds=1)
        assert _code(conflict) == (409, "AGENT_ONBOARDING_CONFLICT")
    assert len(team.foundry.creates()) == 1


def test_resuming_after_an_interrupted_selection_keeps_the_original_choice(team, monkeypatch):
    import shared_context.behavior_profile_selection as selection_module
    grants = _grants(team, SKILLS_ENTRY_ID)
    _approved(monkeypatch, SKILL)
    chosen = _refs(grants, SKILLS_ENTRY_ID)
    original, commands = selection_module.select_profile, []

    def lost_once(*args, **kwargs):
        commands.append(args[-1])
        result = original(*args, **kwargs)
        if len(commands) == 1:
            raise Problem(503, "TEST_RESPONSE_LOST", "selection result was not observed")
        return result
    monkeypatch.setattr(selection_module, "select_profile", lost_once)
    with pytest.raises(Problem):
        team.run(selection=(chosen, (SKILL,)))
    assert onboarding_status(team.a, team.c.human, "onboard-one")["state"] == "registered"
    # A later grant change does not alter the choice: the saved record, not today's options, is reused.
    _grants(team, SKILLS_ENTRY_ID, SKILLIFY_ENTRY_ID)
    finished, value = team.run(selection=(chosen, (SKILL,)))
    assert finished and value["state"] == "ready"
    assert len(commands) == 2 and commands[0] == commands[1]
    _, head, profile = _selected(team, value["agent_id"])
    assert profile.capabilities == chosen
    saved = [row for row in team.a.runtime.store.read_inventory().rows.values()
             if isinstance(row.payload.get("data"), dict) and row.payload["data"].get("agent_id") == value["agent_id"]
             and row.payload["data"].get("kind") == "conversation_profile_selection"]
    assert len(saved) == 1 and saved[0].payload["data"]["selection_id"] == head.selection_id

def test_a_catalog_change_after_admission_keeps_the_same_request_as_the_only_way_forward(team, monkeypatch):
    grants = _grants(team, SKILLS_ENTRY_ID)
    _approved(monkeypatch, SKILL)
    chosen = _refs(grants, SKILLS_ENTRY_ID)
    real_save = onboarding._save_role

    def revised_before_s5(*args, **kwargs):
        real_save(*args, **kwargs)
        _approved(monkeypatch, SKILL.model_copy(update={"sha256": "d" * 64}))
    monkeypatch.setattr(onboarding, "_save_role", revised_before_s5)
    with pytest.raises(Problem) as stale:
        team.run(selection=(chosen, (SKILL,)))
    body = stale.value.body["error"]
    # Never one of the pre-write refusal codes: 8765 must not treat this as "nothing written" and mint a new ID.
    assert (stale.value.status, body["code"], body["cause"]) == (
        409, "AGENT_ONBOARDING_SELECTION_STALE", "PROFILE_SELECTION_SOURCES_CHANGED")
    assert onboarding_status(team.a, team.c.human, "onboard-one")["state"] == "registered"
    monkeypatch.setattr(onboarding, "_save_role", real_save)
    _approved(monkeypatch, SKILL)
    finished, value = team.run(selection=(chosen, (SKILL,)))
    assert finished and value["state"] == "ready"
    assert len(team.foundry.creates()) == 1


def _team_mcp(team, monkeypatch):
    """A's Team MCP service as the app build installs it (team_mcp_api.py), offering one ``team-*`` server."""
    from shared_context.team_mcp_wire import TEAM_MCP_ENABLED_ENV, TeamMcpServer, team_mcp_grant
    monkeypatch.setenv(TEAM_MCP_ENABLED_ENV, "1")
    grant = team_mcp_grant(team.config.identity.team_id, TeamMcpServer(
        server_label="stratechery", server_url="https://mcp.example.test/mcp", allowed_tools=("search",), version="v1"))
    team.a.runtime.team_mcp = SimpleNamespace(team_grants=lambda tenant, team_id: (grant,))
    lookup = _grants(team)
    return (*_refs(lookup, *lookup), CapabilityRef(entry_id=grant.entry_id, entry_sha256=grant.entry_sha256))


def test_the_default_choice_with_a_team_mcp_server_reaches_ready_on_the_new_agent(team, monkeypatch):
    # The page pre-ticks A's selection; S5 checks it on the new Agent's peer runtime, built before any app for it.
    chosen = _team_mcp(team, monkeypatch)
    finished, value = team.run(selection=(chosen, ()))
    assert finished and value["state"] == "ready"
    assert len(team.foundry.creates()) == 1
    assert _selected(team, value["agent_id"])[2].capabilities == tuple(sorted(chosen, key=lambda ref: ref.entry_id))


def test_a_request_left_stale_by_a_peer_without_team_mcp_resumes_ready_with_the_same_id(team, monkeypatch, caplog):
    chosen = _team_mcp(team, monkeypatch)
    real_save = onboarding._save_role

    def peer_without_team_mcp(composition, principal, value, text):
        real_save(composition, principal, value, text)
        composition.for_agent(value.target.agent_id).runtime.team_mcp = None  # the build before this fix
    monkeypatch.setattr(onboarding, "_save_role", peer_without_team_mcp)
    with caplog.at_level(logging.WARNING, logger=onboarding.__name__), pytest.raises(Problem) as stale:
        team.run(selection=(chosen, ()))
    body = stale.value.body["error"]
    assert (stale.value.status, body["code"], body["cause"]) == (
        409, "AGENT_ONBOARDING_SELECTION_STALE", "CAPABILITY_SOURCES_CHANGED")
    logged = [r.getMessage() for r in caplog.records if r.getMessage().startswith("AGENT_ONBOARDING_SELECTION_STALE")]
    assert logged == ["AGENT_ONBOARDING_SELECTION_STALE request_id=onboard-one cause=CAPABILITY_SOURCES_CHANGED"]
    assert onboarding_status(team.a, team.c.human, "onboard-one")["state"] == "registered"
    monkeypatch.setattr(onboarding, "_save_role", real_save)
    # The fixed build starts with no cached peer runtime (an API restart); the same request ID finishes the job.
    for peer in list(team.a._peers.values()):
        peer.close()
    finished, value = team.run(selection=(chosen, ()))
    assert finished and value["state"] == "ready"
    assert len(team.foundry.creates()) == 1


def test_a_peer_built_without_its_app_offers_the_teams_mcp_and_connector_grants(team, monkeypatch):
    # Routines, the Teams relay and agent messages reach a peer through for_agent() only, never through its app.
    from shared_context.behavior_capabilities import runtime_grants
    from shared_context.team_connector import current_team_connector_grants
    chosen = _team_mcp(team, monkeypatch)
    connector = object()
    team.a.runtime.team_connectors = SimpleNamespace(team_grants=lambda tenant, team_id: (connector,))
    finished, value = team.run()
    assert finished and value["state"] == "ready"
    for peer in list(team.a._peers.values()):
        peer.close()
    peer = team.a.for_agent(value["agent_id"])
    assert chosen[-1].entry_id in runtime_grants(peer.runtime)
    assert current_team_connector_grants(peer.runtime) == (connector,)


def test_a_with_skill_artifacts_can_seed_a_chosen_agent(team, monkeypatch):
    grants = _grants(team, SKILLS_ENTRY_ID)
    _approved(monkeypatch, SKILL)
    real = onboarding._role_profile

    def with_skills(profile, selection=None):
        if selection is None:
            raise Problem(409, "AGENT_ONBOARDING_PROFILE_UNSUPPORTED", "copy of A refused in this test")
        return real(profile.model_copy(update={"skill_artifacts": ("receipt-guide",)}), selection)
    monkeypatch.setattr(onboarding, "_role_profile", with_skills)
    finished, value = team.run(selection=(_refs(grants, SKILLS_ENTRY_ID), (SKILL,)))
    assert finished and value["state"] == "ready"
    assert _selected(team, value["agent_id"])[0].policy.profile.skill_artifacts == ()


def test_legacy_and_chosen_records_resume_and_pass_the_real_startup_readers(team, monkeypatch):
    from shared_context.behavior_migration import MigrationStore, installed_origin
    from test_resident_worker_poc import outcome_of
    config = team.config

    def readers():
        store = MigrationStore(team.c.h.composition.clients[0], config.table.scope)
        return (outcome_of(lambda: read_startup(team.c)), outcome_of(lambda: installed_origin(store, config)))
    baseline = readers()
    assert baseline[0] == "ok"
    grants = _grants(team, SKILLS_ENTRY_ID)
    _approved(monkeypatch, SKILL)
    team.foundry.ready_after = 10**6
    team.run(rid="onboard-legacy", name="Legacy", deadline_seconds=1)
    team.run(rid="onboard-chosen", name="Chosen", selection=(_refs(grants, SKILLS_ENTRY_ID), (SKILL,)),
             deadline_seconds=1)
    ids = config.identity
    legacy = team.a.runtime.store.read_record(onboarding_key(ids.tenant_id, ids.team_id, "onboard-legacy"))
    assert "selection" not in legacy.payload["data"]
    assert readers() == baseline
    team.foundry.ready_after = 0
    assert team.run(rid="onboard-legacy", name="Legacy")[1]["state"] == "ready"
    assert team.run(rid="onboard-chosen", name="Chosen",
                    selection=(_refs(grants, SKILLS_ENTRY_ID), (SKILL,)))[1]["state"] == "ready"
    assert readers() == baseline
    legacy_id = onboarding_status(team.a, team.c.human, "onboard-legacy")["agent_id"]
    assert _selected(team, legacy_id)[1] is None


def test_route_refuses_half_a_choice_or_too_many_tools_before_any_write(team):
    from shared_context.behavior_api import _install_error_handlers
    from shared_context.behavior_capabilities import MAX_CAPABILITIES
    from shared_context.behavior_work_policy_api import install_work_policy_routes
    app = FastAPI()
    _install_error_handlers(app)
    install_work_policy_routes(app, team.a)
    headers = team.c.h.headers(team.signing_key, "Behavior.Read Behavior.Approve")
    body = {"request_id": "route-pick", "display_name": "Picker", "role_prompt": "Pick things."}
    many = [{"entry_id": f"fa-tool-{index:02d}-v1", "entry_sha256": "a" * 64} for index in range(MAX_CAPABILITIES + 1)]
    before = team.a.runtime.store.read_inventory()
    with TestClient(app, backend_options={"loop_factory": lambda: team.loop}) as client:
        for extra in ({"capabilities": []}, {"skills": []}, {"capabilities": many, "skills": []}):
            refused = client.post("/work-policy/v1/agents", headers=headers, json={**body, **extra})
            assert refused.status_code == 422 and refused.json()["error"]["code"] == "AGENT_ONBOARDING_INVALID"
    after = team.a.runtime.store.read_inventory()
    assert (after.rows, after.receipts) == (before.rows, before.receipts) and team.foundry.calls == []


ORIGIN = "fa-instruction-context-origin-v1"


def _a_selects(team, rid, **settings):
    from shared_context.behavior_profile_selection import SelectConversationProfile, current_selection, select_profile
    from test_conversation_profile_selection import template
    control = team.a.runtime.work_policy
    head = current_selection(control.runtime.store, control.policy)
    select_profile(control, getattr(getattr(team.a, "assistant", None), "authority", None), team.c.human,
                   SelectConversationProfile(request_id=rid, expected_selection_id=None if head is None else head.selection_id,
                                             template=template("fa-instruction-v2", **settings)))
    return control


@pytest.mark.parametrize("chosen", [False, True], ids=["copy-a", "owner-choice"])
def test_a_new_agent_takes_the_turn_origin_a_actually_uses_without_changing_a(team, monkeypatch, chosen):
    grants = _grants(team, SKILLS_ENTRY_ID)
    _approved(monkeypatch, SKILL)
    from shared_context.behavior_profile_selection import current_selection
    control = _a_selects(team, "a-origin-on", turn_origin_profile=ORIGIN)
    assert control.policy.profile.turn_origin_profile is None
    a_policy, a_head = control.policy.model_dump_json(), current_selection(control.runtime.store, control.policy)
    assert a_head.command.template.settings.turn_origin_profile == ORIGIN
    selection = (_refs(grants, SKILLS_ENTRY_ID), (SKILL,)) if chosen else None
    finished, value = team.run(selection=selection)
    assert finished and value["state"] == "ready"
    peer, head, profile = _selected(team, value["agent_id"])
    assert peer.policy.profile.turn_origin_profile == ORIGIN
    if chosen:
        assert head.command.template.settings.turn_origin_profile == ORIGIN and profile.turn_origin_profile == ORIGIN
    else:
        assert head is None
    stored = onboarding._read(team.a.runtime.store, onboarding_key(
        team.config.identity.tenant_id, team.config.identity.team_id, "onboard-one"))
    assert stored.turn_origin_profile == ORIGIN
    # A's own standing policy and its current selection are unchanged; only the new Agent's settings follow A.
    assert control.policy.model_dump_json() == a_policy
    assert current_selection(control.runtime.store, control.policy) == a_head


def test_the_admitted_turn_origin_is_kept_when_a_changes_before_the_agent_is_registered(team):
    _a_selects(team, "a-origin-on", turn_origin_profile=ORIGIN)
    team.foundry.ready_after = 10**6
    team.run(deadline_seconds=1)
    assert onboarding_status(team.a, team.c.human, "onboard-one")["state"] == "creating"
    _a_selects(team, "a-origin-off")
    team.foundry.ready_after = 0
    finished, value = team.run()
    assert finished and value["state"] == "ready"
    assert team.a.for_agent(value["agent_id"]).runtime.work_policy.policy.profile.turn_origin_profile == ORIGIN


def test_without_an_a_selection_the_record_and_new_agent_keep_the_standing_value(team):
    finished, value = team.run()
    assert finished and value["state"] == "ready"
    ids = team.config.identity
    stored = team.a.runtime.store.read_record(onboarding_key(ids.tenant_id, ids.team_id, "onboard-one"))
    assert "turn_origin_profile" not in stored.payload["data"]
    standing = team.a.runtime.work_policy.policy.profile.turn_origin_profile
    assert team.a.for_agent(value["agent_id"]).runtime.work_policy.policy.profile.turn_origin_profile == standing


def test_an_onboarding_registered_before_the_profile_change_resumes_on_its_registration(team, monkeypatch):
    """Registered with agent-role v1 before the deploy, resumed after NEW_AGENT_PROFILE became v2."""
    real_save = onboarding._save_role

    def lost(*_args, **_kwargs):
        raise Problem(503, "TEST_RESPONSE_LOST", "the role save was not observed")

    monkeypatch.setattr(onboarding, "NEW_AGENT_PROFILE", "fa-instruction-agent-role-v1")
    monkeypatch.setattr(onboarding, "_save_role", lost)
    with pytest.raises(Problem):
        team.run()
    assert onboarding_status(team.a, team.c.human, "onboard-one")["state"] == "registered"
    monkeypatch.setattr(onboarding, "NEW_AGENT_PROFILE", "fa-instruction-agent-role-v2")
    monkeypatch.setattr(onboarding, "_save_role", real_save)
    finished, value = team.run()
    assert finished and value["state"] == "ready"
    policy = team.a.for_agent(value["agent_id"]).runtime.work_policy.policy
    assert policy.profile.prompt_profile == "fa-instruction-agent-role-v1"


def test_a_selection_written_before_the_profile_change_is_repeated_on_resume(team, monkeypatch):
    """S5 committed under agent-role v1, its response lost; after the switch the same command is sent again."""
    grants = _grants(team, SKILLS_ENTRY_ID)
    _approved(monkeypatch, SKILL)
    chosen = _refs(grants, SKILLS_ENTRY_ID)
    real_select = onboarding._select_tools

    def lost(*args, **kwargs):
        real_select(*args, **kwargs)
        raise Problem(503, "TEST_RESPONSE_LOST", "the selection result was not observed")

    monkeypatch.setattr(onboarding, "NEW_AGENT_PROFILE", "fa-instruction-agent-role-v1")
    monkeypatch.setattr(onboarding, "_select_tools", lost)
    with pytest.raises(Problem):
        team.run(selection=(chosen, (SKILL,)))
    assert onboarding_status(team.a, team.c.human, "onboard-one")["state"] == "registered"
    monkeypatch.setattr(onboarding, "NEW_AGENT_PROFILE", "fa-instruction-agent-role-v2")
    monkeypatch.setattr(onboarding, "_select_tools", real_select)
    finished, value = team.run(selection=(chosen, (SKILL,)))
    assert finished and value["state"] == "ready"
    _, head, profile = _selected(team, value["agent_id"])
    assert head.command.template.role.profile == "fa-instruction-agent-role-v1"
    assert profile.prompt_profile == "fa-instruction-agent-role-v1"


def test_a_resumed_selection_resends_its_saved_command_when_this_build_builds_another(team, monkeypatch):
    """S5 saved under an older build (e.g. with A's catalog budget); the resume resends that command, not a new one."""
    grants = _grants(team, SKILLS_ENTRY_ID)
    _approved(monkeypatch, SKILL)
    chosen = _refs(grants, SKILLS_ENTRY_ID)
    real_select, real_template = onboarding._select_tools, onboarding._selection_template

    def lost(*args, **kwargs):
        real_select(*args, **kwargs)
        raise Problem(503, "TEST_RESPONSE_LOST", "the selection result was not observed")

    monkeypatch.setattr(onboarding, "_select_tools", lost)
    with pytest.raises(Problem):
        team.run(selection=(chosen, (SKILL,)))
    _, saved, _ = _selected(team, onboarding_status(team.a, team.c.human, "onboard-one")["agent_id"])
    monkeypatch.setattr(onboarding, "_select_tools", real_select)

    def other(base, selection, state_profile=None, budget=None):
        template = real_template(base, selection, state_profile, budget)
        return template.model_copy(update={"settings": template.settings.model_copy(
            update={"skill_catalog_budget_profile": "fa-skill-catalog-budget-v1"})})

    monkeypatch.setattr(onboarding, "_selection_template", other)
    finished, value = team.run(selection=(chosen, (SKILL,)))
    assert finished and value["state"] == "ready"
    _, head, _ = _selected(team, value["agent_id"])
    assert head.command == saved.command and head.command.template.settings.skill_catalog_budget_profile is None


# B1 TC (2026-10-03): new native conversations default to auto-no-custom-v1.
def test_a_new_agents_first_selection_defaults_to_automatic_compaction(team, monkeypatch):

    grants = _grants(team, SKILLS_ENTRY_ID)
    _approved(monkeypatch, SKILL)
    assert team.a.runtime.work_policy.policy.profile.checkpoint_profile is None
    finished, value = team.run(selection=(_refs(grants, SKILLS_ENTRY_ID), (SKILL,)))
    assert finished and value["state"] == "ready"
    control, head, profile = _selected(team, value["agent_id"])
    from shared_context.behavior_sdk_state import local_platform
    assert head.command.template.settings.checkpoint_profile.model_dump(mode="json") == {
        "name": "auto-no-custom-v1", "platform": local_platform(), "provider": "openai-completions"}
    assert profile.checkpoint_profile.name == "auto-no-custom-v1"
    # The registered standing profile is unchanged; only the new Agent's own new-conversation selection defaults.
    assert control.policy.profile.checkpoint_profile is None


# TC-04 (2026-10-03): a new Agent's native conversations use the server-declared model facts like A and B's
# selections (stage conversation_defaults), not the runtime's 128000 default.
def test_a_new_agents_first_selection_defaults_to_the_declared_model_capacity(team, monkeypatch):
    grants = _grants(team, SKILLS_ENTRY_ID)
    _approved(monkeypatch, SKILL)
    assert team.a.runtime.work_policy.policy.profile.model_capacity_profile is None
    finished, value = team.run(selection=(_refs(grants, SKILLS_ENTRY_ID), (SKILL,)))
    assert finished and value["state"] == "ready"
    control, head, profile = _selected(team, value["agent_id"])
    assert head.command.template.settings.model_capacity_profile == "fa-model-capacity-v1"
    assert profile.model_capacity_profile == "fa-model-capacity-v1"
    assert control.policy.profile.model_capacity_profile is None


@pytest.fixture
def declared_capacity(instruction_models, tmp_path):  # noqa: F811
    """A declared input window (like gpt-5.4-mini's documented facts) so A can select a catalog budget."""
    facts = tmp_path / "OFFLINE-model-facts.json"
    values = json.loads(facts.read_text(encoding="utf-8"))
    for value in values.values():
        value.update(context_window_tokens=400000, max_prompt_tokens=272000)
    facts.write_text(json.dumps(values), encoding="utf-8")


@pytest.mark.parametrize("a_budget", [True, False], ids=["a-budgeted", "a-unbudgeted"])
def test_a_new_agent_copies_the_catalog_budget_a_actually_uses(declared_capacity, team, monkeypatch, a_budget):
    """SK-03/SK-04: native presents the budget (#75), so the owner's choice keeps A's new-conversation budget."""
    from shared_context.behavior_profile_selection import current_selection
    grants = _grants(team, SKILLS_ENTRY_ID)
    _approved(monkeypatch, SKILL)
    budget = "fa-skill-catalog-budget-v1" if a_budget else None
    control = _a_selects(team, "a-budget", skill_catalog_budget_profile=budget, model_capacity_profile="fa-model-capacity-v1",
                         capabilities=_refs(grants, SKILLS_ENTRY_ID))
    assert control.policy.profile.skill_catalog_budget_profile is None
    a_policy, a_head = control.policy.model_dump_json(), current_selection(control.runtime.store, control.policy)
    finished, value = team.run(selection=(_refs(grants, SKILLS_ENTRY_ID), (SKILL,)))
    assert finished and value["state"] == "ready"
    _, head, profile = _selected(team, value["agent_id"])
    assert head.command.template.settings.skill_catalog_budget_profile == budget
    assert profile.skill_catalog_budget_profile == budget
    stored = team.a.runtime.store.read_record(onboarding_key(
        team.config.identity.tenant_id, team.config.identity.team_id, "onboard-one")).payload["data"]
    assert stored.get("skill_catalog_budget_profile") == budget
    assert control.policy.model_dump_json() == a_policy
    assert current_selection(control.runtime.store, control.policy) == a_head


def test_a_new_agent_without_the_skills_tool_does_not_copy_as_catalog_budget(declared_capacity, team, monkeypatch):
    """The budget presents a skills catalog; an owner choice without the skills tool has none to present."""
    grants = _grants(team, SKILLS_ENTRY_ID)
    _a_selects(team, "a-budget", skill_catalog_budget_profile="fa-skill-catalog-budget-v1",
               model_capacity_profile="fa-model-capacity-v1", capabilities=_refs(grants, SKILLS_ENTRY_ID))
    finished, value = team.run(selection=((), ()))
    assert finished and value["state"] == "ready"
    _, head, profile = _selected(team, value["agent_id"])
    assert head.command.template.settings.skill_catalog_budget_profile is None
    assert profile.skill_catalog_budget_profile is None


def test_a_selection_saved_before_the_default_is_resent_unchanged(team, monkeypatch):
    """S5 saved by the previous build (no checkpoint profile), its reply lost: the resume resends that command."""

    grants = _grants(team, SKILLS_ENTRY_ID)
    _approved(monkeypatch, SKILL)
    chosen = _refs(grants, SKILLS_ENTRY_ID)
    real_select, real_template = onboarding._select_tools, onboarding._selection_template

    def previous_build(base, selection, state_profile=None, budget=None):
        template = real_template(base, selection, state_profile, budget)
        return template.model_copy(update={"settings": template.settings.model_copy(
            update={"checkpoint_profile": None, "model_capacity_profile": None})})

    def lost(*args, **kwargs):
        real_select(*args, **kwargs)
        raise Problem(503, "TEST_RESPONSE_LOST", "the selection result was not observed")

    monkeypatch.setattr(onboarding, "_selection_template", previous_build)
    monkeypatch.setattr(onboarding, "_select_tools", lost)
    with pytest.raises(Problem):
        team.run(selection=(chosen, (SKILL,)))
    monkeypatch.setattr(onboarding, "_selection_template", real_template)
    monkeypatch.setattr(onboarding, "_select_tools", real_select)
    finished, value = team.run(selection=(chosen, (SKILL,)))
    assert finished and value["state"] == "ready"
    _, head, _ = _selected(team, value["agent_id"])
    assert head.command.template.settings.checkpoint_profile is None
    assert head.command.template.settings.model_capacity_profile is None


@pytest.mark.parametrize("a_lane", ["responses", "native"])
def test_a_new_agent_starts_new_conversations_on_the_lane_a_uses(team, monkeypatch, a_lane):
    """A on Responses: the new Agent's own selection is Responses too; A still native: the new Agent stays native."""
    from shared_context.behavior_profile_selection import current_selection
    grants = _grants(team, SKILLS_ENTRY_ID)
    _approved(monkeypatch, SKILL)
    lane = {"responses": "foundry-responses-v1", "native": "foundry-native-v1"}[a_lane]
    control = _a_selects(team, "a-lane", state_profile=lane)
    a_head = current_selection(control.runtime.store, control.policy)
    finished, value = team.run(selection=(_refs(grants, SKILLS_ENTRY_ID), (SKILL,)))
    assert finished and value["state"] == "ready"
    _, head, _ = _selected(team, value["agent_id"])
    assert head.command.template.settings.state_profile == lane
    stored = team.a.runtime.store.read_record(onboarding_key(
        team.config.identity.tenant_id, team.config.identity.team_id, "onboard-one")).payload["data"]
    assert stored.get("state_profile") == (lane if a_lane == "responses" else None)
    assert current_selection(control.runtime.store, control.policy) == a_head

