"""Environment profile loading and offline parameter rendering."""

import importlib.util
import json
import os
from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checked_in_stage_profile_is_valid():
    module = load("deployment_config", ROOT / "deploy" / "shared-context" / "deployment_config.py")
    profile = module.load_config(ROOT / "deploy" / "stage" / "stage.json")
    assert profile["environment"] == "stage"
    assert all(agent["agent_id"] for agent in profile["agents"])


def test_loader_rejects_runtime_profile_without_agent_ids(tmp_path):
    module = load("deployment_config_missing_ids", ROOT / "deploy" / "shared-context" / "deployment_config.py")
    profile = json.loads((ROOT / "deploy" / "stage" / "stage.json").read_text(encoding="utf-8"))
    profile["agents"][0].pop("agent_id")
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(SystemExit, match="agent_id"):
        module.load_config(path)
    module.load_config(path, require_runtime=False)


def test_prepare_renders_isolated_names_and_min_replicas(tmp_path):
    module = load("prepare_environment", ROOT / "deploy" / "shared-context" / "prepare_environment.py")
    source = {
        "name_prefix": "fa-agent-v18",
        "subscription_id": "00000000-0000-0000-0000-000000000018",
        "tenant_id": "00000000-0000-0000-0000-000000000019",
        "location": "westus3",
        "resource_group": "rg-fa-agent-v18",
        "operator_object_id": "00000000-0000-0000-0000-00000000005b",
        "read_write_role_definition_guid": "00000000-0000-0000-0000-00000000005c",
    }
    result = module.resolve(source)
    profile = result["profile"]
    params = result["parameters"]
    assert profile["names"]["registry"] == "crfaagentv18demo"
    assert params["foundation.parameters.json"]["vnetName"] == "vnet-fa-agent-v18"
    assert params["behavior-api.parameters.json"]["minReplicas"] == 1
    assert params["behavior-api.parameters.json"]["appName"] == "ca-fa-behavior-v18"
    assert params["foundation.parameters.json"]["apiSubnetNatEgress"] is False


def test_prepare_renders_stage_monitoring_contract_for_the_new_environment():
    module = load("prepare_environment_monitoring", ROOT / "deploy" / "shared-context" / "prepare_environment.py")
    result = module.resolve(_source())
    monitoring = result["parameters"]["monitoring.parameters.json"]
    contract = (ROOT / "infra" / "shared-context" / "stage-monitoring.bicep").read_text(encoding="utf-8")
    declared = set(re.findall(r"(?m)^param\s+([A-Za-z]\w*)\s+", contract))
    assert set(monitoring) == declared
    assert monitoring["location"] == "westus3"
    assert monitoring["workspaceName"] == "log-fa-agent-v18"
    assert monitoring["appInsightsName"] == "appi-fa-agent-v18"
    assert monitoring["foundryResourceGroupName"] == "rg-fa-agent-v18"
    assert monitoring["foundryAccountName"] == "cog-fa-agent-v18-demo"
    assert monitoring["projectName"] == "fa-agent-v18"
    assert monitoring["connectionName"] == "fa-agent-v18-appinsights"
    assert monitoring["foundryDeploymentPrefix"] == "fa-agent-v18"
    assert monitoring["dailyQuotaGb"] == "0.5"
    assert monitoring["tags"] == result["profile"]["tags"]
    assert result["profile"]["log_analytics_workspace_resource_id"].endswith(
        "/workspaces/log-fa-agent-v18")


def _source(**extra):
    return {
        "name_prefix": "fa-agent-v18",
        "subscription_id": "00000000-0000-0000-0000-000000000018",
        "tenant_id": "00000000-0000-0000-0000-000000000019",
        "location": "westus3",
        "resource_group": "rg-fa-agent-v18",
        "operator_object_id": "00000000-0000-0000-0000-00000000005b",
        "read_write_role_definition_guid": "00000000-0000-0000-0000-00000000005c",
        **extra,
    }


