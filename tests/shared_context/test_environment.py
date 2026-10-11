from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from shared_context.errors import Problem
from test_hosted import client

A_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
B_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture
def private_home(monkeypatch):
    home = Path(__file__).resolve().parent / f".environment-fixture-{uuid4().hex}"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FOUNDRY_AGENT_ID", A_ID)
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", "fa-context-a")
    monkeypatch.setenv("FOUNDRY_AGENT_VERSION", "5")
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", "synthetic-session-a")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(os, "geteuid", lambda: 10001, raising=False)
    try:
        yield home
    finally:
        shutil.rmtree(home)


def request(probe_action, **fields):
    return {
        "operation": "environment", "action": probe_action,
        "target_agent_id": A_ID, "run_id": "m1-example", **fields,
    }


@pytest.fixture
def external_tools(monkeypatch):
    from shared_context import environment

    observed = {"profiles": [], "seed_markers": [], "overrides": {}, "tool_calls": []}

    def browser(command, *, persistent_profile, seed_profile_marker):
        observed["profiles"].append(persistent_profile)
        observed["seed_markers"].append(seed_profile_marker)
        preferences = json.loads((persistent_profile / "Default" / "Preferences").read_text())
        cookie = persistent_profile / "synthetic-cookie"
        if seed_profile_marker is not None:
            cookie.write_text(seed_profile_marker)
        return {
            "probe_status": "passed", "cleanup": {"success": True},
            "profile_closed_cleanly": True,
            "accept_language": preferences["intl"]["accept_languages"] + ",en;q=0.9",
            "profile_marker_sha256": hashlib.sha256(cookie.read_bytes()).hexdigest(),
            "versions": {"chromium": "synthetic-1", "openbox": "synthetic-2", "xvfb": "synthetic-3"},
            **observed["overrides"],
        }

    def run(args, **kwargs):
        observed["tool_calls"].append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="1\n", stderr="")

    monkeypatch.setattr(environment, "run_gui_probe", browser)
    monkeypatch.setattr(environment.subprocess, "run", run)
    return observed


def test_private_environment_survives_a_new_client_without_recreating_files(private_home, monkeypatch, external_tools):
    first, credential = client(lambda _: pytest.fail("M1 does not call Azure data or models"))
    seeded = first.dispatch(request("seed", nonce="amber-v1"))["outcome"]
    expected_hash = hashlib.sha256(b"m1 synthetic work: amber-v1\n").hexdigest()
    assert seeded["files"]["work.txt"]["sha256"] == expected_hash
    assert seeded["profile"]["accept_language"].startswith("zh-TW")
    assert seeded["binding"]["agent_id"] == A_ID
    assert seeded["settings_applied"]["python_utf8"] == 1
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", "synthetic-session-a")
    second, _ = client(lambda _: pytest.fail("M1 does not call Azure data or models"))
    recalled = second.dispatch(request(
        "read", checkpoint_sha256=seeded["checkpoint_sha256"],
    ))["outcome"]
    assert recalled["files"] == seeded["files"]
    assert recalled["checkpoint_sha256"] == seeded["checkpoint_sha256"]
    assert recalled["profile"]["accept_language"].startswith("zh-TW")
    assert recalled["profile"]["marker_sha256"] == seeded["profile"]["marker_sha256"]
    assert len(external_tools["profiles"]) == 2 and external_tools["profiles"][0] == external_tools["profiles"][1]
    assert external_tools["profiles"][0].is_relative_to(private_home)
    assert external_tools["seed_markers"][0] and external_tools["seed_markers"][1] is None
    for args, kwargs in external_tools["tool_calls"]:
        assert kwargs["env"]["PYTHONUTF8"] == "1"
        assert set(kwargs["env"]) == {"PATH", "LANG", "PYTHONUTF8"}
        assert kwargs["timeout"] == 10 and kwargs["check"] is True
    assert recalled["hosted_continuity_validated"] is False
    assert credential.scopes == []


@pytest.mark.parametrize("extra", [
    {"path": "/etc/passwd"}, {"command": "anything"}, {"url": "https://example.com"},
    {"actor": "agent-a"}, {"run_id": "../escape"}, {"include_screenshot": "true"},
    {"target_agent_id": "not-a-guid"}, {"nonce": "reseed"}, {"action": "restore"},
])
def test_environment_rejects_caller_paths_identity_injection_and_unsupported_actions(private_home, extra):
    hosted, credential = client(lambda _: pytest.fail("no Azure call"))
    with pytest.raises(Problem) as exc:
        hosted.dispatch(request("inspect", **extra))
    assert exc.value.status == 422 and credential.scopes == []


