"""Explicit CLI for one registered owner's Team (ADR-0006 T6a/T6b).

The original operator initializes a Team from the organization defaults in the server config, keeping or
lowering them. The Team's active owner may afterwards only lower its own Team's values (`narrow`). Every
command keeps one request ID: a lost reply is read back with the matching reconcile action, never re-sent
under a new ID.
"""

import argparse
import getpass
import json
import os
import sys

from starlette.requests import Request

from .behavior_auth import BehaviorJwtVerifier
from .behavior_team_authority import initialize_team, narrow_team, read_team_authority
from .errors import Problem

ACTIONS = ("show", "init", "reconcile", "narrow", "narrow-reconcile")


def _shown(team_id, found):
    if found is None:
        return {"state": "missing", "team_id": team_id}
    team, ledger = found
    return {"state": "initialized", "team": team.model_dump(mode="json"), "limits": team.limits.model_dump(mode="json"),
            "ledger": ledger.model_dump(mode="json")}


def operate(composition, action, authorization, *, team_id, request_id=None, model_cap_microusd=None,
            max_agents=None, connectors=None):
    if action not in ACTIONS:
        raise Problem(422, "TEAM_AUTHORITY_COMMAND", "one supported Team action is required")
    registry = composition.verifier.teams
    if registry is None:
        raise Problem(503, "TEAM_REGISTRY_UNAVAILABLE", "the server Team registry is not configured")
    owner_action = action.startswith("narrow")
    verifier = composition.verifier
    if not owner_action and action != "init":
        # Reading back is not renewed Team membership or permission to write.
        verifier = BehaviorJwtVerifier(verifier.auth, verifier.settings, verifier.tokens.keys)
    principal = verifier(Request({"type": "http", "headers": [(b"authorization", authorization.encode("ascii"))]}))
    store = registry.store
    if owner_action:
        if request_id is None:
            raise Problem(422, "TEAM_AUTHORITY_COMMAND", "retain the original request ID")
        if action == "narrow":
            composition.installation()
        receipt = narrow_team(store, registry, principal, request_id, team_id,
                              model_cap_microusd=model_cap_microusd, max_agents=max_agents,
                              connectors=connectors, reconcile=action == "narrow-reconcile")
        return {"state": "recorded", "request_id": receipt.payload["request"]["request_id"],
                "result": receipt.payload["result"]}
    writing = action == "init"
    registry.require_operator(principal, write=writing)
    if action == "show":
        return _shown(team_id, read_team_authority(store, store._scope.authority_domain, team_id))
    if request_id is None:
        raise Problem(422, "TEAM_AUTHORITY_COMMAND", "retain the original request ID")
    if writing:
        composition.installation()
    receipt = initialize_team(store, registry, principal, request_id, team_id,
                              defaults=composition.config.team_defaults, model_cap_microusd=model_cap_microusd,
                              max_agents=max_agents, connectors=connectors, reconcile=not writing)
    return {"state": "recorded", "request_id": receipt.payload["request"]["request_id"],
            "result": receipt.payload["result"]}


def _connectors(args):
    if args.no_connectors and args.connector:
        raise Problem(422, "TEAM_AUTHORITY_COMMAND", "choose connectors or --no-connectors, not both")
    if args.no_connectors:
        return ()
    return tuple(args.connector) if args.connector else None


def main(argv=None):
    from .behavior_server import build_server
    from .behavior_server_config import load_configuration

    parser = argparse.ArgumentParser(
        description="Initialize a registered owner's Team (operator) or lower its values (that Team's owner).")
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--config", required=True)
    parser.add_argument("--team-id", required=True)
    parser.add_argument("--request-id")
    parser.add_argument("--model-cap-microusd", type=int)
    parser.add_argument("--max-agents", type=int)
    parser.add_argument("--connector", action="append", default=[],
                        help="omit to keep the organization list (init) or the current list (narrow)")
    parser.add_argument("--no-connectors", action="store_true", help="explicitly choose no connector")
    args = parser.parse_args(argv)
    try:
        connectors = _connectors(args)
        token = os.environ.get("FA_TEAM_OPERATOR_TOKEN")
        if not token:
            if not sys.stdin.isatty():
                raise Problem(401, "TEAM_OPERATOR_TOKEN_REQUIRED", "the original delegated bearer token is required")
            token = getpass.getpass("Original operator or Team owner bearer token (hidden): ")
        if not token or len(token) > 16384 or any(character.isspace() for character in token):
            raise Problem(401, "TEAM_OPERATOR_TOKEN_INVALID", "one original bearer token is required")
        with build_server(load_configuration(args.config)) as composition:
            result = operate(composition, args.action, "Bearer " + token, team_id=args.team_id,
                             request_id=args.request_id, model_cap_microusd=args.model_cap_microusd,
                             max_agents=args.max_agents, connectors=connectors)
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
        return 0
    except Problem as error:
        print(json.dumps({"error": {"code": error.body["error"]["code"], "status": error.status}},
                         separators=(",", ":")), file=sys.stderr)
        return 1
    except (ValueError, OSError):
        print('{"error":{"code":"TEAM_OPERATOR_CONFIGURATION_INVALID"}}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
