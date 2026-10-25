"""Operator CLI: retire one registered Team Agent (ADR-0006 amendment). Tombstone first; Foundry cleanup is optional.

1. ``TeamRegistry.retire_agent`` writes the create-only tombstone and deactivates the Agent identity in one CAS (the
   API's own write path). Nothing in the Team's Table is deleted; a rerun returns the original tombstone.
2. ``--delete-foundry`` deletes the Foundry agent with ``force=true`` (without it the service answers 409 while any
   version has sessions; with it every version and session is deleted with the agent) and the FA role assignments
   of that agent's own instance identity at the onboarding role scope (with ``FA_ONBOARDING_ACCOUNT_ROLE=true`` also
   its OpenAI User on the project's account). ``preview`` lists exactly these targets and
   writes nothing; ``retire`` needs ``--confirm <agent-id>``. Every target comes from the tombstoned Agent's original
   registration, never another Agent's. Each part is read before and after, so a rerun reports what is already
   gone and finishes the rest.
3. An onboarding stuck in 'registered' (``--stuck-onboarding``, or registered over an hour ago) is retired
   the same way; the tombstone's CAS also drops its request ID from the pending list. One still finishing is refused,
   unless the Agent is already tombstoned (an older CLI, even while 'creating'): then it reads as already retired and
   ``team-agent-retire-release`` drops the leftover pending entry once.
4. ``--failed-agent-name``: a failed onboarding never registered an Agent, so there is no tombstone; preview and
   ``--confirm <name>`` delete only the Foundry agent whose every version names that failed onboarding.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import json
import logging
import os
import sys
from types import SimpleNamespace

import httpx

from .behavior_agent_onboarding import (
    _METADATA_RID, _METADATA_TEAM, FoundryCallError, ProjectAgentFoundry, _latest_attempt, _read, derived_names,
    onboarding_key, onboarding_not_ready,
)
from .behavior_agent_setup import (
    ACCOUNT_ROLE_ENV, ARM, ARM_API_VERSION, ARM_TOKEN_SCOPE, OPENAI_USER, ROLE_SCOPE_ENV, ROLES, NewAgentSetup,
    _arm_code,
)
from .behavior_operations import _parse
from .behavior_team_registry import TeamAgent, TeamAgentRetired, _roster_ids, agent_key, retired_key
from .errors import Problem

# An onboarding that entered 'registered' this long ago is no longer finishing on its own.
STUCK_ONBOARDING_SECONDS = 3600


def _utcnow():
    return datetime.now(timezone.utc)


class RetiringFoundry(ProjectAgentFoundry):
    """The registered Agent's project; ``delete_agent`` is FA's only agent delete and runs only from this CLI."""

    def list_sessions(self, agent_name):
        return self._call(lambda agents: [item.as_dict() for item in agents.list_sessions(agent_name, limit=100)],
                          read=True)

    def delete_agent(self, agent_name):
        return self._call(lambda agents: agents.delete(agent_name, force=True).as_dict())


class AgentRoleAssignments:
    """One principal's ARM role assignments at the onboarding role scope (what ``behavior_agent_setup`` assigns), and
    with ``account`` also its OpenAI User on the project's account (the account role of Cosmos memory)."""

    def __init__(self, credential, scope, *, account=False, transport=None):
        self.credential, self.scope, self.transport = credential, scope, transport
        self.account = scope.rsplit("/projects/", 1)[0].lower() if account else None

    def _send(self, method, path, **params):
        try:
            token = self.credential.get_token(ARM_TOKEN_SCOPE).token
            with httpx.Client(transport=self.transport or httpx.HTTPTransport(retries=0), trust_env=False,
                              follow_redirects=False, timeout=httpx.Timeout(30, connect=5)) as client:
                return client.request(method, ARM + path, params={"api-version": ARM_API_VERSION, **params},
                                      headers={"Authorization": "Bearer " + token})
        except Exception as error:  # noqa: BLE001 - token or transport: the outcome is unknown, never a token in logs
            raise Problem(503, "ROLE_ASSIGNMENTS_UNAVAILABLE", "ARM could not be reached",
                          exception_type=type(error).__name__) from None

    def list(self, principal_id):
        """(targets, kept): FA's roles exactly at the scope are targets; the principal's other assignments are kept."""
        # ARM lists the assignments above the scope too, so the account-scope one is in this same page.
        # shortcut: one page (no nextLink); an Agent identity has the 2 FA roles (3 with the account role), upgrade if
        # one ever holds 100+.
        response = self._send("GET", self.scope + "/providers/Microsoft.Authorization/roleAssignments",
                              **{"$filter": f"principalId eq '{principal_id}'"})
        try:
            items = response.json().get("value") if response.status_code == 200 else None
        except (ValueError, AttributeError):
            items = None
        if not isinstance(items, list):
            raise Problem(503, "ROLE_ASSIGNMENTS_UNREADABLE", "the agent identity's role assignments could not be read",
                          upstream_status=response.status_code, arm_code=_arm_code(response))
        targets, kept = [], []
        for item in items:
            properties = item.get("properties") if isinstance(item, dict) else None
            if not isinstance(properties, dict) or str(properties.get("principalId", "")).lower() != principal_id:
                continue
            role = str(properties.get("roleDefinitionId", "")).rsplit("/", 1)[-1].lower()
            entry = {"id": item.get("id"), "role": role, "scope": properties.get("scope")}
            scope = str(entry["scope"]).lower()
            fa_role = (scope == self.scope.lower() and role in ROLES) or (scope == self.account and role == OPENAI_USER)
            (targets if fa_role and isinstance(entry["id"], str) else kept).append(entry)
        return targets, kept

    def delete(self, assignment_id):
        try:
            response = self._send("DELETE", assignment_id)
        except Problem as error:
            return "failed " + error.body["error"]["code"]
        if response.status_code == 200:
            return "deleted"
        if response.status_code == 204:
            return "already_deleted"
        # 403: the API identity's ABAC delegation never deletes (ADR-0006 §1); a person removes the listed ID, then reruns.
        kind = "refused" if response.status_code == 403 else "failed"
        return f"{kind} status={response.status_code} code={_arm_code(response)}"


