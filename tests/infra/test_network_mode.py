"""ADR-0010 FA_NETWORK_MODE, compiled locally; no Azure login, what-if or deployment.

`public` (the default) must deploy exactly what it did before the option existed: stage and dev use these modules and
the shared Foundry account. The proof is mechanical. Each compiled template is reduced to its *leaves*: every AVM module
call (the inputs it gets plus the AVM template hash) and every native resource, with each expression rewritten in terms
of the entry template's own parameters. Inside the tree, parameters and variables are inlined, module outputs are
replaced by the expression that produces them, and `networkMode` is fixed to 'public' before constant folding (so a
private-only module drops out and `if(private, X, Y)` becomes Y); foundation.bicep's new `createNetwork` takes its
default. An FA module's condition is part of each leaf under it. Module names, nesting and deployment order are
not part of a leaf, so moving the VNet out of foundation does not count as a change; any changed property or
condition does.

public_baseline.json holds the leaves of infra/azd/main.bicep, foundation.bicep and foundry.bicep compiled from main
at 6ec23c65 (before this option) with Bicep 0.48.1. Since then only main.bicep's outputs changed: ADR-0011 added
FA_COSMOS_ENDPOINT, which is '' on public, and ADR-0006 FA_AGENT_ONBOARDING_ACCOUNT_ROLE, which is false on public
(no resource changed). To regenerate it from a compiled template, run this
file with the template path; only do that for an intended change to the public deployment.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).with_name("public_baseline.json")
DEPLOYMENTS = "Microsoft.Resources/deployments"
# Entry parameters that take their default, like networkMode takes the mode: foundation.bicep's createNetwork and the
# ADR-0010 phase 2b inputs (an existing VNet), whose defaults must reproduce the deployment from before them.
PINNED_DEFAULTS = {"createNetwork", "vnetResourceId", "apiSubnetName", "agentSubnetName", "privateEndpointSubnetName",
                   "dnsMode", "apiSubnetResourceId", "privateEndpointSubnetResourceId", "privateDnsZones"}

# ---- ARM expressions -> canonical JSON ------------------------------------------------------------------------------

TOKEN = re.compile(r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>-?\d+)|(?P<name>[A-Za-z_$][\w$#]*)|(?P<op>[(),.\[\]]))\s*")


def parse(text):
    tokens, position = [], 0
    while position < len(text):
        match = TOKEN.match(text, position)
        assert match, (text, position)
        tokens.append((match.lastgroup, match.group(match.lastgroup)))
        position = match.end()
    position = 0

    def take(expected=None):
        nonlocal position
        kind, value = tokens[position]
        assert expected is None or value == expected, (text, position, value)
        position += 1
        return kind, value

    def expression():
        kind, value = take()
        if kind == "str":
            node = value[1:-1].replace("''", "'")
        elif kind == "num":
            node = int(value)
        else:
            while tokens[position][1] == ".":  # a namespaced function such as __bicep.name(...)
                value += take(".")[1] + take()[1]
            take("(")
            args = []
            while tokens[position][1] != ")":
                args.append(expression())
                if tokens[position][1] == ",":
                    take(",")
            take(")")
            node = {"$call": value.lower(), "args": args}
        while position < len(tokens) and tokens[position][1] in (".", "["):
            if take()[1] == ".":
                node = {"$get": node, "key": take()[1]}
            else:
                node = {"$get": node, "key": expression()}
                take("]")
        return node

    node = expression()
    assert position == len(tokens), text
    return node


class Scope:
    """One template in the deployment tree: its parameter bindings (canonical, in the parent's terms) and RG."""

    def __init__(self, template, bindings, rg, path, parent=None, mode="public"):
        self.template, self.bindings, self.rg, self.path, self.parent = template, bindings, rg, path, parent
        self.mode = parent.mode if parent else mode

    def resources(self):
        values = self.template.get("resources", {})
        return values.items() if isinstance(values, dict) else [(None, item) for item in values]

    def deployment(self, key):
        """A nested deployment by symbolic name (language 2.0) or by its evaluated name (1.x resourceId)."""
        for symbol, item in self.resources():
            if item.get("type") == DEPLOYMENTS and (symbol == key or self.value(item["name"]) == key):
                return item
        return None

    def child(self, item):
        """The Scope of a nested deployment, with the parameter values it is passed."""
        rg = self.value(item["resourceGroup"]) if "resourceGroup" in item else self.rg
        bindings = {name: entry_value(self.value(entry)) for name, entry in item["properties"]["parameters"].items()}
        return Scope(item["properties"]["template"], bindings, rg, [*self.path, self.value(item["name"])], self)

    def value(self, raw):
        """JSON from a template, with every expression string canonicalized and folded in this scope."""
        if isinstance(raw, str):
            if raw.startswith("[[") or not (raw.startswith("[") and raw.endswith("]")):
                return raw[1:] if raw.startswith("[[") else raw
            return fold(self.node(parse(raw[1:-1])))
        if isinstance(raw, list):
            return [self.value(item) for item in raw]
        if isinstance(raw, dict):
            return {key: self.value(item) for key, item in raw.items()}
        return raw

    def node(self, node):
        if not isinstance(node, dict):
            return node
        if "$get" in node:
            output = self.module_output(node)
            if output is not None:
                return output
            return fold({"$get": self.node(node["$get"]), "key": self.node(node["key"])})
        name, args = node["$call"], node["args"]
        if name == "parameters":
            return self.parameter(args[0])
        if name == "variables":
            return self.variable(args[0])
        args = [self.node(arg) for arg in args]
        if name in ("resourceid", "subscriptionresourceid", "resourcegroup", "extensionresourceid", "reference"):
            if name == "reference" and isinstance(args[0], str) and args[0] in dict(self.resources()):
                item = dict(self.resources())[args[0]]  # a symbolic resource: name it by type and name
                args = [{"$resource": item["type"], "name": self.value(item["name"]), "rg": self.rg}, *args[1:]]
            return {"$call": name, "args": args, "rg": self.rg}
        return fold({"$call": name, "args": args})

    def module_output(self, node):
        """reference(<deployment>).outputs.<name>.value -> the expression that output evaluates."""
        chain = node
        if chain.get("key") != "value" or "$get" not in chain["$get"]:
            return None
        output, outputs = chain["$get"], chain["$get"]["$get"]
        if outputs.get("key") != "outputs" or not isinstance(outputs.get("$get"), dict):
            return None
        call = outputs["$get"]
        if call.get("$call") != "reference":
            return None
        key = self.node(call["args"][0])
        if isinstance(key, dict) and key.get("$call") in ("resourceid", "extensionresourceid")                 and DEPLOYMENTS in key["args"]:
            key = key["args"][key["args"].index(DEPLOYMENTS) + 1]
        item = self.deployment(key) if key is not None else None
        if item is None:
            return None
        child = self.child(item)
        return child.value(child.template["outputs"][self.node(output["key"])]["value"])

    def parameter(self, name):
        if name in self.bindings:
            return self.bindings[name]
        spec = self.template["parameters"][name]
        if self.parent is None:
            # Entry parameters stay symbolic, except this option's own switches: networkMode is the mode under test
            # and createNetwork (foundation.bicep) takes its default, so public must reproduce main with both.
            if name == "networkMode":
                return self.mode
            return self.value(spec["defaultValue"]) if name in PINNED_DEFAULTS else {"$param": name}
        return self.value(spec["defaultValue"]) if "defaultValue" in spec else None

    def variable(self, name):
        variables = self.template.get("variables", {})
        if name in variables:
            return self.value(variables[name])
        return {"$copy": [*self.path, name]}  # a `copy` variable (main.bicep's model loop): compared via its source


def fold(node):
    if not isinstance(node, dict) or not {"$call", "$get"} & set(node):
        return node
    if "$get" in node:
        base, key = node["$get"], node["key"]
        if isinstance(base, dict) and "$call" not in base and "$get" not in base and isinstance(key, str) \
                and not key.startswith("$") and key in base:
            return base[key]
        if isinstance(base, list) and isinstance(key, int):
            return base[key]
        return node
    name, args = node["$call"], node["args"]
    literal = all(not isinstance(arg, dict) or not ({"$call", "$get", "$param", "$copy", "$resource"} & set(arg))
                  for arg in args)
    if name in ("true", "false"):
        return name == "true"
    if name == "null":
        return None
    if name == "createarray":
        return list(args)
    if name == "concat" and args and all(isinstance(arg, list) for arg in args):
        return [item for arg in args for item in arg]
    if name == "createobject" and all(isinstance(key, str) for key in args[::2]):
        return dict(zip(args[::2], args[1::2]))
    if name == "if" and isinstance(args[0], bool):
        return args[1] if args[0] else args[2]
    if name == "equals" and "" in args and any(isinstance(arg, dict) and arg.get("$call") == "format"
                                               and isinstance(arg["args"][0], str)
                                               and re.sub(r"\{\d+\}", "", arg["args"][0]) for arg in args):
        return False  # a format() with literal text is never '' (an existing VNet's subnet ID == '')
    if name == "equals" and literal:
        return args[0] == args[1]
    if name == "not" and isinstance(args[0], bool):
        return not args[0]
    if name in ("or", "and"):
        known = [arg for arg in args if isinstance(arg, bool)]
        if name == "or" and any(known):
            return True
        if name == "and" and known and not all(known):
            return False
        rest = [arg for arg in args if not isinstance(arg, bool)]
        return rest[0] if len(rest) == 1 else (name == "and") if not rest else {"$call": name, "args": rest}
    return node


def entry_value(entry):
    """A module parameter entry, `{"value": X}` or an expression that yields one, as the value X."""
    if isinstance(entry, dict) and set(entry) == {"value"}:
        return entry["value"]
    if isinstance(entry, dict) and entry.get("$call") == "if":
        return {"$call": "if", "args": [entry["args"][0], *(entry_value(arg) for arg in entry["args"][1:])]}
    return entry


def leaves(template, mode="public", bindings=None):
    """Every deployed AVM call and native resource of a compiled entry template, keyed by name, for one networkMode;
    `bindings` gives entry parameters a value instead of their symbol or pinned default."""
    found = {}

    def walk(scope, inherited=True):
        for _, item in scope.resources():
            if item.get("existing"):
                continue
            # An FA module's condition gates every leaf under it, so a module made conditional changes its leaves.
            condition = fold({"$call": "and", "args": [inherited, scope.value(item.get("condition", True))]})
            if condition is False:
                continue
            name = scope.value(item["name"])
            if item["type"] == DEPLOYMENTS and "name" not in item["properties"]["template"].get("metadata", {}):
                walk(scope.child(item), condition)  # an FA module: its leaves are compared, not the module itself
                continue
            if item["type"] == DEPLOYMENTS:
                child = scope.child(item)
                specs = child.template["parameters"]
                # null for a nullable parameter without a default is what leaving it out gives the AVM module.
                passed = {key: value for key, value in child.bindings.items()
                          if not (value is None and specs[key].get("nullable") and "defaultValue" not in specs[key])}
                leaf = {"condition": condition, "rg": child.rg, "parameters": passed,
                        "template": item["properties"]["template"]["metadata"]["_generator"]["templateHash"]}
            else:
                leaf = {key: scope.value(value) for key, value in item.items() if key != "dependsOn"}
                leaf["rg"] = scope.rg
                if condition is not True or "condition" in item:
                    leaf["condition"] = condition
            key = json.dumps([item["type"], name], sort_keys=True)
            assert key not in found, f"two deployed leaves named {key}"
            found[key] = leaf

    root = Scope(template, dict(bindings or {}), None, [], mode=mode)
    walk(root)
    if "copy" in template.get("variables", {}):  # main.bicep's model loop, which variable() leaves symbolic
        found["variables.copy"] = root.value(template["variables"]["copy"])
    found["outputs"] = {name: root.value(spec["value"]) for name, spec in template.get("outputs", {}).items()}
    return json.loads(json.dumps(found, sort_keys=True))


# ---- compiled templates ---------------------------------------------------------------------------------------------

ENTRIES = ("infra/azd/main.bicep", "infra/shared-context/foundation.bicep", "infra/shared-context/foundry.bicep")


@pytest.fixture(scope="module")
def compiled(tmp_path_factory):
    compiler = os.environ.get("BICEP_CLI") or shutil.which("bicep")
    assert compiler, "Set BICEP_CLI to the local Bicep executable; these checks never install or deploy"
    work = tmp_path_factory.mktemp("network-mode")
    for path in (ROOT / "infra").rglob("*.bicep"):
        destination = work / path.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    catalog = Path("src/fa-shared-context-poc/model-catalog.json")
    (work / catalog).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / catalog, work / catalog)
    cache = Path(os.environ.get("BICEP_MODULE_CACHE", str(work / "cache"))).resolve()
    (work / "bicepconfig.json").write_text(json.dumps({"cacheRootDirectory": str(cache)}), encoding="utf-8")
    result = {}
    for entry in ENTRIES:
        output = (work / entry).with_suffix(".json")
        process = subprocess.run([compiler, "build", str(work / entry), "--outfile", str(output)],
                                 capture_output=True, text=True, encoding="utf-8", timeout=600)
        assert process.returncode == 0, f"{entry}:\n{process.stdout}\n{process.stderr}"
        result[entry] = json.loads(output.read_text(encoding="utf-8"))
    return result


