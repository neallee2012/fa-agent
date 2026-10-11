"""Build a server configuration and fresh-initialization inputs from an environment profile (ADR-0008, #415 W9).

Offline, deterministic: no Azure call. Every environment-specific value comes from the selected profile or a
code-pinned contract constant; nothing is copied from another environment's configuration, policy, receipts or
evidence.

usage:
  PYTHONPATH=src/fa-shared-context-poc python deploy/stage/make_runtime.py
  PYTHONPATH=src/fa-shared-context-poc python deploy/stage/make_runtime.py --config C:\targets\v18.json --out C:\targets\v18\runtime
"""
import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "shared-context"))
from deployment_config import config_path, load_config
from shared_context.behavior_assistant import EXECUTOR_VERSION, PROTECTION_VERSION
from shared_context.behavior_assistant_wire import CHECKS
from shared_context.behavior_fresh_installation import FreshInstallationInputs
from shared_context.behavior_server_config import ServerConfiguration
from shared_context.behavior_work_policy import SteadyWorkPolicy
from shared_context.source_materials import digest

STAGE = load_config()
OUT = HERE / "runtime"
TOKEN = re.sub(r"[^a-z0-9-]+", "-", STAGE["environment"].lower()).strip("-") or "environment"


def _version(name):
    # Keep the existing stage bytes stable while giving separately prepared profiles
    # collision-resistant identifiers.
    return f"{name}-v1" if TOKEN == "stage" else f"{name}-{TOKEN}-v1"

# Approved for W9 (#415; values confirmed by the coordinator, see docs/progress.md stage entry).
POLICY = {
    "work_estimate_microusd": 2_000_000,
    "cost_max_age_seconds": 3600,
    "queue_seconds": 600,
    "max_model_requests": 6,
}
OPENING_BALANCE_MICROUSD = 0
# Neal 2026-10-02 17:29: the stage USD200/month cap is tracked, not an automatic gate.
BUDGET_ENFORCED = False
# A fixed note instead of a reviewed metrics source: stage has none, and nothing may treat it as reviewed.
SOURCE_NOTE = f"fa-{TOKEN}-v1: no reviewed metrics source is installed in this environment (ADR-0008).".encode()
# The legacy one-shot binding intake is closed from the start; native turns use the standing policy.
LEGACY_INTAKE_VALID_UNTIL = datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc)


def _arm(kind, name):
    provider = {"storage": "Microsoft.Storage/storageAccounts", "app": "Microsoft.App/containerApps",
                "job": "Microsoft.App/jobs"}[kind]
    return f"/subscriptions/{STAGE['subscription_id']}/resourceGroups/{STAGE['resource_group']}/providers/{provider}/{name}"