def _registered(composition, agent_id, *, deleting=False, stuck=False, now=_utcnow):
    """(registration, stuck onboarding or None), read directly: ``resolve_agent_scope`` refuses a retired Agent."""
    store, config = composition.runtime.store, composition.config
    row = store.read_record(agent_key(config.identity.tenant_id, agent_id))
    if row is None or any(target.agent_id == agent_id for target in config.targets):
        raise Problem(404, "TEAM_AGENT_NOT_FOUND", "only a registered (non-configured) Agent can be retired")
    agent = _parse(row.payload["data"], TeamAgent)
    target = agent.target
    if agent.team_id != config.identity.team_id or any(
            configured.agent_name == target.agent_name or configured.agent_oid == target.agent_oid
            for configured in config.targets):
        raise Problem(409, "TEAM_AGENT_CONFLICT", "the registration shares a configured Agent's name or identity")
    # --delete-foundry deletes by (project, name): refuse when any other roster Agent, retired or not, has the same.
    others = () if not deleting else tuple(
        agent_key(config.identity.tenant_id, other) for other in _roster_ids(store, config) if other != agent_id)
    for other in store.read_snapshot(others).values() if others else ():
        if other is not None and _same_foundry_agent(_parse(other.payload["data"], TeamAgent).target, target):
            raise Problem(409, "TEAM_AGENT_CONFLICT", "another registered Agent has the same Foundry project and name")
    # Read directly: a crash between register_agent and the 'registered' step leaves it 'creating' with no target.
    current = _read(store, onboarding_key(config.identity.tenant_id, agent.team_id, agent.request_id))
    if current is None or current.state in {"ready", "failed"} or (
            current.target is not None and current.target.agent_id != agent_id):
        return agent, None
    # Already tombstoned (e.g. by an older CLI while 'creating'): a resend 410s, so nothing is finishing; release it.
    if store.read_record(retired_key(config.identity.tenant_id, agent_id)) is not None:
        return agent, current
    if current.state != "registered":
        raise onboarding_not_ready()
    # Stuck when the operator says so or it entered 'registered' long ago (older rows: its last create claim).
    since = None if stuck else current.registered_at or getattr(_latest_attempt(store, current), "claimed_at", None)
    if not stuck and (since is None or (now() - since).total_seconds() < STUCK_ONBOARDING_SECONDS):
        raise onboarding_not_ready()
    return agent, current