@pytest.mark.parametrize("entry", ENTRIES)
def test_public_mode_deploys_every_resource_exactly_as_before(compiled, entry):
    expected = json.loads(BASELINE.read_text(encoding="utf-8"))[entry]
    actual = leaves(compiled[entry])
    assert sorted(actual) == sorted(expected)
    for key in expected:
        assert actual[key] == expected[key], key


def test_the_shared_foundry_account_is_pinned_by_the_baseline():
    # The account every dev and stage project lives in: public network access on, Allow, no injection.
    expected = json.loads(BASELINE.read_text(encoding="utf-8"))
    for entry in ("infra/azd/main.bicep", "infra/shared-context/foundry.bicep"):
        account = expected[entry][json.dumps([DEPLOYMENTS, "shared-context-foundry-account"])]["parameters"]
        assert account["publicNetworkAccess"] == "Enabled"
        assert account["networkAcls"] == {"defaultAction": "Allow", "ipRules": [], "virtualNetworkRules": []}
        assert "networkInjections" not in account and "privateEndpoints" not in account


def leaf(found, name):
    return found[json.dumps([DEPLOYMENTS, name])]["parameters"]


def rid(kind, *names):
    """The canonical resourceId(...) of a resource in the azd environment's resource group."""
    group = {"$call": "if", "args": [{"$call": "empty", "args": [{"$param": "resourceGroupName"}]},
                                     {"$call": "format", "args": ["rg-{0}", ENV]}, {"$param": "resourceGroupName"}]}
    return {"$call": "resourceid", "args": [kind, *names], "rg": group}


