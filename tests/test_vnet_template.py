"""Local contracts; compile infra/vnet/main.bicep into infra/vnet/.azure/main.json for ARM checks."""

import ipaddress
import json
import os
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra" / "vnet"
COMPILED = INFRA / ".azure" / "main.json"
COMPLETION_COMPILED = INFRA / ".azure" / "completion.json"
SOURCES = {path.name: path.read_text(encoding="utf-8") for path in INFRA.glob("*.bicep")}
RESOURCE_DECLARATION = re.compile(
    r"^\s*resource\s+\w+\s+'([^'@]+)@[^']+'\s+(existing\s+)?=",
    re.MULTILINE,
)
PINNED_MODULES = {
    "network/public-ip-address": "0.13.0",
    "network/nat-gateway": "2.1.1",
    "network/network-security-group": "0.5.3",
    "network/virtual-network": "0.10.2",
    "network/private-dns-zone": "0.8.1",
    "network/private-endpoint": "0.12.1",
    "compute/virtual-machine": "0.22.3",
}


def resource(template, *, name=None, resource_type=None):
    resources = template["resources"]
    if isinstance(resources, dict):
        resources = resources.values()
    matches = [
        item
        for item in resources
        if (name is None or item["name"] == name)
        and (resource_type is None or item["type"] == resource_type)
    ]
    if len(matches) != 1:
        raise AssertionError(f"Expected one resource for {name or resource_type}: {len(matches)}")
    return matches[0]


def module_template(template, name):
    return resource(template, name=name)["properties"]["template"]


def module_params(template, name):
    return {
        key: item["value"]
        for key, item in resource(template, name=name)["properties"]["parameters"].items()
    }