def _same_foundry_agent(left, right):
    return (left.project_endpoint.lower(), left.agent_name.casefold()) == (
        right.project_endpoint.lower(), right.agent_name.casefold())


def _foundry_state(foundry, name):
    try:
        foundry.get_agent(name)
    except FoundryCallError as error:
        if error.status == 404:
            return {"name": name, "state": "absent"}
        raise Problem(503, "FOUNDRY_AGENT_UNREADABLE", "the Foundry agent could not be read",
                      upstream_status=error.status) from None
    try:
        versions = sorted((str(item.get("version")) for item in foundry.list_versions(name)), key=lambda v: (len(v), v))
        sessions = [{"id": item.get("agent_session_id"), "status": item.get("status")}
                    for item in foundry.list_sessions(name)]
    except FoundryCallError as error:
        raise Problem(503, "FOUNDRY_AGENT_UNREADABLE", "the Foundry agent's versions or sessions could not be read",
                      upstream_status=error.status) from None
    return {"name": name, "state": "present", "versions": versions, "sessions": sessions}


def preview(composition, agent_id, *, foundry=None, roles=None, stuck=False, now=_utcnow):
    """Read-only: the tombstone (or that one would be written) and, with Foundry cleanup, exactly what it removes."""
    agent, onboarding = _registered(composition, agent_id, deleting=foundry is not None, stuck=stuck, now=now)
    tombstone = composition.runtime.store.read_record(retired_key(composition.config.identity.tenant_id, agent_id))
    result = {"agent_id": agent_id, "agent_name": agent.target.agent_name, "agent_oid": agent.target.agent_oid,
              "tombstone": None if tombstone is None
              else _parse(tombstone.payload["data"], TeamAgentRetired).model_dump(mode="json")}
    if onboarding is not None:
        # The tombstone's CAS also drops this request ID from the pending list (one slot back; the roster slot stays).
        result["stuck_onboarding"] = {"request_id": onboarding.request_id, "state": onboarding.state}
    if foundry is not None:
        result["foundry_agent"] = _foundry_state(foundry, agent.target.agent_name)
    if roles is not None:
        result["role_assignments"], result["kept_role_assignments"] = roles.list(agent.target.agent_oid)
    return result


def retire(composition, principal, *, agent_id, request_id, reason, confirm, foundry=None, roles=None, stuck=False,
           now=_utcnow):
    """Tombstone (idempotent), then the confirmed Foundry cleanup; ``state`` is ok only when it reads back done."""
    if confirm != agent_id:
        raise Problem(409, "RETIRE_CONFIRMATION_REQUIRED", "read the preview, then confirm with the exact Agent ID")
    teams = getattr(composition.verifier, "teams", None)
    if teams is None:
        raise Problem(503, "TEAM_REGISTRY_UNAVAILABLE", "the server Team registry is not configured")
    teams.require_operator(principal, write=True)
    _, onboarding = _registered(composition, agent_id, deleting=foundry is not None, stuck=stuck, now=now)
    composition.installation()
    value, created = teams.retire_agent(principal, request_id, agent_id, reason, now=now(), onboarding=onboarding)
    result = {"agent_id": agent_id, "state": "ok",
              "tombstone": {"outcome": "written" if created else "already_retired", **value.model_dump(mode="json")}}
    if foundry is None and roles is None:
        return result
    before = preview(composition, agent_id, foundry=foundry, roles=roles, stuck=stuck, now=now)
    current = before.get("foundry_agent", {"state": "absent"})
    outcome = "already_deleted"
    if foundry is not None and current["state"] == "present":
        try:
            foundry.delete_agent(current["name"])
            outcome = "deleted"
        except FoundryCallError as error:
            outcome = "already_deleted" if error.status == 404 else f"failed status={error.status}"
    if foundry is not None:
        result["foundry_agent"] = {**current, "outcome": outcome}
    if roles is not None:
        result["role_assignments"] = [{**item, "outcome": roles.delete(item["id"])}
                                      for item in before["role_assignments"]]
        result["kept_role_assignments"] = before["kept_role_assignments"]
    after = preview(composition, agent_id, foundry=foundry, roles=roles, stuck=stuck, now=now)
    remaining = {"foundry_agent": after.get("foundry_agent", {}).get("state", "absent"),
                 "role_assignments": after.get("role_assignments", [])}
    if remaining["foundry_agent"] != "absent" or remaining["role_assignments"]:
        result.update(state="partial", remaining=remaining)
    return result