ENV = {"$call": "tolower", "args": [{"$param": "environmentName"}]}
VNET = {"$call": "format", "args": ["vnet-{0}", ENV]}
ZONES = ["privatelink.cognitiveservices.azure.com", "privatelink.openai.azure.com", "privatelink.services.ai.azure.com"]


def module(template, suffix):
    """A module of infra/azd/main.bicep by its name suffix, e.g. 'network' for fa-<env>-network."""
    return next(item for item in template["resources"] if item["name"].endswith(f"-{suffix}', variables('env'))]"))


@pytest.fixture(scope="module")
def private(compiled):
    return leaves(compiled["infra/azd/main.bicep"], "private")


def test_private_injects_the_foundry_account_and_closes_its_public_access(private):
    # ADR-0010 section 4 (AVM cognitive-services/account 0.19 single-object networkInjections).
    account = leaf(private, "shared-context-foundry-account")
    assert account["networkInjections"] == {"scenario": "agent", "useMicrosoftManagedNetwork": False,
                                            "subnetResourceId": rid("Microsoft.Network/virtualNetworks/subnets",
                                                                    VNET, "snet-agents")}
    assert account["publicNetworkAccess"] == "Disabled"
    assert account["networkAcls"] == {"defaultAction": "Deny", "bypass": "AzureServices", "ipRules": [],
                                      "virtualNetworkRules": []}
    (endpoint,) = account["privateEndpoints"]
    assert endpoint["subnetResourceId"] == rid("Microsoft.Network/virtualNetworks/subnets", VNET,
                                               "snet-private-endpoints")
    # One DNS zone group config per Foundry zone, from the network module's output.
    configs = endpoint["privateDnsZoneGroup"]["privateDnsZoneGroupConfigs"]
    assert configs["$call"] == "map" and configs["args"][0]["args"][0] == ZONES
    # Everything else on the account is the public baseline (models, local auth off, identity, tags).
    public = json.loads(BASELINE.read_text(encoding="utf-8"))["infra/azd/main.bicep"]
    before = public[json.dumps([DEPLOYMENTS, "shared-context-foundry-account"])]["parameters"]
    changed = {"networkInjections", "publicNetworkAccess", "networkAcls", "privateEndpoints"}
    assert {key: value for key, value in account.items() if key not in changed} == {
        key: value for key, value in before.items() if key not in changed}