class VnetSourceTests(unittest.TestCase):
    def test_only_owned_templates_and_pinned_avm_modules(self):
        self.assertEqual(
            set(SOURCES),
            {
                "main.bicep",
                "network.bicep",
                "controller.bicep",
                "project-access.bicep",
                "registry-access.bicep",
                "registry-connection.bicep",
                "completion.bicep",
            },
        )
        pins = re.findall(r"'br/public:avm/res/([^:]+):([^']+)'", "\n".join(SOURCES.values()))
        self.assertEqual(dict(pins), PINNED_MODULES)
        for source in SOURCES.values():
            for reference in re.findall(r"^\s*module\s+\w+\s+'([^']+)'", source, re.MULTILINE):
                if not reference.startswith("br/public:"):
                    self.assertIn(Path(reference).name, SOURCES)

    def test_support_resources_are_existing_references_only(self):
        support = {
            "Microsoft.KeyVault/vaults",
            "Microsoft.Storage/storageAccounts",
            "Microsoft.ContainerRegistry/registries",
        }
        seen = set()
        for source in SOURCES.values():
            for resource_type, existing in RESOURCE_DECLARATION.findall(source):
                if resource_type in support:
                    self.assertTrue(existing, f"{resource_type} must never be PUT by this template")
                    seen.add(resource_type)
        self.assertEqual(seen, support)

    def test_no_extra_services_secret_retrieval_or_runtime_scripts(self):
        source = "\n".join(SOURCES.values())
        for forbidden in (
            "Microsoft.CognitiveServices/accounts/deployments",
            "Microsoft.DocumentDB/",
            "Microsoft.Search/",
            "Microsoft.Insights/",
            "Microsoft.OperationalInsights/",
            "Microsoft.Resources/deploymentScripts",
            "Microsoft.CognitiveServices/accounts/capabilityHosts",
            "listKeys(",
            "listSecrets(",
            "secretsExportConfiguration",
            "allowSharedKeyAccess:",
            "customData:",
            "userData:",
            "extensionCustomScriptConfig:",
        ):
            self.assertNotIn(forbidden, source)

    def test_new_injected_account_is_not_the_old_account(self):
        source = SOURCES["main.bicep"]
        self.assertIn("param accountName string = 'cog-fasample-vnet-demo'", source)
        self.assertNotIn("cog-contosodemo01", source)
        declaration = next(
            existing
            for resource_type, existing in RESOURCE_DECLARATION.findall(source)
            if resource_type == "Microsoft.CognitiveServices/accounts"
        )
        self.assertFalse(declaration)
        self.assertIn("networkInjections: [", source)
        self.assertIn(
            "subnetArmId: resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, agentSubnetName)",
            source,
        )
        self.assertIn("useMicrosoftManagedNetwork: false", source)

    def test_network_default_ranges_do_not_overlap(self):
        defaults = dict(
            re.findall(r"param (\w+Prefix) string = '([^']+)'", SOURCES["network.bicep"])
        )
        vnet = ipaddress.ip_network(defaults.pop("vnetAddressPrefix"))
        self.assertEqual(str(vnet), "192.168.80.0/22")
        subnets = [ipaddress.ip_network(value) for value in defaults.values()]
        self.assertEqual(len(subnets), 3)
        for index, subnet in enumerate(subnets):
            self.assertEqual(subnet.prefixlen, 24)
            self.assertTrue(subnet.subnet_of(vnet))
            for other in subnets[index + 1 :]:
                self.assertFalse(subnet.overlaps(other))
        for other in ("10.0.0.0/16", "10.42.0.0/16"):
            self.assertFalse(vnet.overlaps(ipaddress.ip_network(other)))

    def test_controller_has_no_public_ip_or_password_configuration(self):
        controller = SOURCES["controller.bicep"]
        self.assertIn("disablePasswordAuthentication: true", controller)
        self.assertIn("@minLength(32)\n@secure()\nparam sshPublicKey string\n", controller)
        self.assertNotIn("adminPassword:", controller)
        self.assertNotIn("pipConfiguration:", controller)
        self.assertNotIn("publicIPAddress:", controller)
        self.assertNotIn("public-ip-address:", controller)
        self.assertIn("param enableController bool = false", SOURCES["main.bicep"])

    def test_default_controller_size_and_nvme_contract(self):
        self.assertIn("param controllerVmSize string = 'Standard_D2ls_v7'", SOURCES["main.bicep"])
        self.assertIn("param vmSize string = 'Standard_D2ls_v7'", SOURCES["controller.bicep"])
        self.assertIn("diskControllerType: 'NVMe'", SOURCES["controller.bicep"])

    def test_role_scopes_and_verified_guids(self):
        project_access = SOURCES["project-access.bicep"]
        registry_access = SOURCES["registry-access.bicep"]
        self.assertIn("53ca6127-db72-4b80-b1b0-d745d6d5456d", project_access)
        self.assertIn("scope: project", project_access)
        self.assertIn("7f951dda-4ed3-4680-a7ca-43fe172d538d", registry_access)
        self.assertIn("scope: registry", registry_access)
        for source in SOURCES.values():
            self.assertNotIn("scope: subscription()", source)
            self.assertNotIn("scope: vault", source)

    def test_parameter_preset_does_not_enable_controller_or_store_key(self):
        preset = json.loads((INFRA / "main.parameters.json").read_text(encoding="utf-8"))
        parameters = preset["parameters"]
        self.assertFalse(parameters["enableController"]["value"])
        self.assertNotIn("controllerSshPublicKey", parameters)
        prefix = (
            "/subscriptions/00000000-0000-0000-0000-000000000012/"
            "resourceGroups/rg-faexample/providers/"
        )
        for name in (
            "existingKeyVaultResourceId",
            "existingStorageResourceId",
            "existingContainerRegistryResourceId",
        ):
            self.assertTrue(parameters[name]["value"].startswith(prefix))

    def test_compiled_artifact_is_ignored(self):
        ignored = (INFRA / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".azure/", ignored)
        self.assertIn("/main.json", ignored)
        self.assertIn("/completion.json", ignored)


class VnetCompiledTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not COMPILED.exists():
            if os.environ.get("VNET_REQUIRE_COMPILED") == "1":
                raise AssertionError(f"Build the Bicep template first: {COMPILED}")
            raise unittest.SkipTest("Build infra/vnet/main.bicep to enable compiled ARM checks")
        newest_source = max(path.stat().st_mtime for path in INFRA.glob("*.bicep"))
        if COMPILED.stat().st_mtime < newest_source:
            raise AssertionError("Compiled ARM is stale; rebuild infra/vnet/main.bicep")
        cls.template = json.loads(COMPILED.read_text(encoding="utf-8-sig"))
        cls.network = module_template(cls.template, "vnet-foundry-network")
        cls.controller = module_template(cls.template, "vnet-private-controller")
        cls.connection = module_template(cls.template, "vnet-project-registry-connection")

    def test_injection_id_is_preflight_resolvable_and_dependency_ordered(self):
        account = resource(self.template, resource_type="Microsoft.CognitiveServices/accounts")
        subnet_id = account["properties"]["networkInjections"][0]["subnetArmId"]
        self.assertNotIn("reference(", subnet_id)
        self.assertEqual(
            subnet_id,
            "[resourceId('Microsoft.Network/virtualNetworks/subnets', "
            "parameters('vnetName'), parameters('agentSubnetName'))]",
        )
        self.assertIn(
            "[resourceId('Microsoft.Resources/deployments', 'vnet-foundry-network')]",
            account["dependsOn"],
        )
        network_module = resource(self.template, name="vnet-foundry-network")
        self.assertNotIn("resourceGroup", network_module)
        network_inputs = module_params(self.template, "vnet-foundry-network")
        self.assertEqual(network_inputs["vnetName"], "[parameters('vnetName')]")
        self.assertEqual(network_inputs["agentSubnetName"], "[parameters('agentSubnetName')]")
        vnet_inputs = module_params(self.network, "vnet-network")
        self.assertEqual(vnet_inputs["name"], "[parameters('vnetName')]")
        self.assertEqual(vnet_inputs["subnets"][0]["name"], "[parameters('agentSubnetName')]")

    def test_account_create_contract_and_no_support_puts(self):
        account = resource(self.template, resource_type="Microsoft.CognitiveServices/accounts")
        self.assertEqual(account["kind"], "AIServices")
        self.assertEqual(account["sku"]["name"], "S0")
        self.assertEqual(account["identity"], {"type": "SystemAssigned"})
        properties = account["properties"]
        self.assertTrue(properties["allowProjectManagement"])
        self.assertTrue(properties["disableLocalAuth"])
        self.assertEqual(properties["publicNetworkAccess"], "Disabled")
        injection = properties["networkInjections"]
        self.assertEqual(len(injection), 1)
        self.assertEqual(injection[0]["scenario"], "agent")
        self.assertFalse(injection[0]["useMicrosoftManagedNetwork"])
        self.assertIn("resourceId('Microsoft.Network/virtualNetworks/subnets'", injection[0]["subnetArmId"])
        types = {
            item["type"] for item in self.template["resources"]
            if item["type"] != "Microsoft.Resources/deployments"
        }
        self.assertEqual(
            types,
            {
                "Microsoft.CognitiveServices/accounts",
                "Microsoft.CognitiveServices/accounts/projects",
                "Microsoft.CognitiveServices/accounts/projects/capabilityHosts",
            },
        )

    def test_only_basic_project_capability_host(self):
        caphost = resource(
            self.template,
            resource_type="Microsoft.CognitiveServices/accounts/projects/capabilityHosts",
        )
        self.assertEqual(caphost["properties"], {"capabilityHostKind": "Agents"})
        self.assertTrue(caphost["dependsOn"])

    def test_injection_and_controller_share_explicit_nat_not_a_subnet(self):
        network = module_params(self.network, "vnet-network")
        agent, endpoint, controller = network["subnets"]
        self.assertEqual(agent["delegation"], "Microsoft.App/environments")
        self.assertNotIn("delegation", controller)
        self.assertNotIn("delegation", endpoint)
        self.assertNotEqual(agent["name"], controller["name"])
        self.assertEqual(agent["natGatewayResourceId"], controller["natGatewayResourceId"])
        for subnet in (agent, endpoint, controller):
            self.assertFalse(subnet["defaultOutboundAccess"])
        self.assertEqual(endpoint["privateEndpointNetworkPolicies"], "Disabled")
        nat = module_params(self.network, "vnet-outbound-nat")
        public_ip = module_params(self.network, "vnet-outbound-public-ip")
        self.assertEqual(nat["natGatewaySku"], "Standard")
        self.assertEqual(len(nat["publicIpResourceIds"]), 1)
        self.assertEqual(public_ip["skuName"], "Standard")
        self.assertEqual(public_ip["publicIPAllocationMethod"], "Static")

    def test_nsg_preserves_outbound_and_blocks_controller_inbound(self):
        self.assertEqual(module_params(self.network, "vnet-agent-nsg")["securityRules"], [])
        rules = module_params(self.network, "vnet-controller-nsg")["securityRules"]
        self.assertEqual(len(rules), 1)
        properties = rules[0]["properties"]
        self.assertEqual(properties["direction"], "Inbound")
        self.assertEqual(properties["access"], "Deny")
        for name in ("sourceAddressPrefix", "destinationAddressPrefix", "destinationPortRange", "protocol"):
            self.assertEqual(properties[name], "*")

    def test_network_module_resource_ids_are_preflight_resolvable(self):
        dependencies = {
            "vnet-outbound-nat": ["vnet-outbound-public-ip"],
            "vnet-network": ["vnet-outbound-nat", "vnet-agent-nsg", "vnet-controller-nsg"],
        }
        for name, required in dependencies.items():
            with self.subTest(module=name):
                inputs = module_params(self.network, name)
                self.assertNotIn("reference(", json.dumps(inputs))
                actual = resource(self.network, name=name)["dependsOn"]
                for dependency in required:
                    self.assertIn(
                        f"[resourceId('Microsoft.Resources/deployments', '{dependency}')]",
                        actual,
                    )
        nat = module_params(self.network, "vnet-outbound-nat")
        self.assertEqual(
            nat["publicIpResourceIds"],
            ["[resourceId('Microsoft.Network/publicIPAddresses', parameters('natPublicIpName'))]"],
        )
        for module, parameter in (
            ("vnet-outbound-public-ip", "natPublicIpName"),
            ("vnet-outbound-nat", "natGatewayName"),
            ("vnet-agent-nsg", "agentNsgName"),
            ("vnet-controller-nsg", "controllerNsgName"),
        ):
            self.assertEqual(module_params(self.network, module)["name"], f"[parameters('{parameter}')]")
        subnets = module_params(self.network, "vnet-network")["subnets"]
        for subnet, nsg_parameter in [(subnets[0], "agentNsgName"), (subnets[2], "controllerNsgName")]:
            self.assertEqual(
                subnet["natGatewayResourceId"],
                "[resourceId('Microsoft.Network/natGateways', parameters('natGatewayName'))]",
            )
            self.assertEqual(
                subnet["networkSecurityGroupResourceId"],
                f"[resourceId('Microsoft.Network/networkSecurityGroups', parameters('{nsg_parameter}'))]",
            )
        dns = next(item for item in self.network["resources"] if "copy" in item)
        self.assertNotIn("reference(", json.dumps(dns["properties"]["parameters"]))
        self.assertIn(
            "[resourceId('Microsoft.Resources/deployments', 'vnet-network')]",
            dns["dependsOn"],
        )

    def test_main_module_resource_ids_are_preflight_resolvable(self):
        controller = resource(self.template, name="vnet-private-controller")
        endpoints = next(item for item in self.template["resources"] if "copy" in item)
        for deployment in (controller, endpoints):
            with self.subTest(module=deployment["name"]):
                self.assertNotIn("reference(", json.dumps(deployment["properties"]["parameters"]))
                self.assertIn(
                    "[resourceId('Microsoft.Resources/deployments', 'vnet-foundry-network')]",
                    deployment["dependsOn"],
                )
        self.assertEqual(
            module_params(self.template, "vnet-private-controller")["controllerSubnetId"],
            "[resourceId('Microsoft.Network/virtualNetworks/subnets', "
            "parameters('vnetName'), parameters('controllerSubnetName'))]",
        )
        endpoint_inputs = endpoints["properties"]["parameters"]
        self.assertEqual(
            endpoint_inputs["subnetResourceId"]["value"],
            "[resourceId('Microsoft.Network/virtualNetworks/subnets', "
            "parameters('vnetName'), parameters('privateEndpointSubnetName'))]",
        )
        self.assertIn(
            "resourceId('Microsoft.Network/privateDnsZones', variables('privateDnsZoneNames')",
            json.dumps(endpoint_inputs["privateDnsZoneGroup"]),
        )
        self.assertEqual(
            self.template["variables"]["privateDnsZoneNames"],
            self.network["variables"]["privateDnsZoneNames"],
        )
        self.assertNotIn("reference(", json.dumps(self.template["variables"]["endpointDefinitions"]))
        network_inputs = module_params(self.template, "vnet-foundry-network")
        subnets = module_params(self.network, "vnet-network")["subnets"]
        for index, parameter in [(1, "privateEndpointSubnetName"), (2, "controllerSubnetName")]:
            self.assertEqual(network_inputs[parameter], f"[parameters('{parameter}')]")
            self.assertEqual(subnets[index]["name"], f"[parameters('{parameter}')]")

    def test_generated_principals_remain_runtime_values(self):
        project_pull = module_params(self.template, "vnet-project-acr-pull")["projectPrincipalId"]
        self.assertIn("reference(", project_pull)
        self.assertIn("identity.principalId", project_pull)
        controller_user = module_params(self.controller, "vnet-controller-project-user")["principalId"]
        self.assertIn("reference(", controller_user)
        self.assertIn("outputs.systemAssignedMIPrincipalId.value", controller_user)

    def test_all_private_endpoint_groups_and_dns_zones(self):
        definitions = self.template["variables"]["endpointDefinitions"]
        self.assertEqual(
            {item["groupId"]: item["dnsZoneIndexes"] for item in definitions},
            {"account": [0, 1, 2], "vault": [3], "file": [4], "registry": [5]},
        )
        zones = self.network["variables"]["privateDnsZoneNames"]
        self.assertEqual(len(zones), 6)
        self.assertEqual(
            zones[:4],
            [
                "privatelink.cognitiveservices.azure.com",
                "privatelink.openai.azure.com",
                "privatelink.services.ai.azure.com",
                "privatelink.vaultcore.azure.net",
            ],
        )
        self.assertIn("privatelink.file.", zones[4])
        self.assertEqual(zones[5], "privatelink.azurecr.io")
        dns_module = next(item for item in self.network["resources"] if "copy" in item)
        dns_params = dns_module["properties"]["parameters"]
        link = dns_params["virtualNetworkLinks"]["value"][0]
        self.assertFalse(link["registrationEnabled"])
        self.assertEqual(
            link["virtualNetworkResourceId"],
            "[resourceId('Microsoft.Network/virtualNetworks', parameters('vnetName'))]",
        )
        self.assertNotIn("resourceGroup", dns_module)
        endpoint_module = next(item for item in self.template["resources"] if "copy" in item)
        self.assertIn("privateDnsZoneGroup", endpoint_module["properties"]["parameters"])
        self.assertIn(
            "resourceId('Microsoft.Network/virtualNetworks/subnets'",
            str(endpoint_module["properties"]["parameters"]),
        )
        self.assertIn("Microsoft.CognitiveServices/accounts", str(endpoint_module["dependsOn"]))

    def test_registry_connection_uses_project_identity(self):
        connection = resource(
            self.connection,
            resource_type="Microsoft.CognitiveServices/accounts/projects/connections",
        )["properties"]
        self.assertEqual(connection["category"], "ContainerRegistry")
        self.assertEqual(connection["authType"], "ManagedIdentity")
        self.assertTrue(connection["useWorkspaceManagedIdentity"])
        self.assertTrue(connection["credentials"])
        pull = module_params(self.template, "vnet-project-acr-pull")
        self.assertIn("Microsoft.CognitiveServices/accounts/projects", pull["projectPrincipalId"])
        self.assertIn("identity.principalId", pull["projectPrincipalId"])

    def test_registry_identity_credentials_match_official_azd(self):
        connection = resource(
            self.connection,
            resource_type="Microsoft.CognitiveServices/accounts/projects/connections",
        )["properties"]
        self.assertEqual(set(connection["credentials"]), {"clientId", "resourceId"})
        self.assertEqual(
            connection["credentials"]["clientId"],
            "[reference(resourceId('Microsoft.CognitiveServices/accounts/projects', "
            "parameters('accountName'), parameters('projectName')), "
            "'2025-04-01-preview', 'full').identity.principalId]",
        )
        self.assertEqual(connection["credentials"]["resourceId"], connection["metadata"]["ResourceId"])
        self.assertIn("Microsoft.ContainerRegistry/registries", connection["credentials"]["resourceId"])
        self.assertNotIn("Microsoft.ManagedIdentity/userAssignedIdentities", str(connection["credentials"]))
        registry_id_expression = connection["metadata"]["ResourceId"][1:-1]
        self.assertEqual(
            connection["target"],
            f"[reference({registry_id_expression}, '2023-07-01').loginServer]",
        )

    def test_controller_is_optional_private_keyless_and_small(self):
        controller_module = resource(self.template, name="vnet-private-controller")
        self.assertEqual(controller_module["condition"], "[parameters('enableController')]")
        self.assertFalse(self.template["parameters"]["enableController"]["defaultValue"])
        key = self.controller["parameters"]["sshPublicKey"]
        self.assertEqual(key["type"].lower(), "securestring")
        self.assertGreaterEqual(key["minLength"], 32)
        self.assertNotIn("defaultValue", key)
        vm = module_params(self.controller, "vnet-controller-vm")
        self.assertTrue(vm["disablePasswordAuthentication"])
        self.assertTrue(vm["provisionVMAgent"])
        self.assertTrue(vm["allowExtensionOperations"])
        self.assertEqual(vm["managedIdentities"], {"systemAssigned": True})
        self.assertEqual(vm["imageReference"]["offer"], "ubuntu-24_04-lts")
        self.assertEqual(vm["imageReference"]["sku"], "minimal")
        self.assertEqual(vm["osDisk"]["managedDisk"]["storageAccountType"], "StandardSSD_LRS")
        self.assertEqual(len(vm["nicConfigurations"]), 1)
        nic = vm["nicConfigurations"][0]
        self.assertFalse(nic["enableIPForwarding"])
        self.assertNotIn("public", json.dumps(nic).lower())
        self.assertEqual(nic["ipConfigurations"][0]["subnetResourceId"], "[parameters('controllerSubnetId')]")
        self.assertNotIn("adminPassword", vm)
        self.assertNotIn("customData", vm)
        self.assertNotIn("dataDisks", vm)

    def test_controller_permission_is_project_only(self):
        access = module_template(self.controller, "vnet-controller-project-user")
        self.assertEqual(len(access["resources"]), 1)
        assignment = access["resources"][0]
        self.assertEqual(assignment["type"], "Microsoft.Authorization/roleAssignments")
        self.assertIn("Microsoft.CognitiveServices/accounts/projects", assignment["scope"])
        self.assertIn("principalId", assignment["name"])
        self.assertEqual(access["variables"]["foundryUserRoleId"], "53ca6127-db72-4b80-b1b0-d745d6d5456d")

    def test_nvme_default_reaches_avm_vm_storage_profile(self):
        self.assertEqual(
            self.template["parameters"]["controllerVmSize"]["defaultValue"], "Standard_D2ls_v7"
        )
        self.assertEqual(self.controller["parameters"]["vmSize"]["defaultValue"], "Standard_D2ls_v7")
        self.assertEqual(
            module_params(self.template, "vnet-private-controller")["vmSize"],
            "[parameters('controllerVmSize')]",
        )
        self.assertEqual(module_params(self.controller, "vnet-controller-vm")["diskControllerType"], "NVMe")
        avm = module_template(self.controller, "vnet-controller-vm")
        vm = resource(avm, resource_type="Microsoft.Compute/virtualMachines")
        self.assertEqual(vm["properties"]["storageProfile"]["diskControllerType"], "[parameters('diskControllerType')]")

    def test_standard_ssd_reaches_actual_vm_os_disk(self):
        inputs = module_params(self.controller, "vnet-controller-vm")
        avm = module_template(self.controller, "vnet-controller-vm")
        vm = resource(avm, resource_type="Microsoft.Compute/virtualMachines")
        managed_disk = vm["properties"]["storageProfile"]["osDisk"]["managedDisk"]
        self.assertEqual(
            managed_disk["storageAccountType"],
            "[tryGet(parameters('osDisk').managedDisk, 'storageAccountType')]",
        )
        self.assertEqual(inputs["osDisk"]["managedDisk"]["storageAccountType"], "StandardSSD_LRS")

    def test_outputs_are_complete_and_do_not_contain_secrets(self):
        outputs = self.template["outputs"]
        for name in (
            "accountId", "accountName", "accountEndpoint", "accountPrincipalId",
            "projectId", "projectName", "projectEndpoint", "projectPrincipalId",
            "agentSubnetId", "privateEndpointSubnetId", "controllerSubnetId",
            "controllerVmId", "controllerVmName", "controllerPrincipalId",
            "privateEndpointNames", "privateEndpointIds", "privateDnsZoneIds",
            "existingKeyVaultId", "existingStorageId", "existingContainerRegistryId",
        ):
            self.assertIn(name, outputs)
        for name, value in outputs.items():
            self.assertNotRegex(name.lower(), r"secret|password|ssh|token")
            self.assertNotIn("listKeys", json.dumps(value))
        for name in ("controllerVmId", "controllerVmName", "controllerPrincipalId"):
            self.assertIn("if(parameters('enableController')", outputs[name]["value"])


class VnetCompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for path in (COMPILED, COMPLETION_COMPILED):
            if not path.exists():
                if os.environ.get("VNET_REQUIRE_COMPILED") == "1":
                    raise AssertionError(f"Build both VNet entrypoints first: {path}")
                raise unittest.SkipTest("Build both main.bicep and completion.bicep for completion checks")
            if path.stat().st_mtime < max(source.stat().st_mtime for source in INFRA.glob("*.bicep")):
                raise AssertionError(f"Compiled ARM is stale: {path}")
        cls.main = json.loads(COMPILED.read_text(encoding="utf-8-sig"))
        cls.completion = json.loads(COMPLETION_COMPILED.read_text(encoding="utf-8-sig"))

    def test_completion_only_deploys_shared_connection_and_controller(self):
        deployments = self.completion["resources"]
        self.assertEqual(
            {(item["name"], item["type"]) for item in deployments},
            {
                ("vnet-project-registry-connection", "Microsoft.Resources/deployments"),
                ("vnet-private-controller", "Microsoft.Resources/deployments"),
            },
        )
        for name in ("vnet-project-registry-connection", "vnet-private-controller"):
            self.assertEqual(module_template(self.completion, name), module_template(self.main, name))
        connection = module_template(self.completion, "vnet-project-registry-connection")
        self.assertEqual(
            [item["type"] for item in connection["resources"]],
            ["Microsoft.CognitiveServices/accounts/projects/connections"],
        )
        self.assertIn(
            "[resourceId('Microsoft.Resources/deployments', 'vnet-project-registry-connection')]",
            resource(self.completion, name="vnet-private-controller")["dependsOn"],
        )

    def test_completed_prerequisites_are_existing_only(self):
        declarations = RESOURCE_DECLARATION.findall(SOURCES["completion.bicep"])
        self.assertEqual(
            {kind for kind, _ in declarations},
            {
                "Microsoft.CognitiveServices/accounts",
                "Microsoft.CognitiveServices/accounts/projects",
                "Microsoft.Network/virtualNetworks",
                "Microsoft.Network/virtualNetworks/subnets",
                "Microsoft.ContainerRegistry/registries",
            },
        )
        self.assertTrue(all(existing for _, existing in declarations))
        self.assertNotIn("networkInjections", SOURCES["completion.bicep"])
        self.assertNotIn("registry-access.bicep", SOURCES["completion.bicep"])
        self.assertNotIn("project-access.bicep", SOURCES["completion.bicep"])
        self.assertNotIn("network.bicep", SOURCES["completion.bicep"])

    def test_completion_preserves_approved_controller_configuration(self):
        parameters = self.completion["parameters"]
        required = {name for name, spec in parameters.items() if "defaultValue" not in spec}
        self.assertEqual(required, {"controllerSshPublicKey"})
        self.assertEqual(parameters["controllerSshPublicKey"]["type"].lower(), "securestring")
        self.assertGreaterEqual(parameters["controllerSshPublicKey"]["minLength"], 32)
        self.assertEqual(parameters["controllerVmSize"]["defaultValue"], "Standard_D2ls_v7")
        self.assertEqual(parameters["controllerImageVersion"]["defaultValue"], "24.04.202608270")
        self.assertEqual(parameters["controllerOsDiskSizeGB"]["defaultValue"], 32)
        inputs = module_params(self.completion, "vnet-private-controller")
        for child, parent in (
            ("sshPublicKey", "controllerSshPublicKey"),
            ("vmSize", "controllerVmSize"),
            ("imageVersion", "controllerImageVersion"),
        ):
            self.assertEqual(inputs[child], f"[parameters('{parent}')]")
        self.assertEqual(
            inputs["controllerSubnetId"],
            "[resourceId('Microsoft.Network/virtualNetworks/subnets', "
            "parameters('vnetName'), parameters('controllerSubnetName'))]",
        )
        self.assertNotIn("condition", resource(self.completion, name="vnet-private-controller"))

    def test_completion_inputs_are_resolvable_and_outputs_are_nonsecret(self):
        for deployment in self.completion["resources"]:
            self.assertNotIn("reference(", json.dumps(deployment["properties"]["parameters"]))
        outputs = self.completion["outputs"]
        for name in (
            "containerRegistryConnectionId", "containerRegistryEndpoint",
            "controllerVmId", "controllerVmName", "controllerPrincipalId",
            "accountId", "projectId", "projectEndpoint", "projectPrincipalId",
            "controllerSubnetId", "existingContainerRegistryId",
        ):
            self.assertIn(name, outputs)
        for name in outputs:
            self.assertNotRegex(name.lower(), r"secret|password|ssh|token|credentials")


if __name__ == "__main__":
    unittest.main()
