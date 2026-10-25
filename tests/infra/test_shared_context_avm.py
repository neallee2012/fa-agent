"""Local Bicep compilation and deployment contracts; no Azure login or deployment."""

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra" / "shared-context"
ENTRYPOINTS = tuple(path.name for path in sorted(INFRA.glob("*.bicep")))
PINS = {
    "app/container-app": "0.23.0",
    "app/job": "0.7.0",
    "app/managed-environment": "0.16.0",
    "managed-identity/user-assigned-identity": "0.6.0",
    "storage/storage-account": "0.33.0",
    "container-registry/registry": "0.13.0",
    "network/virtual-network": "0.10.2",
    "network/public-ip-address": "0.13.0",
    "network/nat-gateway": "2.1.1",
    "network/private-endpoint": "0.12.1",
    "network/private-dns-zone": "0.8.1",
    "network/private-dns-zone/virtual-network-link": "0.1.0",
    "network/network-security-group": "0.5.3",
    "operational-insights/workspace": "0.16.1",
    "insights/component": "0.8.0",
    "signal-r-service/web-pub-sub": "0.7.2",
    "cognitive-services/account": "0.19.0",
    "document-db/database-account": "0.21.1",
}


@pytest.fixture(scope="module")
def compiled(tmp_path_factory):
    compiler = os.environ.get("BICEP_CLI") or shutil.which("bicep")
    assert compiler, "Set BICEP_CLI to the local Bicep executable; these checks never install or deploy"
    work = tmp_path_factory.mktemp("avm")
    for path in (ROOT / "infra").rglob("*.bicep"):
        destination = work / path.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    cache = Path(os.environ.get("BICEP_MODULE_CACHE", str(work / "cache"))).resolve()
    (work / "bicepconfig.json").write_text(json.dumps({"cacheRootDirectory": str(cache)}), encoding="utf-8")
    result = {}
    for filename in ENTRYPOINTS:
        output = work / (Path(filename).stem + ".json")
        process = subprocess.run(
            [compiler, "build", str(work / "infra" / "shared-context" / filename), "--outfile", str(output)],
            capture_output=True, text=True, encoding="utf-8", timeout=180,
        )
        assert process.returncode == 0, f"{filename}:\n{process.stdout}\n{process.stderr}"
        result[filename] = json.loads(output.read_text(encoding="utf-8"))
    return result


def resources(template):
    values = template.get("resources", {})
    values = values.values() if isinstance(values, dict) else values
    return [item for item in values if not item.get("existing")]


def deployment(template, name):
    return next(item for item in resources(template)
                if item["type"] == "Microsoft.Resources/deployments" and item["name"] == name)


def settings(template, name):
    return {key: item["value"] if isinstance(item, dict) else item
            for key, item in deployment(template, name)["properties"]["parameters"].items()}


@pytest.mark.parametrize("filename", ENTRYPOINTS)
def test_shared_context_entrypoint_compiles(compiled, filename):
    assert compiled[filename]["metadata"]["_generator"]["name"] == "bicep"


def test_official_resource_modules_use_the_audited_fixed_versions():
    observed = {}
    for path in INFRA.glob("*.bicep"):
        for name, version in re.findall(r"'br/public:avm/res/([^':]+):([^']+)'", path.read_text(encoding="utf-8")):
            assert re.fullmatch(r"\d+\.\d+\.\d+", version), (path, name, version)
            assert name not in observed or observed[name] == version
            observed[name] = version
    assert observed == PINS


