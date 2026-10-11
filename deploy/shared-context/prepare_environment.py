"""Render one environment input into deterministic Azure deployment parameters.

This command is deliberately offline.  It does not log in, create resources, or
read an existing subscription.  It only expands a small target description into
the parameter files consumed by the shared-context Bicep templates.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

UUID = re.compile(r"^[0-9a-fA-F-]{36}$")
ALNUM = re.compile(r"[^a-z0-9]")
ROOT = Path(__file__).resolve().parents[2]
MODEL_CATALOG = ROOT / "src" / "fa-shared-context-poc" / "model-catalog.json"
UPGRADE_OPTIONS = ("NoAutoUpgrade", "OnceNewDefaultVersionAvailable", "OnceCurrentVersionExpired")


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot read environment input {path}: {error}") from None
    if not isinstance(value, dict):
        raise SystemExit("environment input must be a JSON object")
    return value


def _required(value: dict[str, Any], key: str) -> Any:
    result = value.get(key)
    if result in (None, ""):
        raise SystemExit(f"environment input is missing {key}")
    return result


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    if not result:
        raise SystemExit("name_prefix must contain at least one ASCII letter or digit")
    return result


def _compact(value: str) -> str:
    return ALNUM.sub("", value.lower())


def _name(value: dict[str, Any], key: str, default: str) -> str:
    result = (value.get("names") or {}).get(key, default)
    if not isinstance(result, str) or not result:
        raise SystemExit(f"names.{key} must be a non-empty string")
    return result


def _model_deployments(source: dict[str, Any], catalog_path: Path = MODEL_CATALOG) -> list[dict[str, Any]]:
    """One Foundry deployment per model-catalog entry, so every model the Portal offers can run.

    `model_capacity` overrides a deployment's capacity (quota differs per subscription). Versions stay pinned
    (NoAutoUpgrade) unless `model_version_upgrade_option` gives one option for all deployments or a
    deployment-name map, so a rerun can keep an existing environment's per-deployment setting."""
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot read model catalog {catalog_path}: {error}") from None
    known = {item["deployment"] for item in catalog}
    capacity = source.get("model_capacity") or {}
    if not isinstance(capacity, dict):
        raise SystemExit("model_capacity must map deployment names to capacities")
    option = source.get("model_version_upgrade_option", "NoAutoUpgrade")
    options = option if isinstance(option, dict) else {}
    unknown = sorted((set(capacity) | set(options)) - known)
    if unknown:
        raise SystemExit(f"model settings name deployments outside the model catalog: {', '.join(unknown)}")
    result = []
    for item in catalog:
        value = capacity.get(item["deployment"], item["sku"]["capacity"])
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise SystemExit(f"capacity of {item['deployment']} must be a positive integer")
        upgrade = options.get(item["deployment"], "NoAutoUpgrade") if isinstance(option, dict) else option
        if upgrade not in UPGRADE_OPTIONS:
            raise SystemExit(f"model_version_upgrade_option must be one of {', '.join(UPGRADE_OPTIONS)}")
        result.append({
            "name": item["deployment"],
            "model": {key: item["model"][key] for key in ("format", "name", "version")},
            "sku": {"name": item["sku"]["name"], "capacity": value},
            "versionUpgradeOption": upgrade,
        })
    return result


def resolve(source: dict[str, Any]) -> dict[str, Any]:
    prefix = _slug(str(_required(source, "name_prefix")))
    suffix = prefix.removeprefix("fa-agent-") or prefix
    compact = _compact(prefix)
    location = str(_required(source, "location"))
    subscription_id = str(_required(source, "subscription_id"))
    tenant_id = str(_required(source, "tenant_id"))
    resource_group = str(_required(source, "resource_group"))
    operator_object_id = str(_required(source, "operator_object_id"))
    for key, value in (("subscription_id", subscription_id), ("tenant_id", tenant_id),
                       ("operator_object_id", operator_object_id)):
        if UUID.fullmatch(value) is None:
            raise SystemExit(f"{key} must be a UUID")

    tags = {
        "workload": "hosted-sandbox-poc",
        "architecture": "shared-context",
        "dataClassification": "synthetic-only",
        "environment": prefix,
        **(source.get("tags") or {}),
    }
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in tags.items()):
        raise SystemExit("tags must map strings to strings")

    names = {
        "foundry_account": _name(source, "foundry_account", f"cog-{prefix}-demo")[:24],
        "project": _name(source, "project", prefix),
        "registry": _name(source, "registry", f"cr{compact}demo")[:50],
        "container_apps_environment": _name(source, "container_apps_environment", f"cae-{prefix}"),
        "api_identity": _name(source, "api_identity", f"id-{prefix}"),
        "storage": _name(source, "storage", f"st{compact}demo")[:24],
        "backup_storage": _name(source, "backup_storage", f"st{compact}bk")[:24],
        "vnet": _name(source, "vnet", f"vnet-{prefix}"),
        "api_app": _name(source, "api_app", f"ca-fa-behavior-{suffix}"),
        "sync_job": _name(source, "sync_job", f"job-fa-agent-sync-{suffix}"),
        "webpubsub": _name(source, "webpubsub", f"wps-fa-agent-{suffix}"),
        "log_analytics": _name(source, "log_analytics", f"log-{prefix}"),
        "app_insights": _name(source, "app_insights", f"appi-{prefix}"),
        "backup_private_endpoint": _name(source, "backup_private_endpoint", f"pe-{compact}-backup-blob"),
    }
    table_name = str(source.get("table_name", "workstate"))
    backup_container = str(source.get("backup_container", "m1-environment-backups"))
    artifacts_container = str(source.get("artifacts_container", "m3-artifacts"))
    foundry_resource_group = str(source.get("foundry_resource_group", resource_group))
    role_guid = source.get("read_write_role_definition_guid")
    if not isinstance(role_guid, str) or UUID.fullmatch(role_guid) is None:
        raise SystemExit("read_write_role_definition_guid must be a UUID")
    nat_egress = source.get("api_subnet_nat_egress", False)
    if not isinstance(nat_egress, bool):
        raise SystemExit("api_subnet_nat_egress must be true or false")

    profile = {
        "environment": prefix,
        "tenant_id": tenant_id,
        "subscription_id": subscription_id,
        "location": location,
        "resource_group": resource_group,
        "foundry_resource_group": foundry_resource_group,
        "operator_object_id": operator_object_id,
        "name_prefix": prefix,
        "names": names,
        "tags": tags,
        "table_name": table_name,
        "backup_container": backup_container,
        "artifacts_container": artifacts_container,
        "read_write_role_definition_guid": role_guid,
        "api_subnet_nat_egress": nat_egress,
        "model_deployments": _model_deployments(source),
        "log_analytics_workspace_resource_id": (
            f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
            f"/providers/Microsoft.OperationalInsights/workspaces/{names['log_analytics']}"
        ),
    }
    return {
        "profile": profile,
        "parameters": _parameters(profile),
    }


