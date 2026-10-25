"""azd environment profile (deploy/azd/profile.py) against the real consumers, offline and synthetic.

The profile must pass the same loader, the CI's pre-build checks, make_runtime.py and the fresh-plan rebuild that
run_fresh_job.py does after the Job, and the Portal export chain. All IDs below are synthetic; nothing calls Azure.
"""

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
TENANT = "0a1b2c3d-1111-4222-8333-444455556666"
SUBSCRIPTION = "1b2c3d4e-2222-4333-8444-555566667777"
OWNER = "2c3d4e5f-3333-4444-8555-666677778888"
API_CLIENT = "3d4e5f60-4444-4555-8666-777788889999"
CONSOLE_CLIENT = "4e5f6071-5555-4666-8777-88889999aaaa"
PROJECT_ID = (f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-dev/providers/Microsoft.CognitiveServices"
              "/accounts/cog-tok3n/projects/fa-dev")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def values(**extra):
    base = {
        "AZURE_ENV_NAME": "dev", "FA_ENVIRONMENT": "dev", "AZURE_TENANT_ID": TENANT,
        "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION, "AZURE_RESOURCE_GROUP": "rg-dev", "AZURE_LOCATION": "westus3",
        "FA_FOUNDRY_ACCOUNT": "cog-tok3n", "FA_PROJECT": "fa-dev",
        "FA_PROJECT_ENDPOINT": "https://cog-tok3n.services.ai.azure.com/api/projects/fa-dev",
        "FA_PROJECT_RESOURCE_ID": PROJECT_ID, "FA_REGISTRY": "crtok3n.azurecr.io", "FA_REGISTRY_NAME": "crtok3n",
        "FA_CONTAINER_APPS_ENVIRONMENT": "cae-dev", "FA_API_APP": "ca-api-dev",
        "FA_API_URL": "https://ca-api-dev.example-1234.westus3.azurecontainerapps.io",
        "FA_API_AUDIENCE": f"api://{TENANT}/fa-dev-0123456789", "FA_API_IDENTITY": "id-fa-api-dev",
        "FA_API_IDENTITY_CLIENT_ID": API_CLIENT, "FA_SYNC_JOB": "job-sync-dev", "FA_CONSOLE_APP": "ca-console-dev",
        "FA_CONSOLE_APP_ID": CONSOLE_CLIENT, "FA_LOG_ANALYTICS": "log-dev", "FA_TEAM_OWNER_OID": OWNER,
        "FA_TABLE_ACCOUNT": "sttok3n", "FA_TABLE_NAME": "workstate", "FA_BACKUP_STORAGE": "stbktok3n",
        "FA_BACKUP_CONTAINER": "m1-environment-backups", "FA_ARTIFACTS_CONTAINER": "m3-artifacts",
        "FA_REALTIME_ENDPOINT": "https://wps-tok3n.webpubsub.azure.com",
    }
    for index, letter in enumerate("ABCD"):
        base[f"FA_AGENT_{letter}_ID"] = f"5f60718{index}-6666-4777-8888-9999aaaabbbb"
        base[f"FA_AGENT_{letter}_OID"] = f"60718293-7777-4888-8999-aaaabbbbccc{index}"
    return {**base, **extra}


@pytest.fixture(scope="module")
def azd_profile():
    return load("fa_azd_profile", ROOT / "deploy" / "azd" / "profile.py")


@pytest.fixture
def profile_file(tmp_path, azd_profile):
    path = tmp_path / "profile.json"
    azd_profile.write(path, azd_profile.build(values()))
    return path