def test_backup_dns_link_is_an_avm_child_without_a_parent_zone_put(compiled):
    template = compiled["backup-foundation.bicep"]
    link = deployment(template, "m1-backup-blob-dns-link")
    assert link["condition"] == "[parameters('createBlobDnsVnetLink')]"
    assert deployment(template, "m1-backup-blob-dns")["condition"] == "[parameters('createBlobPrivateDnsZone')]"
    values = settings(template, "m1-backup-blob-dns-link")
    assert values == {
        "name": "[parameters('blobDnsVnetLinkName')]",
        "privateDnsZoneName": "[variables('blobDnsZoneName')]",
        "location": "global",
        "tags": "[variables('tags')]",
        "registrationEnabled": False,
        # ADR-0010 phase 2b: an existing VNet's ID when given, else the original same-group VNet.
        "virtualNetworkResourceId": "[if(equals(parameters('vnetResourceId'), ''), createObject('value', resourceId("
                                    "'Microsoft.Network/virtualNetworks', parameters('vnetName'))), "
                                    "createObject('value', parameters('vnetResourceId')))]",
        "enableTelemetry": False,
    }
    assert template["parameters"]["vnetResourceId"]["defaultValue"] == ""
    child = link["properties"]["template"]
    assert [item["type"] for item in resources(child) if item["type"] != "Microsoft.Resources/deployments"] == [
        "Microsoft.Network/privateDnsZones/virtualNetworkLinks"]
    assert child["parameters"]["resolutionPolicy"]["nullable"] is True
    assert template["parameters"]["blobDnsVnetLinkName"]["defaultValue"] == "facontext-api-link"
    assert template["parameters"]["createBlobPrivateDnsZone"]["defaultValue"] is True
    assert template["parameters"]["createBlobDnsVnetLink"]["defaultValue"] is True


def test_additive_artifacts_keep_parent_storage_and_container_metadata_untouched(compiled):
    template = compiled["m3-artifacts.bicep"]
    actual = resources(template)
    assert [item["type"] for item in actual] == [
        "Microsoft.Storage/storageAccounts/blobServices/containers", "Microsoft.Authorization/roleAssignments"]
    container, access = actual
    assert container["properties"] == {"publicAccess": "None"}
    assert container["name"] == "[format('{0}/{1}/{2}', parameters('storageName'), 'default', parameters('containerName'))]"
    assert access["name"] == (
        "[guid(resourceId('Microsoft.Storage/storageAccounts/blobServices/containers', parameters('storageName'), "
        "'default', parameters('containerName')), resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', "
        "parameters('apiIdentityName')), parameters('readWriteRoleDefinitionGuid'))]")
    assert access["properties"]["principalType"] == "ServicePrincipal"
    assert set(template["outputs"]) == {"containerId", "roleAssignmentId"}


def test_upgrade_snapshots_get_their_own_account_with_the_30_day_rule(compiled):
    # Neal 2026-10-17: a dedicated account, never the no-auto-gc backup account, so the delete rule is safe there.
    template = compiled["upgrade-snapshots.bicep"]
    assert template["variables"]["snapshotPrefix"] == "[format('{0}/fa-upgrade-snapshots/', parameters('containerName'))]"
    value = settings(template, "fa-upgrade-snapshot-storage")
    assert value["name"] == "[parameters('storageName')]"
    assert value["kind"] == "StorageV2" and value["skuName"] == "Standard_LRS"
    blob = value["blobServices"]
    assert blob["isVersioningEnabled"] is False
    assert blob["containers"] == [{"name": "[parameters('containerName')]", "publicAccess": "None"}]
    # Same delete lock as the backup account; the module (and so the lock) is gated by deployUpgradeSnapshots.
    assert value["lock"] == {"kind": "CanNotDelete", "name": "upgrade-snapshot-account-delete-protection"}
    assert deployment(template, "fa-upgrade-snapshot-storage")["condition"] == "[parameters('deployUpgradeSnapshots')]"
    (rule,) = value["managementPolicyRules"]
    # The platform matches prefixMatch from the container name on (lifecycle-management-policy-structure): one
    # entry, rooted at the container, ending in the snapshot root's '/', so only that root's blobs match.
    assert rule == {"name": "fa-upgrade-snapshots-30d", "enabled": True, "type": "Lifecycle", "definition": {
        "filters": {"blobTypes": ["blockBlob"], "prefixMatch": ["[variables('snapshotPrefix')]"]},
        "actions": {"baseBlob": {"delete": {"daysAfterCreationGreaterThan": 30}}}}}


def test_upgrade_snapshots_are_opt_in_and_the_default_creates_nothing(compiled):
    template = compiled["upgrade-snapshots.bicep"]
    assert template["parameters"]["deployUpgradeSnapshots"]["defaultValue"] is False
    created = resources(template)
    assert sorted(item["type"] for item in created) == [
        "Microsoft.Authorization/roleAssignments", "Microsoft.Resources/deployments", "Microsoft.Resources/deployments"]
    assert all(item["condition"] == "[parameters('deployUpgradeSnapshots')]" for item in created)
    assert template["outputs"]["containerUrl"]["value"].startswith("[if(parameters('deployUpgradeSnapshots'), ")
    stage = json.loads((INFRA / "upgrade-snapshots.stage.parameters.json").read_text(encoding="utf-8"))["parameters"]
    assert stage["deployUpgradeSnapshots"]["value"] is False
    assert set(stage) <= set(template["parameters"])
    backup = json.loads((INFRA / "backup-foundation.stage.parameters.json").read_text(encoding="utf-8"))["parameters"]
    assert stage["storageName"]["value"] != backup["storageName"]["value"]
    foundation = json.loads((INFRA / "foundation.stage.parameters.json").read_text(encoding="utf-8"))["parameters"]
    assert stage["tags"] == foundation["tags"]