def test_private_adds_the_agent_subnet_with_nat_and_its_own_nsg(private):
    vnet = leaf(private, "shared-context-vnet")
    assert vnet["addressPrefixes"] == ["192.168.96.0/24", "192.168.97.0/24"]
    api, endpoints, agents = vnet["subnets"]
    nat = rid("Microsoft.Network/natGateways", {"$call": "format", "args": ["{0}-nat", VNET]})
    assert agents == {"name": "snet-agents", "addressPrefix": "192.168.97.0/24",
                      "delegation": "Microsoft.App/environments", "natGatewayResourceId": nat,
                      "networkSecurityGroupResourceId": rid("Microsoft.Network/networkSecurityGroups", {
                          "$call": "format", "args": ["{0}-agents-nsg", VNET]}),
                      "defaultOutboundAccess": False}
    # The same NAT serves snet-api (it is the apiSubnetNatEgress layout), whatever FA_API_SUBNET_NAT_EGRESS says.
    assert api["name"] == "snet-api" and api["natGatewayResourceId"] == nat
    assert endpoints["name"] == "snet-private-endpoints" and endpoints["defaultOutboundAccess"] is False
    for name in ("shared-context-outbound-public-ip", "shared-context-outbound-nat"):
        assert leaf(private, name)  # NAT is created when apiSubnetNatEgress or private
    nsg = leaf(private, "shared-context-agents-nsg")
    assert "securityRules" not in nsg  # default rules only; the user adds outbound rules


def test_private_links_the_three_foundry_dns_zones_to_the_vnet(compiled, private):
    network = module(compiled["infra/azd/main.bicep"], "network")["properties"]["template"]
    assert network["variables"]["foundryDnsZones"] == ZONES
    (key,) = [key for key in private if "shared-context-foundry-dns-" in key]
    zone = private[key]
    assert zone["condition"] is True
    assert zone["parameters"]["name"] == {"$get": ZONES, "key": {"$call": "copyindex", "args": []}}
    loop = next(item for item in network["resources"] if item["name"].startswith("[format('shared-context-foundry-dns"))
    assert loop["copy"]["count"] == "[length(variables('foundryDnsZones'))]"
    (link,) = zone["parameters"]["virtualNetworkLinks"]
    assert link["virtualNetworkResourceId"] == rid("Microsoft.Network/virtualNetworks", VNET)
    assert link["registrationEnabled"] is False