def configuration():
    run, tenant = STAGE["runtime"], STAGE["tenant_id"]
    domain, team, human = run["authority_domain"], run["team_id"], run["human_oid"]
    table_host = f"{run['table_account']}.table.core.windows.net"
    control = hashlib.sha256(json.dumps({
        "layout": 1, "endpoint": ["https", table_host, ""], "domain": domain,
        "table": run["table_name"], "partition": domain,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    agents = STAGE["agents"]
    a = agents[0]
    binding = {
        "binding_id": f"{TOKEN}-binding-v1", "tenant_id": tenant, "team_id": team, "human_oid": human,
        "agent_oid": a["agent_oid"],
        "owner": {"owner_record_id": f"{TOKEN}-owner-v1", "principal_id": a["agent_oid"], "epoch": 1,
                  "conversation_id": f"{TOKEN}-conversation-v1", "policy_id": f"{TOKEN}-policy-v1",
                  "root_budget_id": "root"},
        "ingress_id": "authenticated-api", "source_ids": [f"{TOKEN}-source-v1"],
        "claim_ids": [f"{TOKEN}-claim-v1"],
        "subject": f"{TOKEN}-subject-v1", "event_id": f"{TOKEN}-event-v1",
        "root_request_id": f"{TOKEN}-root-v1",
        "job_id": f"{TOKEN}-job-v1",
        "work": {"work_id": f"{TOKEN}-work-v1",
                 "goal": "Use the reviewed metrics to compute remaining items and save my result.",
                 "criteria_version": f"assistant-criteria-{TOKEN}-v1", "result_kind": "answer",
                 "required_checks": list(CHECKS), "human_checks": []},
        "route": {"tenant_id": tenant, "account_id": f"fa-{TOKEN}-account", "recipient_id": human,
                  "channel_id": "authenticated-pull", "thread_id": f"{TOKEN}-thread-v1"},
        "operation_id": f"{TOKEN}-operation-v1", "attempt_id": f"{TOKEN}-operation-attempt-v1",
        "grant_id": f"{TOKEN}-operation-grant-v1", "allowed_input_refs": [f"{TOKEN}-goal-v1"],
        "context_versions": {"work_revision": 1, "release_version": _version("release"),
                             "catalog_version": _version("catalog"), "memory_schema_version": _version("memory")},
    }

    def blob(container, prefix):
        return {"account_resource_id": _arm("storage", run["blob_account"]), "protection": {
            "mode": "no-auto-gc", "authority_domain": domain, "generation": f"no-gc-{TOKEN}-v1",
            "container_url": f"https://{run['blob_account']}.blob.core.windows.net/{container}",
            "protected_prefix": f"{prefix}{tenant}/", "configuration_version": f"fa-storage-{TOKEN}-v1",
            # Pin of a stage protection observation that has not been produced: Blob source/backup consumers
            # fail closed until a real observation exists (the fresh path neither needs nor fakes one).
            "evidence_sha256": digest([f"fa-{TOKEN}-v1", "protection-observation-not-produced", container]),
        }}

    raw = {
        "schema_version": "fa-behavior-server-v1",
        "deployment_id": f"fa-{TOKEN}-v1",
        "identity": {
            "tenant_id": tenant, "audience": STAGE["api_audience"], "team_id": team, "human_oid": human,
            "admin_client_id": run["admin_client_id"], "managed_identity_client_id": STAGE["api_identity_client_id"],
            "agents": [{"oid": agent["agent_oid"], "actor": "agent-" + agent["name"][-1]} for agent in agents],
        },
        "table": {"account_resource_id": _arm("storage", run["table_account"]), "endpoint": f"https://{table_host}",
                  "scope": {"authority_domain": domain, "table_name": run["table_name"], "partition_key": domain,
                            "max_operations": 100, "max_batch_bytes": 4194304, "max_payload_bytes": 49152}},
        "sources": blob(run["sources_container"], "m3/v1/"),
        "backups": blob(run["backups_container"], "v1/"),
        "delivery": {
            "runtime_version": "fa-delivery-contract-v1", "control_binding": control, "team_id": team,
            "entry_id": f"behavior-{TOKEN}-api", "protection_version": PROTECTION_VERSION,
            "consumer_version": "behavior-controller-v1", "timer_version": "bounded-tick-v1",
            "verifier_version": "assistant-result-verifier-v1",
            "channel": {"channel_id": "authenticated-pull", "version": "fa-pull-inbox-v1",
                        "supported_levels": ["client_received"], "max_confirmation_level": "client_received"},
            "max_queue_seconds": 3600, "review_interval_seconds": 60, "normal_control_units": 8,
            "max_delivery_versions": 4, "max_receipts_per_delivery": 4, "max_due_generations": 4,
            "scan_page_size": 4, "model_repair_contract": None, "model_repair_units": 1,
        },
        "workers": {
            "workspace_id": domain, "control_binding": control, "executor_id": "native-pool",
            "executor_version": EXECUTOR_VERSION, "protection_version": PROTECTION_VERSION, "capacity": 1,
            "pending_limit": 16, "max_generations": 4, "control_units": 8, "reservation_units": 1,
            "lease_seconds": 60, "queries_per_generation": 16,
        },
        "bindings": [binding],
        "policies": [{"binding_id": binding["binding_id"], "policy": {
            "binding_digest": digest(["fa-behavior-api-v1", "native", binding]),
            "operation_policy_revision": 1, "active": True, "source_visibility": "team", "purpose": "metrics",
            "allowed_purposes": ["metrics"], "max_commands": 64, "max_grant_seconds": 300,
            "valid_until": LEGACY_INTAKE_VALID_UNTIL.isoformat().replace("+00:00", "Z"),
            "human_text_enabled": False, "text_policy_version": None,
        }}],
        "roots": [{"root_budget_id": "root", "root_request_id": binding["root_request_id"], "attempts": 1000,
                   "units": 10000, "control_units": 10000, "scans": 1000, "pending_limit": 32}],
        "operations": [{"input_ref": f"{TOKEN}-goal-v1", "binding_id": binding["binding_id"],
                        "goal": "Review the prepared result against the pinned work requirements."}],
        "targets": [{"executor_id": f"executor-{agent['name'][-1]}", "project_endpoint": STAGE["project_endpoint"],
                     "agent_id": agent["agent_id"], "agent_name": agent["name"], "agent_version": "1",
                     "session_id": f"{agent['name']}-session-v1", "agent_oid": agent["agent_oid"]} for agent in agents],
        # No controller app is required here; the ARM-shaped ID only pins the resource bindings.
        "controller_id": f"fa-{TOKEN}-controller-v1",
        "controller_resource_id": _arm("job", f"job-fa-behavior-controller-{TOKEN}"),
        "api_resource_id": _arm("app", STAGE["api_app"]),
        "authentication_version": "behavior-jwt-v1",
        "review_policy_version": "human-review-v1",
        "evidence": {"path": f"/tmp/fa-{TOKEN}-runtime/evidence.json", "catalog_id": f"fa-{TOKEN}-native-v1",
                     "observer_oid": human, "minimum_revision": 1, "max_age_seconds": 14400,
                     "inventory_scopes": ["fa-runtime", "storage-lifecycle", "defender-response", "private-network"]},
        "tokenizer": {"version": "0.11.0", "encoding": "o200k_base", "cache_directory": "/opt/fa-tokenizer",
                      "accounting_scope": "context-text-sections-only"},
        "supervisor": {"ticks": 1, "page_limit": 1, "interval_seconds": 2.0, "max_run_seconds": 120.0},
        "team_pending_limit": 32, "entry_pending_limit": 32, "max_context_tokens": 8192,
        "max_context_bytes": 1048576, "workload_version": "assistant-v1.2", "automatic_model_repairs": 0,
        "total_poc_budget_usd": 100, "memory_projections": [],
        "assistant": {
            "profiles": [{
                "binding_id": binding["binding_id"],
                "source": {"source_id": f"{TOKEN}-source-v1", "version": 1,
                           "sha256": hashlib.sha256(SOURCE_NOTE).hexdigest(), "byte_length": len(SOURCE_NOTE)},
                "claim_id": f"{TOKEN}-claim-v1", "legacy_source_work": f"{TOKEN}-legacy-source-none",
                "artifact_id": f"{TOKEN}-report-v1", "artifact_version": "0" * 32,
                "policy_version": "assistant-own-result-v1", "max_model_requests": 6, "require_operation": False,
            }],
            "provider_endpoint": STAGE["project_endpoint"] + "/openai/v1/chat/completions",
            "model": "gpt-4.1-mini", "credential_binding": "server-api-uami",
            "m3_context": {"account_resource_id": _arm("storage", run["table_account"]),
                           "endpoint": f"https://{table_host}", "table_name": run["table_name"]},
            "artifact_container_url": f"https://{run['blob_account']}.blob.core.windows.net/{run['sources_container']}",
        },
    }
    return ServerConfiguration.model_validate_json(json.dumps(raw))


def inputs(config):
    policy = SteadyWorkPolicy(
        policy_id=f"{TOKEN}-standing-policy-v1", revision=1, configuration_sha256=config.sha256, evidence_mode="native",
        authority_domain=config.table.scope.authority_domain, control_binding=config.workers.control_binding,
        active=True, read_enabled=True, template=config.bindings[0], profile=config.assistant.profiles[0],
        legacy_template_policy=config.policies[0].policy, cost_scope_sha256=digest(config.resource_bindings()),
        cost_observer_oid=config.evidence.observer_oid, minimum_observed_microusd=OPENING_BALANCE_MICROUSD,
        work_estimate_microusd=POLICY["work_estimate_microusd"], cost_max_age_seconds=POLICY["cost_max_age_seconds"],
        queue_seconds=POLICY["queue_seconds"], max_model_requests=POLICY["max_model_requests"],
    )
    value = FreshInstallationInputs(policy=policy, peer_agent_id=config.targets[1].agent_id,
                                    peer_registration_id=f"{TOKEN}-b-registration-v1",
                                    initial_accounted_microusd=OPENING_BALANCE_MICROUSD, budget_enforced=BUDGET_ENFORCED)
    return FreshInstallationInputs.model_validate_json(value.model_dump_json())


def _write(name, text):
    path = OUT / name
    data = text.encode("utf-8")
    if path.exists():
        if path.read_bytes() != data:
            raise SystemExit(f"{path} exists with different bytes; an applied configuration is never overwritten")
        return
    OUT.mkdir(exist_ok=True)
    path.write_bytes(data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="environment profile; defaults to FA_DEPLOY_CONFIG or stage.json")
    parser.add_argument("--out", type=Path, help="runtime output directory; defaults to <config>/runtime")
    args = parser.parse_args()
    global OUT, STAGE, TOKEN, SOURCE_NOTE
    STAGE = load_config(args.config)
    TOKEN = re.sub(r"[^a-z0-9-]+", "-", STAGE["environment"].lower()).strip("-") or "environment"
    SOURCE_NOTE = f"fa-{TOKEN}-v1: no reviewed metrics source is installed in this environment (ADR-0008).".encode()
    OUT = args.out or config_path(args.config).parent / "runtime"
    config = configuration()
    values = inputs(config)
    _write("config.json", config.model_dump_json(indent=1) + "\n")
    _write("fresh-inputs.json", values.model_dump_json(indent=1) + "\n")
    print(json.dumps({"configuration_sha256": config.sha256, "control_binding": config.workers.control_binding,
                      "input_sha256": digest(values.model_dump(mode="json"))}))


if __name__ == "__main__":
    main()
