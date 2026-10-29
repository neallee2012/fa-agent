from __future__ import annotations

import base64
import errno
import hashlib
import importlib
import io
import json
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import httpx
from pydantic import ValidationError

from shared_context import gui


@pytest.fixture
def owned_root():
    root = Path(__file__).resolve().parent / f".gui-fixture-{uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        if root.exists():
            shutil.rmtree(root)


@pytest.fixture
def helpers(monkeypatch):
    original = Path(__file__).resolve().parents[2] / "src" / "fa-hosted-sandbox-poc"
    monkeypatch.syspath_prepend(str(original))
    return importlib.import_module("desktop_smoke")


def command(**kwargs):
    return gui.GuiProbe(operation="gui", run_id="synthetic-run", nonce="amber-1", **kwargs)


@pytest.mark.parametrize("environment,message", [
    (SimpleNamespace(name="nt"), "Linux"),
    (SimpleNamespace(name="posix", geteuid=lambda: 0), "non-root"),
])
def test_gui_refuses_non_linux_or_root(monkeypatch, environment, message):
    monkeypatch.setattr(gui, "os", environment)
    with pytest.raises(RuntimeError, match=message):
        gui.prerequisites()


def test_missing_tools_are_not_pass(monkeypatch):
    monkeypatch.setattr(gui, "os", SimpleNamespace(name="posix", geteuid=lambda: 10001))
    monkeypatch.setattr(gui.shutil, "which", lambda name: None if name == "chromium" else "/synthetic")
    result = gui.run_gui_probe(command())
    assert result["probe_status"] == "unavailable" and "chromium" in result["error"]["message"]
    assert not gui.GUI_LOCK.locked()


def test_gui_serializes_invocations():
    gui.GUI_LOCK.acquire()
    try:
        result = gui.run_gui_probe(command())
        assert result["probe_status"] == "unavailable" and result["error"]["code"] == "GUI_BUSY"
    finally:
        gui.GUI_LOCK.release()


def test_gui_uses_owned_temporary_workspace_not_platform_home(monkeypatch, owned_root, helpers):
    def no_home():
        pytest.fail("GUI scratch files must not depend on platform HOME")

    def make_scratch(*, prefix, dir):
        assert prefix == "fagui-" and dir == "/tmp"
        socket_path = f"{dir}/{prefix}12345678/runtime/org.chromium.Chromium.123456/SingletonSocket"
        assert len(socket_path.encode()) < 108
        return str(owned_root)

    monkeypatch.setattr(gui.Path, "home", no_home)
    monkeypatch.setattr(gui, "prerequisites", lambda: 10001)
    monkeypatch.setattr("tempfile.mkdtemp", make_scratch)
    monkeypatch.setattr(gui, "exercise", lambda request, root, uid, helper: {
        "probe_status": "synthetic", "workspace": str(root), "uid": uid,
    })
    result = gui.run_gui_probe(gui.GuiProbe(operation="gui", run_id="x" * 64, nonce="amber-1"))
    assert result["workspace"] == str(owned_root)
    assert result["uid"] == 10001


def test_browser_is_headed_sandboxed_and_loopback_only(owned_root):
    args = gui.browser_command(owned_root, "http://127.0.0.1:54321/")
    assert args[0] == "chromium" and args[-1] == "http://127.0.0.1:54321/"
    assert not any(arg.startswith("--headless") or arg == "--no-sandbox" for arg in args)
    assert any(arg.startswith("--host-resolver-rules=") for arg in args)
    assert "setpriv" not in args
    rendered = gui.page(command()).decode("utf-8")
    assert "https://" not in rendered and 'fetch("/complete?' in rendered
    assert gui.artifact_text(command()) == "shared-context-poc|synthetic-run|amber-1|GUI_COMPLETE\n"


def test_browser_dns_deny_rule_exempts_the_actual_numeric_loopback_fixture(owned_root):
    args = gui.browser_command(owned_root, "http://127.0.0.1:54321/")
    rule = next(arg for arg in args if arg.startswith("--host-resolver-rules="))
    assert "MAP * ~NOTFOUND" in rule
    assert "EXCLUDE 127.0.0.1" in rule
    assert "EXCLUDE localhost" in rule


def test_screenshot_hash_inline_limit_and_original_download_validation(owned_root, helpers):
    image = owned_root / "test.png"
    data = b"\x89PNG\r\n\x1a\nsynthetic-bytes-not-a-real-render"
    image.write_bytes(data)
    result = gui.screenshot(image, True, helpers)
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    assert base64.b64decode(result["png_base64"]) == data
    assert "png_base64" not in gui.screenshot(image, False, helpers)
    expected = gui.artifact_text(command()).encode()
    helpers._verify_completion("amber-1", "amber-1", expected, expected)
    with pytest.raises(RuntimeError, match="nonce"):
        helpers._verify_completion("amber-1", "wrong", expected, expected)
    with pytest.raises(RuntimeError, match="download bytes"):
        helpers._verify_completion("amber-1", "amber-1", b"wrong", expected)
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * gui.MAX_SCREENSHOT_BYTES)
    with pytest.raises(RuntimeError, match="bounded PNG"):
        gui.screenshot(image, True, helpers)