def test_defaults_only_use_keys_and_values_the_stage_profile_already_has(azd_profile):
    stage = json.loads((ROOT / "deploy" / "stage" / "stage.json").read_text(encoding="utf-8"))
    base = azd_profile.defaults()
    for section in ("api_environment", "assistant_environment", "console_environment"):
        assert set(base[section]) <= set(stage[section]), section
        assert all(stage[section][key] == value for key, value in base[section].items()), section
    # ADR-0009 2d-ii: like stage, a new environment starts with 2d-i on (the API sends foundry-native-v2 only).
    assert base["api_environment"]["FA_NATIVE_SESSION_GUARD_ENFORCE"] == "1"
    assert base["conversation_defaults"] == stage["conversation_defaults"]
    # A subset, like the sections above: stage may turn on more than a new azd environment starts with.
    overlay = stage["agents"][0]["hosted_environment"]
    assert base["assistant_hosted_environment"].items() <= overlay.items()
    assert base["hosted_idle_timeout_seconds"] == stage["hosted_idle_timeout_seconds"]
    # No identifier, endpoint or environment name: those come from the azd environment.
    raw = json.dumps(base)
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", raw)
    assert "https://" not in raw and "stage" not in raw
    # No toolbox or feed reader in a new environment; the "+" settings need the project ID, so profile.py adds them.
    for key in ("FA_ONBOARDING_ROLE_SCOPE", "FA_CAPABILITY_TOOLBOX_ENTRIES", "FA_REMOTE_CONSOLE_CLIENT_ID",
                "FEED_MCP_PUBLIC_KEY"):
        assert key not in raw
    for section, target in (("assistant", stage["assistant_environment"]), ("api", stage["api_environment"])):
        assert base["memory_environment"][section].items() <= target.items()


def test_a_new_environment_has_no_team_connector_and_needs_no_teams_or_github_app(azd_profile):
    # Stage turns the Teams connector on (deploy/stage/stage.json); `azd up` does not, so a new environment needs
    # no Work IQ Entra app, Teams client ID or GitHub credential, and the API leaves the connector routes out.
    for overrides in ({}, {"FA_ENABLE_WEBIQ": "true", "FA_AGENT_ONBOARDING": "true"}):
        profile = azd_profile.build(values(**overrides))
        text = json.dumps(profile)
        assert "FA_TEAM_CONNECTORS_ENABLED" not in text
        assert "FA_CONNECTOR_" not in text
        assert "workiq" not in text.lower()
        assert "GITHUB" not in text.upper() and "GH_TOKEN" not in text
        # Team MCP (keyed servers the owner adds) stays on; it needs no Teams or GitHub app either.
        assert profile["api_environment"]["FA_TEAM_MCP_ENABLED"] == "1"
    stage = json.loads((ROOT / "deploy" / "stage" / "stage.json").read_text(encoding="utf-8"))
    assert stage["api_environment"]["FA_TEAM_CONNECTORS_ENABLED"] == "1"


def test_upgrade_snapshot_url_reaches_the_api_only_from_the_opt_in_output(azd_profile):
    assert "FA_UPGRADE_SNAPSHOT_CONTAINER_URL" not in azd_profile.build(values())["api_environment"]
    assert "FA_UPGRADE_SNAPSHOT_CONTAINER_URL" not in azd_profile.build(
        values(FA_UPGRADE_SNAPSHOT_CONTAINER_URL=""))["api_environment"]
    url = "https://stsnaptok3n.blob.core.windows.net/upgrade-snapshots"
    on = azd_profile.build(values(FA_UPGRADE_SNAPSHOT_CONTAINER_URL=url))["api_environment"]
    assert on["FA_UPGRADE_SNAPSHOT_CONTAINER_URL"] == url


def test_team_mcp_is_off_when_bicep_does_not_grant_the_api_contributor(azd_profile):
    on = azd_profile.build(values())["api_environment"]
    assert on["FA_TEAM_MCP_ENABLED"] == "1" and "FA_TEAM_MCP_PROJECT_RESOURCE_ID" in on
    off = azd_profile.build(values(FA_ENABLE_TEAM_MCP_CONNECTIONS="false"))
    assert not [key for key in off["api_environment"] if key.startswith("FA_TEAM_MCP")]
    assert not [key for key in off["console_environment"] if key.startswith("FA_TEAM_MCP")]


