"""Bounded operator CLI: local build, or explicitly requested READ-ONLY metadata.

No login, account switch, token dump, resource write, grant, app installation,
Graph call, or message send exists in this module. Azure CLI stderr/stdout errors
stay inside the reader and are replaced by fixed Problems. AZURE_CONFIG_DIR is
supplied only to the child process, never changed globally.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import uuid
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .errors import Problem
from .behavior_delivery import _json
from .teams_deployment import (
    HOST_RESOURCE_ID, INTENDED_USER, PersonalTeamsConfig, SUBSCRIPTION, TENANT,
    UAMI_RESOURCE_ID, build_artifacts, load_config, validate_manifest_schema,
)


class _Reader:
    def __init__(self, az_config_dir: Path, run):
        if not az_config_dir.is_dir():
            raise Problem(422, "TEAMS_AZ_CONTEXT_REQUIRED", "an existing isolated Azure CLI configuration directory is required")
        self.env = {**os.environ, "AZURE_CONFIG_DIR": str(az_config_dir.resolve())}
        self.run = run

    def get(self, command: tuple[str, ...], query: str):
        args = [shutil.which("az") or "az", *command, "--query", query, "--output", "json", "--only-show-errors"]
        if command[:2] != ("account", "show"):
            args += ["--subscription", SUBSCRIPTION]
        try:
            result = self.run(args, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              encoding="utf-8", timeout=30, check=False)
            if result.returncode != 0 or not isinstance(result.stdout, str) or len(result.stdout) > 65536:
                raise ValueError
            return json.loads(result.stdout, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (OSError, subprocess.SubprocessError, ValueError, UnicodeError, RecursionError):
            raise Problem(409, "TEAMS_OPERATOR_READ_FAILED", "scoped Azure metadata is unavailable; no write or credential fallback was attempted") from None


def _identity(reader: _Reader) -> dict:
    account = reader.get(("account", "show"), "{subscriptionId:id,tenantId:tenantId,environmentName:environmentName}")
    if _json(account) != _json({"subscriptionId": SUBSCRIPTION, "tenantId": TENANT, "environmentName": "AzureCloud"}):
        raise Problem(403, "TEAMS_OPERATOR_CONTEXT_DENIED", "the selected Azure CLI cloud, subscription and tenant must match the POC")
    identity = reader.get(("identity", "show", "--ids", UAMI_RESOURCE_ID),
                          "{id:id,clientId:clientId,principalId:principalId,tenantId:tenantId}")
    try:
        if (identity["id"].lower() != UAMI_RESOURCE_ID.lower() or identity["tenantId"] != TENANT
                or identity["clientId"] == identity["principalId"]
                or any(str(uuid.UUID(identity[key])) != identity[key] or uuid.UUID(identity[key]).int == 0
                       for key in ("clientId", "principalId"))):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise Problem(403, "TEAMS_OPERATOR_IDENTITY_DENIED", "the existing UAMI metadata does not match the pinned tenant/resource") from None
    return {key: identity[key] for key in ("id", "clientId", "principalId", "tenantId")}


def inspect_identity(*, az_config_dir: Path, run=subprocess.run) -> dict:
    identity = _identity(_Reader(az_config_dir, run))
    return {"status": "identity_metadata_matches", "identity": identity, "native_gate": "closed",
            "actual_environment": "NOT_RUN"}


def verify_metadata(config: PersonalTeamsConfig, *, az_config_dir: Path, run=subprocess.run) -> dict:
    config = PersonalTeamsConfig.model_validate(config.model_dump())
    reader = _Reader(az_config_dir, run)
    identity = _identity(reader)
    if identity["clientId"] != config.uami_client_id or identity["principalId"] != config.uami_principal_id:
        raise Problem(403, "TEAMS_OPERATOR_IDENTITY_DENIED", "configured UAMI client/principal IDs differ from the existing resource")
    provider = reader.get(("provider", "show", "--namespace", "Microsoft.BotService"), "registrationState")
    if provider != "Registered":
        raise Problem(409, "TEAMS_PROVIDER_NOT_REGISTERED", "an authorized operator must register Microsoft.BotService before deployment")
    bot = reader.get(("resource", "show", "--ids", config.bot_resource_id, "--api-version", "2022-09-15"),
                     "{id:id,kind:kind,location:location,sku:sku.name,properties:properties.{msaAppType:msaAppType,msaAppId:msaAppId,msaAppTenantId:msaAppTenantId,msaAppMSIResourceId:msaAppMSIResourceId,endpoint:endpoint,disableLocalAuth:disableLocalAuth,publicNetworkAccess:publicNetworkAccess,isStreamingSupported:isStreamingSupported}}")
    expected_properties = {
        "msaAppType": "UserAssignedMSI", "msaAppId": config.uami_client_id, "msaAppTenantId": TENANT,
        "msaAppMSIResourceId": UAMI_RESOURCE_ID, "endpoint": config.endpoint, "disableLocalAuth": True,
        "publicNetworkAccess": "Enabled", "isStreamingSupported": False,
    }
    if _json(bot) != _json({"id": config.bot_resource_id, "kind": "azurebot", "location": "global", "sku": "F0",
                           "properties": expected_properties}):
        raise Problem(403, "TEAMS_BOT_METADATA_DENIED", "BotService identity, endpoint or authentication profile differs")
    channel_id = config.bot_resource_id + "/channels/MsTeamsChannel"
    channel = reader.get(("resource", "show", "--ids", channel_id, "--api-version", "2022-09-15"),
                         "{id:id,properties:properties.{channelName:channelName,properties:properties.{isEnabled:isEnabled,enableCalling:enableCalling}}}")
    if _json(channel) != _json({"id": channel_id, "properties": {
        "channelName": "MsTeamsChannel", "properties": {"isEnabled": True, "enableCalling": False},
    }}):
        raise Problem(403, "TEAMS_CHANNEL_METADATA_DENIED", "the enabled non-calling Teams channel was not verified")
    host = reader.get(("resource", "show", "--ids", HOST_RESOURCE_ID, "--api-version", "2024-03-01"),
                      "{id:id,identity:identity.{type:type,userAssignedIdentities:userAssignedIdentities},ingress:properties.configuration.ingress.{external:external,allowInsecure:allowInsecure,fqdn:fqdn}}")
    try:
        identities = host["identity"]["userAssignedIdentities"]
        attached = next(value for key, value in identities.items() if key.lower() == UAMI_RESOURCE_ID.lower())
        if (host["id"].lower() != HOST_RESOURCE_ID.lower()
                or "UserAssigned" not in host["identity"]["type"].split(", ")
                or attached["clientId"] != config.uami_client_id or attached["principalId"] != config.uami_principal_id
                or _json(host["ingress"]) != _json({"external": True, "allowInsecure": False,
                                                   "fqdn": config.callback_origin.removeprefix("https://")})):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError, StopIteration):
        raise Problem(403, "TEAMS_HOST_METADATA_DENIED", "callback host identity or HTTPS ingress differs; no host mutation was attempted") from None
    return {
        "status": "prerequisite_metadata_matches", "configuration_digest": config.configuration_digest,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "metadata_sha256": {name: hashlib.sha256(_json(value)).hexdigest() for name, value in (
            ("identity", identity), ("bot", bot), ("channel", channel), ("host", host),
        )},
        "resource_ids": [UAMI_RESOURCE_ID, config.bot_resource_id, channel_id, HOST_RESOURCE_ID],
        "intended_user_oid": INTENDED_USER, "native_gate": "closed",
        "installation": "NOT_RUN", "signed_connector_route": "NOT_RUN",
        "callback_reachability": "NOT_RUN", "credential_acquisition": "NOT_RUN",
        "actual_environment": "NOT_RUN",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "identity", "verify"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--az-config-dir", type=Path)
    parser.add_argument("--schema-file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "identity":
            if args.az_config_dir is None:
                raise Problem(422, "TEAMS_AZ_CONTEXT_REQUIRED", "--az-config-dir is required for this read-only action")
            result = inspect_identity(az_config_dir=args.az_config_dir)
        else:
            if args.config is None:
                raise Problem(422, "TEAMS_CONFIG_REQUIRED", "--config is required")
            config = load_config(args.config)
            if args.action == "build":
                if args.output is None:
                    raise Problem(422, "TEAMS_OUTPUT_REQUIRED", "--output must be a new directory")
                if args.schema_file is not None:
                    validate_manifest_schema(config, args.schema_file)
                result = build_artifacts(config, args.output)
                result["official_schema_validated"] = args.schema_file is not None
            else:
                if args.az_config_dir is None:
                    raise Problem(422, "TEAMS_AZ_CONTEXT_REQUIRED", "--az-config-dir is required for this read-only action")
                result = verify_metadata(config, az_config_dir=args.az_config_dir)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Problem as error:
        print(json.dumps(error.body, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
