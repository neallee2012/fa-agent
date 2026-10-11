"""deploy/stage/prepare_native_sessions.py prepares a seeded Agent once, never resends, and trusts only the read-back."""

import importlib.util
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
AGENT = {"name": "fa-v18-a", "kind": "assistant", "agent_id": "00000000-0000-0000-0000-00000000005f"}


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location(
        "offline_prepare_native", ROOT / "deploy" / "stage" / "prepare_native_sessions.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Reply:
    def __init__(self, value, status=200):
        self.value, self.status_code = value, status

    def raise_for_status(self):
        assert self.status_code == 200
        return self

    def json(self):
        return self.value


class _Api:
    """The three native-turn routes as a seeded Agent answers them."""

    def __init__(self, prepared=None, fail_prepare=None, observe_code=None, version="3"):
        self.prepared, self.fail_prepare, self.observe_code, self.version = prepared, fail_prepare, observe_code, version
        self.posts = []

    def get(self, path, headers=None):
        assert path.endswith("/environment")
        return _Reply({"agent_id": AGENT["agent_id"], "hosted_session_id": "session-a",
                       "hosted_agent_version": "3", "state": "needs_follow", "observed_sessions": []})

    def post(self, path, json=None, headers=None):
        action = path.rsplit("/", 1)[-1]
        self.posts.append(action)
        if action == "reconcile":
            if self.observe_code is not None:
                return _Reply({"error": {"code": self.observe_code}}, 403)
            if self.prepared is None:
                return _Reply({"error": {"code": "NATIVE_SESSION_CHANGED"}}, 409)
            return _Reply({"agent_id": AGENT["agent_id"], "preparation": self.prepared})
        assert action == "prepare" and set(json) == {"request_id"}
        if self.fail_prepare is not None:
            raise self.fail_prepare
        self.prepared = {"agent_id": AGENT["agent_id"], "request_id": json["request_id"],
                         "hosted_session_id": "session-a", "hosted_agent_version": self.version,
                         "state": "ready", "model_started": False, "original_work_replayed": False}
        return _Reply(self.prepared)


def test_dry_run_reads_and_observes_only(script, tmp_path):
    api = _Api()
    result, preparation = script.prepare(api, {}, tmp_path, AGENT, "r1", True)
    assert result["outcome"] == "dry-run" and result["would_prepare"] == "r1-fa-v18-a" and preparation is None
    assert api.posts == ["reconcile"] and list(tmp_path.iterdir()) == []


def test_prepares_once_and_a_rerun_only_reconciles(script, tmp_path):
    api = _Api()
    result, preparation = script.prepare(api, {}, tmp_path, AGENT, "r1", False)
    assert result["outcome"] == "http_200" and api.posts == ["reconcile", "prepare", "reconcile"]
    assert script.problems(preparation, AGENT, {"hosted_agent_version": "3"}) == []
    assert preparation["request_id"] == "r1-fa-v18-a" and result["model_calls"] == 0
    assert [path.name for path in tmp_path.iterdir()] == ["prepare-fa-v18-a-r1-intent.json"]
    again, _ = script.prepare(api, {}, tmp_path, AGENT, "r1", False)
    assert again["outcome"] == "intent-exists-reconciled" and api.posts.count("prepare") == 1


def test_an_existing_preparation_is_reported_not_prepared_again(script, tmp_path):
    earlier = {"agent_id": AGENT["agent_id"], "request_id": "portal-1", "hosted_session_id": "session-a",
               "hosted_agent_version": "3", "state": "ready", "model_started": False, "original_work_replayed": False}
    api = _Api(prepared=earlier)
    result, preparation = script.prepare(api, {}, tmp_path, AGENT, "r1", False)
    assert result["outcome"] == "already-prepared" and preparation == earlier
    assert api.posts == ["reconcile"] and list(tmp_path.iterdir()) == []


def test_an_unknown_outcome_is_never_resent(script, tmp_path):
    api = _Api(fail_prepare=httpx.ReadTimeout("lost"))
    result, preparation = script.prepare(api, {}, tmp_path, AGENT, "r1", False)
    assert result["outcome"] == "unknown:ReadTimeout" and preparation is None
    assert result["reconcile"] == {"http": 409, "error_code": "NATIVE_SESSION_CHANGED"}
    api.fail_prepare = None
    again, _ = script.prepare(api, {}, tmp_path, AGENT, "r1", False)
    assert again["outcome"] == "intent-exists-reconciled" and api.posts.count("prepare") == 1


def test_an_unclear_observation_stops_before_any_write(script, tmp_path):
    api = _Api(observe_code="BEHAVIOR_FORBIDDEN")
    with pytest.raises(SystemExit):
        script.prepare(api, {}, tmp_path, AGENT, "r1", False)
    assert api.posts == ["reconcile"] and list(tmp_path.iterdir()) == []


def test_a_preparation_for_another_version_or_with_a_model_call_does_not_count(script):
    ready = {"agent_id": AGENT["agent_id"], "hosted_agent_version": "3", "state": "ready",
             "model_started": False, "original_work_replayed": False}
    assert script.problems(ready, AGENT, {"hosted_agent_version": "3"}) == []
    assert script.problems(ready, AGENT, {"hosted_agent_version": "4"}) == ["hosted_agent_version"]
    assert script.problems({**ready, "model_started": True}, AGENT, {"hosted_agent_version": "3"}) == ["model_started"]
    assert script.problems(None, AGENT, {"hosted_agent_version": "3"}) == ["preparation"]