def test_profile_takes_every_identifier_from_the_azd_environment(azd_profile):
    profile = azd_profile.build(values())
    text = json.dumps(profile)
    stage = json.loads((ROOT / "deploy" / "stage" / "stage.json").read_text(encoding="utf-8"))
    for value in (stage["tenant_id"], stage["subscription_id"], stage["runtime"]["human_oid"],
                  stage["project_endpoint"], stage["api_identity_client_id"]):
        assert value not in text
    assert [(agent["name"], agent["kind"]) for agent in profile["agents"]] == [
        ("fa-dev-a", "assistant"), ("fa-dev-b", "assistant"), ("fa-dev-c", "executor"), ("fa-dev-d", "executor")]
    assert "deploy_client_id" not in profile
    assert profile["api_environment"]["AZURE_CLIENT_ID"] == API_CLIENT
    assert profile["api_environment"]["FA_REMOTE_CONSOLE_CLIENT_ID"] == CONSOLE_CLIENT
    assert profile["api_environment"]["FA_TEAM_MCP_PROJECT_RESOURCE_ID"] == PROJECT_ID
    assert profile["console_environment"]["FA_REALTIME_ENDPOINT"] == profile["api_environment"]["FA_REALTIME_ENDPOINT"]
    assert profile["runtime"] == {
        "team_id": "team-dev", "authority_domain": "fa-dev-v1", "human_oid": OWNER,
        "admin_client_id": "04b07795-8ddb-461a-bbee-02f9e1bf7b46", "table_account": "sttok3n",
        "table_name": "workstate", "blob_account": "stbktok3n", "sources_container": "m3-artifacts",
        "backups_container": "m1-environment-backups"}
    assert profile["assistant_environment"]["TZ"] == "UTC"
    assert profile["assistant_environment"]["FA_PRIVATE_MEMORY_STORE"] == "poc-team-memory"  # on by default
    assert profile["api_environment"]["FA_TEAM_MEMORY_ENABLED"] == "1"
    off = azd_profile.build(values(FA_ENABLE_MEMORY="false"))
    assert "FA_PRIVATE_MEMORY_STORE" not in off["assistant_environment"]
    assert "FA_TEAM_MEMORY_ENABLED" not in off["api_environment"]
    assert "FA_CAPABILITY_TOOLBOX_ENTRIES" not in profile["api_environment"]
    assert not [key for key in json.dumps(profile).split('"') if key.startswith(("FA_MEMORY_BACKEND", "FA_COSMOS"))]


COSMOS = "https://cosmos-tok3n.documents.azure.com:443/"


def test_private_memory_is_cosmos_db_through_the_api(azd_profile):
    # ADR-0011: no Memory Store name; both sides name the backend; only the API gets the Bicep endpoint output.
    profile = azd_profile.build(values(FA_NETWORK_MODE="private", FA_COSMOS_ENDPOINT=COSMOS,
                                       FA_MODELS="gpt-5.4-mini", FA_MEMORY_CHAT_MODEL="gpt-5.4-mini"))
    assistant, api = profile["assistant_environment"], profile["api_environment"]
    assert "FA_PRIVATE_MEMORY_STORE" not in assistant
    assert {key: assistant[key] for key in ("FA_MEMORY_BACKEND", "FA_MEMORY_EXTRACTION_MODEL",
                                            "FA_TEAM_MEMORY_ENABLED")} == {
        "FA_MEMORY_BACKEND": "cosmos", "FA_MEMORY_EXTRACTION_MODEL": "gpt-5.4-mini", "FA_TEAM_MEMORY_ENABLED": "1"}
    assert {key: api[key] for key in ("FA_MEMORY_BACKEND", "FA_COSMOS_ENDPOINT", "FA_TEAM_MEMORY_ENABLED")} == {
        "FA_MEMORY_BACKEND": "cosmos", "FA_COSMOS_ENDPOINT": COSMOS, "FA_TEAM_MEMORY_ENABLED": "1"}
    assert "FA_COSMOS_ENDPOINT" not in assistant  # Hosted never connects to Cosmos DB
    with pytest.raises(SystemExit, match="FA_COSMOS_ENDPOINT is missing"):
        azd_profile.build(values(FA_NETWORK_MODE="private", FA_COSMOS_ENDPOINT=""))
    off = azd_profile.build(values(FA_NETWORK_MODE="private", FA_ENABLE_MEMORY="false"))
    keys = json.dumps(off).split('"')
    assert not [key for key in keys if key.startswith(("FA_MEMORY_", "FA_COSMOS", "FA_PRIVATE_MEMORY"))]


