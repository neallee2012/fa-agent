from __future__ import annotations

import base64
import hashlib
import html
import http.server
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse

from .errors import safe_text
from .models import Identifier, StrictModel

GUI_LOCK = threading.Lock()
REQUIRED_COMMANDS = ("Xvfb", "openbox", "chromium", "xdotool", "import")
MAX_SCREENSHOT_BYTES = 256 * 1024


class GuiProbe(StrictModel):
    operation: Literal["gui"]
    run_id: Identifier
    nonce: Identifier
    include_screenshot: bool = False


def prerequisites() -> int:
    if os.name != "posix":
        raise RuntimeError("GUI requires a Linux Hosted runtime")
    uid = os.geteuid()
    if uid == 0:
        raise RuntimeError("GUI must run as a non-root user; no root or --no-sandbox fallback")
    missing = [command for command in REQUIRED_COMMANDS if shutil.which(command) is None]
    if missing:
        raise RuntimeError(f"GUI commands missing: {', '.join(missing)}")
    return uid


def artifact_text(command: GuiProbe) -> str:
    return f"shared-context-poc|{command.run_id}|{command.nonce}|GUI_COMPLETE\n"


def page(command: GuiProbe) -> bytes:
    title = html.escape(f"FA Shared Context {command.run_id}")
    content = f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title></head>