def test_other_agent_is_rejected_before_home_access(private_home, monkeypatch):
    from shared_context import environment
    hosted, _ = client(lambda _: pytest.fail("no Azure call"))
    monkeypatch.setattr(environment, "Path", lambda _: pytest.fail("unauthorized request must not inspect HOME"))
    with pytest.raises(Problem) as exc:
        hosted.dispatch(request("inspect", target_agent_id=B_ID))
    assert exc.value.status == 403


@pytest.mark.parametrize("variable", [
    "FOUNDRY_AGENT_ID", "FOUNDRY_AGENT_NAME", "FOUNDRY_AGENT_VERSION", "FOUNDRY_AGENT_SESSION_ID", "HOME",
])
def test_environment_never_falls_back_when_platform_binding_is_missing(private_home, monkeypatch, variable):
    monkeypatch.delenv(variable)
    hosted, _ = client(lambda _: pytest.fail("no Azure call"))
    with pytest.raises(Problem) as exc:
        hosted.dispatch(request("inspect"))
    assert exc.value.status == 503


def test_environment_rejects_root_and_inspection_does_not_write(private_home, monkeypatch):
    hosted, _ = client(lambda _: pytest.fail("no Azure call"))
    inspected = hosted.dispatch(request("inspect"))["outcome"]
    assert inspected["home_write_validated"] is False and inspected["seeded"] is False
    assert list(private_home.iterdir()) == []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(Problem) as exc:
        hosted.dispatch(request("seed", nonce="amber"))
    assert exc.value.status == 503 and exc.value.body["error"]["code"] == "ENVIRONMENT_RUNTIME_UNAVAILABLE"


def test_private_home_permission_failure_is_blocked_without_temporary_fallback(private_home, monkeypatch, external_tools):
    hosted, _ = client(lambda _: pytest.fail("no Azure call"))

    def denied(*args, **kwargs):
        raise PermissionError(13, "synthetic platform HOME ownership")

    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(Problem) as exc:
        hosted.dispatch(request("seed", nonce="amber"))
    assert exc.value.status == 503 and exc.value.body["error"]["code"] == "ENVIRONMENT_HOME_NOT_WRITABLE"
    assert external_tools["profiles"] == []


def test_duplicate_seed_is_not_retried_and_read_requires_an_independent_receipt(private_home, external_tools):
    hosted, _ = client(lambda _: pytest.fail("no Azure call"))
    hosted.dispatch(request("seed", nonce="amber"))
    for payload, status, code in [
        (request("seed", nonce="changed"), 409, "ENVIRONMENT_ALREADY_SEEDED"),
        (request("read", checkpoint_sha256="0" * 64), 409, "ENVIRONMENT_CHECKPOINT_CHANGED"),
        (request("read"), 422, "INVALID_REQUEST"),
    ]:
        with pytest.raises(Problem) as exc:
            hosted.dispatch(payload)
        assert exc.value.status == status and exc.value.body["error"]["code"] == code
    assert len(external_tools["profiles"]) == 1


@pytest.mark.parametrize("filename", ["work.txt", "tool-settings.json", "checkpoint.json"])
def test_changed_private_files_are_not_recreated_on_read(private_home, external_tools, filename):
    hosted, _ = client(lambda _: pytest.fail("no Azure call"))
    seeded = hosted.dispatch(request("seed", nonce="amber"))["outcome"]
    path = Path(seeded["private_root"]) / filename
    path.write_text("changed")
    with pytest.raises(Problem) as exc:
        hosted.dispatch(request("read", checkpoint_sha256=seeded["checkpoint_sha256"]))
    assert exc.value.status == 409 and path.read_text() == "changed"
    assert len(external_tools["profiles"]) == 1


