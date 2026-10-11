"""One-off, signed-human peer setup; no HTTP route, worker launch or new ledger."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from dataclasses import replace

from starlette.requests import Request

from .behavior_agent_registry import (
    _check_policy, _result, read_agent_registry, register_agent_scope, registration_request, resolve_agent_policy,
)
from .auth import Actor
from .behavior_auth import BehaviorPrincipal
from .behavior_operations import control_key
from .behavior_server import build_server
from .behavior_server_config import load_configuration
from .behavior_team_registry import resolve_agent_scope
from .behavior_work_policy import AgentScopeReference, SteadyWorkPolicy
from .errors import Problem
from .models import CreateWork
from .service import ContextService
from .source_materials import digest


def _peer_policy(config, original, agent_id, request_id, scope=None):
    scope = config.agent_scope(agent_id) if scope is None else scope
    if (scope.legacy_keys or original.agent_scope is not None
            or original.legacy_template_policy.source_visibility != "team"
            or original.profile.memory_scope is not None or original.profile.instruction is not None):
        raise Problem(403, "AGENT_OPERATOR_TEMPLATE_DENIED",
                      "only a configured peer and the original shared Team template may be registered")
    seed = control_key("agent-operator-registration", original.authority_domain, agent_id, request_id)

    def named(kind):
        return control_key("agent-operator-" + kind, seed)

    binding = original.template.model_copy(update={
        "binding_id": named("binding"), "agent_oid": scope.identity.oid,
        "owner": original.template.owner.model_copy(update={
            "owner_record_id": named("owner"), "conversation_id": named("conversation"),
            "policy_id": named("operation-policy"), "principal_id": scope.identity.oid,
        }),
        "event_id": named("event"), "job_id": named("job"),
        "work": original.template.work.model_copy(update={"work_id": named("work")}),
        "operation_id": named("operation"), "attempt_id": named("attempt"), "grant_id": named("grant"),
        "source_ids": (original.profile.source.source_id,), "claim_ids": (original.profile.claim_id,),
    })
    profile = original.profile.model_copy(update={
        "binding_id": binding.binding_id, "legacy_source_work": named("source-work"),
        "artifact_id": named("artifact"), "memory_scope": None,
    })
    policy = original.model_copy(update={
        "policy_id": named("policy"), "template": binding, "profile": profile,
        "legacy_template_policy": original.legacy_template_policy.model_copy(update={
            "binding_digest": digest(["fa-behavior-api-v1", original.evidence_mode,
                                      binding.model_dump(mode="json")]),
        }),
        "agent_scope": AgentScopeReference(registry_id=request_id, agent_id=agent_id),
    })
    policy = SteadyWorkPolicy.model_validate_json(policy.model_dump_json())
    _check_policy(config, original, policy, scope)
    return policy


def _view(registry):
    return {
        "state": "registered", **_result(registry),
        "agent_name": registry.scopes[1].target.agent_name,
        "original_target_version": registry.scopes[1].target.agent_version,
        "source_work": registry.policy.profile.legacy_source_work,
        "control_request_id": registration_request(registry).request_id,
        "control_request_digest": registration_request(registry).request_digest,
    }


def _owned_result_work(composition, policy):
    scope = resolve_agent_scope(composition.runtime.store, composition.config, policy.agent_id)
    context = composition.assistant.artifacts.context
    actor = context.auth.actors.get(scope.identity.oid)
    if actor is None:
        # A Team-registry Agent is resolved above, never in the static allowlist.
        actor = Actor(scope.identity.oid, scope.identity.actor, scope.team_id)
        auth = replace(context.auth, actors={**context.auth.actors, actor.oid: actor})
        context = ContextService(context.repository, context.memory, auth)
    identifier = policy.profile.legacy_source_work
    goal = "Result storage for registered assistant " + policy.agent_id
    try:
        current = context.get(actor, scope.team_id, identifier)
    except Problem as error:
        if error.status != 404 or error.body["error"]["code"] != "NOT_FOUND":
            raise
        # The verified Team approver assigns an empty work to the configured B
        # owner. No A content, current profile selection or private memory is copied.
        current = context.create(actor, scope.team_id, identifier, CreateWork(goal=goal))
    if (current.payload.get("work_id") != identifier or current.payload.get("team") != scope.team_id
            or current.payload.get("owner") != actor.actor or current.payload.get("goal") != goal
            or current.payload.get("revision") != 1 or current.payload.get("status") != "draft"
            or current.payload.get("progress") != "" or current.payload.get("artifact", {}).get("text") != ""):
        raise Problem(409, "AGENT_OPERATOR_SOURCE_CONFLICT",
                      "the original empty B-owned result work differs; do not replace it")


def operate(composition, action, agent_id, authorization, *, request_id=None):
    """Verify the original Entra human before reading or preparing this peer."""
    if action not in {"show", "register", "reconcile"}:
        raise Problem(422, "AGENT_OPERATOR_COMMAND", "show, register or reconcile is required")
    request = Request({"type": "http", "headers": [(b"authorization", authorization.encode("ascii"))]})
    principal = composition.verifier(request)
    identity = composition.config.identity
    if (not isinstance(principal, BehaviorPrincipal) or principal.kind != "human"
            or principal.actor_id is not None or principal.tenant_id != identity.tenant_id
            or principal.oid != identity.human_oid):
        raise Problem(403, "AGENT_SCOPE_DENIED", "the original verified human is required")
    principal.require(identity.team_id, "approve" if action == "register" else "read")
    scope = resolve_agent_scope(composition.runtime.store, composition.config, agent_id)
    if scope.legacy_keys:
        raise Problem(403, "AGENT_SCOPE_DENIED", "the original A installation must not be replaced")
    if action == "register" and composition.runtime.work_policy is None:
        composition.installation()
    store = composition.runtime.store
    try:
        existing = read_agent_registry(store, composition.config, agent_id)
    except Problem as error:
        if error.body["error"]["code"] != "AGENT_SCOPE_MISSING":
            raise
        existing = None
    if action == "show":
        return ({"state": "missing", "agent_id": agent_id, "agent_name": scope.target.agent_name,
                 "runtime_activated": False} if existing is None else _view(existing))
    if request_id is None:
        raise Problem(422, "AGENT_OPERATOR_COMMAND", "retain one original registration request ID")
    original = resolve_agent_policy(store, composition.config, composition.config.targets[0].agent_id)
    policy = _peer_policy(composition.config, original, agent_id, request_id, scope)
    if existing is not None and existing.policy != policy:
        raise Problem(409, "AGENT_SCOPE_CONFLICT", "the original peer registration cannot be replaced")
    if action == "register" and existing is None:
        _owned_result_work(composition, policy)
    register_agent_scope(store, composition.config, policy, principal,
                         now=composition.runtime.clock(), reconcile=action == "reconcile")
    return _view(read_agent_registry(store, composition.config, agent_id))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="One-off B registration with the API's managed identity and original delegated human.")
    parser.add_argument("action", choices=("show", "register", "reconcile"))
    parser.add_argument("--config", required=True, help="The exact current API configuration file.")
    parser.add_argument("--agent-id", required=True, help="An original configured peer agent_id, not an OID.")
    parser.add_argument("--request-id", help="One retained ID for register/reconcile; never replace it after unknown.")
    args = parser.parse_args(argv)
    try:
        token = os.environ.get("FA_AGENT_OPERATOR_TOKEN")
        if not token:
            if not sys.stdin.isatty():
                raise Problem(401, "AGENT_OPERATOR_TOKEN_REQUIRED", "an original delegated human token is required")
            token = getpass.getpass("Original delegated human bearer token (hidden): ")
        if not token or len(token) > 16384 or any(char.isspace() for char in token):
            raise Problem(401, "AGENT_OPERATOR_TOKEN_INVALID", "one original bearer token is required")
        with build_server(load_configuration(args.config)) as composition:
            result = operate(composition, args.action, args.agent_id, "Bearer " + token,
                             request_id=args.request_id)
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
        return 0
    except Problem as error:
        print(json.dumps({"error": {"code": error.body["error"]["code"], "status": error.status}},
                         separators=(",", ":")), file=sys.stderr)
        return 1
    except (ValueError, OSError):
        print('{"error":{"code":"AGENT_OPERATOR_CONFIGURATION_INVALID"}}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