def test_only_the_api_identity_writes_upgrade_snapshots_on_the_one_container(compiled):
    template = compiled["upgrade-snapshots.bicep"]
    (access,) = [item for item in resources(template) if item["type"] == "Microsoft.Authorization/roleAssignments"]
    container = ("resourceId('Microsoft.Storage/storageAccounts/blobServices/containers', parameters('storageName'), "
                 "'default', parameters('containerName'))")
    assert access["scope"] == f"[{container}]"
    # backup-role.bicep's read/write custom role (no delete: the lifecycle rule deletes), looked up as backup-foundation does.
    role = ("subscriptionResourceId('Microsoft.Authorization/roleDefinitions', "
            "guid(resourceGroup().id, 'fa-m1-backup-data-read-write-v1'))")
    assert access["name"] == (f"[guid({container}, resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', "
                              f"parameters('apiIdentityName')), {role})]")
    assert access["properties"] == {
        "roleDefinitionId": f"[{role}]",
        "principalId": "[reference('apiIdentity').principalId]", "principalType": "ServicePrincipal"}
    assert template["resources"]["apiIdentity"]["existing"] is True
    assert "storageBlobDataContributor" not in template["variables"]
    role_template = (INFRA / "backup-role.bicep").read_text(encoding="utf-8")
    assert "guid(backupResourceGroupId, 'fa-m1-backup-data-read-write-v1')" in role_template
    assert "blobs/delete" not in role_template


@pytest.mark.parametrize("filename, module", [
    ("foundation.bicep", "shared-context-state-storage"),
    ("backup-foundation.bicep", "m1-backup-storage"),
    ("upgrade-snapshots.bicep", "fa-upgrade-snapshot-storage"),
])
def test_avm_storage_keeps_explicit_private_keyless_configuration(compiled, filename, module):
    value = settings(compiled[filename], module)
    assert value["enableTelemetry"] is False
    assert value["publicNetworkAccess"] == "Disabled"
    assert value["allowSharedKeyAccess"] is False
    assert value["defaultToOAuthAuthentication"] is True
    assert value["allowBlobPublicAccess"] is False
    assert value["requireInfrastructureEncryption"] is True
    assert value["supportsHttpsTrafficOnly"] is True
    assert value["minimumTlsVersion"] == "TLS1_2"
    assert value["networkAcls"] == {"defaultAction": "Deny", "bypass": "None", "ipRules": [], "virtualNetworkRules": []}


def test_avm_backups_preserve_versioning_retention_and_delete_protection(compiled):
    value = settings(compiled["backup-foundation.bicep"], "m1-backup-storage")
    blob = value["blobServices"]
    assert blob["isVersioningEnabled"] is True
    assert blob["deleteRetentionPolicyEnabled"] is True and blob["deleteRetentionPolicyDays"] == 7
    assert blob["containerDeleteRetentionPolicyEnabled"] is True and blob["containerDeleteRetentionPolicyDays"] == 7
    assert blob["deleteRetentionPolicyAllowPermanentDelete"] is False
    assert value["lock"] == {"kind": "CanNotDelete", "name": "m1-backup-account-delete-protection"}


def test_avm_container_app_and_job_keep_replica_identity_and_manual_retry_contracts(compiled):
    app = settings(compiled["behavior-api.bicep"], "fa-behavior-api")
    assert app["ingressAllowInsecure"] is False and app["ingressTargetPort"] == 8080
    assert app["activeRevisionsMode"] == "Single"
    assert app["scaleSettings"] == {"minReplicas": "[parameters('minReplicas')]", "maxReplicas": 1}
    assert app["managedIdentities"]["systemAssigned"] is False
    job = settings(compiled["behavior-job.bicep"], "fa-behavior-manual-job")
    assert job["triggerType"] == "Manual"
    assert job["manualTriggerConfig"] == {"parallelism": 1, "replicaCompletionCount": 1}
    assert job["replicaRetryLimit"] == 0
    assert job["managedIdentities"]["systemAssigned"] is False
    stage = json.loads((INFRA / "behavior-api.stage.parameters.json").read_text(encoding="utf-8"))["parameters"]
    assert stage["minReplicas"]["value"] == 1
    assert compiled["behavior-api.bicep"]["parameters"]["minReplicas"]["defaultValue"] == 0


