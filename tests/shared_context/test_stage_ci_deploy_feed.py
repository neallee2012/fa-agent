"""deploy/stage/ci_deploy.py feed reader steps and secret ENV references, against a fake `az rest` (offline)."""

import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = "A" * 43 + "="
FEED_KEYS = ("feed_app", "feed_environment", "api_secret_environment")


def load_ci():
    spec = importlib.util.spec_from_file_location("offline_stage_ci_deploy", ROOT / "deploy" / "stage" / "ci_deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STAGE = copy.deepcopy(module.STAGE)
    return module


@pytest.fixture
def ci(monkeypatch):
    module = load_ci()
    # Each test starts without the feed settings and adds only what it checks; the real values are checked below.
    for key in FEED_KEYS:
        module.STAGE.pop(key, None)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    return module


def fake_app(ci, monkeypatch, *, env, secrets=()):
    state = {"env": copy.deepcopy(env), "patches": []}

    def rest(method, path, body=None):
        if method == "PATCH":
            state["patches"].append(copy.deepcopy(body))
            state["env"] = body["properties"]["template"]["containers"][0]["env"]
            state["revision"] = body["properties"]["template"]["revisionSuffix"]
            return None
        if "/revisions/" in path:
            return {"properties": {"runningState": "Running", "healthState": "Healthy"}}
        name = path.split("/containerApps/")[1].split("?")[0]
        return {"properties": {
            "latestRevisionName": f"{name}--{state.get('revision', 'old')}",
            "configuration": {"secrets": [{"name": item} for item in secrets]},
            "template": {"containers": [{"name": "app", "image": "old", "env": copy.deepcopy(state["env"]),
                                         "resources": {"cpu": 0.25}}]}}}

    monkeypatch.setattr(ci, "rest", rest)
    return state


def test_without_feed_app_the_feed_image_is_skipped_and_prints_no_reference(ci, capsys):
    assert "feed_app" not in ci.STAGE and ci.feed_config() is None and ci.api_secrets() == {}
    ci.build("feed")
    out = capsys.readouterr()
    assert out.out == "" and "skipped" in out.err


def test_the_stage_json_feed_settings_pass_the_format_checks():
    # feed_config() and api_secrets() stop (SystemExit) on any malformed value, so meta passes with this stage.json.
    module = load_ci()
    assert module.feed_config() is not None and module.api_secrets()


@pytest.mark.parametrize("settings", [
    {"feed_environment": {"FEED_MCP_PUBLIC_KEY": PUBLIC}},
    {"feed_app": "ca-fa-feed-stage"},
    {"feed_app": "ca-fa-feed-stage", "feed_environment": {"FEED_MCP_PUBLIC_KEY": "short"}},
    {"feed_app": "ca-fa-feed-stage", "feed_environment": {"FEED_MCP_PUBLIC_KEY": PUBLIC, "OTHER": "1"}},
    {"feed_app": "Bad_App", "feed_environment": {"FEED_MCP_PUBLIC_KEY": PUBLIC}},
    {"api_secret_environment": {"FA_FEED_MCP_SIGNING_KEY": "Bad_Name"}},
    {"api_secret_environment": {"PYTHONUTF8": "fa-feed-mcp-signing-key"}},
    {"api_secret_environment": ["fa-feed-mcp-signing-key"]},
])
def test_meta_stops_on_malformed_feed_or_secret_settings_before_any_build(ci, monkeypatch, settings):
    ci.STAGE.update(settings)
    monkeypatch.setattr(ci, "release", lambda: "test")
    with pytest.raises(SystemExit):
        ci.meta()


def test_guard_stops_when_a_declared_api_secret_is_missing(ci, monkeypatch):
    ci.STAGE["api_secret_environment"] = {"FA_FEED_MCP_SIGNING_KEY": "fa-feed-mcp-signing-key"}
    fake_app(ci, monkeypatch, env=[{"name": "PYTHONUTF8", "value": "1"}], secrets=("other",))
    with pytest.raises(SystemExit) as stopped:
        ci.guard()
    assert "fa-feed-mcp-signing-key" in str(stopped.value)


def test_the_feed_app_gets_its_image_and_only_its_public_key(ci, monkeypatch):
    ci.STAGE.update(feed_app="ca-fa-feed-stage", feed_environment={"FEED_MCP_PUBLIC_KEY": PUBLIC})
    state = fake_app(ci, monkeypatch, env=[{"name": "FEED_MCP_PUBLIC_KEY", "value": "old"}])
    ci.feed("registry/fa-feed-mcp@sha256:" + "a" * 64)
    [patch] = state["patches"]
    container = patch["properties"]["template"]["containers"][0]
    assert container["image"].endswith("a" * 64) and container["env"] == [{"name": "FEED_MCP_PUBLIC_KEY", "value": PUBLIC}]


def test_the_workflow_updates_the_feed_app_only_after_follow():
    workflow = (ROOT / ".github" / "workflows" / "deploy-staging.yml").read_text(encoding="utf-8")
    assert workflow.index("ci_deploy.py follow") < workflow.index("ci_deploy.py feed")
    assert "if: steps.feed.outputs.ref != ''" in workflow


def test_api_secret_env_is_a_reference_to_an_existing_app_secret(ci, monkeypatch):
    plain = {"PYTHONUTF8": "1"}
    state = fake_app(ci, monkeypatch, env=[{"name": "PYTHONUTF8", "value": "1"},
                                           {"name": "FA_FEED_MCP_SIGNING_KEY", "secretRef": "fa-feed-mcp-signing-key"}],
                     secrets=("fa-feed-mcp-signing-key",))
    # Undeclared, a deployed secret reference is never dropped silently.
    with pytest.raises(SystemExit):
        ci._update_container("containerApps", "ca-api", "img", "s1", environment=plain)
    ci._update_container("containerApps", "ca-api", "img", "s2", environment=plain,
                         secret_environment={"FA_FEED_MCP_SIGNING_KEY": "fa-feed-mcp-signing-key"})
    assert state["env"] == [{"name": "PYTHONUTF8", "value": "1"},
                            {"name": "FA_FEED_MCP_SIGNING_KEY", "secretRef": "fa-feed-mcp-signing-key"}]
    patches = len(state["patches"])
    for secrets in ({"FA_FEED_MCP_SIGNING_KEY": "missing-secret"}, {"PYTHONUTF8": "fa-feed-mcp-signing-key"},
                    {"FA_FEED_MCP_SIGNING_KEY": "Bad_Name"}):
        with pytest.raises(SystemExit):
            ci._update_container("containerApps", "ca-api", "img", "s3", environment=plain, secret_environment=secrets)
    assert len(state["patches"]) == patches


def test_without_secret_env_the_api_update_is_unchanged(ci, monkeypatch):
    state = fake_app(ci, monkeypatch, env=[{"name": "PYTHONUTF8", "value": "1"}])
    ci._update_container("containerApps", "ca-api", "img", "s1", environment={"PYTHONUTF8": "1"})
    assert state["env"] == [{"name": "PYTHONUTF8", "value": "1"}]
