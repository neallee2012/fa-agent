"""azd entry (azure.yaml, infra/azd) compiled and checked locally; no Azure login, what-if or deployment."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import importlib.util
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
AZD = ROOT / "infra" / "azd"
CATALOG = ROOT / "src" / "fa-shared-context-poc" / "model-catalog.json"
HOOKS = ROOT / "deploy" / "azd" / "fa_azd.py"
PROFILE = ROOT / "deploy" / "azd" / "profile.py"
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    """Every infra/*.bicep (and the catalog main.bicep loads) copied beside a bicepconfig that names the module cache."""
    compiler = os.environ.get("BICEP_CLI") or shutil.which("bicep")
    assert compiler, "Set BICEP_CLI to the local Bicep executable; these checks never install or deploy"
    work = tmp_path_factory.mktemp("azd")
    for path in (ROOT / "infra").rglob("*.bicep"):
        destination = work / path.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    catalog = work / CATALOG.relative_to(ROOT)
    catalog.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CATALOG, catalog)
    cache = Path(os.environ.get("BICEP_MODULE_CACHE", str(work / "cache"))).resolve()
    (work / "bicepconfig.json").write_text(json.dumps({"cacheRootDirectory": str(cache)}), encoding="utf-8")
    return compiler, work


def build(workspace, template):
    compiler, work = workspace
    output = (work / template).with_suffix(".json")
    process = subprocess.run([compiler, "build", str(work / template), "--outfile", str(output)],
                             capture_output=True, text=True, encoding="utf-8", timeout=600)
    assert process.returncode == 0, f"{process.stdout}\n{process.stderr}"
    return json.loads(output.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def compiled(workspace):
    return build(workspace, "infra/azd/main.bicep")


# Templates azd deploys on their own (deploy/azd/fa_azd.py console_app) or kept beside them; main.bicep never builds them.
@pytest.mark.parametrize("template", ["infra/web-console/web-console.bicep", "infra/feed-mcp/feed-mcp.bicep"])
def test_standalone_templates_build(workspace, template):
    assert build(workspace, template)["resources"]


def deployments(template):
    return {resource["name"]: resource for resource in template["resources"].values()} \
        if isinstance(template["resources"], dict) else {item["name"]: item for item in template["resources"]}


def test_subscription_scope_and_one_resource_group(compiled):
    assert compiled["$schema"].endswith("subscriptionDeploymentTemplate.json#")
    text = json.dumps(compiled)
    assert text.count('"type": "Microsoft.Resources/resourceGroups"') == 1


def test_model_deployments_come_from_the_one_catalog(compiled):
    # loadJsonContent is inlined as a generated variable that `catalog` points at.
    assert compiled["variables"]["catalog"] == "[variables('$fxv#0')]"
    assert compiled["variables"]["$fxv#0"] == json.loads(CATALOG.read_text(encoding="utf-8"))


def test_azd_offers_only_hosted_agent_regions_and_deploys_the_chosen_models(compiled):
    location = compiled["parameters"]["location"]
    assert len(location["allowedValues"]) == 31 and {"westus3", "eastus2", "swedencentral"} <= set(
        location["allowedValues"])
    assert location["metadata"]["azd"] == {"type": "location",
                                           "usageName": ["OpenAI.GlobalStandard.text-embedding-3-small,10"]}
    parameters = json.loads((AZD / "main.parameters.json").read_text(encoding="utf-8"))["parameters"]
    assert parameters["location"]["value"] == "${AZURE_LOCATION}"  # azd's picker fills it before any hook runs
    assert parameters["modelDeploymentNames"]["value"] == "${FA_MODEL_DEPLOYMENTS}"
    assert compiled["variables"]["chosenCatalog"] == (
        "[filter(variables('catalog'), lambda('item', contains(variables('chosen'), lambdaVariables('item').deployment)))]")
    loop = next(item for item in compiled["variables"]["copy"] if item["name"] == "modelDeployments")
    assert loop["count"] == "[length(variables('chosenCatalog'))]"


def test_modules_keep_the_standard_flow_order(compiled):
    resources = deployments(compiled)
    by_suffix = {}
    for name, resource in resources.items():
        match = re.search(r"-(network|foundry|monitoring|foundation|realtime|backup-role|backup-foundation|"
                          r"m3-artifacts|onboarding-delegation)'", name)
        if match:
            by_suffix[match.group(1)] = json.dumps(resource)
    assert set(by_suffix) == {"network", "foundry", "monitoring", "foundation", "realtime", "backup-role",
                              "backup-foundation", "m3-artifacts", "onboarding-delegation"}
    # ADR-0010 section 2: the VNet exists before the Foundry account (injection needs the subnet at creation), so
    # azd deploys it on its own and the foundation does not deploy it a second time.
    foundry, foundation = (json.loads(by_suffix[key]) for key in ("foundry", "foundation"))
    assert any("-network'" in item for item in foundry["dependsOn"])
    assert any("-network'" in item for item in foundation["dependsOn"])
    assert foundation["properties"]["parameters"]["createNetwork"] == {"value": False}
    nested = foundation["properties"]["template"]["resources"]["privateStateNetwork"]
    assert nested["condition"] == "[parameters('createNetwork')]"
    # The foundation reads the monitoring workspace from its output (ARM orders and verifies it).
    assert "-monitoring', variables('env'))), '2025-04-01').outputs.workspaceResourceId" in by_suffix["foundation"]
    assert "-foundry'" in by_suffix["monitoring"]
    assert "-backup-role'" in by_suffix["backup-foundation"] and "-foundation'" in by_suffix["backup-foundation"]
    assert "fa-m1-backup-data-read-write-v1" in by_suffix["m3-artifacts"]
    assert by_suffix["onboarding-delegation"].count("enableAgentOnboarding") >= 1


def test_plus_onboarding_delegation_is_on_by_default_with_an_opt_out(compiled):
    # Neal 2026-10-15: a new azd environment creates the #468 delegation; `azd env set FA_ENABLE_AGENT_ONBOARDING
    # false` skips the module, and the output tells deploy whether to give the API the FA_ONBOARDING_* settings.
    parameters = json.loads((AZD / "main.parameters.json").read_text(encoding="utf-8"))["parameters"]
    assert parameters["enableAgentOnboarding"]["value"] == "${FA_ENABLE_AGENT_ONBOARDING=true}"
    assert compiled["parameters"]["enableAgentOnboarding"]["defaultValue"] is True
    module = next(resource for name, resource in deployments(compiled).items() if "onboarding-delegation" in name)
    assert module["condition"] == "[parameters('enableAgentOnboarding')]"
    # The constrained RBAC Administrator: only Foundry User / OpenAI User, only service principals, no deletes.
    text = json.dumps(module)
    for role in ("f58310d9-a9f6-439a-9e8d-f62e7b41a168", "53ca6127-db72-4b80-b1b0-d745d6d5456d",
                 "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd", "ServicePrincipal", "roleAssignments/delete"):
        assert role in text, role
    # The module default keeps stage's deployed description; azd names its own environment instead of "stage".
    stage_text = "FA #468: stage onboarding may only add Foundry User / OpenAI User for service principals; no deletes"
    assert module["properties"]["template"]["parameters"]["assignmentDescription"]["defaultValue"] == stage_text
    passed = module["properties"]["parameters"]["assignmentDescription"]["value"]
    assert "variables('env')" in passed and "stage" not in passed
    assert compiled["outputs"]["FA_AGENT_ONBOARDING"]["value"] == "[parameters('enableAgentOnboarding')]"


def rendered(template):
    """The ABAC condition text ARM builds from a compiled delegation module's format('...', variables(...))."""
    variables = template["variables"]
    match = re.fullmatch(r"\[format\('(.*)', (.*)\)\]", variables["condition"])
    names = re.findall(r"variables\('(\w+)'\)", match.group(2))
    return re.sub(r"\{\{|\}\}|\{(\d+)\}", lambda found: variables[names[int(found.group(1))]] if found.group(1)
                  else found.group(0)[0], match.group(1).replace("''", "'"))


def test_cosmos_onboarding_may_add_only_openai_user_on_the_account(compiled):
    # ADR-0006 (Neal 2026-10-24): Cosmos memory embeds at the account root, so "+" also needs the account-scope role.
    resources = deployments(compiled)
    (name,) = [name for name in resources if "onboarding-account-role" in name]
    module = resources[name]
    assert module["condition"] == "[and(parameters('enableAgentOnboarding'), variables('cosmosMemory'))]"
    # deploy/azd/profile.py turns the account PUT on from this output, not from FA_MEMORY_BACKEND.
    assert compiled["outputs"]["FA_AGENT_ONBOARDING_ACCOUNT_ROLE"] == {"type": "bool", "value": module["condition"]}
    inner = module["properties"]["template"]
    (assignment,) = [item for item in deployments(inner).values() if not item.get("existing")]
    assert assignment["type"] == "Microsoft.Authorization/roleAssignments"
    assert assignment["scope"] == "[resourceId('Microsoft.CognitiveServices/accounts', parameters('accountName'))]"
    properties = assignment["properties"]
    assert inner["variables"]["rbacAdministrator"] == "f58310d9-a9f6-439a-9e8d-f62e7b41a168"
    assert properties["roleDefinitionId"] == (
        "[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', variables('rbacAdministrator'))]")
    assert properties["principalType"] == "ServicePrincipal" and properties["conditionVersion"] == "2.0"
    # The project delegation's exact condition with its write set cut to OpenAI User; deletes stay refused.
    project = next(resource for name, resource in resources.items() if "onboarding-delegation" in name)
    expected = rendered(project["properties"]["template"]).replace("53ca6127-db72-4b80-b1b0-d745d6d5456d, ", "")
    assert rendered(inner) == expected
    assert "GuidEquals {5e0bd9bd-7b93-4f28-af87-19fc36ad61bd} AND" in expected and "53ca6127" not in expected
    # Literally: the write clause names only OpenAI User; the delete clause only the all-zero GUID.
    assert re.findall(r"GuidEquals \{([^}]*)\}", rendered(inner)) == [
        "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd", "00000000-0000-0000-0000-000000000000"]


def test_upgrade_snapshot_account_is_opt_in_and_off_by_default(compiled):
    # Neal 2026-10-17: a dedicated account (never the backup one); the default adds no deployment or resource.
    parameters = json.loads((AZD / "main.parameters.json").read_text(encoding="utf-8"))["parameters"]
    assert parameters["deployUpgradeSnapshots"]["value"] == "${FA_DEPLOY_UPGRADE_SNAPSHOTS=false}"
    assert compiled["parameters"]["deployUpgradeSnapshots"]["defaultValue"] is False
    (name,) = [name for name in deployments(compiled) if "upgrade-snapshots" in name]
    module = deployments(compiled)[name]
    assert module["condition"] == "[parameters('deployUpgradeSnapshots')]"
    passed = module["properties"]["parameters"]
    assert passed["deployUpgradeSnapshots"]["value"] == "[parameters('deployUpgradeSnapshots')]"
    assert passed["storageName"]["value"] == "[variables('names').snapshotStorage]"
    assert compiled["variables"]["names"]["snapshotStorage"] != compiled["variables"]["names"]["backupStorage"]
    assert "-backup-foundation'" in json.dumps(module["dependsOn"])
    # The flag gates only this module; every other deployment is unchanged.
    gated = [key for key, resource in deployments(compiled).items()
             if "deployUpgradeSnapshots" in str(resource.get("condition", ""))]
    assert gated == [name]
    assert compiled["outputs"]["FA_UPGRADE_SNAPSHOT_CONTAINER_URL"]["value"].startswith(
        "[if(parameters('deployUpgradeSnapshots'), ")


def test_no_identifier_or_other_environment_value_in_the_azd_entry():
    stage = json.loads((ROOT / "deploy" / "stage" / "stage.json").read_text(encoding="utf-8"))
    names = {value for key, value in stage.items() if isinstance(value, str) and key not in {"environment"}}
    for path in (AZD / "main.bicep", AZD / "main.parameters.json", ROOT / "azure.yaml",
                 ROOT / "deploy" / "azd" / "profile.defaults.json"):
        text = path.read_text(encoding="utf-8")
        assert not UUID.search(text), path
        assert not [name for name in names if name in text], path


def test_parameters_file_maps_every_required_parameter_to_azd_values(compiled):
    parameters = json.loads((AZD / "main.parameters.json").read_text(encoding="utf-8"))["parameters"]
    required = {name for name, spec in compiled["parameters"].items() if "defaultValue" not in spec}
    assert required <= set(parameters)
    assert set(parameters) <= set(compiled["parameters"])
    for name, item in parameters.items():
        assert re.fullmatch(r"\$\{(AZURE|FA)_[A-Z_]+(=[A-Za-z][A-Za-z-]*)?\}", item["value"]), name


def test_network_mode_is_an_azd_input_that_defaults_to_public(compiled):
    # ADR-0010 section 1: one option, two values; an unset FA_NETWORK_MODE keeps today's public deployment.
    parameters = json.loads((AZD / "main.parameters.json").read_text(encoding="utf-8"))["parameters"]
    assert parameters["networkMode"]["value"] == "${FA_NETWORK_MODE=public}"
    spec = compiled["parameters"]["networkMode"]
    assert spec["defaultValue"] == "public" and spec["allowedValues"] == ["public", "private"]
    assert "FA_NETWORK_MODE" in HOOKS.read_text(encoding="utf-8").split("USER_INPUTS = ", 1)[1].split(")", 1)[0]


def test_private_regions_are_main_bicep_regions_without_ukwest(compiled):
    # ADR-0010 section 1: ukwest is offered by main.bicep but absent from the Agent Service region table.
    spec = importlib.util.spec_from_file_location("fa_azd_regions", HOOKS)
    module = importlib.util.module_from_spec(spec)
    sys.modules["fa_azd_regions"] = module
    spec.loader.exec_module(module)
    assert module.PRIVATE_REGIONS == set(compiled["parameters"]["location"]["allowedValues"]) - {"ukwest"}


def test_every_value_the_hooks_read_is_an_output_or_written_by_a_hook(compiled):
    sources = HOOKS.read_text(encoding="utf-8") + PROFILE.read_text(encoding="utf-8")
    read = set(re.findall(r'need\("([A-Z0-9_]+)"\)', sources)) | set(re.findall(r'need\(\'([A-Z0-9_]+)\'\)', sources))
    read |= set(re.findall(r'_get\(values, "([A-Z0-9_]+)"\)', sources))
    # Writes only: ctx.set(...) and the (key, value) pairs entra_apps() sets; never ctx.get(key, default).
    written = set(re.findall(r'ctx\.set\("([A-Z0-9_]+)"', sources))
    written |= set(re.findall(r'\("(FA_[A-Z_]+)", (?:keys\[|api\[|console\[|f"https)', sources))
    built_in = {key for key in read if key.startswith("AZURE_")}
    agent = {key for key in read if key.startswith("FA_AGENT_")}
    outputs = set(compiled["outputs"])
    missing = sorted(read - built_in - agent - written - outputs)
    assert missing == [], f"read by the hooks but neither a Bicep output nor written by a hook: {missing}"
    assert "AZURE_RESOURCE_GROUP" in outputs


def test_azure_yaml_runs_only_the_hook_phases_with_both_shells():
    config = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
    assert re.fullmatch(r"[a-z0-9-]{2,}", config["name"])
    assert "services" not in config  # FA's apps/Agents deploy through the shared scripts, not azd service hosts
    assert config["infra"] == {"provider": "bicep", "path": "infra/azd", "module": "main"}
    assert (ROOT / config["infra"]["path"] / "main.bicep").is_file()
    assert "extensions" not in config["requiredVersions"]
    assert config["requiredVersions"]["azd"] == ">=1.32.0"  # env reload after hooks was verified on 1.32 only
    spec = importlib.util.spec_from_file_location("fa_azd_yaml", HOOKS)  # standard library only
    module = importlib.util.module_from_spec(spec)
    sys.modules["fa_azd_yaml"] = module
    spec.loader.exec_module(module)
    phases = set(module.PHASES)
    expected = {"preprovision": "preprovision", "postprovision": "postprovision", "postdeploy": "deploy",
                "predown": "predown", "postdown": "postdown"}
    assert set(config["hooks"]) == set(expected)
    for hook, phase in expected.items():
        assert phase in phases
        for platform, shell, python in (("windows", "pwsh", "python"), ("posix", "sh", "python3")):
            entry = config["hooks"][hook][platform]
            assert entry["shell"] == shell and entry["interactive"] is True and entry["continueOnError"] is False
            assert entry["run"] == f"{python} deploy/azd/fa_azd.py {phase}"
            assert "secrets" not in entry