def _parameters(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    names = profile["names"]
    tags = profile["tags"]
    location = profile["location"]
    foundation = {
        "location": location,
        "environmentName": names["container_apps_environment"],
        "apiIdentityName": names["api_identity"],
        "storageName": names["storage"],
        "tableName": profile["table_name"],
        "registryName": names["registry"],
        "foundryAccountName": names["foundry_account"],
        "projectName": names["project"],
        "foundryResourceGroupName": profile["foundry_resource_group"],
        "logAnalyticsWorkspaceResourceId": profile["log_analytics_workspace_resource_id"],
        "foundryDeploymentPrefix": profile["environment"],
        "registryConnectionName": f"{profile['environment']}-container-registry",
        "vnetName": names["vnet"],
        "apiSubnetNatEgress": profile["api_subnet_nat_egress"],
        "tags": tags,
    }
    return {
        "foundry.parameters.json": {
            "location": location,
            "accountName": names["foundry_account"],
            "projectName": names["project"],
            "operatorObjectId": profile["operator_object_id"],
            "modelDeployments": profile["model_deployments"],
        },
        "foundation.parameters.json": foundation,
        "monitoring.parameters.json": {
            "location": location,
            "workspaceName": names["log_analytics"],
            "appInsightsName": names["app_insights"],
            "foundryResourceGroupName": profile["foundry_resource_group"],
            "foundryAccountName": names["foundry_account"],
            "projectName": names["project"],
            "connectionName": f"{profile['environment']}-appinsights",
            "foundryDeploymentPrefix": profile["environment"],
            "dailyQuotaGb": "0.5",
            "tags": tags,
        },
        "behavior-api.parameters.json": {
            "appName": names["api_app"],
            "environmentName": names["container_apps_environment"],
            "identityName": names["api_identity"],
            "registryName": names["registry"],
            "computeProfile": "assistant-large",
            "minReplicas": 1,
            "startupFailureThreshold": 120,
            "tags": tags,
        },
        "behavior-job.parameters.json": {
            "jobName": names["sync_job"],
            "environmentName": names["container_apps_environment"],
            "identityName": names["api_identity"],
            "registryName": names["registry"],
            "imageRepository": "fa-behavior-api",
            "timeoutSeconds": 1800,
            "containerResources": {"cpu": "0.5", "memory": "1Gi"},
            "tags": tags,
        },
        "realtime.parameters.json": {
            "location": location,
            "name": names["webpubsub"],
            "sku": "Free_F1",
            "apiIdentityName": names["api_identity"],
            "tags": tags,
        },
        "backup-role.parameters.json": {
            "backupResourceGroupName": profile["resource_group"],
            "roleName": f"FA M1 Backup Data Read Write ({profile['environment']})",
        },
        "backup-foundation.parameters.json": {
            "location": location,
            "storageName": names["backup_storage"],
            "containerName": profile["backup_container"],
            "apiIdentityName": names["api_identity"],
            "vnetName": names["vnet"],
            "privateEndpointSubnetName": "snet-private-endpoints",
            "privateEndpointName": names["backup_private_endpoint"],
            "createBlobPrivateDnsZone": True,
            "createBlobDnsVnetLink": True,
            "blobDnsVnetLinkName": f"{profile['environment']}-link",
        },
        "m3-artifacts.parameters.json": {
            "storageName": names["backup_storage"],
            "containerName": profile["artifacts_container"],
            "apiIdentityName": names["api_identity"],
            "readWriteRoleDefinitionGuid": profile["read_write_role_definition_guid"],
        },
    }


def _write(path: Path, value: Any) -> None:
    data = (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if path.exists():
        if path.read_bytes() != data:
            raise SystemExit(f"{path} exists with different bytes; choose a new output directory")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="small environment input JSON")
    parser.add_argument("--out", type=Path, required=True, help="new output directory")
    args = parser.parse_args()
    result = resolve(_read(args.input))
    output = args.out.resolve()
    _write(output / "deployment.json", result["profile"])
    for name, values in result["parameters"].items():
        _write(output / "params" / name, {
            "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
            "contentVersion": "1.0.0.0",
            "parameters": {key: {"value": value} for key, value in values.items()},
        })
    print(json.dumps({
        "output": str(output),
        "resource_group": result["profile"]["resource_group"],
        "environment": result["profile"]["environment"],
        "parameter_files": sorted(result["parameters"]),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
