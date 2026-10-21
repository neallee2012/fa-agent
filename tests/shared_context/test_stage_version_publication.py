"""Stage emits the same explicit publication and non-secret overlay as dev."""

import ast
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


def stage_module(filename="ci_deploy.py"):
    spec = importlib.util.spec_from_file_location("offline_stage_publication", ROOT / "deploy" / "stage" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if filename == "ci_deploy.py":  # ADR-0009 2d-ii deploys only with the 2d-i switch on
        module.STAGE["api_environment"]["FA_NATIVE_SESSION_GUARD_ENFORCE"] = "1"
        module.switch_preconditions = lambda *args, **kwargs: None  # its own tests: test_stage_ci_native_2di.py
    return module


def test_stage_a_b_and_api_enable_the_approved_hitl_groups_and_reader_catalog():
    stage = json.loads((ROOT / "deploy" / "stage" / "stage.json").read_text(encoding="utf-8"))
    wanted = {"FA_NATIVE_HITL_ENABLED": "1", "FA_NATIVE_HITL_TTL_SECONDS": "600", "FA_NATIVE_GROUPS_ENABLED": "1"}
    # ADR-0006: Neal 2026-10-01 chose the longest idle timeout so fewer turns pay a compute restart.
    assert stage["hosted_idle_timeout_seconds"] == 3600
    for key, value in wanted.items():
        assert stage["api_environment"][key] == value
        assert stage["console_environment"][key] == value
    # Only the Hosted process reads the Responses flag; the API and the console ask the served definition.
    assert "FA_RESPONSES_ENABLED" not in stage["api_environment"] and "FA_RESPONSES_ENABLED" not in stage["console_environment"]
    # README#typing-prewarm-stage: only the workbench reads FA_CONSOLE_PREWARM (the Hosted ENVs are pinned below).
    assert stage["console_environment"]["FA_CONSOLE_PREWARM"] == "1" and "FA_CONSOLE_PREWARM" not in stage["api_environment"]
    for agent in stage["agents"]:
        if agent["name"] in {"fa-stage-a", "fa-stage-b"}:
            # README#native-routines-stage: A and B offer propose_routine; the API owns the clock and the approvals.
            # README#native-model-switch: Neal 2026-10-14 turned mid-conversation model switching on for stage A and B.
            assert agent["hosted_environment"] == {**wanted, "FA_RESPONSES_ENABLED": "1",
                                                   "FA_NATIVE_ROUTINES_ENABLED": "1", "FA_NATIVE_ROUTINE_PROPOSALS": "1",
                                                   "FA_NATIVE_MODEL_SWITCH": "1"}
        else:
            assert not agent.get("hosted_environment")
    builtins = stage["api_environment"]["FA_CAPABILITY_BUILTINS"].split(",")
    assert "fa-delegate-reader-v1" in builtins and "fa-skill-resources-v1" in builtins


@pytest.mark.parametrize("responses", [None, "0"])
def test_groups_without_the_responses_lane_stop_before_any_build(monkeypatch, capsys, responses):
    module = stage_module()
    agents = json.loads(json.dumps(module.STAGE["agents"]))
    if responses is None:
        agents[0]["hosted_environment"].pop("FA_RESPONSES_ENABLED")
    else:
        agents[0]["hosted_environment"]["FA_RESPONSES_ENABLED"] = responses
    monkeypatch.setitem(module.STAGE, "agents", agents)
    with pytest.raises(SystemExit) as stopped:
        module.meta()
    assert "FA_NATIVE_GROUPS_ENABLED=1 needs FA_RESPONSES_ENABLED=1" in str(stopped.value)
    assert agents[0]["name"] in str(stopped.value) and capsys.readouterr().out == ""


def test_responses_flag_accepts_only_zero_or_one_and_groups_off_may_leave_it_off(monkeypatch):
    module = stage_module()
    agents = json.loads(json.dumps(module.STAGE["agents"]))
    agents[0]["hosted_environment"].update({"FA_NATIVE_GROUPS_ENABLED": "0", "FA_RESPONSES_ENABLED": "0"})
    monkeypatch.setitem(module.STAGE, "agents", agents)
    assert module.hosted_environment()[agents[0]["agent_id"]]["FA_RESPONSES_ENABLED"] == "0"
    agents[0]["hosted_environment"]["FA_RESPONSES_ENABLED"] = "true"
    with pytest.raises(SystemExit) as invalid:
        module.hosted_environment()
    assert "accepts only" in str(invalid.value)


def test_stage_sync_explicitly_publishes_and_passes_the_separate_environment_overlay():
    script = stage_module()._sync_script("registry.invalid/hosted@sha256:" + "a" * 64)
    tree = ast.parse(script)
    call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "main")
    args = ast.literal_eval(call.args[0])
    assert "--route" in args and "--hosted-environment" in args
    assert args[args.index("--idle-timeout-seconds") + 1] == "3600"
    assert "FA_NATIVE_HITL_ENABLED" in script and "FA_NATIVE_GROUPS_ENABLED" in script
    assert "FA_RESPONSES_ENABLED" in script