def test_avm_network_registry_and_monitoring_keep_existing_explicit_settings(compiled):
    environment = settings(compiled["foundation.bicep"], "shared-context-api-environment")
    assert environment["publicNetworkAccess"] == "Enabled" and environment["zoneRedundant"] is False
    assert environment["workloadProfiles"] == [{"name": "Consumption", "workloadProfileType": "Consumption"}]
    registry = settings(compiled["foundation.bicep"], "shared-context-registry")
    assert registry["acrSku"] == "Basic" and registry["acrAdminUserEnabled"] is False
    assert registry["anonymousPullEnabled"] is False and registry["roleAssignmentMode"] == "LegacyRegistryPermissions"
    network = settings(compiled["private-state-network.bicep"], "shared-context-vnet")
    assert network["addressPrefixes"] == (  # 192.168.97.0/24 only for snet-agents (FA_NETWORK_MODE=private)
        "[if(variables('isPrivate'), createObject('value', createArray('192.168.96.0/24', '192.168.97.0/24')), "
        "createObject('value', createArray('192.168.96.0/24')))]")
    workspace = settings(compiled["stage-monitoring.bicep"], "stage-log-analytics")
    assert workspace["dataRetention"] == 30 and workspace["dailyQuotaGb"] == "[parameters('dailyQuotaGb')]"
    realtime = settings(compiled["realtime.bicep"], "fa-realtime-webpubsub")
    assert realtime["capacity"] == 1 and realtime["disableLocalAuth"] is True and realtime["disableAadAuth"] is False


def test_api_subnet_nat_egress_is_opt_in_and_default_keeps_the_original_subnets(compiled):
    template = compiled["private-state-network.bicep"]
    assert template["parameters"]["apiSubnetNatEgress"] == {
        "type": "bool", "defaultValue": False, "metadata": template["parameters"]["apiSubnetNatEgress"]["metadata"]}
    # The public branch is the original expression; private (tests/infra/test_network_mode.py) adds snet-agents.
    assert deployment(template, "shared-context-vnet")["properties"]["parameters"]["subnets"] == (
        "[if(variables('isPrivate'), createObject('value', concat(variables('natEgressSubnets'), "
        "createArray(variables('agentSubnet')))), "
        "if(parameters('apiSubnetNatEgress'), createObject('value', variables('natEgressSubnets')), "
        "createObject('value', variables('baseSubnets'))))]")
    variables = template["variables"]
    assert template["parameters"]["networkMode"]["defaultValue"] == "public"
    assert variables["isPrivate"] == "[equals(parameters('networkMode'), 'private')]"
    assert variables["natEgress"] == "[or(parameters('apiSubnetNatEgress'), variables('isPrivate'))]"
    assert variables["baseSubnets"] == [
        {"name": "snet-api", "addressPrefix": "192.168.96.0/26", "delegation": "Microsoft.App/environments"},
        {"name": "snet-private-endpoints", "addressPrefix": "192.168.96.64/27", "privateEndpointNetworkPolicies": "Disabled"},
    ]
    assert variables["natEgressSubnets"] == [
        {
            "name": "snet-api",
            "addressPrefix": "192.168.96.0/26",
            "delegation": "Microsoft.App/environments",
            "natGatewayResourceId": "[resourceId('Microsoft.Network/natGateways', variables('natGatewayName'))]",
            "defaultOutboundAccess": False,
        },
        {
            "name": "snet-private-endpoints",
            "addressPrefix": "192.168.96.64/27",
            "privateEndpointNetworkPolicies": "Disabled",
            "defaultOutboundAccess": False,
        },
    ]
    # Never on an existing VNet (ADR-0010 phase 2b: its egress is the user's).
    assert variables["byoVnet"] == "[not(equals(parameters('vnetResourceId'), ''))]"
    for name in ("shared-context-outbound-public-ip", "shared-context-outbound-nat"):
        assert deployment(template, name)["condition"] == "[and(variables('natEgress'), not(variables('byoVnet')))]"
    public_ip = settings(template, "shared-context-outbound-public-ip")
    assert public_ip["skuName"] == "Standard" and public_ip["publicIPAllocationMethod"] == "Static"
    nat = settings(template, "shared-context-outbound-nat")
    assert nat["natGatewaySku"] == "Standard"
    assert nat["publicIpResourceIds"] == [
        "[resourceId('Microsoft.Network/publicIPAddresses', variables('natPublicIpName'))]"]
    foundation = compiled["foundation.bicep"]
    assert foundation["parameters"]["apiSubnetNatEgress"]["defaultValue"] is False
    assert settings(foundation, "shared-context-private-state-network")["apiSubnetNatEgress"] == (
        "[parameters('apiSubnetNatEgress')]")


