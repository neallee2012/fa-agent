"""OFFLINE Azure CLI transport, never ARM/Graph writes or token acquisition."""
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from shared_context.teams_operator import verify_metadata
from shared_context.teams_deployment import HOST_RESOURCE_ID
from shared_context.errors import Problem
from test_teams_deployment import ADMIN, CLIENT, PRINCIPAL, SUBSCRIPTION, TENANT, UAMI, config


def metadata(config):
    return {
        "account": {"subscriptionId": SUBSCRIPTION, "tenantId": TENANT, "environmentName": "AzureCloud"},
        "identity": {"id": UAMI, "clientId": CLIENT, "principalId": PRINCIPAL, "tenantId": TENANT},
        "provider": "Registered",
        "bot": {
            "id": config.bot_resource_id, "kind": "azurebot", "location": "global", "sku": "F0",
            "properties": {
                "msaAppType": "UserAssignedMSI", "msaAppId": CLIENT, "msaAppTenantId": TENANT,
                "msaAppMSIResourceId": UAMI, "endpoint": config.endpoint, "disableLocalAuth": True,
                "publicNetworkAccess": "Enabled", "isStreamingSupported": False,
            },
        },
        "channel": {
            "id": config.bot_resource_id + "/channels/MsTeamsChannel",
            "properties": {"channelName": "MsTeamsChannel", "properties": {"isEnabled": True, "enableCalling": False}},
        },
        "host": {
            "id": HOST_RESOURCE_ID,
            "identity": {"type": "UserAssigned", "userAssignedIdentities": {
                UAMI: {"clientId": CLIENT, "principalId": PRINCIPAL},
            }},
            "ingress": {"external": True, "allowInsecure": False,
                        "fqdn": config.callback_origin.removeprefix("https://")},
        },
    }


class AzureReadTransport:
    def __init__(self, values):
        self.values, self.calls = values, []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        assert Path(args[0]).name.lower() in ("az", "az.cmd", "az.exe")
        assert args[1:3] in (["account", "show"], ["identity", "show"],
                            ["provider", "show"], ["resource", "show"])
        if args[1] == "resource":
            resource_id = args[args.index("--ids") + 1]
            key = "host" if resource_id == HOST_RESOURCE_ID else "channel" if "/channels/" in resource_id else "bot"
        else:
            key = args[1]
        return SimpleNamespace(returncode=0, stdout=json.dumps(self.values[key]), stderr="")


def test_operator_reads_only_scoped_metadata_and_cannot_claim_installation_or_route(tmp_path):
    cfg = config()
    transport = AzureReadTransport(metadata(cfg))
    report = verify_metadata(cfg, az_config_dir=tmp_path, run=transport)
    assert report["status"] == "prerequisite_metadata_matches"
    assert report["native_gate"] == "closed"
    assert report["installation"] == "NOT_RUN"
    assert report["signed_connector_route"] == "NOT_RUN"
    assert report["intended_user_oid"] == ADMIN
    assert len(transport.calls) == 6
    for args, kwargs in transport.calls:
        assert "--query" in args
        assert kwargs["env"]["AZURE_CONFIG_DIR"] == str(tmp_path)
        assert "--debug" not in args
        if args[1] != "account":
            assert args[args.index("--subscription") + 1] == SUBSCRIPTION


@pytest.mark.parametrize("field,value", [("tenantId", CLIENT), ("subscriptionId", CLIENT),
                                        ("environmentName", "AzureUSGovernment")])
def test_wrong_cli_context_is_denied_before_reading_any_resource(tmp_path, field, value):
    values = metadata(config())
    values["account"][field] = value
    transport = AzureReadTransport(values)
    with pytest.raises(Problem):
        verify_metadata(config(), az_config_dir=tmp_path, run=transport)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("subject,field,value", [
    ("identity", "clientId", ADMIN), ("identity", "tenantId", CLIENT),
    ("bot", "msaAppId", PRINCIPAL), ("bot", "msaAppType", "SingleTenant"),
    ("bot", "msaAppTenantId", CLIENT), ("bot", "msaAppMSIResourceId", UAMI + "-wrong"),
    ("bot", "endpoint", "https://another.example.org/behavior/v1/teams/activities"),
    ("bot", "disableLocalAuth", False), ("bot", "disableLocalAuth", 1),
    ("channel", "isEnabled", False), ("channel", "enableCalling", True),
    ("host", "allowInsecure", True),
])
def test_missing_or_wrong_prerequisite_never_becomes_readiness(tmp_path, subject, field, value):
    values = metadata(config())
    target = (values[subject]["properties"] if subject == "bot" else
              values["channel"]["properties"]["properties"] if subject == "channel" else
              values["host"]["ingress"] if subject == "host" else values[subject])
    target[field] = value
    with pytest.raises(Problem):
        verify_metadata(config(), az_config_dir=tmp_path, run=AzureReadTransport(values))


def test_missing_provider_and_host_identity_are_explicit_prerequisites(tmp_path):
    for part in ("provider", "host"):
        values = metadata(config())
        if part == "provider":
            values["provider"] = "NotRegistered"
        else:
            values["host"]["identity"]["userAssignedIdentities"] = {}
        with pytest.raises(Problem):
            verify_metadata(config(), az_config_dir=tmp_path, run=AzureReadTransport(values))


def test_azure_cli_error_output_is_not_logged_or_returned(tmp_path, capsys):
    def failed(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="SYNTHETIC-PRIVATE-TOKEN",
                               stderr="Bearer SYNTHETIC-PRIVATE-TOKEN")
    with pytest.raises(Problem) as error:
        verify_metadata(config(), az_config_dir=tmp_path, run=failed)
    assert "SYNTHETIC-PRIVATE-TOKEN" not in str(error.value.body)
    assert capsys.readouterr().out == ""