def test_stage_only_runs_automatic_follow_after_the_new_api_is_ready():
    module = stage_module()
    for final in (False, True):
        script = module._sync_script("registry.invalid/hosted@sha256:" + "a" * 64, follow=final)
        call = next(node for node in ast.walk(ast.parse(script)) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name) and node.func.id == "main")
        args = ast.literal_eval(call.args[0])
        assert ("--defer-follow" in args) is not final
    workflow = (ROOT / ".github" / "workflows" / "deploy-staging.yml").read_text(encoding="utf-8")
    assert workflow.index("ci_deploy.py api ") < workflow.index("ci_deploy.py follow ")
    assert workflow.count("ci_deploy.py follow ") == 1
    # The console is updated before follow, so an old bridge never reads the new build's Hosted replies.
    assert workflow.index("ci_deploy.py api ") < workflow.index("ci_deploy.py console ") < workflow.index(
        "ci_deploy.py follow ")


def test_fresh_initialization_only_publishes_until_the_api_is_installed():
    script = stage_module("run_fresh_job.py").script("sync", {}, "registry.invalid/hosted@sha256:" + "a" * 64)
    call = next(node for node in ast.walk(ast.parse(script)) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "main")
    args = ast.literal_eval(call.args[0])
    assert "--route" in args and "--defer-follow" in args


def test_hosted_only_changes_do_not_require_a_fake_core_code_change(monkeypatch):
    module = stage_module()
    secrets = [{"name": name} for name in module.api_secrets().values()]
    monkeypatch.setattr(module, "rest", lambda *_a, **_k: {
        "properties": {"latestReadyRevisionName": module.STAGE["api_app"] + "--aaaaaaa-r1-1",
                       # Stage API read back min 1 on 2026-10-08 (README#native-routines-stage guard).
                       "template": {"scale": {"minReplicas": 1}, "containers": [{"env": [
                           {"name": "FA_NATIVE_SESSION_GUARD_ENFORCE", "value": "1"}]}]},  # stage.json: 2d-i on
                       "configuration": {"secrets": secrets, "activeRevisionsMode": "Single"}}})
    monkeypatch.setattr(module.subprocess, "run", lambda *_a, **_k: SimpleNamespace(
        returncode=0, stdout="a" * 40, stderr=""))
    monkeypatch.setattr(module, "run", lambda *args: "" if any(":(glob)" in str(arg) for arg in args)
                        else "src/fa-shared-context-poc/Dockerfile.behavior-assistant-desktop\n")
    monkeypatch.setattr(module, "realtime_ready", lambda: None)
    module.guard()


def test_stage_realtime_settings_are_one_endpoint_for_api_console_and_the_follow_job(monkeypatch):
    module = stage_module()
    endpoint = "https://wps-fa-stage-demo.webpubsub.azure.com"
    assert module.realtime() == (endpoint, "fa")
    assert module.STAGE["console_environment"]["FA_REALTIME_ENDPOINT"] == endpoint
    for api, console in (({"FA_REALTIME_ENDPOINT": "https://evil.example.com"}, {}),
                         ({"FA_REALTIME_ENDPOINT": endpoint, "FA_REALTIME_HUB": "1hub"}, {}),
                         ({"FA_REALTIME_ENDPOINT": endpoint}, {"FA_REALTIME_ENDPOINT": "https://other.webpubsub.azure.com"}),
                         ({}, {"FA_REALTIME_ENDPOINT": endpoint})):
        monkeypatch.setitem(module.STAGE, "api_environment", api)
        monkeypatch.setitem(module.STAGE, "console_environment", console)
        with pytest.raises(SystemExit):
            module.realtime()
    monkeypatch.setitem(module.STAGE, "api_environment", {})
    monkeypatch.setitem(module.STAGE, "console_environment", {})
    assert module.realtime() is None and module.realtime_ready() is None


