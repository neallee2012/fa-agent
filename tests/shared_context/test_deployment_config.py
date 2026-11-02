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


# ---- validate_config: every rule, its exact message, and which rule stops first ------------------------------------

PROFILES = ("deploy/stage/stage.json", "deploy/dev/dev.json")
UUID_MESSAGE = "must be a UUID"
RESOURCE_MESSAGE = "is not a valid Azure resource name"


@pytest.fixture(scope="module")
def config():
    return load("deployment_config_rules", ROOT / "deploy" / "shared-context" / "deployment_config.py")


def stage_profile():
    return json.loads((ROOT / "deploy" / "stage" / "stage.json").read_text(encoding="utf-8"))


def put(key, value):
    return lambda profile: profile.__setitem__(key, value)


def drop(key):
    return lambda profile: profile.pop(key)


def put_agent(key, value, index=0):
    return lambda profile: profile["agents"][index].__setitem__(key, value)


def drop_agent(key, index=0):
    return lambda profile: profile["agents"][index].pop(key)


def put_runtime(key, value):
    return lambda profile: profile["runtime"].__setitem__(key, value)


def drop_runtime(key):
    return lambda profile: profile["runtime"].pop(key)


def rejection(config, profile, **options):
    with pytest.raises(SystemExit) as stopped:
        config.validate_config(profile, **options)
    return stopped.value.code


# In source order: breaking a rule and everything after it must still report that rule.
CONFIG_RULES = [
    ("deployment config is missing: environment", drop("environment")),
    ("deployment config tenant_id must be a UUID", put("tenant_id", "not-a-uuid")),
    ("deployment config subscription_id must be a UUID", put("subscription_id", "not-a-uuid")),
    ("deployment config resource_group is not a valid Azure resource name", put("resource_group", "-bad")),
    ("deployment config registry_name is not a valid Azure resource name", put("registry_name", "-bad")),
    ("deployment config api_app is not a valid Azure resource name", put("api_app", "-bad")),
    ("deployment config api_identity is not a valid Azure resource name", put("api_identity", "-bad")),
    ("deployment config sync_job is not a valid Azure resource name", put("sync_job", "-bad")),
    ("deployment config project_endpoint must be an https URL", put("project_endpoint", "http://x")),
    ("deployment config registry must be an Azure Container Registry hostname", put("registry", "example.com")),
    ("deployment config api_audience must be a non-empty string", put("api_audience", "")),
    ("deployment config hosted_idle_timeout_seconds must be a positive integer",
     put("hosted_idle_timeout_seconds", 0)),
    ("deployment config api_environment must map strings to strings", put("api_environment", {"A": 1})),
    ("deployment config assistant_environment must map strings to strings", put("assistant_environment", {"A": 1})),
    ("deployment config agents must be a non-empty array", put("agents", [])),
    ("deployment config agents[0] must be an object", lambda profile: profile["agents"].__setitem__(0, "agent")),
    ("deployment config agents[0].name must be a non-empty string", put_agent("name", "")),
    ("deployment config agents[0].kind must be a non-empty string", put_agent("kind", "")),
    ("deployment config agents[0].description must be a non-empty string", put_agent("description", "")),
    ("deployment config agents[0].kind is invalid", put_agent("kind", "worker")),
    ("deployment config agents[0].agent_id must be a UUID", put_agent("agent_id", "not-a-uuid")),
    ("deployment config agents[0].agent_oid must be a UUID", put_agent("agent_oid", "not-a-uuid")),
    ("deployment config agents[1] must be an object", lambda profile: profile["agents"].__setitem__(1, "agent")),
    ("deployment config api_identity_client_id is required for runtime deployment", drop("api_identity_client_id")),
    ("deployment config api_url is required for runtime deployment", drop("api_url")),
    ("deployment config api_url must be an https URL", put("api_url", "http://x")),
    ("deployment config runtime must be an object", put("runtime", [])),
    ("deployment config runtime.authority_domain must be a non-empty string", put_runtime("authority_domain", "")),
    ("deployment config runtime.team_id must be a non-empty string", put_runtime("team_id", "")),
    ("deployment config runtime.human_oid must be a non-empty string", put_runtime("human_oid", "")),
    ("deployment config runtime.admin_client_id must be a non-empty string", put_runtime("admin_client_id", "")),
    ("deployment config runtime.table_account must be a non-empty string", put_runtime("table_account", "")),
    ("deployment config runtime.table_name must be a non-empty string", put_runtime("table_name", "")),
    ("deployment config runtime.blob_account must be a non-empty string", put_runtime("blob_account", "")),
    ("deployment config runtime.sources_container must be a non-empty string", put_runtime("sources_container", "")),
    ("deployment config runtime.backups_container must be a non-empty string", put_runtime("backups_container", "")),
    ("deployment config runtime.human_oid must be a UUID", put_runtime("human_oid", "not-a-uuid")),
    ("deployment config console_app is required for runtime deployment", drop("console_app")),
]


@pytest.mark.parametrize("relative", PROFILES)
@pytest.mark.parametrize("require_runtime", [True, False])
def test_checked_in_profiles_stay_valid(config, relative, require_runtime):
    assert config.load_config(ROOT / relative, require_runtime=require_runtime)["agents"]


@pytest.mark.parametrize("first", range(len(CONFIG_RULES)), ids=lambda index: CONFIG_RULES[index][0])
def test_each_rule_alone_stops_with_its_own_message(config, first):
    message, breaks = CONFIG_RULES[first]
    profile = stage_profile()
    breaks(profile)
    assert rejection(config, profile) == message