def fake_exercise(monkeypatch, owned_root, helpers, *, callback_nonce="amber-1", root_browser=False):
    started, terminated, killed = [], [], []
    fake_server = SimpleNamespace(
        server_address=("127.0.0.1", 54321),
        shutdown=lambda: None, server_close=lambda: None,
    )
    fake_thread = SimpleNamespace(join=lambda timeout: None, is_alive=lambda: False)

    def start_server(request, ready, completed, values):
        ready.set()
        completed.set()
        values["nonce"] = callback_nonce
        return fake_server, fake_thread

    def start(args, env, log):
        log.write_text("101\n" if args[0] == "Xvfb" else "", encoding="utf-8")
        process = SimpleNamespace(pid=100 + len(started), poll=lambda: None, wait=lambda timeout: 0)
        started.append((process, args, env))
        return process

    def run(args, **kwargs):
        if args[0] == "import":
            Path(args[-1]).write_bytes(b"\x89PNG\r\n\x1a\nsynthetic-render")
        return subprocess.CompletedProcess(args, 0, stdout="42\n", stderr="")

    def send_input(window, nonce, env):
        (owned_root / "downloads" / "browser-artifact.txt").write_bytes(gui.artifact_text(command()).encode("utf-8"))
        return [["xdotool", "type", nonce]]

    def terminate(process):
        terminated.append(process.pid)
        return {"pid": process.pid, "return_code": 0}

    fake_helpers = SimpleNamespace(
        _start=start, _terminate=terminate, _send_gui_input=send_input, _version=lambda args: "synthetic-version",
        _verify_completion=helpers._verify_completion, _sha256=helpers._sha256,
    )
    monkeypatch.setattr(gui, "_start_server", start_server)
    monkeypatch.setattr(gui.subprocess, "run", run)
    monkeypatch.setattr(gui, "browser_uids", lambda pid: [0 if root_browser else 10001] * 4)
    monkeypatch.setattr(gui, "kill_residual_group", lambda pid: killed.append(pid))
    return fake_helpers, started, terminated, killed


@pytest.mark.parametrize("nonce,root_browser,expected", [
    ("amber-1", False, "passed"), ("wrong", False, "failed"), ("amber-1", True, "failed"),
])
def test_gui_mocked_exercise_validates_hashes_uid_and_cleans_all_resources(
    monkeypatch, owned_root, helpers, nonce, root_browser, expected,
):
    fake_helpers, started, terminated, killed = fake_exercise(
        monkeypatch, owned_root, helpers, callback_nonce=nonce, root_browser=root_browser,
    )
    result = gui.exercise(command(include_screenshot=True), owned_root, 10001, fake_helpers)
    assert result["probe_status"] == expected, result
    assert result["cleanup"]["success"] and not owned_root.exists()
    assert terminated == killed == [102, 101, 100]
    assert [args[0] for process, args, env in started] == ["Xvfb", "openbox", "chromium"]
    assert not any("IDENTITY" in name or "TOKEN" in name or "AZURE" in name for _, _, env in started for name in env)
    if expected == "passed":
        expected_bytes = gui.artifact_text(command()).encode()
        assert result["artifacts"]["browser_download"]["sha256"] == hashlib.sha256(expected_bytes).hexdigest()
        assert result["artifacts"]["after"]["png_base64"]
        assert result["browser_uid_fields"] == [10001] * 4
    else:
        assert result["diagnostics"]["page_ready"] is True
        assert set(result["diagnostics"]["logs"]) == {"xvfb.log", "openbox.log", "chromium.log"}
        assert result["diagnostics"]["screenshot"]["sha256"]


def test_cleanup_failure_attempts_remaining_groups_and_is_not_pass(monkeypatch, owned_root, helpers):
    fake_helpers, started, terminated, killed = fake_exercise(monkeypatch, owned_root, helpers)
    original_terminate = fake_helpers._terminate

    def terminate(process):
        if process.pid == 102:
            raise RuntimeError("synthetic termination failure")
        return original_terminate(process)

    fake_helpers._terminate = terminate
    result = gui.exercise(command(), owned_root, 10001, fake_helpers)
    assert result["probe_status"] == "failed" and result["error"]["code"] == "GUI_CLEANUP_FAILED"
    assert killed == [102, 101, 100] and terminated == [101, 100]
    assert result["cleanup"]["server_stopped"]
    assert result["cleanup"]["files_removed"] is False
    assert owned_root.exists()