@pytest.mark.parametrize("preferences", [{"intl": []}, {"intl": {"accept_languages": "en-US"}}])
def test_changed_browser_preference_is_not_silently_reseeded(private_home, external_tools, preferences):
    hosted, _ = client(lambda _: pytest.fail("no Azure call"))
    seeded = hosted.dispatch(request("seed", nonce="amber"))["outcome"]
    path = Path(seeded["profile"]["path"]) / "Default" / "Preferences"
    path.write_text(json.dumps(preferences))
    with pytest.raises(Problem) as exc:
        hosted.dispatch(request("read", checkpoint_sha256=seeded["checkpoint_sha256"]))
    assert exc.value.body["error"]["code"] == "ENVIRONMENT_PROFILE_CHANGED"
    assert json.loads(path.read_text()) == preferences


@pytest.mark.parametrize("override,code", [
    ({"accept_language": "en-US"}, "ENVIRONMENT_PROFILE_NOT_APPLIED"),
    ({"profile_marker_sha256": None}, "ENVIRONMENT_COOKIE_MISSING"),
    ({"versions": {"chromium": "changed", "openbox": "synthetic-2", "xvfb": "synthetic-3"}}, "ENVIRONMENT_TOOLS_CHANGED"),
    ({"probe_status": "failed", "error": {"code": "SYNTHETIC_GUI_FAILURE"}}, "ENVIRONMENT_BROWSER_FAILED"),
    ({"cleanup": {"success": False}}, "ENVIRONMENT_BROWSER_FAILED"),
    ({"profile_closed_cleanly": False}, "ENVIRONMENT_BROWSER_FAILED"),
])
def test_browser_observations_and_cleanup_must_match_not_just_saved_json(private_home, external_tools, override, code):
    hosted, _ = client(lambda _: pytest.fail("no Azure call"))
    seeded = hosted.dispatch(request("seed", nonce="amber"))["outcome"]
    external_tools["overrides"].update(override)
    with pytest.raises(Problem) as exc:
        hosted.dispatch(request("read", checkpoint_sha256=seeded["checkpoint_sha256"]))
    assert exc.value.body["error"]["code"] == code
    assert external_tools["seed_markers"][1] is None


def test_seed_failure_retains_partial_state_without_a_committed_checkpoint(private_home, external_tools):
    hosted, _ = client(lambda _: pytest.fail("no Azure call"))
    external_tools["overrides"]["probe_status"] = "failed"
    with pytest.raises(Problem) as exc:
        hosted.dispatch(request("seed", nonce="amber"))
    assert exc.value.body["error"]["code"] == "ENVIRONMENT_BROWSER_FAILED"
    inspected = hosted.dispatch(request("inspect"))["outcome"]
    assert inspected["seeded"] is False
    with pytest.raises(Problem) as exc:
        hosted.dispatch(request("seed", nonce="amber"))
    assert exc.value.body["error"]["code"] == "ENVIRONMENT_ALREADY_SEEDED"


def test_environment_operations_are_serialized_at_the_public_entry(private_home, monkeypatch, external_tools):
    from shared_context import environment
    entered, release = Event(), Event()
    original = environment.run_gui_probe

    def blocked(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("synthetic browser was not released")
        return original(*args, **kwargs)

    monkeypatch.setattr(environment, "run_gui_probe", blocked)
    first, _ = client(lambda _: pytest.fail("no Azure call"))
    second, _ = client(lambda _: pytest.fail("no Azure call"))
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(first.dispatch, request("seed", nonce="amber"))
        try:
            assert entered.wait(5)
            with pytest.raises(Problem) as exc:
                second.dispatch(request("inspect"))
            assert exc.value.body["error"]["code"] == "ENVIRONMENT_BUSY"
        finally:
            release.set()
        assert pending.result(timeout=5)["outcome"]["checkpoint_sha256"]
    assert second.dispatch(request("inspect"))["outcome"]["seeded"] is True


@pytest.mark.parametrize("variable,value", [
    ("FOUNDRY_AGENT_SESSION_ID", "replacement-session"), ("FOUNDRY_AGENT_VERSION", "6"),
])
def test_replacement_or_version_change_needs_a_restore_contract(private_home, monkeypatch, external_tools, variable, value):
    hosted, _ = client(lambda _: pytest.fail("no Azure call"))
    seeded = hosted.dispatch(request("seed", nonce="amber"))["outcome"]
    monkeypatch.setenv(variable, value)
    with pytest.raises(Problem) as exc:
        hosted.dispatch(request("read", checkpoint_sha256=seeded["checkpoint_sha256"]))
    assert exc.value.body["error"]["code"] == "ENVIRONMENT_RESTORE_REQUIRED"
    assert len(external_tools["profiles"]) == 1


def test_agent_b_cannot_find_agent_a_state_even_when_given_the_same_home(private_home, monkeypatch, external_tools):
    hosted, _ = client(lambda _: pytest.fail("no Azure call"))
    seeded = hosted.dispatch(request("seed", nonce="amber"))["outcome"]
    monkeypatch.setenv("FOUNDRY_AGENT_ID", B_ID)
    with pytest.raises(Problem) as exc:
        hosted.dispatch(request("read", target_agent_id=B_ID, checkpoint_sha256=seeded["checkpoint_sha256"]))
    assert exc.value.status == 404
    assert len(external_tools["profiles"]) == 1


def test_private_file_symlink_is_rejected_before_read(private_home, monkeypatch, external_tools):
    hosted, _ = client(lambda _: pytest.fail("no Azure call"))
    seeded = hosted.dispatch(request("seed", nonce="amber"))["outcome"]
    target = Path(seeded["private_root"]) / "work.txt"
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == target or original(path))
    with pytest.raises(Problem) as exc:
        hosted.dispatch(request("read", checkpoint_sha256=seeded["checkpoint_sha256"]))
    assert exc.value.body["error"]["code"] == "UNSAFE_ENVIRONMENT_PATH"


