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
def compiled(tmp_path_factory):
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
    output = work / "main.json"
    process = subprocess.run([compiler, "build", str(work / "infra" / "azd" / "main.bicep"), "--outfile", str(output)],
                             capture_output=True, text=True, encoding="utf-8", timeout=600)
    assert process.returncode == 0, f"{process.stdout}\n{process.stderr}"
    return json.loads(output.read_text(encoding="utf-8"))


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
        match = re.search(r"-(foundry|monitoring|foundation|realtime|backup-role|backup-foundation|m3-artifacts|"
                          r"onboarding-delegation)'", name)
        if match:
            by_suffix[match.group(1)] = json.dumps(resource)
    assert set(by_suffix) == {"foundry", "monitoring", "foundation", "realtime", "backup-role", "backup-foundation",
                              "m3-artifacts", "onboarding-delegation"}
    # The foundation reads the monitoring workspace from its output (ARM orders and verifies it).
    assert "-monitoring', variables('env'))), '2025-04-01').outputs.workspaceResourceId" in by_suffix["foundation"]
    assert "-foundry'" in by_suffix["monitoring"]
    assert "-backup-role'" in by_suffix["backup-foundation"] and "-foundation'" in by_suffix["backup-foundation"]
    assert "fa-m1-backup-data-read-write-v1" in by_suffix["m3-artifacts"]
    assert by_suffix["onboarding-delegation"].count("enableAgentOnboarding") >= 1


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
        assert re.fullmatch(r"\$\{(AZURE|FA)_[A-Z_]+(=[A-Za-z]+)?\}", item["value"]), name


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