@pytest.mark.parametrize("state", ["ready", "missing", "local-auth", "no-role", "role-unreadable"])
def test_guard_reads_the_one_time_web_pubsub_resource_and_only_warns_when_it_is_missing(monkeypatch, capsys, state):
    module = stage_module()
    monkeypatch.setattr(module, "AZ", ("az",))
    resource = {"properties": {"disableLocalAuth": state != "local-auth",
                               "hostName": "wps-fa-stage-demo.webpubsub.azure.com"}}
    calls = []

    def run(args, **_kwargs):
        calls.append(tuple(args))
        if args[1] == "role":
            assert args[args.index("--role") + 1] == "12cf5a90-567b-43ae-8102-96cf46c7d9b4"
            holders = [] if state == "no-role" else ["api-principal"]
            return SimpleNamespace(returncode=1 if state == "role-unreadable" else 0,
                                   stdout=json.dumps(holders), stderr="AuthorizationFailed")
        return SimpleNamespace(returncode=1 if state == "missing" else 0, stdout=json.dumps(resource),
                               stderr="ResourceNotFound")
    monkeypatch.setattr(module.subprocess, "run", run)

    def rest(method, path, body=None):
        calls.append((method, path))
        return {"properties": {"principalId": "api-principal"}}
    monkeypatch.setattr(module, "rest", rest)
    assert module.realtime_ready() is None
    out = capsys.readouterr().out
    if state == "ready":
        assert "ready" in out and "::warning::" not in out
    else:
        assert out.startswith("::warning::") and "realtime.bicep" in out and "keeps polling" in out
    # Read-only: one resource GET, the identity GET, and the role list at the resource scope.
    assert calls[0][:4] == ("az", "rest", "--method", "GET") and "webPubSub/wps-fa-stage-demo" in calls[0][5]
    assert all(call[0] == "GET" or call[:4] in {("az", "rest", "--method", "GET"), ("az", "role", "assignment", "list")}
               for call in calls)
    if state in {"ready", "no-role", "role-unreadable"}:
        assert calls[-1][:4] == ("az", "role", "assignment", "list") and calls[-1][5].endswith("wps-fa-stage-demo")


def test_windows_operators_reach_az_through_the_cli_module(monkeypatch):
    # 2026-10-07: a Windows operator could not start the `az` .cmd shim from ci_deploy and patched a temporary copy.
    module = stage_module()
    monkeypatch.setenv("AZ_PYTHON", r"C:\cli\python.exe")
    assert module._az_prefix("posix") == ("az",)
    assert module._az_prefix("nt") == (r"C:\cli\python.exe", "-X", "utf8", "-IBm", "azure.cli")
    monkeypatch.setattr(module, "AZ", module._az_prefix("nt"))
    seen = []

    def fake(command, **kwargs):
        seen.append((tuple(command), kwargs.get("encoding")))
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")
    monkeypatch.setattr(module.subprocess, "run", fake)
    module.run("az", "account", "show")
    module.run("git", "status")
    assert seen == [((r"C:\cli\python.exe", "-X", "utf8", "-IBm", "azure.cli", "account", "show"), "utf-8"),
                    (("git", "status"), "utf-8")]