def test_only_private_network_mode_reaches_the_api(azd_profile):
    # ADR-0010: the API refuses adding Work IQ in private; public and unset leave the API environment unchanged.
    private = azd_profile.build(values(FA_NETWORK_MODE="private", FA_ENABLE_MEMORY="false"))
    assert private["api_environment"]["FA_NETWORK_MODE"] == "private"
    assert "FA_NETWORK_MODE" not in private["assistant_environment"]
    for mode in ({}, {"FA_NETWORK_MODE": "public"}):
        assert azd_profile.build(values(**mode)) == azd_profile.build(values())
        assert "FA_NETWORK_MODE" not in json.dumps(azd_profile.build(values(**mode)))


ONBOARDING = {"FA_ONBOARDING_ENDPOINT_PROTOCOLS": "true", "FA_ONBOARDING_ROLE_SCOPE": PROJECT_ID}


def test_plus_onboarding_is_on_by_default_and_follows_the_bicep_output(azd_profile):
    # Neal 2026-10-15: "+" works right after `azd up`. Provision writes the Bicep output FA_AGENT_ONBOARDING from
    # the main.parameters.json default; the API settings follow it, so `azd env set ... false` leaves none.
    parameters = json.loads((ROOT / "infra" / "azd" / "main.parameters.json").read_text(encoding="utf-8"))
    default = re.fullmatch(r"\$\{FA_ENABLE_AGENT_ONBOARDING=(\w+)\}",
                           parameters["parameters"]["enableAgentOnboarding"]["value"]).group(1)
    api = azd_profile.build(values(FA_AGENT_ONBOARDING=default))["api_environment"]
    assert ONBOARDING.items() <= api.items()
    off = azd_profile.build(values(FA_AGENT_ONBOARDING="false"))["api_environment"]
    assert not [key for key in off if key.startswith("FA_ONBOARDING")]


def test_a_missing_onboarding_output_means_off_and_says_so(azd_profile, capsys):
    # No output = nothing shows provision created the delegation; the API must not get settings that would 403.
    api = azd_profile.build(values())["api_environment"]
    assert not [key for key in api if key.startswith("FA_ONBOARDING")]
    assert "FA_AGENT_ONBOARDING is missing" in capsys.readouterr().err
    stage = json.loads((ROOT / "deploy" / "stage" / "stage.json").read_text(encoding="utf-8"))
    assert stage["api_environment"]["FA_ONBOARDING_ENDPOINT_PROTOCOLS"] == "true"  # the same settings stage has
    assert "FA_ONBOARDING_ROLE_SCOPE" in stage["api_environment"]


def test_the_account_role_of_plus_follows_only_its_bicep_output(azd_profile, capsys):
    # ADR-0006 (Neal 2026-10-24): an older private+cosmos environment that only runs `azd deploy` has no account-scope
    # delegation (and no output), so the API must not send the account PUT that would 403 and stop "+" at creating.
    private = dict(FA_NETWORK_MODE="private", FA_COSMOS_ENDPOINT=COSMOS, FA_AGENT_ONBOARDING="true")
    older = azd_profile.build(values(**private))["api_environment"]
    assert older["FA_MEMORY_BACKEND"] == "cosmos" and ONBOARDING.items() <= older.items()
    assert "FA_ONBOARDING_ACCOUNT_ROLE" not in older
    assert "FA_AGENT_ONBOARDING_ACCOUNT_ROLE is missing" in capsys.readouterr().err
    assert "FA_ONBOARDING_ACCOUNT_ROLE" not in azd_profile.build(values(
        **private, FA_AGENT_ONBOARDING_ACCOUNT_ROLE="false"))["api_environment"]
    on = azd_profile.build(values(**private, FA_AGENT_ONBOARDING_ACCOUNT_ROLE="true"))["api_environment"]
    assert on["FA_ONBOARDING_ACCOUNT_ROLE"] == "true" and ONBOARDING.items() <= on.items()
    assert capsys.readouterr().err == ""
    # Onboarding off leaves every "+" setting out, the account one too; public provisions the output as false.
    off = azd_profile.build(values(**{**private, "FA_AGENT_ONBOARDING": "false"},
                                   FA_AGENT_ONBOARDING_ACCOUNT_ROLE="true"))
    assert not [key for key in off["api_environment"] if key.startswith("FA_ONBOARDING")]
    public = azd_profile.build(values(FA_AGENT_ONBOARDING="true", FA_AGENT_ONBOARDING_ACCOUNT_ROLE="false"))
    assert public == azd_profile.build(values(FA_AGENT_ONBOARDING="true"))
    assert "FA_AGENT_ONBOARDING_ACCOUNT_ROLE is missing" not in capsys.readouterr().err


