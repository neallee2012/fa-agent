"""deploy/stage/ci_deploy.py safety checks, offline: the rollback guard, wait-loop read retries and the API image core.

A throwaway git repository stands in for the checkout; `az` and `docker` are fakes. Standard library + pytest only.
"""

import copy
import importlib.util
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def load_ci():
    spec = importlib.util.spec_from_file_location("offline_stage_ci_deploy_safety",
                                                  ROOT / "deploy" / "stage" / "ci_deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STAGE = copy.deepcopy(module.STAGE)
    return module


@pytest.fixture
def ci(monkeypatch):
    module = load_ci()
    for key in ("feed_app", "feed_environment", "api_secret_environment"):
        module.STAGE.pop(key, None)
    # ADR-0009 2d-ii deploys only with the 2d-i switch on; its own preconditions have their own tests.
    module.STAGE["api_environment"]["FA_NATIVE_SESSION_GUARD_ENFORCE"] = "1"
    monkeypatch.setattr(module, "switch_preconditions", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "api_routines", lambda: False)  # guard's replica check has its own tests
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(module, "realtime_ready", lambda: None)
    return module


# --- guard: the deploying commit must contain the serving one ----------------------------------------------------


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=GIT_ENV).stdout.strip()


@pytest.fixture
def history(tmp_path):
    """A checkout with two commits on main: older -> newer."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commits = []
    for name in ("older", "newer"):
        (repo / f"{name}.txt").write_text(name, encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", name)
        commits.append(git(repo, "rev-parse", "HEAD"))
    return repo, commits


def serving(ci, monkeypatch, repo, revision):
    """The stage API app as stage.json leaves it: 2d-i on (FA_NATIVE_SESSION_GUARD_ENFORCE), single revision mode.
    The turn-on's own checks are in test_stage_ci_native_2di.py."""
    monkeypatch.setattr(ci, "ROOT", repo)
    env = [{"name": ci.SESSION_GUARD, "value": "1"}]
    monkeypatch.setattr(ci, "rest", lambda method, path, body=None: {"properties": {
        "latestReadyRevisionName": f"{ci.STAGE['api_app']}--{revision}", "template": {"containers": [{"env": env}]},
        "configuration": {"activeRevisionsMode": "Single"}}})


@pytest.mark.parametrize("serving_index", [0, 1])
def test_guard_passes_when_this_commit_contains_the_serving_one(ci, monkeypatch, capsys, history, serving_index):
    repo, commits = history
    serving(ci, monkeypatch, repo, f"{commits[serving_index][:7]}-r12-1")
    ci.guard()
    assert f"serving {commits[serving_index][:12]}" in capsys.readouterr().out


def test_guard_stops_a_rerun_that_would_deploy_an_older_commit(ci, monkeypatch, history):
    repo, commits = history
    git(repo, "checkout", "-q", commits[0])  # an old run re-run: its checkout is the older commit
    serving(ci, monkeypatch, repo, f"{commits[1][:7]}-r13-1")
    with pytest.raises(SystemExit) as stopped:
        ci.guard()
    assert "roll the environment back" in str(stopped.value) and commits[1][:12] in str(stopped.value)


def test_guard_stops_on_a_commit_from_another_history(ci, monkeypatch, history):
    repo, commits = history
    git(repo, "checkout", "-q", "--orphan", "other")
    git(repo, "commit", "-q", "-m", "unrelated")
    serving(ci, monkeypatch, repo, f"{commits[1][:7]}-r13-1")
    with pytest.raises(SystemExit, match="roll the environment back"):
        ci.guard()


def test_guard_stops_when_a_script_named_commit_is_missing(ci, monkeypatch, history):
    repo, _ = history
    monkeypatch.delenv("FA_ALLOW_ROLLBACK", raising=False)
    serving(ci, monkeypatch, repo, "1234567-r9-1")
    with pytest.raises(SystemExit, match="not in this checkout's history"):
        ci.guard()


def test_stage_workflow_never_sets_the_rollback_override():
    workflow = ROOT / ".github" / "workflows" / "deploy-staging.yml"
    if not workflow.exists():
        pytest.skip("no .github/workflows in this copy")
    assert "FA_ALLOW_ROLLBACK" not in workflow.read_text(encoding="utf-8")


def test_guard_stop_names_the_one_run_override(ci, monkeypatch, history):
    repo, _ = history
    serving(ci, monkeypatch, repo, "1234567-r9-1")
    monkeypatch.delenv("FA_ALLOW_ROLLBACK", raising=False)
    with pytest.raises(SystemExit, match="FA_ALLOW_ROLLBACK=true"):
        ci.guard()