def test_foundation_run_alone_with_private_keeps_the_azd_private_vnet(compiled, private):
    # A manual foundation.bicep redeploy onto an azd private environment PUTs the same VNet, snet-agents included.
    alone = leaf(leaves(compiled["infra/shared-context/foundation.bicep"], "private"), "shared-context-vnet")
    azd = leaf(private, "shared-context-vnet")
    assert alone["addressPrefixes"] == azd["addressPrefixes"] == ["192.168.96.0/24", "192.168.97.0/24"]
    assert [item["name"] for item in alone["subnets"]] == [item["name"] for item in azd["subnets"]] == [
        "snet-api", "snet-private-endpoints", "snet-agents"]


def test_private_creates_the_network_before_the_account(compiled):
    template = compiled["infra/azd/main.bicep"]
    foundry = module(template, "foundry")
    # From network.outputs: the subnets and DNS zones exist before the account is created.
    assert any("-network'" in item for item in foundry["dependsOn"])
    assert foundry["properties"]["parameters"]["networkMode"] == {"value": "[parameters('networkMode')]"}
    assert module(template, "network")["properties"]["parameters"]["networkMode"] == {
        "value": "[parameters('networkMode')]"}


CAPABILITY_HOSTS = "Microsoft.CognitiveServices/accounts/projects/capabilityHosts"


def capability_hosts(found):
    return {key: value for key, value in found.items() if CAPABILITY_HOSTS in key}


def test_private_alone_gets_a_basic_project_capability_host(compiled, private):
    # ADR-0010 2026-10-28: tools outside Foundry (WebIQ MCP) need it through the VNet. The account one is the
    # platform's (phase 0 Q3), so FA declares no account capability host in either mode.
    for entry in ("infra/azd/main.bicep", "infra/shared-context/foundry.bicep"):
        assert capability_hosts(leaves(compiled[entry])) == {}, entry
        assert "Microsoft.CognitiveServices/accounts/capabilityHosts" not in json.dumps(compiled[entry]), entry
    (host,) = capability_hosts(private).values()
    assert host["apiVersion"] == "2025-06-01" and host["properties"] == {"capabilityHostKind": "Agents"}
    assert host["condition"] is True  # private, folded
    assert host["name"]["args"][-1] == "caphostproj"
    template = compiled["infra/shared-context/foundry.bicep"]
    assert template["resources"]["projectCapabilityHost"]["dependsOn"] == ["project"]  # the project, after the account


MEMORY_ACCOUNT = "fa-memory-cosmos-account"
MEMORY_DNS = "fa-memory-cosmos-dns"
COSMOS = {"$call": "format", "args": ["cosmos-{0}", {"$call": "tolower", "args": [{"$call": "uniquestring", "args": [
    {"$get": {"$call": "subscription", "args": []}, "key": "id"}, ENV, {"$param": "location"}]}]}]}
DATA = "Microsoft.DocumentDB/databaseAccounts"
ITEMS = f"{DATA}/sqlDatabases/containers"


def test_public_has_no_cosmos_memory_whatever_the_backend_says(compiled):
    # The module's condition names networkMode, so even FA_MEMORY_BACKEND=cosmos on public deploys nothing.
    template = compiled["infra/azd/main.bicep"]
    assert template["variables"]["cosmosMemory"] == (
        "[and(equals(parameters('networkMode'), 'private'), equals(parameters('memoryBackend'), 'cosmos'))]")
    assert module(template, "memory-cosmos")["condition"] == "[variables('cosmosMemory')]"
    public = leaves(template)
    assert not [key for key in public if "cosmos" in key or "documents.azure.com" in key]
    # Its output exists in both modes (an azd output cannot be conditional) and is empty on public.
    assert public["outputs"]["FA_COSMOS_ENDPOINT"] == ""


def test_private_cosmos_memory_is_serverless_keyless_and_closed_to_the_internet(private):
    # ADR-0011 section 5, the settings the phase 0c probe deployed with AVM 0.21.1.
    condition = {"$call": "equals", "args": [{"$param": "memoryBackend"}, "cosmos"]}
    assert private[json.dumps([DEPLOYMENTS, MEMORY_ACCOUNT])]["condition"] == condition
    account = leaf(private, MEMORY_ACCOUNT)
    assert account["name"] == COSMOS
    assert account["capacityMode"] == "Serverless" and account["capabilitiesToAdd"] == ["EnableNoSQLVectorSearch"]
    assert account["disableLocalAuthentication"] is True and account["disableKeyBasedMetadataWriteAccess"] is True
    assert account["networkRestrictions"] == {"publicNetworkAccess": "Disabled", "ipRules": [],
                                              "virtualNetworkRules": []}
    assert account["zoneRedundant"] is False and account["backupPolicyContinuousTier"] == "Continuous7Days"
    assert account["enableTelemetry"] is False
    (endpoint,) = account["privateEndpoints"]
    assert endpoint["service"] == "Sql"
    assert endpoint["subnetResourceId"] == rid("Microsoft.Network/virtualNetworks/subnets", VNET,
                                               "snet-private-endpoints")
    (config,) = endpoint["privateDnsZoneGroup"]["privateDnsZoneGroupConfigs"]
    assert config["privateDnsZoneResourceId"] == rid("Microsoft.Network/privateDnsZones",
                                                     "privatelink.documents.azure.com")
    zone = private[json.dumps([DEPLOYMENTS, MEMORY_DNS])]
    assert zone["condition"] == condition and zone["parameters"]["name"] == "privatelink.documents.azure.com"
    (link,) = zone["parameters"]["virtualNetworkLinks"]
    assert link["virtualNetworkResourceId"] == rid("Microsoft.Network/virtualNetworks", VNET)
    assert link["registrationEnabled"] is False