def test_prepare_renders_one_deployment_per_catalog_model():
    module = load("prepare_environment_models", ROOT / "deploy" / "shared-context" / "prepare_environment.py")
    catalog = json.loads((ROOT / "src" / "fa-shared-context-poc" / "model-catalog.json").read_text(encoding="utf-8"))
    deployments = module.resolve(_source())["parameters"]["foundry.parameters.json"]["modelDeployments"]
    assert [item["name"] for item in deployments] == [item["deployment"] for item in catalog]
    for rendered, entry in zip(deployments, catalog):
        assert rendered == {
            "name": entry["deployment"],
            "model": {"format": entry["model"]["format"], "name": entry["model"]["name"],
                      "version": entry["model"]["version"]},
            "sku": {"name": entry["sku"]["name"], "capacity": entry["sku"]["capacity"]},
            "versionUpgradeOption": "NoAutoUpgrade",
        }
    tuned = module.resolve(_source(model_capacity={"gpt-5.4-mini": 500},
                                   model_version_upgrade_option="OnceNewDefaultVersionAvailable"))
    by_name = {item["name"]: item for item in tuned["parameters"]["foundry.parameters.json"]["modelDeployments"]}
    assert by_name["gpt-5.4-mini"]["sku"]["capacity"] == 500
    assert {item["versionUpgradeOption"] for item in by_name.values()} == {"OnceNewDefaultVersionAvailable"}
    # The 2026-10-07 environment: gpt-4.1-mini pinned, three later deployments auto-upgrade, gpt-5.4-mini at 500.
    mixed = module.resolve(_source(
        model_capacity={"gpt-5.4-mini": 500},
        model_version_upgrade_option={name: "OnceNewDefaultVersionAvailable"
                                      for name in ("gpt-5.4-mini", "gpt-6-luna", "gpt-6.1-sol")}))
    by_name = {item["name"]: item for item in mixed["parameters"]["foundry.parameters.json"]["modelDeployments"]}
    assert {name: item["versionUpgradeOption"] for name, item in by_name.items()} == {
        "gpt-4.1-mini": "NoAutoUpgrade", "text-embedding-3-small": "NoAutoUpgrade",
        "gpt-5.4-mini": "OnceNewDefaultVersionAvailable", "gpt-6-luna": "OnceNewDefaultVersionAvailable",
        "gpt-6.1-sol": "OnceNewDefaultVersionAvailable"}


@pytest.mark.parametrize("extra", [
    {"model_capacity": {"gpt-9": 10}},
    {"model_capacity": {"gpt-4.1-mini": 0}},
    {"model_version_upgrade_option": "Always"},
    {"model_version_upgrade_option": {"gpt-9": "NoAutoUpgrade"}},
    {"model_version_upgrade_option": {"gpt-6-luna": "Always"}},
    {"api_subnet_nat_egress": "yes"},
])
def test_prepare_rejects_invalid_model_or_network_options(extra):
    module = load("prepare_environment_invalid", ROOT / "deploy" / "shared-context" / "prepare_environment.py")
    with pytest.raises(SystemExit):
        module.resolve(_source(**extra))


def test_prepare_keeps_nat_egress_opt_in():
    module = load("prepare_environment_nat", ROOT / "deploy" / "shared-context" / "prepare_environment.py")
    params = module.resolve(_source(api_subnet_nat_egress=True))["parameters"]
    assert params["foundation.parameters.json"]["apiSubnetNatEgress"] is True


INTENT_WRITERS = ("deploy/azd/fa_azd.py", "deploy/stage/new_memory_store.py", "deploy/stage/prepare_native_sessions.py",
                  "deploy/stage/seed_agents.py", "deploy/stage/select_all_capabilities.py")


@pytest.fixture(params=INTENT_WRITERS, ids=lambda relative: Path(relative).stem)
def write_x(request, monkeypatch):
    name = "write_x_" + Path(request.param).stem
    spec = importlib.util.spec_from_file_location(name, ROOT / request.param)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)  # fa_azd's dataclasses look their module up here
    spec.loader.exec_module(module)
    return module._write_x


def test_write_x_creates_indent_one_ascii_json_and_syncs_it_after_flushing(write_x, tmp_path, monkeypatch):
    path = tmp_path / "intent.json"
    expected = json.dumps({"b": [1, "\u00e9"], "a": None}, indent=1)
    seen, real_fsync = [], os.fsync

    def fsync(descriptor):
        seen.append(path.read_bytes())  # what the OS already holds when fsync runs
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    assert write_x(path, {"b": [1, "\u00e9"], "a": None}) is None
    assert expected == '{\n "b": [\n  1,\n  "\\u00e9"\n ],\n "a": null\n}'
    assert path.read_bytes() == expected.replace("\n", os.linesep).encode("ascii")  # text mode: platform newlines
    assert seen == [path.read_bytes()]


def test_write_x_never_replaces_an_existing_file(write_x, tmp_path, monkeypatch):
    path = tmp_path / "intent.json"
    path.write_bytes(b"original")
    monkeypatch.setattr(os, "fsync", lambda descriptor: pytest.fail("nothing to sync"))
    with pytest.raises(FileExistsError):
        write_x(path, {"replacement": True})
    assert path.read_bytes() == b"original"


def test_write_x_does_not_create_missing_parent_directories(write_x, tmp_path):
    with pytest.raises(FileNotFoundError):
        write_x(tmp_path / "missing" / "intent.json", {})
    assert not (tmp_path / "missing").exists()
