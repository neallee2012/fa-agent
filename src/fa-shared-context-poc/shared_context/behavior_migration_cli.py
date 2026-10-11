"""Operator-only CLI. Reads existing config/evidence; never bootstraps or deploys.

plan/authorize are review stages; apply requires their exact retained files and
an exclusive local attempt marker. Unknown outcomes only use reconcile/verify.
No credentials are persisted. An existing private-network operator environment
may use --storage-identity with a real delegated operator JWT supplied on stdin;
never embed that JWT in ACA environment variables, ARM parameters, or files.

Operator sequence (parent approval is required before native apply):
1. Freeze the installed package. Save a NEW candidate config changing ONLY the
   two protection evidence_sha256 pins; do not overwrite the old config.
2. Keep old-evidence as historical input. Obtain current exact sources/backups
   observations from real reads; source/backup native policy must be unchanged.
3. plan -> review full quiescence/guard/ETags/preserved hashes -> authorize with
   an explicit short UTC expiry -> apply ONCE -> verify immediately.
4. After an uncertain response, reconcile the original plan/authorization only.
   Missing receipts remain unknown. Never retry through a new scope or reset.
5. The migration does NOT activate the API or mint worker/consumer evidence.
   Publish genuinely fresh worker/inbox readiness via their existing explicit
   publisher methods under the NEW config, never bootstrap/initialize again.
   The installation reader uses the exact retained ORIGINAL atomic probe.
6. While unchanged runtime policy is still valid, native GET of an already
   expired source must remain409; independent recovery with the retained exact
   backup reference/current recovery authorization must return quarantined
   exact bytes, never activation. A stopped API404 or expired overall policy is
   BLOCKED, not source expiry PASS. No TTL/ACL extension is part of migration.

The SAME EGT advances only the guard envelope to v2, binding it to the new
installation SHA. Business rows/receipts remain v1 with their original binding
and bytes. Unmodified old binaries reject that guard; migration-aware clients
use an exact logical projection and preserve v2 on every later transaction.
Do not downgrade the guard, use a raw legacy store, or run bootstrap afterward.
No existing authority/partition/work/pool or budget/owner/revocation is rebuilt.

All guarded writers, including old v1 binaries, are fenced. Out-of-band administrator writes
are not prevented by Azure Table ETags on unrelated rows. Approval time is
checked in the decision and final send hook; Azure Tables has no server-side
NOW predicate, so arbitrarily delayed network commit time is not guaranteed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from azure.data.tables import TableClient
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential, ManagedIdentityCredential
from starlette.requests import Request

from .behavior_auth import BehaviorAuthSettings, BehaviorJwtVerifier
from .behavior_migration import MigrationAuthorization, MigrationPlan, MigrationStore, ProtectionMigration
from .behavior_server_config import ServerConfiguration
from .errors import Problem
from .source_materials import canonical


def _write(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _input(path):
    return Path(path).read_bytes()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "authorize", "apply", "reconcile", "verify"))
    for flag in ("old-config", "new-config", "old-evidence", "sources", "backups", "output"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--migration-id")
    parser.add_argument("--decision-id")
    parser.add_argument("--expires-at", help="Explicit UTC authorization expiry, within fresh observation validity")
    parser.add_argument("--plan")
    parser.add_argument("--authorization")
    parser.add_argument("--operator-token-stdin", action="store_true")
    parser.add_argument("--storage-identity", help="Existing authorized MI client ID; no role assignment is performed")
    args = parser.parse_args(argv)
    if args.operation == "plan" and not args.migration_id:
        parser.error("plan requires --migration-id")
    if args.operation != "plan" and not args.plan:
        parser.error("authorize/apply/reconcile/verify require the retained --plan")
    if args.operation == "authorize" and (not args.decision_id or not args.expires_at):
        parser.error("authorize requires --decision-id and --expires-at")
    if args.operation in {"apply", "reconcile", "verify"} and not args.authorization:
        parser.error("apply/reconcile/verify require --authorization")
    if Path(args.output).exists():
        parser.error("output already exists; original artifacts are never overwritten")
    old = ServerConfiguration.model_validate_json(_input(args.old_config))
    new = ServerConfiguration.model_validate_json(_input(args.new_config))
    prior = json.loads(_input(args.old_evidence))
    previous = {lane: canonical(prior[lane]) for lane in ("sources", "backups")}
    observations = {lane: _input(getattr(args, lane)) for lane in ("sources", "backups")}
    if args.storage_identity and args.storage_identity != old.identity.managed_identity_client_id:
        parser.error("storage MI must be the existing configured application MI")
    with ExitStack() as resources:
        subscription = old.table.account_resource_id.split("/")[2]
        user = AzureCliCredential(subscription=subscription)
        resources.callback(user.close)
        token = sys.stdin.readline(32768).strip() if args.operator_token_stdin else user.get_token(
            old.identity.audience.rstrip("/") + "/.default").token
        verifier = BehaviorJwtVerifier(
            old.identity.auth(), BehaviorAuthSettings(old.identity.human_oid, old.identity.admin_client_id,
                                                       old.identity.team_id),
        )
        principal = verifier(Request({"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())]}))
        del token
        credential = user
        if args.storage_identity:
            credential = ManagedIdentityCredential(client_id=args.storage_identity)
            resources.callback(credential.close)
        table = TableClient(old.table.endpoint, old.table.scope.table_name, credential=credential,
                            retry_total=0, retry_connect=0, retry_read=0, retry_status=0,
                            connection_timeout=10, read_timeout=30)
        resources.callback(table.close)
        service = ProtectionMigration(
            MigrationStore(table, old.table.scope), old, new, observations, previous_observations=previous,
            clock=lambda: datetime.now(timezone.utc),
        )
        if args.operation == "plan":
            result = service.plan(principal, migration_id=args.migration_id).model_dump(mode="json")
        else:
            plan = MigrationPlan.model_validate_json(_input(args.plan))
            if args.operation == "authorize":
                result = service.authorize(
                    principal, plan, decision_id=args.decision_id,
                    expires_at=datetime.fromisoformat(args.expires_at.replace("Z", "+00:00")),
                ).model_dump(mode="json")
            else:
                approval = MigrationAuthorization.model_validate_json(_input(args.authorization))
                if args.operation == "apply":
                    _write(str(Path(args.plan).resolve()) + ".apply-attempt.json", {
                        "migration_id": plan.migration_id, "plan_sha256": plan.sha256,
                        "authorization_sha256": hashlib.sha256(_input(args.authorization)).hexdigest(),
                        "state": "attempted_or_unknown", "automatic_retry": False,
                    })
                    result = service.apply(principal, plan, approval).payload
                elif args.operation == "reconcile":
                    result = service.reconcile(principal, plan, approval).payload
                else:
                    result = service.verify_applied(principal, plan, approval)
        _write(args.output, result)
        print(json.dumps({"operation": args.operation, "output": args.output,
                          "output_sha256": hashlib.sha256(_input(args.output)).hexdigest(),
                          "activation_performed": False}))


def run():
    try:
        main()
    except Problem as exc:
        print(json.dumps({"status": "REJECTED_OR_UNKNOWN", "error": exc.body["error"], "automatic_retry": False}),
              file=sys.stderr)
        raise SystemExit(1) from None
    except (AzureError, ValueError, OSError) as exc:
        print(json.dumps({"status": "REJECTED_OR_UNKNOWN", "error_type": type(exc).__name__,
                          "automatic_retry": False}), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    run()