def test_private_cosmos_memory_has_one_vector_container_partitioned_by_scope(private):
    (database,) = leaf(private, MEMORY_ACCOUNT)["sqlDatabases"]
    assert database["name"] == "fa-memory" and "throughput" not in database  # serverless
    (container,) = database["containers"]
    assert container["name"] == "memories" and container["paths"] == ["/scope"]
    assert "defaultTtl" not in container and "fullTextPolicy" not in container  # no expiry, no full text (no zh)
    assert container["vectorEmbeddingPolicy"] == {"vectorEmbeddings": [
        {"path": "/embedding", "dataType": "float32", "distanceFunction": "cosine", "dimensions": 1536}]}
    assert container["indexingPolicy"] == {
        "indexingMode": "consistent", "automatic": True, "includedPaths": [{"path": "/*"}],
        "excludedPaths": [{"path": "/embedding/*"}, {"path": '/"_etag"/?'}],
        "vectorIndexes": [{"path": "/embedding", "type": "quantizedFlat"}]}
    # The names the API's memory relay has fixed (shared_context/memory_relay.py DATABASE, CONTAINER).
    cosmos = {"$call": "equals", "args": [{"$param": "memoryBackend"}, "cosmos"]}
    endpoint = private["outputs"]["FA_COSMOS_ENDPOINT"]
    assert endpoint["$call"] == "if" and endpoint["args"][0] == cosmos and endpoint["args"][2] == ""


def test_only_the_api_identity_gets_the_five_action_memory_role_on_the_container(compiled, private):
    account = leaf(private, MEMORY_ACCOUNT)
    (role,) = account["sqlRoleDefinitions"]
    assert role["dataActions"] == [f"{DATA}/readMetadata", f"{ITEMS}/items/create", f"{ITEMS}/items/read",
                                   f"{ITEMS}/items/replace", f"{ITEMS}/executeQuery"]
    assert not [action for action in role["dataActions"]
                if action.endswith(("/delete", "/upsert", "/readChangeFeed")) or "*" in action]
    assert "assignableScopes" not in role  # the module's default: this account
    # AVM 0.21.1 passes no scope to a definition's nested assignments (they land on the whole account), so the one
    # assignment is in the account-level list, which does pass it.
    assert "assignments" not in role and "roleAssignments" not in account  # no other principal
    (assignment,) = account["sqlRoleAssignments"]
    assert assignment["roleDefinitionId"] == role["name"]
    identity = {"$resource": "Microsoft.ManagedIdentity/userAssignedIdentities",
                "name": {"$call": "format", "args": ["id-fa-api-{0}", ENV]}, "rg": rid("x")["rg"]}
    assert assignment["principalId"]["key"] == "principalId"
    assert assignment["principalId"]["$get"]["args"][0] == identity
    assert assignment["scope"] == rid(ITEMS, COSMOS, "fa-memory", "memories")
    # Inside the compiled AVM template, that entry's scope is what the assignment deployment gets, and the resource
    # turns the ARM ID into the data-plane form (sqlDatabases -> dbs, containers -> colls).
    memory = module(compiled["infra/azd/main.bicep"], "memory-cosmos")["properties"]["template"]
    avm = memory["resources"]["account"]["properties"]["template"]
    inner = avm["resources"]["databaseAccount_sqlRoleAssignments"]
    assert inner["properties"]["parameters"]["scope"] == {
        "value": "[tryGet(coalesce(parameters('sqlRoleAssignments'), createArray())[copyIndex()], 'scope')]"}
    resource = inner["properties"]["template"]["resources"]["sqlRoleAssignment"]["properties"]
    assert resource["scope"] == "[variables('formattedScope')]"
    assert inner["properties"]["template"]["variables"]["formattedScope"] == (
        "[replace(replace(coalesce(parameters('scope'), resourceId('Microsoft.DocumentDB/databaseAccounts', "
        "parameters('databaseAccountName'))), '/sqlDatabases/', '/dbs/'), '/containers/', '/colls/')]")


