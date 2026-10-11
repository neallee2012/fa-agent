"""Operator-file bootstrap for an existing, empty Table; no login or provisioning."""

import argparse
import json
from pathlib import Path
import sys

from .behavior_fresh_installation import (
    FreshInitializer, FreshInstallationAuthorization, FreshInstallationInputs, FreshInstallationPlan,
)
from .behavior_server import build_server
from .behavior_server_config import load_configuration, read_document
from .errors import Problem
from .source_materials import digest


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Approved empty-Table initialization; existing data is never reset, migrated or deleted.")
    commands = parser.add_subparsers(dest="action", required=True)
    schema = commands.add_parser("schema")
    schema.add_argument("kind", choices=("inputs", "plan", "authorization"))
    plan = commands.add_parser("plan")
    plan.add_argument("--config", type=Path, required=True)
    plan.add_argument("--inputs", type=Path, required=True)
    plan.add_argument("--request-id", required=True)
    plan.add_argument("--installation-id", required=True)
    plan.add_argument("--output", type=Path, required=True, help="New private plan file; never overwritten.")
    for action in ("apply", "reconcile"):
        command = commands.add_parser(action)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--plan", type=Path, required=True)
        command.add_argument("--authorization", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "schema":
            model = {"inputs": FreshInstallationInputs, "plan": FreshInstallationPlan,
                     "authorization": FreshInstallationAuthorization}[args.kind]
            result = model.model_json_schema()
        else:
            config = load_configuration(args.config)
            with build_server(config) as composition:
                installer = FreshInitializer(composition)
                if args.action == "plan":
                    inputs = read_document(args.inputs, FreshInstallationInputs)
                    value = installer.plan(inputs, request_id=args.request_id, installation_id=args.installation_id)
                    with args.output.open("x", encoding="utf-8", newline="\n") as output:
                        output.write(value.model_dump_json(indent=2) + "\n")
                    result = {
                        "state": "planned", "table_writes": 0, "request_id": value.request_id,
                        "installation_id": value.installation_id, "configuration_sha256": config.sha256,
                        "plan_sha256": value.sha256, "input_sha256": value.input_sha256,
                        "approval_required": True, "native_runtime_prepared": False,
                    }
                else:
                    value = read_document(args.plan, FreshInstallationPlan)
                    approval = read_document(args.authorization, FreshInstallationAuthorization)
                    receipt = installer.execute(value, approval, reconcile=args.action == "reconcile")
                    result = {**receipt.payload["result"], "receipt_sha256": digest(receipt.payload)}
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
        return 0
    except Problem as error:
        details = error.body["error"]
        result = {"error": {"code": details["code"], "status": error.status}}
        for name in ("request_id", "request_digest"):
            if name in details:
                result["error"][name] = details[name]
        print(json.dumps(result, separators=(",", ":")), file=sys.stderr)
        return 1
    except (ValueError, OSError):
        print('{"error":{"code":"FRESH_OPERATOR_CONFIGURATION_INVALID"}}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