@pytest.mark.parametrize("value", ["1", "yes", "True", ""])
def test_guard_override_needs_exactly_true(ci, monkeypatch, history, value):
    repo, _ = history
    serving(ci, monkeypatch, repo, "1234567-r9-1")
    monkeypatch.setenv("FA_ALLOW_ROLLBACK", value)
    with pytest.raises(SystemExit, match="not in this checkout's history"):
        ci.guard()


def test_guard_override_warns_and_continues_when_the_serving_commit_is_gone(ci, monkeypatch, capsys, history):
    repo, _ = history
    serving(ci, monkeypatch, repo, "1234567-r9-1")
    monkeypatch.setenv("FA_ALLOW_ROLLBACK", "true")
    ci.guard()
    out = capsys.readouterr().out
    assert out.startswith("::warning::") and "1234567" in out and "FA_ALLOW_ROLLBACK=true" in out


def test_guard_override_does_not_excuse_a_commit_that_is_present_but_not_contained(ci, monkeypatch, history):
    repo, commits = history
    git(repo, "checkout", "-q", commits[0])
    serving(ci, monkeypatch, repo, f"{commits[1][:7]}-r13-1")
    monkeypatch.setenv("FA_ALLOW_ROLLBACK", "true")
    with pytest.raises(SystemExit, match="roll the environment back"):
        ci.guard()


@pytest.mark.parametrize("revision", ["manualfix", "0000001", "k3j2h1x", "local-r0-1"])
def test_guard_only_warns_on_a_revision_this_script_did_not_name(ci, monkeypatch, capsys, history, revision):
    repo, _ = history
    serving(ci, monkeypatch, repo, revision)
    ci.guard()
    out = capsys.readouterr().out
    assert out.startswith("::warning::") and "not named by this deploy script" in out


def test_guard_refuses_a_live_only_env_before_anything_else_runs(ci, monkeypatch):
    """The same refusal as api's update, before build/sync publish Hosted (2026-10-22: dev's retired WebIQ ENV)."""
    calls = []
    env = [{"name": ci.SESSION_GUARD, "value": "1"}, {"name": ci.API_CONFIG_ENV, "value": "{}"},
           {"name": "FA_CAPABILITY_SECRET_WEBIQ_API_KEY", "secretRef": "webiq-api-key"}]
    monkeypatch.setattr(ci, "rest", lambda method, path, body=None: calls.append((method, path)) or {"properties": {
        "latestReadyRevisionName": f"{ci.STAGE['api_app']}--1234567-r9-1", "template": {"containers": [{"env": env}]}}})
    for later in ("api_routines", "feed_config", "serving_commit", "switch_preconditions", "realtime_ready"):
        monkeypatch.setattr(ci, later, lambda *args, **kwargs: pytest.fail("ran after the refusal"))
    with pytest.raises(SystemExit) as stopped:
        ci.guard()
    assert str(stopped.value) == (f"{ci.STAGE['api_app']}: deployed ENV ['FA_CAPABILITY_SECRET_WEBIQ_API_KEY'] is not "
                                  "in the deployment profile; add it first")
    assert [method for method, _ in calls] == ["GET"]
    # A declared secret ENV and the installed configuration are known names: guard goes on to its next check.
    ci.STAGE["api_secret_environment"] = {"FA_CAPABILITY_SECRET_WEBIQ_API_KEY": "webiq-api-key"}
    monkeypatch.setattr(ci, "_require_secrets", lambda *args: None)

    def next_check():
        raise SystemExit("next check")

    monkeypatch.setattr(ci, "api_routines", next_check)
    with pytest.raises(SystemExit, match="^next check$"):
        ci.guard()


# --- wait loops: a failed read is retried, a write never is ------------------------------------------------------


class FlakyArm:
    """`rest` fake: the first `failures` wait-loop GETs fail like `az rest` does (SystemExit from run())."""

    def __init__(self, failures, *, app="ca-api"):
        self.failures, self.calls, self.app = failures, [], app

    @property
    def patched(self):
        return any(method == "PATCH" for method, _ in self.calls)

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        if method != "GET":
            return {"name": "exec-1"} if "/start?" in path else None
        if ("/executions/" in path or "/revisions/" in path or self.patched) and self.failures:
            self.failures -= 1
            raise SystemExit("command failed (1): az rest --method GET")
        if "/executions/" in path:
            return {"properties": {"status": "Succeeded"}}
        if "/revisions/" in path:
            return {"properties": {"runningState": "Running", "healthState": "Healthy"}}
        container = {"name": "app", "image": "old", "env": [], "resources": {"cpu": 0.25}}
        return {"properties": {"latestRevisionName": f"{self.app}--s1" if self.patched else f"{self.app}--old",
                               "configuration": {}, "template": {"containers": [container]}}}