def test_opt_ins_add_only_their_own_settings(azd_profile):
    plain = azd_profile.build(values())["api_environment"]
    both = azd_profile.build(values(FA_ENABLE_WEBIQ="true", FA_TIMEZONE="Asia/Taipei"))
    added = {key: value for key, value in both["api_environment"].items() if key not in plain}
    assert added == {"FA_CAPABILITY_TOOLBOX_ENTRIES": "WebIQ"}
    assert both["assistant_environment"]["TZ"] == "Asia/Taipei"


def test_the_portal_offers_only_the_chosen_models(azd_profile):
    every = azd_profile.build(values())["api_environment"]
    assert every["FA_INSTRUCTION_MODELS"] == "gpt-4.1-mini,gpt-5.4-mini,gpt-6-luna,gpt-6.1-sol"
    assert every["FA_INSTRUCTION_DEFAULT_MODEL"] == "gpt-6.1-sol"
    # What the preprovision hook writes back before the first provision (models.Choice.environment).
    written = {"FA_MODELS": "gpt-5.4-mini,gpt-6-luna", "FA_DEFAULT_MODEL": "gpt-6-luna",
               "FA_MEMORY_CHAT_MODEL": "gpt-6-luna",
               "FA_MODEL_DEPLOYMENTS": "gpt-5.4-mini,gpt-6-luna,text-embedding-3-small"}
    chosen = azd_profile.build(values(**written))
    assert chosen["api_environment"]["FA_INSTRUCTION_MODELS"] == "gpt-5.4-mini,gpt-6-luna"
    assert chosen["api_environment"]["FA_INSTRUCTION_DEFAULT_MODEL"] == "gpt-6-luna"
    with pytest.raises(SystemExit, match="must be one of FA_MODELS"):
        azd_profile.build(values(**{**written, "FA_DEFAULT_MODEL": "gpt-6.1-sol"}))


def test_before_the_seed_only_runtime_fields_may_be_missing(azd_profile):
    early = values()
    for key in [key for key in early if key.startswith("FA_AGENT_")] + ["FA_CONSOLE_APP_ID"]:
        early.pop(key)
    profile = azd_profile.build(early, require_runtime=False)
    assert all("agent_id" not in agent for agent in profile["agents"])
    with pytest.raises(SystemExit, match="FA_CONSOLE_APP_ID"):
        azd_profile.build(early)
    with pytest.raises(SystemExit, match="FA_AGENT_A_ID"):
        azd_profile.build({**early, "FA_CONSOLE_APP_ID": CONSOLE_CLIENT})
    for broken, message in (({"FA_ENVIRONMENT": "Dev_1"}, "FA_ENVIRONMENT"),
                            ({"FA_HOSTED_IDLE_TIMEOUT_SECONDS": "60"}, "120-3600"),
                            ({"FA_ENABLE_WEBIQ": "yes"}, "true or false"),
                            ({"FA_TIMEZONE": "UTC; rm"}, "IANA"),
                            ({"FA_REALTIME_ENDPOINT": ""}, "FA_REALTIME_ENDPOINT")):
        with pytest.raises(SystemExit, match=message):
            azd_profile.build(values(**broken))


