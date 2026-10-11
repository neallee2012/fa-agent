"""Explicit operator CLI for Team owners; no automatic provisioning or HTTP writer."""

import argparse
import getpass
import json
import os
import sys

from starlette.requests import Request

from .behavior_auth import BehaviorJwtVerifier
from .behavior_server import build_server
from .behavior_server_config import load_configuration
from .errors import Problem


def operate(composition, action, authorization, *, owner_oid, team_id=None, request_id=None):
    if action not in {"show", "register", "reconcile", "deactivate", "deactivate-reconcile"}:
        raise Problem(422, "TEAM_OPERATOR_COMMAND", "one supported operator action is required")
    writing = action in {"register", "deactivate"}
    verifier = composition.verifier
    if not writing:
        # Reconcile is an original operator read, not renewed Team membership or permission to write.
        verifier = BehaviorJwtVerifier(verifier.auth, verifier.settings, verifier.tokens.keys)
    principal = verifier(Request({
        "type": "http", "headers": [(b"authorization", authorization.encode("ascii"))],
    }))
    registry = composition.verifier.teams
    if registry is None:
        raise Problem(503, "TEAM_REGISTRY_UNAVAILABLE", "the server Team registry is not configured")
    registry.require_operator(principal, write=writing)
    if action == "show":
        value = registry.lookup(owner_oid)
        return ({"state": "missing", "owner_oid": owner_oid} if value is None
                else {"state": "registered", "identity": value.model_dump(mode="json")})
    if request_id is None or action in {"register", "reconcile"} and team_id is None:
        raise Problem(422, "TEAM_OPERATOR_COMMAND", "retain the original request ID and registration Team")
    if writing:
        composition.installation()
    if action in {"register", "reconcile"}:
        receipt = registry.register_owner(principal, request_id, team_id, owner_oid, reconcile=not writing)
    else:
        receipt = registry.deactivate_owner(principal, request_id, owner_oid, reconcile=not writing)
    return {"state": "recorded", "request_id": receipt.payload["request"]["request_id"],
            "result": receipt.payload["result"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Register or deactivate one Team owner with the original operator.")
    parser.add_argument("action", choices=("show", "register", "reconcile", "deactivate", "deactivate-reconcile"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--owner-oid", required=True)
    parser.add_argument("--team-id")
    parser.add_argument("--request-id")
    args = parser.parse_args(argv)
    try:
        token = os.environ.get("FA_TEAM_OPERATOR_TOKEN")
        if not token:
            if not sys.stdin.isatty():
                raise Problem(401, "TEAM_OPERATOR_TOKEN_REQUIRED", "the original delegated operator token is required")
            token = getpass.getpass("Original operator bearer token (hidden): ")
        if not token or len(token) > 16384 or any(character.isspace() for character in token):
            raise Problem(401, "TEAM_OPERATOR_TOKEN_INVALID", "one original bearer token is required")
        with build_server(load_configuration(args.config)) as composition:
            result = operate(composition, args.action, "Bearer " + token, owner_oid=args.owner_oid,
                             team_id=args.team_id, request_id=args.request_id)
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