def wait_step(ci, step):
    if step == "revision":
        return ci._wait_revision("ca-api", "ca-api--s1")
    if step == "update":
        return ci._update_container("containerApps", "ca-api", "img", "s1")
    return ci._run_job("img", "print(1)")


@pytest.mark.parametrize("failures", [1, 3])
@pytest.mark.parametrize("step", ["revision", "update", "job"])
def test_wait_loops_retry_a_failed_read(ci, monkeypatch, step, failures):
    arm = FlakyArm(failures)
    monkeypatch.setattr(ci, "rest", arm)
    result = wait_step(ci, step)
    assert result == {"revision": None, "update": "ca-api--s1", "job": ("Succeeded", "exec-1")}[step]
    assert arm.failures == 0
    # The one write (PATCH or job start) is sent once.
    assert sum(method != "GET" for method, _ in arm.calls) == (0 if step == "revision" else 1)


@pytest.mark.parametrize("step", ["revision", "update", "job"])
def test_wait_loops_stop_after_three_retries(ci, monkeypatch, step):
    arm = FlakyArm(4)
    monkeypatch.setattr(ci, "rest", arm)
    with pytest.raises(SystemExit, match="read 4 times"):
        wait_step(ci, step)
    gets = sum(method == "GET" for method, _ in arm.calls)
    assert gets == (4 if step == "revision" else 5)  # update/job read the resource once before their write
    assert sum(method != "GET" for method, _ in arm.calls) == (0 if step == "revision" else 1)


def test_a_failed_write_is_never_retried(ci, monkeypatch):
    calls = []

    def rest(method, path, body=None):
        calls.append(method)
        if method == "PATCH":
            raise SystemExit("command failed (1): az rest --method PATCH")
        return {"properties": {"latestRevisionName": "ca-api--old", "configuration": {},
                               "template": {"containers": [{"name": "app", "image": "old", "env": [],
                                                            "resources": {}}]}}}
    monkeypatch.setattr(ci, "rest", rest)
    with pytest.raises(SystemExit, match="PATCH"):
        ci._update_container("containerApps", "ca-api", "img", "s1")
    assert calls == ["GET", "PATCH"]


# --- build api: the core is read from the built image, not from the build log ------------------------------------


def fake_docker(ci, monkeypatch, *, image_core, build_log="#5 CACHED\n", run_code=0):
    """docker/az fake; returns the commands run. `docker build` prints a log without FA_DEPLOYED_CORE (cache hit)."""
    commands = []

    def run(command, **_kwargs):
        command = list(command)
        commands.append(command)
        if command[:2] == ["docker", "build"]:
            return SimpleNamespace(returncode=0, stdout=build_log, stderr="")
        if command[:2] == ["docker", "run"]:
            return SimpleNamespace(returncode=run_code, stdout=f"{image_core}\n", stderr="")
        if command[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=f"{ci.STAGE['registry']}/fa-behavior-api@sha256:{'b' * 64}\n",
                                   stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")  # az acr login, docker push
    monkeypatch.setattr(ci.subprocess, "run", run)
    monkeypatch.setattr(ci, "AZ", ("az",))
    return commands


def test_api_build_reads_the_core_from_the_image_when_docker_used_its_cache(ci, monkeypatch, capsys):
    commands = fake_docker(ci, monkeypatch, image_core=ci.core())
    ci.build("api")
    assert capsys.readouterr().out.strip().endswith("@sha256:" + "b" * 64)
    [probe] = [command for command in commands if command[:2] == ["docker", "run"]]
    assert "--rm" in probe and probe[probe.index("--network") + 1] == "none"
    assert probe[probe.index("--entrypoint") + 1] == "python" and "code_sha256" in probe[-1]
    assert commands.index(probe) < next(i for i, c in enumerate(commands) if c[:2] == ["docker", "push"])


@pytest.mark.parametrize("image_core, run_code", [("c" * 64, 0), ("", 0), ("not-a-core", 0), (None, 125)])
def test_api_build_refuses_to_push_another_or_unreadable_core(ci, monkeypatch, image_core, run_code):
    log = f"FA_DEPLOYED_CORE={ci.core()}\n"  # even a matching log line is not trusted over the image
    commands = fake_docker(ci, monkeypatch, image_core=image_core or "", build_log=log, run_code=run_code)
    with pytest.raises(SystemExit, match="refusing to push"):
        ci.build("api")
    assert not any(command[:2] == ["docker", "push"] for command in commands)


def test_a_core_mismatch_message_prints_both_identities(ci, monkeypatch):
    fake_docker(ci, monkeypatch, image_core="c" * 64)
    with pytest.raises(SystemExit) as stopped:
        ci.build("api")
    assert "c" * 64 in str(stopped.value) and ci.core() in str(stopped.value)