def test_profile_passes_the_ci_pre_build_checks(profile_file, monkeypatch):
    monkeypatch.setenv("FA_DEPLOY_CONFIG", str(profile_file))
    ci = load("ci_deploy_azd", ROOT / "deploy" / "stage" / "ci_deploy.py")
    ci.STAGE = ci.load_config(profile_file)
    overlay = ci.hosted_environment()
    assert set(overlay) == {ci.STAGE["agents"][0]["agent_id"], ci.STAGE["agents"][1]["agent_id"]}
    # Mid-conversation model switch on for both assistants, like stage A/B since release 29 (coordinator 2026-10-15).
    assert all(environment["FA_NATIVE_MODEL_SWITCH"] == "1" for environment in overlay.values())
    assert ci.api_routines() is True
    assert ci.realtime() == ("https://wps-tok3n.webpubsub.azure.com", "fa")
    assert ci.console_overlay(ci.STAGE["console_environment"])
    assert ci.feed_config() is None and ci.api_secrets() == {}
    ci.check()  # azd runs it after the seed on a first install and first on every upgrade
    # build only needs the registry: an environment builds its images before its Agents exist.
    early = json.loads(profile_file.read_text(encoding="utf-8"))
    for agent in early["agents"]:
        agent.pop("agent_id"), agent.pop("agent_oid")
    early_file = profile_file.with_name("early.json")
    early_file.write_text(json.dumps(early), encoding="utf-8")
    monkeypatch.setenv("FA_DEPLOY_CONFIG", str(early_file))
    load("ci_deploy_azd_early", ROOT / "deploy" / "stage" / "ci_deploy.py")


def test_a_first_install_passes_the_2di_turn_on_checks_and_a_serving_api_without_p6_does_not(profile_file,
                                                                                            monkeypatch, capsys):
    """ADR-0009 2d-ii: a new azd environment starts with 2d-i on. Its first install runs sync before fa_azd creates
    the API app, so no API serves and nothing old converges: switch_preconditions passes with no app, or with an app
    whose revision never got ready. An environment that already serves an API without P5+P6 is still refused.
    The expected not-found prints nothing (the azd e2e run 2026-10-21 showed az's raw "ERROR: Not Found" line); a
    failed read of an app that exists still shows az's error."""
    monkeypatch.setenv("FA_DEPLOY_CONFIG", str(profile_file))
    ci = load("ci_deploy_azd_first", ROOT / "deploy" / "stage" / "ci_deploy.py")
    ci.STAGE = ci.load_config(profile_file)
    assert ci.STAGE["api_environment"]["FA_NATIVE_SESSION_GUARD_ENFORCE"] == "1"
    ci.check()

    # Only the az processes are stubbed: the GET goes through the real rest() and run(), so what the test sees is
    # run()'s own copy of az's stderr. The app lookup (az resource show) answers with ARM's own text.
    lookup = {"stderr": "(ResourceNotFound) The Resource was not found."}

    def az(command, **kwargs):
        if "rest" in command:
            return SimpleNamespace(returncode=3, stdout="",
                                   stderr='ERROR: Not Found({"error":{"code":"ResourceNotFound"}})\n')
        return SimpleNamespace(returncode=0 if not lookup["stderr"] else 3, stdout="", stderr=lookup["stderr"])
    monkeypatch.setattr(ci, "subprocess", SimpleNamespace(run=az))
    capsys.readouterr()
    ci.switch_preconditions()  # no API app yet
    assert capsys.readouterr().err == ""
    lookup["stderr"] = ""
    with pytest.raises(SystemExit, match="az rest"):  # the app exists: an unreadable one still stops
        ci.switch_preconditions()
    assert "ERROR: Not Found" in capsys.readouterr().err
    lookup["stderr"] = "AuthorizationFailed"  # the lookup fails too: both az errors are shown
    with pytest.raises(SystemExit, match="lookup failed"):
        ci.switch_preconditions()
    shown = capsys.readouterr().err
    assert "ERROR: Not Found" in shown and "AuthorizationFailed" in shown

    ready = f"{ci.STAGE['api_app']}--abcdef1-r1-1"
    app = {"properties": {"configuration": {"activeRevisionsMode": "Single"},
                          "template": {"containers": [{"env": [{"name": "PYTHONUTF8", "value": "1"}]}]}}}
    revisions = {"value": [{"name": ready, "properties": {"active": False}}]}
    monkeypatch.setattr(ci, "rest", lambda method, path, body=None: revisions if "/revisions?" in path else app)
    ci.switch_preconditions()  # an app whose revision never got ready and none is active
    revisions["value"][0]["properties"]["active"] = True  # no ready name, but a revision serves: not a first install
    with pytest.raises(SystemExit, match="needs one active revision, the latest ready one"):
        ci.switch_preconditions()
    app["properties"]["latestReadyRevisionName"] = ready  # an API serves, without the switch and without P5+P6
    monkeypatch.setattr(ci, "serving_commit", lambda revision: "base")
    monkeypatch.setattr(ci, "missing_2di", lambda commit=None: ["NATIVE_2DI_SCHEMA"] if commit == "base" else [])
    with pytest.raises(SystemExit, match="2d-i \\(P5\\+P6\\) on needs"):
        ci.switch_preconditions()