def test_follow_job_runs_with_the_api_realtime_settings_and_keeps_its_other_env(monkeypatch):
    module = stage_module()
    started = []

    def rest(method, path, body=None):
        if method == "POST":
            started.append(body)
            return {"name": "execution"}
        if "/executions/" in path:
            return {"properties": {"status": "Succeeded"}}
        if "/containerApps/" in path:  # the API app sync's switch check reads: stage.json has 2d-i on
            return {"properties": {"configuration": {"activeRevisionsMode": "Single"}, "template": {"containers": [
                {"env": [{"name": "FA_NATIVE_SESSION_GUARD_ENFORCE", "value": "1"}]}]}}}
        return {"properties": {"template": {"containers": [{"name": "sync", "resources": {}, "env": [
            {"name": "FA_REALTIME_ENDPOINT", "value": "stale"}, {"name": "KEEP", "secretRef": "kept"}]}]}}}
    monkeypatch.setattr(module, "rest", rest)
    monkeypatch.setattr(module, "_sync_script", lambda *_a, **_k: "pass")
    module.sync("api:ref", module.STAGE["registry"] + "/fa-behavior-assistant-desktop@sha256:" + "a" * 64, follow=True)
    env = started[0]["containers"][0]["env"]
    assert {"name": "KEEP", "secretRef": "kept"} in env
    assert [item for item in env if item["name"].startswith("FA_REALTIME_")] == [
        {"name": "FA_REALTIME_ENDPOINT", "value": "https://wps-fa-stage-demo.webpubsub.azure.com"},
        {"name": "FA_REALTIME_HUB", "value": "fa"}]


def test_seed_matches_the_complete_definition_not_an_older_same_build_flag_set():
    module = stage_module("seed_agents.py")
    agent = module.STAGE["agents"][0]
    desired = module.definition(agent, "registry.invalid/agent@sha256:" + "a" * 64)
    older = {**desired, "environment_variables": {**desired["environment_variables"], "FA_NATIVE_HITL_ENABLED": "0"}}
    values = [
        {"version": "4", "status": "active", "metadata": {"fa_build": "same-code"}, "definition": older},
        {"version": "5", "status": "active", "metadata": {"fa_build": "same-code"}, "definition": desired},
    ]
    project = SimpleNamespace(agents=SimpleNamespace(list_versions=lambda *_a, **_k: [
        SimpleNamespace(as_dict=lambda value=value: value) for value in values]))
    matched = module._of_definition(project, agent["name"], "same-code", module.definition_sha256(desired))
    assert [item["version"] for item in matched] == ["5"]
    assert desired["environment_variables"]["FA_NATIVE_HITL_ENABLED"] == "1"


def test_console_flag_overlay_preserves_existing_secret_references_and_identity(monkeypatch):
    module = stage_module()
    patched = []
    original = [{"name": "AZURE_CLIENT_ID", "value": "original-identity"},
                {"name": "FA_CONSOLE_CONFIGURATION", "secretRef": "original-config"}]

    def rest(method, _path, body=None):
        if method == "PATCH":
            patched.append(body)
        return {"properties": {"latestRevisionName": "offline-console--offline",
                               "template": {"containers": [{"name": "console", "image": "old:image",
                                                           "env": original, "resources": {}}]}}}

    monkeypatch.setattr(module, "rest", rest)
    module._update_container("containerApps", "offline-console", "new:image", "offline",
                             environment_overrides={"FA_NATIVE_HITL_ENABLED": "1", "FA_NATIVE_GROUPS_ENABLED": "1",
                                                    "FA_REALTIME_ENDPOINT": "https://wps-fa-stage-demo.webpubsub.azure.com",
                                                    "FA_CONSOLE_PREWARM": "1"})
    values = patched[0]["properties"]["template"]["containers"][0]["env"]
    assert all(item in values for item in original)
    assert {"name": "FA_NATIVE_HITL_ENABLED", "value": "1"} in values
    assert {"name": "FA_NATIVE_GROUPS_ENABLED", "value": "1"} in values
    assert {"name": "FA_REALTIME_ENDPOINT", "value": "https://wps-fa-stage-demo.webpubsub.azure.com"} in values
    assert {"name": "FA_CONSOLE_PREWARM", "value": "1"} in values
    for refused in ({"FA_REALTIME_ENDPOINT": "https://elsewhere.example.com"},
                    {"FA_CONSOLE_PREWARM": "true"}, {"FA_CONSOLE_PREWARM": "2"}, {"FA_CONSOLE_PREWARM": 1}):
        with pytest.raises(SystemExit):
            module._update_container("containerApps", "offline-console", "new:image", "offline",
                                     environment_overrides=refused)
    assert len(patched) == 1


@pytest.mark.parametrize("console", [{"FA_CONSOLE_PREWARM": "true"}, {"FA_NATIVE_HITL_ENABLED": "yes"},
                                     {"FA_UNKNOWN": "1"}])