def preview_failed(composition, agent_name, *, foundry):
    """Read-only: the Foundry agent a failed onboarding left behind (it never got a Team registration to retire).

    Only an agent whose every version names one onboarding of this Team, whose derived name it is and which is
    'failed'; a configured or roster Agent's name is refused. FA assigns roles only to an active version, so none."""
    config, store = composition.config, composition.runtime.store
    tenant, team = config.identity.tenant_id, config.identity.team_id
    probe = SimpleNamespace(project_endpoint=config.targets[0].project_endpoint, agent_name=agent_name)
    roster = tuple(agent_key(tenant, other) for other in _roster_ids(store, config))
    if any(_same_foundry_agent(target, probe) for target in config.targets) or any(
            row is not None and _same_foundry_agent(_parse(row.payload["data"], TeamAgent).target, probe)
            for row in (store.read_snapshot(roster).values() if roster else ())):
        raise Problem(409, "TEAM_AGENT_CONFLICT", "the name belongs to a configured or registered Agent")
    state = _foundry_state(foundry, agent_name)
    if state["state"] == "absent":
        return {"agent_name": agent_name, "foundry_agent": state}
    try:
        metadata = [item.get("metadata") or {} for item in foundry.list_versions(agent_name)]
    except FoundryCallError as error:
        raise Problem(503, "FOUNDRY_AGENT_UNREADABLE", "the Foundry agent's versions could not be read",
                      upstream_status=error.status) from None
    rids = {item.get(_METADATA_RID) for item in metadata}
    rid = next(iter(rids)) if len(rids) == 1 else None
    if (not isinstance(rid, str) or {item.get(_METADATA_TEAM) for item in metadata} != {team}
            or derived_names(tenant, team, rid)["agent_name"] != agent_name):
        raise Problem(409, "FAILED_ONBOARDING_MISMATCH", "the agent's versions do not all belong to one onboarding "
                                                          "of this Team under its derived name")
    current = _read(store, onboarding_key(tenant, team, rid))
    if current is None or current.state != "failed":
        raise Problem(409, "AGENT_ONBOARDING_NOT_FAILED", "only a failed onboarding's leftover agent is deleted here")
    return {"agent_name": agent_name, "foundry_agent": state,
            "onboarding": {"request_id": rid, "state": current.state, "reason": current.reason}}


def retire_failed(composition, principal, *, agent_name, confirm, foundry):
    """Delete the previewed leftover (force=true) and read back; writes nothing to the Team's Table."""
    if confirm != agent_name:
        raise Problem(409, "RETIRE_CONFIRMATION_REQUIRED", "read the preview, then confirm with the exact agent name")
    teams = getattr(composition.verifier, "teams", None)
    if teams is None:
        raise Problem(503, "TEAM_REGISTRY_UNAVAILABLE", "the server Team registry is not configured")
    teams.require_operator(principal, write=True)
    before = preview_failed(composition, agent_name, foundry=foundry)
    current, outcome = before["foundry_agent"], "already_deleted"
    if current["state"] == "present":
        try:
            foundry.delete_agent(agent_name)
            outcome = "deleted"
        except FoundryCallError as error:
            outcome = "already_deleted" if error.status == 404 else f"failed status={error.status}"
    logging.getLogger(__name__).warning("FA_RETIRE_FAILED_LEFTOVER agent_name=%s request_id=%s outcome=%s",
                                        agent_name, before.get("onboarding", {}).get("request_id"), outcome)
    result = {**before, "state": "ok", "foundry_agent": {**current, "outcome": outcome}}
    if _foundry_state(foundry, agent_name)["state"] != "absent":
        result.update(state="partial", remaining={"foundry_agent": "present"})
    return result


def _cleanup(composition, agent_id, role_scope, *, stuck=False):
    """The Foundry and ARM clients for the registered Agent's own project, with the API's credential."""
    agent, _ = _registered(composition, agent_id, deleting=True, stuck=stuck)
    credential = composition.clients[0].credential
    scope = role_scope or os.environ.get(ROLE_SCOPE_ENV)
    checked = NewAgentSetup.from_environment(agent.target.project_endpoint, credential, environ={
        ROLE_SCOPE_ENV: scope or "", ACCOUNT_ROLE_ENV: os.environ.get(ACCOUNT_ROLE_ENV, "")})
    if checked.role_scope is None:
        raise Problem(422, "ROLE_SCOPE_INVALID", f"--role-scope (or {ROLE_SCOPE_ENV}) must name the Agent's project")
    return RetiringFoundry(agent.target.project_endpoint, credential), AgentRoleAssignments(
        credential, checked.role_scope, account=checked.account_role)