def runtime(profile_file, tmp_path, monkeypatch):
    monkeypatch.setenv("FA_DEPLOY_CONFIG", str(profile_file))
    make_runtime = load("make_runtime_azd", ROOT / "deploy" / "stage" / "make_runtime.py")
    monkeypatch.setattr(sys, "argv", ["make_runtime.py", "--config", str(profile_file),
                                      "--out", str(tmp_path / "runtime")])
    make_runtime.main()
    return make_runtime, tmp_path / "runtime"


def test_make_runtime_accepts_the_azd_profile(profile_file, tmp_path, monkeypatch, capsys):
    _, out = runtime(profile_file, tmp_path, monkeypatch)
    config = json.loads((out / "config.json").read_text(encoding="utf-8"))
    assert json.loads(capsys.readouterr().out)["configuration_sha256"]
    assert config["identity"]["tenant_id"] == TENANT and config["identity"]["human_oid"] == OWNER
    assert config["deployment_id"] == "fa-dev-v1"
    assert [target["agent_name"] for target in config["targets"]] == ["fa-dev-a", "fa-dev-b", "fa-dev-c", "fa-dev-d"]
    assert config["api_resource_id"].endswith("/resourceGroups/rg-dev/providers/Microsoft.App/containerApps/ca-api-dev")


def planned_case(profile_file, tmp_path, monkeypatch):
    """A plan computed by the real FreshInitializer.plan; only the Table read and the job input are stand-ins."""
    from shared_context.behavior_fresh_installation import FreshInitializer, FreshInstallationInputs
    from shared_context.behavior_server_config import load_configuration, read_document

    _, out = runtime(profile_file, tmp_path, monkeypatch)
    config = load_configuration(out / "config.json")
    inputs = read_document(out / "fresh-inputs.json", FreshInstallationInputs)
    observed = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)
    composition = SimpleNamespace(config=config, clients=[object()],
                                  runtime=SimpleNamespace(clock=lambda: observed, job_input=lambda binding: b"job"))
    import shared_context.behavior_fresh_installation as installation
    monkeypatch.setattr(installation, "MigrationStore",
                        lambda client, scope: SimpleNamespace(require_empty_table=lambda: None))
    initializer = FreshInitializer(composition)
    plan = initializer.plan(inputs, request_id="dev-fresh-001", installation_id="dev-native-v1")
    # Exactly what the Job prints (run_fresh_job.script and behavior_fresh_operator.main).
    saved = json.loads(plan.model_dump_json())
    scalars = {key: saved[key] for key in ("empty_table_observed_at", "code_sha256", "job_input_sha256")}
    planned = {"state": "planned", "table_writes": 0, "request_id": plan.request_id,
               "installation_id": plan.installation_id, "configuration_sha256": config.sha256,
               "plan_sha256": plan.sha256, "input_sha256": plan.input_sha256, "approval_required": True,
               "native_runtime_prepared": False}
    lines = ["FA_W9_CORE " + plan.code_sha256, "FA_W9_PLAN_SCALARS " + json.dumps(scalars),
             json.dumps(planned, separators=(",", ":")), "FA_W9_EXIT 0"]
    return plan, out, lines