def test_a_bad_console_flag_stops_the_run_before_any_build_or_write(monkeypatch, capsys, console):
    module = stage_module()
    monkeypatch.setitem(module.STAGE, "console_environment", {**module.STAGE["console_environment"], **console})
    with pytest.raises(SystemExit) as stopped:
        module.meta()
    assert "console overlay" in str(stopped.value) and capsys.readouterr().out == ""


@pytest.mark.parametrize("guard", ["0", None])
def test_a_profile_with_the_2di_switch_off_stops_the_run_before_any_build_or_write(monkeypatch, capsys, guard):
    """ADR-0009 2d-ii: the API sends and supports only foundry-native-v2, so the switch cannot be off (or left out)."""
    module = stage_module()
    environment = {key: value for key, value in module.STAGE["api_environment"].items()
                   if key != "FA_NATIVE_SESSION_GUARD_ENFORCE"}
    monkeypatch.setitem(module.STAGE, "api_environment",
                        environment if guard is None else {**environment, "FA_NATIVE_SESSION_GUARD_ENFORCE": guard})
    with pytest.raises(SystemExit) as stopped:
        module.meta()
    assert 'FA_NATIVE_SESSION_GUARD_ENFORCE="1" is required from ADR-0009 2d-ii' in str(stopped.value)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("status", [404, 403, 503])
def test_seed_only_treats_confirmed_missing_agent_as_empty(tmp_path, status):
    from azure.core.exceptions import AzureError

    module, created = stage_module("seed_agents.py"), []
    agent = module.STAGE["agents"][0]

    def listed(*_args, **_kwargs):
        if not created:
            error = AzureError("OFFLINE platform read")
            error.status_code = status
            raise error
        return [SimpleNamespace(as_dict=lambda: {
            **created[0], "version": "1", "status": "active"})]

    project = SimpleNamespace(agents=SimpleNamespace(
        list_versions=listed,
        create_version=lambda _name, body: created.append(body),
        get_version=lambda *_args: SimpleNamespace(as_dict=lambda: {
            **created[0], "version": "1", "status": "active"})))
    if status == 404:
        result = module.seed(project, tmp_path, agent, "offline:image", "a" * 64)
        assert result["version"] == "1" and len(created) == 1
    else:
        with pytest.raises(SystemExit):
            module.seed(project, tmp_path, agent, "offline:image", "a" * 64)
        assert not created


@pytest.fixture
def seed_readback(tmp_path, monkeypatch):
    """Synthetic SDK readback with the observed v18 service default; no Azure requests."""
    module = stage_module("seed_agents.py")
    agent = {**module.STAGE["agents"][0], "name": "fa-v18-a"}
    core, digest = "86f7b85ccf276941cc9e665504b25cfa66e837eef8fff28164e4352bbc1c9c07", "df4cd1c9" + "d" * 56
    image = module.STAGE["registry"] + "/fa-behavior-assistant-desktop@sha256:" + digest
    desired = module.definition(agent, image)
    body = {"definition": desired, "description": agent["description"], "metadata": {
        "fa_build": core, "fa_source": "stage-seed", module.METADATA_DEFINITION: module.definition_sha256(desired)}}
    observed = {**copy.deepcopy(body), "name": agent["name"], "version": "1", "status": "active"}
    observed["definition"]["container_protocol_versions"] = []
    state = SimpleNamespace(module=module, agent=agent, core=core, image=image, desired=desired, body=body,
                            listed=observed, observed=copy.deepcopy(observed), reads=[], out=tmp_path)

    def listed(name, **kwargs):
        state.reads.append(("list", name))
        return [SimpleNamespace(as_dict=lambda: copy.deepcopy(state.listed))]

    def get(name, version):
        state.reads.append(("get", name, version))
        return SimpleNamespace(as_dict=lambda: copy.deepcopy(state.observed))

    state.project = SimpleNamespace(agents=SimpleNamespace(
        list_versions=listed, get_version=get,
        create_version=lambda *_a, **_k: pytest.fail("an existing seed intent must never resend create")))
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    return state