def test_private_profile_is_reused_without_resetting_preferences_or_deleting_it(monkeypatch, owned_root, helpers):
    profile = owned_root.parent / f".profile-fixture-{uuid4().hex}"
    (profile / "Default").mkdir(parents=True)
    preferences = profile / "Default" / "Preferences"
    preferences.write_text(json.dumps({"intl": {"accept_languages": "zh-TW"}, "synthetic_setting": "keep"}))
    original = preferences.read_bytes()
    real_sync, real_replace = gui.os.fsync, gui.os.replace
    calls = []

    def sync(descriptor):
        assert preferences.read_bytes() == original
        real_sync(descriptor)
        calls.append("fsync")

    def replace(source, target):
        assert Path(source).parent == preferences.parent and Path(target) == preferences
        assert preferences.read_bytes() == original and calls == ["fsync"]
        assert json.loads(Path(source).read_text())["synthetic_setting"] == "keep"
        real_replace(source, target)
        calls.append("replace")

    try:
        fake_helpers, started, _, _ = fake_exercise(monkeypatch, owned_root, helpers)
        monkeypatch.setattr(gui.os, "fsync", sync)
        monkeypatch.setattr(gui.os, "replace", replace)
        result = gui.exercise(command(), owned_root, 10001, fake_helpers, persistent_profile=profile)
        assert result["probe_status"] == "passed" and result["cleanup"]["success"]
        assert result["profile_closed_cleanly"] is True
        assert not owned_root.exists() and profile.is_dir()
        saved = json.loads(preferences.read_text())
        assert saved["intl"]["accept_languages"] == "zh-TW" and saved["synthetic_setting"] == "keep"
        assert saved["download"]["default_directory"] == str(owned_root / "downloads")
        browser_args = started[2][1]
        assert f"--user-data-dir={profile}" in browser_args
        assert calls == ["fsync", "replace"]
        assert sorted(path.name for path in preferences.parent.iterdir()) == ["Preferences"]
    finally:
        shutil.rmtree(profile)


@pytest.mark.parametrize("failure", ["write", "flush", "fsync", "replace"])
def test_profile_write_failure_preserves_original_bytes(monkeypatch, owned_root, helpers, failure):
    profile = owned_root.parent / f".profile-fixture-{uuid4().hex}"
    directory = profile / "Default"
    directory.mkdir(parents=True)
    preferences = directory / "Preferences"
    original = b'{"intl":{"accept_languages":"zh-TW"}}'
    preferences.write_bytes(original)
    original_open = io.open

    class FailingWrite:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, data):
            if failure == "write":
                self.stream.write(data[:5])
                self.stream.flush()
                raise OSError(errno.ENOSPC, "synthetic disk full during Preferences write")
            return self.stream.write(data)

        def flush(self):
            if failure == "flush":
                raise OSError(errno.ENOSPC, "synthetic disk full during Preferences flush")
            return self.stream.flush()

    def fault_open(file, mode="r", *args, **kwargs):
        stream = original_open(file, mode, *args, **kwargs)
        if isinstance(file, (str, Path)) and Path(file).is_relative_to(directory) and "w" in mode:
            return FailingWrite(stream)
        return stream

    try:
        fake_helpers, started, _, _ = fake_exercise(monkeypatch, owned_root, helpers)
        monkeypatch.setattr(io, "open", fault_open)
        if failure in ("fsync", "replace"):
            def fail(*args):
                raise OSError(errno.ENOSPC, f"synthetic disk full during Preferences {failure}")
            monkeypatch.setattr(gui.os, failure, fail)
        result = gui.exercise(command(), owned_root, 10001, fake_helpers, persistent_profile=profile)
        assert result["probe_status"] == "failed" and "synthetic disk full" in result["error"]["message"]
        assert preferences.read_bytes() == original
        assert sorted(path.name for path in directory.iterdir()) == ["Preferences"]
        assert started == [] and result["cleanup"]["success"]
    finally:
        shutil.rmtree(profile)


def test_loopback_fixture_observes_cookie_and_language_but_does_not_reseed_on_read():
    marker = "a" * 32
    with httpx.Client(trust_env=False) as browser:
        for seed in (marker, None):
            ready, completed, values = threading.Event(), threading.Event(), {}
            server, thread = gui._start_server(command(), ready, completed, values, seed_profile_marker=seed)
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}"
                response = browser.get(url + "/")
                assert response.status_code == 200
                assert ("set-cookie" in response.headers) is (seed is not None)
                assert browser.get(url + "/ready", headers={"Accept-Language": "zh-TW,en;q=0.9"}).status_code == 204
                assert ready.wait(1)
                assert values["accept_language"] == "zh-TW,en;q=0.9"
                assert values["profile_marker_sha256"] == hashlib.sha256(marker.encode()).hexdigest()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                assert not thread.is_alive()