def test_real_invocation_entry_does_not_run_legacy_home_probe_before_authorization(private_home, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    import hosted_main

    monkeypatch.setattr(hosted_main, "client", client(lambda _: pytest.fail("no Azure call"))[0])
    def no_home():
        raise RuntimeError("M1 must not run the old HOME marker probe")

    monkeypatch.setattr(Path, "home", no_home)
    with TestClient(hosted_main.app) as http:
        denied = http.post("/invocations?agent_session_id=synthetic-session-a", json=request(
            "inspect", target_agent_id=B_ID,
        ))
        assert denied.status_code == 403
        inspected = http.post("/invocations?agent_session_id=synthetic-session-a", json=request("inspect"))
        assert inspected.status_code == 200
        assert inspected.json()["runtime"]["home_probe"]["status"] == "not_requested"
        assert inspected.json()["runtime"]["process_boot_marker"]
        assert list(private_home.iterdir()) == []


@pytest.fixture
def file_runtime(monkeypatch):
    from shared_context import environment
    identity = {
        "Uid": [10001] * 4, "Gid": [10001] * 4, "Groups": [],
        "CapEff": "0", "CapPrm": "0", "CapAmb": "0",
    }
    monkeypatch.setattr(environment, "process_identity", lambda: identity, raising=False)
    monkeypatch.delenv("FA_BOOTSTRAP_RECEIPT", raising=False)
    monkeypatch.setattr(environment, "run_gui_probe", lambda *a, **kw: pytest.fail("file-only must not launch GUI"))
    monkeypatch.setattr(environment.subprocess, "run", lambda *a, **kw: pytest.fail("file-only must not launch tools"))
    return identity


def test_file_only_roundtrip_uses_original_file_and_no_gui(private_home, file_runtime):
    hosted, credential = client(lambda _: pytest.fail("no Azure data or models"))
    seeded = hosted.dispatch(request("seed_file", nonce="amber-file"))["outcome"]
    assert seeded["probe_scope"] == "file-only" and seeded["gui_invoked"] is False
    assert seeded["process_identity"] == file_runtime
    assert seeded["files"] == {"work.txt": {
        "bytes": len(b"m1 synthetic work: amber-file\n"),
        "sha256": hashlib.sha256(b"m1 synthetic work: amber-file\n").hexdigest(),
    }}
    assert {p.name for p in Path(seeded["private_root"]).iterdir()} == {"work.txt", "checkpoint.json"}
    recalled = hosted.dispatch(request("read_file", checkpoint_sha256=seeded["checkpoint_sha256"]))["outcome"]
    assert recalled["files"] == seeded["files"]
    assert recalled["hosted_continuity_validated"] is False
    assert credential.scopes == []


@pytest.mark.parametrize("fields", [
    {"nonce": "amber", "include_screenshot": True}, {},
    {"nonce": "amber", "checkpoint_sha256": "0" * 64},
])
def test_file_only_rejects_incompatible_fields_before_writing(private_home, file_runtime, fields):
    hosted, _ = client(lambda _: pytest.fail("no Azure call"))
    with pytest.raises(Problem) as exc:
        hosted.dispatch(request("seed_file", **fields))
    assert exc.value.status == 422 and list(private_home.iterdir()) == []


def test_file_only_rejects_reseed_corruption_and_wrong_receipt(private_home, file_runtime):
    hosted, _ = client(lambda _: pytest.fail("no Azure call"))
    seeded = hosted.dispatch(request("seed_file", nonce="amber"))["outcome"]
    for payload, code in [
        (request("seed_file", nonce="changed"), "ENVIRONMENT_ALREADY_SEEDED"),
        (request("read_file", checkpoint_sha256="0" * 64), "ENVIRONMENT_CHECKPOINT_CHANGED"),
    ]:
        with pytest.raises(Problem) as exc:
            hosted.dispatch(payload)
        assert exc.value.body["error"]["code"] == code
    file = Path(seeded["private_root"]) / "work.txt"
    file.write_text("changed")
    with pytest.raises(Problem) as exc:
        hosted.dispatch(request("read_file", checkpoint_sha256=seeded["checkpoint_sha256"]))
    assert exc.value.body["error"]["code"] == "ENVIRONMENT_FILES_CHANGED"
    assert file.read_text() == "changed"


@pytest.mark.parametrize("variable,value", [
    ("FOUNDRY_AGENT_VERSION", "different-version"), ("FOUNDRY_AGENT_SESSION_ID", "different-session"),
])
def test_file_only_still_requires_matching_restore_binding(private_home, file_runtime, monkeypatch, variable, value):
    hosted, _ = client(lambda _: pytest.fail("no Azure call"))
    seeded = hosted.dispatch(request("seed_file", nonce="amber"))["outcome"]
    monkeypatch.setenv(variable, value)
    with pytest.raises(Problem) as exc:
        hosted.dispatch(request("read_file", checkpoint_sha256=seeded["checkpoint_sha256"]))
    assert exc.value.body["error"]["code"] == "ENVIRONMENT_RESTORE_REQUIRED"


def test_file_only_checkpoint_cannot_satisfy_full_profile_probe(private_home, file_runtime):
    hosted, _ = client(lambda _: pytest.fail("no Azure call"))
    seeded = hosted.dispatch(request("seed_file", nonce="amber"))["outcome"]
    with pytest.raises(Problem) as exc:
        hosted.dispatch(request("read", checkpoint_sha256=seeded["checkpoint_sha256"]))
    assert exc.value.body["error"]["code"] == "ENVIRONMENT_SCOPE_CHANGED"


def test_full_checkpoint_cannot_satisfy_file_only_probe(private_home, external_tools, monkeypatch):
    from shared_context import environment
    hosted, _ = client(lambda _: pytest.fail("no Azure call"))
    seeded = hosted.dispatch(request("seed", nonce="amber"))["outcome"]
    monkeypatch.setattr(environment, "run_gui_probe", lambda *a, **kw: pytest.fail("no second GUI"))
    with pytest.raises(Problem) as exc:
        hosted.dispatch(request("read_file", checkpoint_sha256=seeded["checkpoint_sha256"]))
    assert exc.value.body["error"]["code"] == "ENVIRONMENT_SCOPE_CHANGED"


def test_file_only_http_entry_reports_bootstrap_and_still_refuses_root(private_home, file_runtime, monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("FA_BOOTSTRAP_RECEIPT", '{"initial_uid":0,"created":true}')
    import hosted_main
    monkeypatch.setattr(hosted_main, "client", client(lambda _: pytest.fail("no Azure call"))[0])
    with TestClient(hosted_main.app) as http:
        reply = http.post("/invocations?agent_session_id=synthetic-session-a", json=request("seed_file", nonce="amber"))
        assert reply.status_code == 200
        assert reply.json()["outcome"]["bootstrap"]["initial_uid"] == 0
        assert reply.json()["runtime"]["session_id"] == "synthetic-session-a"
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        refused = http.post("/invocations?agent_session_id=synthetic-session-a", json=request("inspect"))
        assert refused.status_code == 503
        assert refused.json()["error"]["code"] == "ENVIRONMENT_RUNTIME_UNAVAILABLE"