def main(argv=None):
    from starlette.requests import Request
    from .behavior_server import build_server
    from .behavior_server_config import load_configuration
    parser = argparse.ArgumentParser(prog="agent-retire", description=(
        "Retire one registered Team Agent: an append-only tombstone; with --delete-foundry also its Foundry agent "
        "(force=true) and its identity's FA role assignments. Run preview first."))
    parser.add_argument("action", choices=("preview", "retire"))
    parser.add_argument("--config", required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--agent-id")
    target.add_argument("--failed-agent-name",
                        help="instead: delete the Foundry agent a failed onboarding left behind (no tombstone)")
    parser.add_argument("--request-id", help="retire: the original retirement request ID")
    parser.add_argument("--reason", help="retire: short reason kept in the tombstone")
    parser.add_argument("--confirm", help="retire: the exact Agent ID (or failed agent name) again, after the preview")
    parser.add_argument("--delete-foundry", action="store_true",
                        help="also delete the Foundry agent (force=true) and its identity's FA role assignments")
    parser.add_argument("--role-scope", help=f"the onboarding role scope (default: {ROLE_SCOPE_ENV})")
    parser.add_argument("--stuck-onboarding", action="store_true",
                        help="retire an Agent whose onboarding stays 'registered' even if its create is recent")
    args = parser.parse_args(argv)
    if args.action == "retire" and (args.confirm is None or args.agent_id is not None and (
            args.request_id is None or args.reason is None)):
        parser.error("retire requires --confirm, and with --agent-id also --request-id and --reason")
    try:
        token = os.environ.get("FA_TEAM_OPERATOR_TOKEN")
        if not token:
            if not sys.stdin.isatty():
                raise Problem(401, "TEAM_OPERATOR_TOKEN_REQUIRED", "the original delegated operator token is required")
            token = getpass.getpass("Original operator bearer token (hidden): ")
        if not token or len(token) > 16384 or any(character.isspace() for character in token):
            raise Problem(401, "TEAM_OPERATOR_TOKEN_INVALID", "one original bearer token is required")
        with build_server(load_configuration(args.config)) as composition:
            principal = composition.verifier(Request({
                "type": "http", "headers": [(b"authorization", ("Bearer " + token).encode("ascii"))]}))
            teams = getattr(composition.verifier, "teams", None)
            if teams is None:
                raise Problem(503, "TEAM_REGISTRY_UNAVAILABLE", "the server Team registry is not configured")
            teams.require_operator(principal, write=args.action == "retire")
            if args.failed_agent_name is not None:
                # Onboarding creates every agent in A's project (default_foundry).
                foundry = RetiringFoundry(composition.config.targets[0].project_endpoint,
                                          composition.clients[0].credential)
                result = ({"state": "preview", **preview_failed(composition, args.failed_agent_name, foundry=foundry)}
                          if args.action == "preview" else retire_failed(
                              composition, principal, agent_name=args.failed_agent_name, confirm=args.confirm,
                              foundry=foundry))
            else:
                stuck = args.stuck_onboarding
                foundry, roles = (_cleanup(composition, args.agent_id, args.role_scope, stuck=stuck)
                                  if args.delete_foundry else (None, None))
                if args.action == "preview":
                    result = {"state": "preview", **preview(composition, args.agent_id, foundry=foundry, roles=roles,
                                                            stuck=stuck)}
                else:
                    result = retire(composition, principal, agent_id=args.agent_id, request_id=args.request_id,
                                    reason=args.reason, confirm=args.confirm, foundry=foundry, roles=roles,
                                    stuck=stuck)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["state"] in {"ok", "preview"} else 1
    except Problem as error:
        print(json.dumps({"error": {"code": error.body["error"]["code"], "status": error.status}},
                         separators=(",", ":")), file=sys.stderr)
        return 1
    except (ValueError, OSError):
        print('{"error":{"code":"AGENT_RETIRE_CONFIGURATION_INVALID"}}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