# ---- ADR-0010 phase 2b: an existing VNet ------------------------------------------------------------------------------

# Private without FA_VNET_RESOURCE_ID deploys identical resources (a normalized leaf comparison, not byte-identical
# JSON): sha256 of the canonical private leaves of infra/azd/main.bicep on main at e9692be1 (without phase 2b), as
# computed below, less the project capability host added on 2026-10-28; a changed value means it deploys something
# different. Diff with
# `python tests/infra/test_network_mode.py <compiled.json> private` against that commit's output.
PRIVATE_DEFAULT_SHA256 = "adaf9eb46f887c8a0e27774fdd6a1a21c441fa02527667bcccc8f501923f2b80"
USER_VNET = "/subscriptions/s/resourceGroups/rg-net/providers/Microsoft.Network/virtualNetworks/vnet-hub"
BYO = {"vnetResourceId": USER_VNET, "apiSubnetName": "app", "agentSubnetName": "agents",
       "privateEndpointSubnetName": "endpoints"}
FA_NETWORK = {"shared-context-vnet", "shared-context-agents-nsg", "shared-context-outbound-nat",
              "shared-context-outbound-public-ip"}
FA_ZONES = {"shared-context-table-dns", "m1-backup-blob-dns", "m1-backup-blob-dns-link", MEMORY_DNS}


def byo_subnet(name):
    return {"$call": "format", "args": ["{0}/subnets/{1}", USER_VNET, name]}


def deployed(found):
    """Names of the deployed AVM calls (as JSON when computed), and the types of the native resources."""
    keys = [json.loads(key) for key in found if key not in ("outputs", "variables.copy")]
    return {(kind if kind != DEPLOYMENTS else name if isinstance(name, str) else json.dumps(name))
            for kind, name in keys}


def endpoint_subnets(found):
    """Every private endpoint's subnet: the AVM private-endpoint calls and the accounts' own privateEndpoints."""
    subnets = {name: leaf(found, name)["subnetResourceId"] for name in (
        "shared-context-table-private-endpoint", "m1-backup-blob-private-endpoint",
        "fa-upgrade-snapshot-blob-private-endpoint")}
    for name in ("shared-context-foundry-account", MEMORY_ACCOUNT):
        (endpoint,) = leaf(found, name)["privateEndpoints"]
        subnets[name] = endpoint["subnetResourceId"]
    return subnets


@pytest.fixture(scope="module")
def byo(compiled):
    return leaves(compiled["infra/azd/main.bicep"], "private", BYO)


def test_private_without_an_existing_vnet_deploys_exactly_what_it_did_before(private):
    # The project capability host (2026-10-28) is the only addition since; everything else hashes as before.
    before = {key: value for key, value in private.items() if key not in capability_hosts(private)}
    assert len(before) == len(private) - 1
    assert hashlib.sha256(json.dumps(before, sort_keys=True).encode("utf-8")).hexdigest() == PRIVATE_DEFAULT_SHA256


def test_public_ignores_an_existing_vnet(compiled):
    # FA_VNET_RESOURCE_ID is private-only in Bicep too (the hook also refuses it): public stays the baseline.
    expected = json.loads(BASELINE.read_text(encoding="utf-8"))["infra/azd/main.bicep"]
    assert leaves(compiled["infra/azd/main.bicep"], "public", {**BYO, "dnsMode": "existing"}) == expected


def test_an_existing_vnet_gets_no_fa_vnet_subnet_nsg_or_nat(byo, private):
    assert FA_NETWORK <= deployed(private)  # the same names exist without it
    assert not FA_NETWORK & deployed(byo)
    assert not [kind for kind in deployed(byo) if kind.startswith("Microsoft.Network/")]  # nor a native one
    assert byo["outputs"]["FA_VNET"] == {"$call": "last", "args": [{"$call": "split", "args": [USER_VNET, "/"]}]}


def test_injection_container_apps_and_every_private_endpoint_use_the_users_subnets(byo):
    account = leaf(byo, "shared-context-foundry-account")
    assert account["networkInjections"] == {"scenario": "agent", "useMicrosoftManagedNetwork": False,
                                            "subnetResourceId": byo_subnet("agents")}
    assert leaf(byo, "shared-context-api-environment")["infrastructureSubnetResourceId"] == byo_subnet("app")
    assert list(endpoint_subnets(byo).values()) == [byo_subnet("endpoints")] * 5
    # The rest of the account is the private one (public access off, Deny).
    assert account["publicNetworkAccess"] == "Disabled" and account["networkAcls"]["defaultAction"] == "Deny"