def test_foundation_deploys_the_network_unless_told_otherwise(compiled):
    # Stage and the manual flow run foundation.bicep without createNetwork; only infra/azd/main.bicep passes false.
    foundation = compiled["foundation.bicep"]
    assert foundation["parameters"]["createNetwork"]["defaultValue"] is True
    assert deployment(foundation, "shared-context-private-state-network")["condition"] == (
        "[parameters('createNetwork')]")
    assert foundation["parameters"]["networkMode"]["defaultValue"] == "public"
    assert settings(foundation, "shared-context-private-state-network")["networkMode"] == "[parameters('networkMode')]"


def test_model_deployments_are_a_parameter_whose_default_keeps_the_original_two(compiled):
    template = compiled["foundry.bicep"]
    default = template["parameters"]["modelDeployments"]["defaultValue"]
    assert [(item["name"], item["sku"]["capacity"], item["versionUpgradeOption"]) for item in default] == [
        ("gpt-4.1-mini", 10, "NoAutoUpgrade"), ("text-embedding-3-small", 10, "NoAutoUpgrade")]
    account = deployment(template, "shared-context-foundry-account")
    assert account["properties"]["parameters"]["deployments"]["value"] == "[parameters('modelDeployments')]"


def test_generated_parameter_files_remain_compatible_with_the_compiled_modules(compiled):
    spec = importlib.util.spec_from_file_location(
        "offline_avm_prepare", ROOT / "deploy" / "shared-context" / "prepare_environment.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = json.loads((ROOT / "deploy" / "shared-context" / "environment.example.json").read_text(encoding="utf-8"))
    source.update(
        subscription_id="11111111-1111-1111-1111-111111111111",
        tenant_id="22222222-2222-2222-2222-222222222222",
        operator_object_id="33333333-3333-3333-3333-333333333333",
        read_write_role_definition_guid="44444444-4444-4444-4444-444444444444",
    )
    values = module.resolve(source)["parameters"]
    template_overrides = {"monitoring.parameters.json": "stage-monitoring.bicep"}
    assert set(template_overrides) <= set(values)
    for name, parameters in values.items():
        filename = template_overrides.get(name, name.removesuffix(".parameters.json") + ".bicep")
        assert set(parameters) <= set(compiled[filename]["parameters"]), name
    assert values["behavior-api.parameters.json"]["minReplicas"] == 1


WORKFLOWS = ROOT / ".github" / "workflows"


@pytest.mark.skipif(
    not WORKFLOWS.is_dir(),
    reason=".github/workflows/ is absent: the public copy excludes it (scrub_export.py EXCLUDED_PREFIXES)",
)
def test_infrastructure_workflow_compiles_without_azure_credentials_or_deployment():
    path = WORKFLOWS / "validate-infrastructure.yml"
    workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert workflow["permissions"] == {"contents": "read"}
    assert "infra/**" in workflow["on"]["pull_request"]["paths"]
    assert set(workflow["jobs"]) == {"compile-contracts"}
    job = workflow["jobs"]["compile-contracts"]
    assert "environment" not in job and "permissions" not in job
    steps = job["steps"]
    assert steps[0]["with"]["persist-credentials"] == "false"
    commands = "\n".join(item.get("run", "") for item in steps)
    assert "v0.48.1/bicep-linux-x64" in commands and "sha256sum --check" in commands
    assert "tests/infra" in commands
    assert not any("azure/login" in item.get("uses", "") for item in steps)
    assert not re.search(r"\baz\s|azd\s", commands)