@pytest.mark.parametrize("first", range(len(CONFIG_RULES)), ids=lambda index: CONFIG_RULES[index][0])
def test_the_earliest_broken_rule_is_the_one_reported(config, first):
    profile = stage_profile()
    for _, breaks in reversed(CONFIG_RULES[first:]):  # reversed: an earlier rule overwrites a later one on the same key
        breaks(profile)
    assert rejection(config, profile) == CONFIG_RULES[first][0]


@pytest.mark.parametrize("message, breaks", [
    *[(f"deployment config {key} {UUID_MESSAGE}", put(key, bad))
      for key in ("tenant_id", "subscription_id") for bad in (1, None, "0" * 35, "0" * 37, "g" * 36, "0" * 36 + "\n")],
    *[(f"deployment config {key} {RESOURCE_MESSAGE}", put(key, bad))
      for key in ("resource_group", "registry_name", "api_app", "api_identity", "sync_job")
      for bad in (1, None, "", "a" * 64, "a b", "a\n")],
    *[("deployment config project_endpoint must be an https URL", put("project_endpoint", bad))
      for bad in (1, None, "https://", "https://a b", "https://x\n")],
    *[("deployment config registry must be an Azure Container Registry hostname", put("registry", bad))
      for bad in (1, None, "x.azurecr.io.evil")],
    ("deployment config api_audience must be a non-empty string", put("api_audience", 1)),
    *[("deployment config hosted_idle_timeout_seconds must be a positive integer",
       put("hosted_idle_timeout_seconds", bad)) for bad in ("300", None, -1, 1.5, False)],
    *[(f"deployment config {key} must map strings to strings", put(key, bad))
      for key in ("api_environment", "assistant_environment") for bad in ([], "A=B", None, {1: "B"})],
    *[("deployment config agents must be a non-empty array", put("agents", bad)) for bad in ({"name": "a"}, "a", None)],
    *[(f"deployment config agents[0].{key} must be a non-empty string", drop_agent(key))
      for key in ("name", "kind", "description")],
    *[(f"deployment config agents[0].{key} must be a non-empty string", put_agent(key, 1))
      for key in ("name", "kind", "description")],
    ("deployment config agents[1].name must be a non-empty string", drop_agent("name", 1)),
    ("deployment config agents[1].kind is invalid", put_agent("kind", "worker", 1)),
    ("deployment config agents[1].agent_oid must be a UUID", put_agent("agent_oid", "not-a-uuid", 1)),
    *[(f"deployment config agents[0].{key} {UUID_MESSAGE}", put_agent(key, 1)) for key in ("agent_id", "agent_oid")],
    *[(f"deployment config agents[0].{key} {UUID_MESSAGE}", drop_agent(key)) for key in ("agent_id", "agent_oid")],
    *[(f"deployment config {key} is required for runtime deployment", put(key, bad))
      for key in ("api_identity_client_id", "api_url") for bad in ("", 1, None)],
    ("deployment config api_url must be an https URL", put("api_url", "https://x\n")),
    ("deployment config runtime must be an object", drop("runtime")),
    ("deployment config runtime.human_oid must be a UUID", put_runtime("human_oid", "0" * 36 + "\n")),
    *[(f"deployment config runtime.{key} must be a non-empty string", drop_runtime(key))
      for key in ("authority_domain", "backups_container")],
    ("deployment config runtime.team_id must be a non-empty string", put_runtime("team_id", 1)),
])
def test_each_kind_of_bad_value_is_refused(config, message, breaks):
    profile = stage_profile()
    breaks(profile)
    assert rejection(config, profile) == message


@pytest.mark.parametrize("allowed", [
    put("hosted_idle_timeout_seconds", 1),
    put("hosted_idle_timeout_seconds", True),  # bool is an int and not below 1
    put("api_environment", {}),
    put("assistant_environment", {}),
    put("resource_group", "a" * 63),
    put("registry", ".azurecr.io"),
    put("console_app", None),  # the rule is only that the key exists
    put_agent("kind", "assistant"),
    put_agent("kind", "executor"),
])
def test_the_edges_a_rule_still_accepts(config, allowed):
    profile = stage_profile()
    allowed(profile)
    assert config.validate_config(profile) is None


def test_only_the_first_two_rules_name_the_file(config):
    path = Path("profile.json")
    assert rejection(config, [], path=path) == "deployment config in profile.json must be a JSON object"
    assert rejection(config, None) == "deployment config must be a JSON object"
    assert rejection(config, {}, path=path).startswith("deployment config in profile.json is missing: environment, ")
    profile = stage_profile()
    profile["tenant_id"] = "not-a-uuid"
    assert rejection(config, profile, path=path) == "deployment config tenant_id must be a UUID"


def test_missing_keys_are_listed_in_contract_order(config):
    profile = stage_profile()
    for key in ("assistant_environment", "environment", "agents"):
        del profile[key]
    assert rejection(config, profile) == "deployment config is missing: environment, agents, assistant_environment"
    assert rejection(config, {}) == (
        "deployment config is missing: environment, tenant_id, subscription_id, resource_group, project_endpoint, "
        "registry, registry_name, api_app, api_audience, api_identity, sync_job, hosted_idle_timeout_seconds, agents, "
        "api_environment, assistant_environment")


def test_without_require_runtime_no_runtime_rule_applies(config):
    profile = stage_profile()
    for key in ("api_identity_client_id", "api_url", "runtime", "console_app"):
        del profile[key]
    for agent in profile["agents"]:
        del agent["agent_id"], agent["agent_oid"]
    assert config.validate_config(profile, require_runtime=False) is None
    assert rejection(config, profile) == "deployment config agents[0].agent_id must be a UUID"