<body><h1>{title}</h1><p>Local synthetic GUI fixture</p>
<input id="value" autofocus aria-label="Synthetic input"><button id="complete">Complete</button>
<p id="status">WAITING</p><script>
document.getElementById("complete").addEventListener("click", async () => {{
  const value = document.getElementById("value").value;
  document.getElementById("status").textContent = "GUI_COMPLETE:" + value;
  await fetch("/complete?value=" + encodeURIComponent(value));
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([{json.dumps(artifact_text(command))}], {{type:"text/plain"}}));
  link.download = "browser-artifact.txt"; link.click();
}});
window.addEventListener("load", () => {{ document.getElementById("value").focus(); fetch("/ready"); }});
</script></body></html>"""
    return content.encode("utf-8")


def browser_command(profile: Path, url: str) -> list[str]:
    return [
        "chromium", f"--user-data-dir={profile}", "--no-first-run",
        "--disable-dev-shm-usage", "--disable-gpu", "--disable-background-networking",
        "--disable-component-update", "--disable-sync", "--disable-default-apps",
        "--password-store=basic", "--window-size=1200,700",
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1", url,
    ]


def _start_server(command: GuiProbe, ready, completed, values, *, seed_profile_marker: str | None = None):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            pass

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/ready":
                values["accept_language"] = self.headers.get("Accept-Language", "")
                cookies = SimpleCookie()
                cookies.load(self.headers.get("Cookie", ""))
                marker = cookies.get("fa_m1_marker")
                values["profile_marker_sha256"] = hashlib.sha256(marker.value.encode()).hexdigest() if marker else None
                ready.set()
            elif parsed.path == "/complete":
                values["nonce"] = parse_qs(parsed.query).get("value", [""])[0]
                completed.set()
            elif parsed.path == "/":
                body = page(command)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                if seed_profile_marker is not None:
                    self.send_header("Set-Cookie", f"fa_m1_marker={seed_profile_marker}; Max-Age=86400; SameSite=Strict; Path=/")
                self.end_headers()
                self.wfile.write(body)
                return
            else:
                self.send_error(404)
                return
            self.send_response(204)
            self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    try:
        thread.start()
    except Exception:
        server.server_close()
        raise
    return server, thread


def screenshot(path: Path, include: bool, helpers) -> dict:
    content = path.read_bytes()
    if not content.startswith(b"\x89PNG\r\n\x1a\n") or len(content) > MAX_SCREENSHOT_BYTES:
        raise RuntimeError("GUI screenshot is not a bounded PNG")
    result = {"sha256": helpers._sha256(path), "size": len(content)}
    if include:
        result["png_base64"] = base64.b64encode(content).decode("ascii")
    return result


def failure_diagnostics(root: Path, env: dict, ready: threading.Event, include: bool, helpers) -> dict:
    result = {"page_ready": ready.is_set(), "logs": {}}
    for name in ("xvfb.log", "openbox.log", "chromium.log"):
        try:
            with (root / name).open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 4096))
                result["logs"][name] = safe_text(stream.read(4096).decode("utf-8", errors="replace"))
        except OSError as exc:
            result["logs"][name] = {"unavailable": safe_text(exc)}
    if env.get("DISPLAY"):
        try:
            path = root / "failure.png"
            subprocess.run(["import", "-display", env["DISPLAY"], "-window", "root", str(path)],
                           env=env, capture_output=True, text=True, timeout=10, check=True)
            result["screenshot"] = screenshot(path, include, helpers)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            result["screenshot_error"] = safe_text(exc)
    return result


def browser_uids(pid: int) -> list[int]:
    status = Path(f"/proc/{pid}/status").read_text()
    match = re.search(r"^Uid:\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", status, re.MULTILINE)
    return [int(value) for value in match.groups()] if match else []


def kill_residual_group(pid: int) -> None:
    # The original helper waits for the launcher; also terminate any surviving children.
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def cleanup(processes, server, thread, root: Path, helpers) -> dict:
    results, errors = [], []
    for process in reversed(processes):
        try:
            try:
                results.append(helpers._terminate(process))
            finally:
                kill_residual_group(process.pid)
        except Exception as exc:
            errors.append({"pid": process.pid, "message": safe_text(exc)})
    stopped = server is None
    if server:
        try:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            stopped = not thread.is_alive()
        except Exception as exc:
            errors.append({"server": safe_text(exc)})
    removed = False
    if not errors and stopped:
        try:
            shutil.rmtree(root)
            removed = True
        except OSError as exc:
            errors.append({"files": safe_text(exc)})
    return {
        "processes": results, "server_stopped": stopped, "files_removed": removed,
        "errors": errors, "success": stopped and removed and not errors,
    }


def _write_preferences(path: Path, preferences: dict) -> None:
    content = json.dumps(preferences).encode("utf-8")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=".Preferences-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def exercise(
    command: GuiProbe, root: Path, uid: int, helpers, *,
    persistent_profile: Path | None = None, seed_profile_marker: str | None = None,
) -> dict:
    processes, server, server_thread = [], None, None
    env = {}
    result = {"probe_status": "failed", "browser_uid": uid, "browser_sandbox_disabled": False}
    ready, completed, values = threading.Event(), threading.Event(), {}
    try:
        downloads, runtime = root / "downloads", root / "runtime"
        profile = persistent_profile if persistent_profile is not None else root / "profile"
        if persistent_profile is not None and (not profile.is_absolute() or profile.is_relative_to(root)):
            raise RuntimeError("private profile must be outside the disposable GUI workspace")
        if seed_profile_marker is not None and (
            persistent_profile is None or not re.fullmatch(r"[a-f0-9]{32}", seed_profile_marker)
        ):
            raise RuntimeError("profile seed requires a bounded synthetic marker and private profile")
        preferences_path = profile / "Default" / "Preferences"
        preferences = json.loads(preferences_path.read_text(encoding="utf-8")) if persistent_profile is not None else {}
        for directory in (downloads, profile / "Default", runtime):
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        # Only disposable download routing changes on read; never recreate language or browser-owned cookies.
        preferences["download"] = {
            **preferences.get("download", {}), "default_directory": str(downloads),
            "prompt_for_download": False, "directory_upgrade": True,
        }
        _write_preferences(preferences_path, preferences)
        env = {
            "PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8", "HOME": str(root),
            "XDG_RUNTIME_DIR": str(runtime), "TMPDIR": str(runtime),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache"),
        }
        if seed_profile_marker is None:
            server, server_thread = _start_server(command, ready, completed, values)
        else:
            server, server_thread = _start_server(command, ready, completed, values, seed_profile_marker=seed_profile_marker)
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        xvfb_log = root / "xvfb.log"
        xvfb = helpers._start([
            "Xvfb", "-displayfd", "1", "-screen", "0", "1280x800x24", "-nolisten", "tcp",
        ], env, xvfb_log)
        processes.append(xvfb)
        deadline = time.monotonic() + 10
        display = None
        while display is None:
            text = xvfb_log.read_text(encoding="utf-8", errors="replace")
            number = next((line for line in text.splitlines() if re.fullmatch(r"\d{1,5}", line)), None)
            if number:
                display = f":{number}"
                break
            if xvfb.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(f"Xvfb did not become ready: {safe_text(text)}")
            time.sleep(0.1)
        env["DISPLAY"] = display
        openbox = helpers._start(["openbox"], env, root / "openbox.log")
        processes.append(openbox)
        browser_log = root / "chromium.log"
        args = browser_command(profile, url)
        browser = helpers._start(args, env, browser_log)
        processes.append(browser)
        deadline, window = time.monotonic() + 25, ""
        while not window:
            if browser.poll() is not None or openbox.poll() is not None:
                raise RuntimeError("Chromium/Openbox exited: " + safe_text(browser_log.read_text(errors="replace")))
            found = subprocess.run([
                "xdotool", "search", "--onlyvisible", "--name", f"FA Shared Context {command.run_id}",
            ], env=env, capture_output=True, text=True, timeout=5, check=False)
            if found.returncode == 0 and found.stdout.strip():
                window = found.stdout.splitlines()[0]
                break
            if time.monotonic() > deadline:
                raise RuntimeError("headed Chromium window did not appear")
            time.sleep(0.2)
        if not ready.wait(timeout=10):
            raise RuntimeError("local synthetic page did not become ready")
        before, after = root / "before.png", root / "after.png"

        def capture(path):
            subprocess.run(["import", "-display", display, "-window", "root", str(path)],
                           env=env, capture_output=True, text=True, timeout=15, check=True)

        capture(before)
        commands = helpers._send_gui_input(window, command.nonce, env)
        if not completed.wait(timeout=15):
            raise RuntimeError("GUI input did not produce a completion callback")
        download = downloads / "browser-artifact.txt"
        deadline = time.monotonic() + 15
        while not download.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not download.exists():
            raise RuntimeError("browser did not download the synthetic artifact")
        expected = artifact_text(command).encode("utf-8")
        helpers._verify_completion(command.nonce, values.get("nonce"), download.read_bytes(), expected)
        capture(after)
        uids = browser_uids(browser.pid)
        if len(uids) != 4 or any(value != uid or value == 0 for value in uids):
            raise RuntimeError("could not verify the browser process is non-root")

        def version(args):
            if persistent_profile is None:
                return helpers._version(args)
            checked = subprocess.run(args, env=env, capture_output=True, text=True, timeout=10, check=True)
            return (checked.stdout or checked.stderr).strip()

        result.update({
            "probe_status": "passed", "display": display, "loopback_url": url,
            "page_ready": True, "completed_value": values["nonce"], "browser_pid": browser.pid,
            "accept_language": values.get("accept_language", ""),
            "profile_marker_sha256": values.get("profile_marker_sha256"),
            "browser_command_line": args, "browser_uid_fields": uids,
            "gui_commands": commands,
            "versions": {
                "xvfb": version(["dpkg-query", "-W", "-f=${Version}", "xvfb"]),
                "openbox": version(["openbox", "--version"]),
                "chromium": version(["chromium", "--version"]),
            },
            "artifacts": {
                "before": screenshot(before, command.include_screenshot, helpers),
                "after": screenshot(after, command.include_screenshot, helpers),
                "browser_download": {"sha256": helpers._sha256(download), "size": download.stat().st_size,
                                     "text": expected.decode("utf-8")},
            },
        })
        if persistent_profile is not None:
            subprocess.run(
                ["xdotool", "windowactivate", "--sync", window, "key", "--clearmodifiers", "alt+F4"],
                env=env, capture_output=True, text=True, timeout=10, check=True,
            )
            if browser.wait(timeout=10) != 0:
                raise RuntimeError("private-profile Chromium did not close cleanly")
            result["profile_closed_cleanly"] = True
    except Exception as exc:
        result.update({"probe_status": "failed", "error": {"code": "GUI_PROBE_FAILED", "message": safe_text(exc)}})
        result["diagnostics"] = failure_diagnostics(root, env, ready, command.include_screenshot, helpers)
    finally:
        result["cleanup"] = cleanup(processes, server, server_thread, root, helpers)
        if not result["cleanup"]["success"]:
            result["probe_status"] = "failed"
            result.setdefault("error", {"code": "GUI_CLEANUP_FAILED", "message": "GUI cleanup did not complete"})
    return result


def run_gui_probe(
    command: GuiProbe, *, persistent_profile: Path | None = None, seed_profile_marker: str | None = None,
) -> dict:
    if not GUI_LOCK.acquire(blocking=False):
        return {"probe_status": "unavailable", "error": {"code": "GUI_BUSY", "message": "one GUI probe is already running"}}
    try:
        try:
            uid = prerequisites()
            # Reuse portable GUI primitives, not the old root/setpriv/mount-aware entry.
            import desktop_smoke as helpers
        except (RuntimeError, ImportError) as exc:
            return {"probe_status": "unavailable", "error": {"code": "GUI_UNAVAILABLE", "message": safe_text(exc)}}
        try:
            root = Path(tempfile.mkdtemp(prefix="fagui-", dir="/tmp"))
        except OSError as exc:
            return {"probe_status": "unavailable", "error": {"code": "GUI_WORKSPACE_UNAVAILABLE", "message": safe_text(exc)}}
        if persistent_profile is None and seed_profile_marker is None:
            result = exercise(command, root, uid, helpers)
        else:
            result = exercise(
                command, root, uid, helpers,
                persistent_profile=persistent_profile, seed_profile_marker=seed_profile_marker,
            )
        result["workspace_storage"] = "ephemeral-temporary-directory"
        result["browser_profile_storage"] = "private-home-candidate" if persistent_profile is not None else "ephemeral"
        return result
    finally:
        GUI_LOCK.release()