def seed_intent(state, *, legacy=False, core=None):
    body = copy.deepcopy(state.body)
    if legacy:
        body["metadata"].pop(state.module.METADATA_DEFINITION)
    if core is not None:
        body["metadata"]["fa_build"] = core
    path = state.out / f"seed-{state.agent['name']}-intent.json"
    saved = {"at": "2026-10-07T00:00:00+00:00", "core": body["metadata"]["fa_build"], "image": state.image,
             "automatic_retry": False, "body_sha256": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()}
    path.write_text(json.dumps(saved), encoding="utf-8")
    return path, path.read_bytes()


@pytest.mark.parametrize("legacy", [False, True])
def test_seed_reads_active_service_normalized_definition_without_rewriting_or_resending(seed_readback, legacy):
    state = seed_readback
    path, original = seed_intent(state, legacy=legacy)
    if legacy:
        for item in (state.listed, state.observed):
            item["metadata"].pop(state.module.METADATA_DEFINITION)
    result = state.module.seed(state.project, state.out, state.agent, state.image, state.core)
    assert result["version"] == "1" and result["status"] == "active"
    assert result["definition"]["container_protocol_versions"] == []
    assert state.reads == [("list", state.agent["name"]), ("get", state.agent["name"], "1")]
    assert path.read_bytes() == original
    assert list(state.out.iterdir()) == [path]
    # The saved fingerprint is still the exact original definition, not a new normalization codec.
    raw = json.dumps(state.desired, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    assert state.module.definition_sha256(state.desired) == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("read", ["listed", "observed"])
@pytest.mark.parametrize("change", [
    {"container_configuration": {"image": "registry.invalid/another@sha256:" + "a" * 64}},
    {"environment_variables": {"FA_NATIVE_HITL_ENABLED": "0"}},
    {"cpu": "4"}, {"memory": "8Gi"},
    {"protocol_versions": [{"protocol": "responses", "version": "1.0.0"}]},
    {"session_configuration": {"idle_timeout_seconds": 120}},
    {"container_protocol_versions": [{"protocol": "invocations", "version": "2.0.0"}]},
    {"container_protocol_versions": None}, {"container_protocol_versions": {}},
    {"unknown_service_field": []},
])
def test_seed_still_rejects_meaningful_readback_changes(seed_readback, read, change):
    state = seed_readback
    path, original = seed_intent(state)
    getattr(state, read)["definition"].update(copy.deepcopy(change))
    message = "version not settled" if read == "listed" else "exact seed definition did not read back"
    with pytest.raises(SystemExit, match=message):
        state.module.seed(state.project, state.out, state.agent, state.image, state.core)
    assert path.read_bytes() == original and list(state.out.iterdir()) == [path]


@pytest.mark.parametrize("core", ["g" * 64, "a" * 63, "a" * 65, "sha256:" + "a" * 64, "df4cd1c9" + "d" * 56])
def test_seed_cli_rejects_invalid_core_or_image_digest_before_credentials_and_intents(
        seed_readback, monkeypatch, core):
    state = seed_readback
    out = state.out / "not-created"
    monkeypatch.setattr(state.module, "AzureCliCredential",
                        lambda **_k: pytest.fail("invalid --core must stop before credentials"))
    monkeypatch.setattr(state.module.sys, "argv", [
        "seed_agents.py", "--out", str(out), "--image", state.image, "--core", core])
    with pytest.raises(SystemExit, match="code identity"):
        state.module.main()
    assert not out.exists() and state.reads == []


def test_corrected_core_cannot_replace_an_existing_seed_intent(seed_readback):
    state = seed_readback
    path, original = seed_intent(state, core=state.image.split("@sha256:")[1])
    with pytest.raises(SystemExit, match="original seed intent differs"):
        state.module.seed(state.project, state.out, state.agent, state.image, state.core)
    assert path.read_bytes() == original and state.reads == []


def test_stage_sync_pins_the_code_identity_not_the_hosted_manifest_digest():
    from shared_context.behavior_code_identity import code_sha256

    module = stage_module()
    image = "registry.invalid/hosted@sha256:" + "d" * 64
    script = module._sync_script(image)
    call = next(node for node in ast.walk(ast.parse(script)) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "main")
    args = ast.literal_eval(call.args[0])
    assert args[args.index("--core") + 1] == module.core() == code_sha256()
    assert args[args.index("--core") + 1] != image.split("@sha256:")[1]