def test_plan_rebuilt_from_the_job_log_has_the_jobs_hash(profile_file, tmp_path, monkeypatch):
    plan, out, lines = planned_case(profile_file, tmp_path, monkeypatch)
    fresh = load("run_fresh_job_azd", ROOT / "deploy" / "stage" / "run_fresh_job.py")
    scalars, planned = fresh.plan_from_log(lines)
    assert fresh.rebuild_plan(out, scalars, planned).sha256 == plan.sha256
    with pytest.raises(SystemExit, match="differs"):
        fresh.rebuild_plan(out, {**scalars, "job_input_sha256": "0" * 64}, planned)
    for broken in (lines[:-1], [*lines[:2], *lines[1:]], [line.replace("EXIT 0", "EXIT 1") for line in lines]):
        with pytest.raises(SystemExit, match="single successful plan"):
            fresh.plan_from_log(broken)


def test_the_log_wait_says_it_is_waiting_and_keeps_saying_so(profile_file, tmp_path, monkeypatch, capsys):
    # azd new-user test 2026-10-15 #6: a new workspace took 5-15 min to ingest and the wait printed nothing.
    monkeypatch.setenv("FA_DEPLOY_CONFIG", str(profile_file))
    fresh = load("run_fresh_job_wait", ROOT / "deploy" / "stage" / "run_fresh_job.py")
    clock = [0.0]
    monkeypatch.setattr(fresh.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(fresh.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    answers = iter([{"customerId": "w"}, *[SystemExit("az failed: SEM0100")] * 4,
                    {"tables": [{"columns": [{"name": "Log"}], "rows": [["FA_W9_EXIT 0"]]}]}])

    def az(*_args):
        answer = next(answers)
        if isinstance(answer, SystemExit):
            raise answer
        return answer
    monkeypatch.setattr(fresh, "az", az)
    fresh.logs(tmp_path, "exec-1")
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "waiting for the job log in Log Analytics (a new workspace can take 5-15 minutes the first time)"
    assert out[1:] == ["still waiting for the job log (1 min so far)", "still waiting for the job log (2 min so far)",
                       "FA_W9_EXIT 0"]


def test_portal_bundle_chain_runs_offline_from_the_rebuilt_plan(profile_file, tmp_path, monkeypatch):
    plan, out, _ = planned_case(profile_file, tmp_path, monkeypatch)
    hooks = load("fa_azd_bundle", ROOT / "deploy" / "azd" / "fa_azd.py")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan.model_dump_json(indent=2) + "\n", encoding="utf-8")
    ctx = hooks.Context(values=values(), state_root=tmp_path / "state", run=hooks.default_runner)
    files = hooks.console_bundle(ctx, out, plan_path)
    # The three mounted files the container's args name, plus one config pair per exported Agent (A and B).
    assert {"console.json", "instructions.json", "agents.json"} <= set(files["configFiles"])
    assert len(files["configFiles"]) == 5
    assert all(re.fullmatch(r"[0-9a-f]{64}", files[key])
               for key in ("consoleConfigSha256", "instructionsSha256", "agentsSha256"))
    console = json.loads(files["configFiles"]["console.json"])
    assert CONSOLE_CLIENT in json.dumps(console) and console["human_oid"] == OWNER
    # Rerunning reuses the create-only outputs instead of exporting again.
    assert hooks.console_bundle(ctx, out, plan_path) == files


def test_an_azd_environment_always_arms_the_compatibility_gate(azd_profile):
    """ADR-0009 2d-ii (deploy/README.md#compat-gate): the API serves v2 only, so the gate is armed with no setting."""
    for extra in ({}, {"FA_COMPAT_GATE_ARMED": "false"}):  # the former opt-in is ignored
        assert azd_profile.build(values(**extra))["compatibility_gate_armed"] is True


def test_an_azd_environment_always_has_2d_i_on(azd_profile):
    """ADR-0009 2d-ii (src README#native-2di-switch): on from the first install; there is no setting to turn it off."""
    for extra in ({}, {"FA_NATIVE_2DI": "false"}):  # the former opt-in is ignored
        assert azd_profile.build(values(**extra))["api_environment"]["FA_NATIVE_SESSION_GUARD_ENFORCE"] == "1"