def test_azd_dns_links_every_fa_zone_to_the_users_vnet(byo):
    zones = [zone for key, zone in byo.items() if "shared-context-foundry-dns-" in key
             or key in (json.dumps([DEPLOYMENTS, "shared-context-table-dns"]), json.dumps([DEPLOYMENTS, MEMORY_DNS]))]
    assert len(zones) == 3  # the Foundry zone loop, Table, Cosmos DB (the backup zone links through its own child)
    assert {zone["parameters"]["virtualNetworkLinks"][0]["virtualNetworkResourceId"] for zone in zones} == {USER_VNET}
    assert leaf(byo, "m1-backup-blob-dns-link")["virtualNetworkResourceId"] == USER_VNET
    (endpoint,) = leaf(byo, "shared-context-foundry-account")["privateEndpoints"]
    assert endpoint["privateDnsZoneGroup"]["privateDnsZoneGroupConfigs"]["$call"] == "map"


def test_existing_dns_creates_no_zone_link_or_zone_group(compiled):
    found = leaves(compiled["infra/azd/main.bicep"], "private", {**BYO, "dnsMode": "existing"})
    assert not FA_ZONES & deployed(found)
    assert not [key for key in found if "dns" in key.lower()]
    for name in ("shared-context-table-private-endpoint", "m1-backup-blob-private-endpoint",
                 "fa-upgrade-snapshot-blob-private-endpoint"):
        assert "privateDnsZoneGroup" not in leaf(found, name), name
    for name in ("shared-context-foundry-account", MEMORY_ACCOUNT):
        (endpoint,) = leaf(found, name)["privateEndpoints"]
        assert endpoint.get("privateDnsZoneGroup") is None, name
    assert list(endpoint_subnets(found).values()) == [byo_subnet("endpoints")] * 5


def test_only_private_cosmos_onboarding_gets_the_account_scope_delegation(compiled, private):
    # ADR-0006 (Neal 2026-10-24): the account-scope RBAC Administrator exists only with Cosmos memory; public leaves
    # (the baseline) have none on the account itself.
    def on_account(found):
        return [value for key, value in found.items() if key.startswith('["Microsoft.Authorization/roleAssignments"')
                and (value.get("scope") or {}).get("args", [None])[0] == "Microsoft.CognitiveServices/accounts"
                and "f58310d9-a9f6-439a-9e8d-f62e7b41a168" in json.dumps(value["properties"]["roleDefinitionId"])]
    assert on_account(leaves(compiled["infra/azd/main.bicep"])) == []
    (delegation,) = on_account(private)
    assert delegation["condition"] == {"$call": "and", "args": [
        {"$param": "enableAgentOnboarding"}, {"$call": "equals", "args": [{"$param": "memoryBackend"}, "cosmos"]}]}
    # Its output (profile.py turns the account PUT on from it) is the same condition, and false on public.
    assert private["outputs"]["FA_AGENT_ONBOARDING_ACCOUNT_ROLE"] == delegation["condition"]
    assert leaves(compiled["infra/azd/main.bicep"])["outputs"]["FA_AGENT_ONBOARDING_ACCOUNT_ROLE"] is False


def test_canonical_form_inlines_outputs_and_folds_the_private_branch():
    child = {"parameters": {"name": {"type": "string"}}, "outputs": {"id": {
        "type": "string", "value": "[resourceId('Microsoft.Network/virtualNetworks', parameters('name'))]"}}}
    template = {
        "parameters": {"networkMode": {"type": "string", "defaultValue": "public"}, "vnet": {"type": "string"}},
        "variables": {"isPrivate": "[equals(parameters('networkMode'), 'private')]"},
        "resources": {
            "net": {"type": DEPLOYMENTS, "name": "net", "properties": {
                "parameters": {"name": {"value": "[parameters('vnet')]"}}, "template": child}},
            "zone": {"type": "Microsoft.Network/privateDnsZones", "name": "z", "condition": "[variables('isPrivate')]"},
            "link": {"type": "Microsoft.Network/thing", "name": "l", "properties": {
                "a": "[reference('net').outputs.id.value]",
                "b": "[if(variables('isPrivate'), 'Disabled', 'Enabled')]",
                "c": "[if(or(parameters('nat'), variables('isPrivate')), createObject('x', 1), null())]"}}}}
    template["parameters"]["nat"] = {"type": "bool"}
    found = leaves(template)
    assert list(found) == ["[\"Microsoft.Network/thing\", \"l\"]", "outputs"]
    properties = found["[\"Microsoft.Network/thing\", \"l\"]"]["properties"]
    assert properties["a"] == {"$call": "resourceid", "args": ["Microsoft.Network/virtualNetworks", {"$param": "vnet"}],
                               "rg": None}
    assert properties["b"] == "Enabled"
    assert properties["c"] == {"$call": "if", "args": [{"$param": "nat"}, {"x": 1}, None]}


if __name__ == "__main__":
    # python tests/infra/test_network_mode.py <compiled.json> [private]: one entry of public_baseline.json (or the
    # private leaves PRIVATE_DEFAULT_SHA256 hashes), a leaf per line.
    found = leaves(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")), *sys.argv[2:3])
    print("{\n" + ",\n".join(f" {json.dumps(key)}: {json.dumps(value, sort_keys=True, ensure_ascii=False)}"
                             for key, value in sorted(found.items())) + "\n}")
