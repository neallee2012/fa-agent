from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version(command: list[str]) -> str:
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=10, check=False
    )
    return (completed.stdout or completed.stderr).strip()


def _terminate(process: subprocess.Popen[str] | None) -> dict[str, Any] | None:
    if process is None:
        return None
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    return {"pid": process.pid, "return_code": process.returncode}


def _start(
    command: list[str], env: dict[str, str], log_path: Path
) -> subprocess.Popen[str]:
    with log_path.open("w", encoding="utf-8") as log:
        return subprocess.Popen(
            command,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )


def _send_gui_input(window: str, nonce: str, env: dict[str, str]) -> list[list[str]]:
    # xdotool type consumes all remaining arguments; key must be a separate process.
    commands = [
        ["xdotool", "windowactivate", "--sync", window, "windowfocus", "--sync", window],
        ["xdotool", "type", "--clearmodifiers", "--delay", "20", nonce],
        ["xdotool", "key", "--clearmodifiers", "Tab", "Return"],
    ]
    for command in commands:
        subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=15, check=True
        )
    return commands


def _verify_completion(
    nonce: str, completed_value: str | None, downloaded: bytes, expected: bytes
) -> None:
    if completed_value != nonce:
        raise RuntimeError("GUI_OR_BROWSER_RUNTIME: GUI callback nonce does not match")
    if downloaded != expected:
        raise RuntimeError("GUI_OR_BROWSER_RUNTIME: browser download bytes do not match")


def run_desktop_smoke(
    *,
    poc_id: str,
    run_id: str,
    nonce: str,
    evidence_root: Path,
    shared_run_root: Path,
    publish_relative_path: Any = None,
) -> dict[str, Any]:
    if os.name != "posix":
        raise RuntimeError("GUI_OR_BROWSER_RUNTIME: desktop smoke requires Linux")
    import pwd

    required = ["Xvfb", "openbox", "chromium", "xdotool", "import", "setpriv"]
    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        raise RuntimeError(f"GUI_OR_BROWSER_RUNTIME: missing commands: {missing}")

    output_dir = evidence_root / poc_id / run_id / "g3-desktop"
    output_dir.mkdir(parents=True, exist_ok=True)
    download_dir = output_dir / "downloads"
    profile_dir = output_dir / "chromium-profile"
    download_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Default").mkdir(parents=True, exist_ok=True)
    preferences = {
        "download": {
            "default_directory": str(download_dir),
            "prompt_for_download": False,
            "directory_upgrade": True,
        },
    }
    (profile_dir / "Default" / "Preferences").write_text(
        json.dumps(preferences), encoding="utf-8"
    )
    browser_user = pwd.getpwnam("pocbrowser")
    for path in (
        output_dir,
        download_dir,
        profile_dir,
        profile_dir / "Default",
        profile_dir / "Default" / "Preferences",
    ):
        os.chown(path, browser_user.pw_uid, browser_user.pw_gid)

    completed_event = threading.Event()
    ready_event = threading.Event()
    completed_value: dict[str, str] = {}
    artifact_text = f"{poc_id}|{run_id}|{nonce}|GUI_COMPLETE\n"

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/ready":
                ready_event.set()
                self.send_response(204)
                self.end_headers()
                return
            if parsed.path == "/complete":
                completed_value["value"] = urllib.parse.parse_qs(parsed.query).get(
                    "value", [""]
                )[0]
                completed_event.set()
                self.send_response(204)
                self.end_headers()
                return
            if parsed.path == "/":
                page = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>FA Hosted Sandbox POC</title></head>
<body>
  <h1>FA Hosted Sandbox POC</h1>
  <p id="marker">{poc_id} / {run_id} / {nonce}</p>
  <input id="value" autofocus aria-label="POC input">
  <button id="complete">Complete</button>
  <p id="status">WAITING</p>
  <script>
    document.getElementById("complete").addEventListener("click", async () => {{
      const value = document.getElementById("value").value;
      document.getElementById("status").textContent = "GUI_COMPLETE:" + value;
      await fetch("/complete?value=" + encodeURIComponent(value));
      const blob = new Blob([{json.dumps(artifact_text)}], {{type: "text/plain"}});
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "browser-artifact.txt";
      link.click();
    }});
    window.addEventListener("load", () => {{
      document.getElementById("value").focus();
      fetch("/ready");
    }});
  </script>
</body></html>"""
                encoded = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
                return
            self.send_error(404)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    port = server.server_address[1]

    display_number = 90 + (int(hashlib.sha256(run_id.encode()).hexdigest(), 16) % 9)
    display = f":{display_number}"
    env = os.environ.copy()
    env.update({"DISPLAY": display, "HOME": str(output_dir)})

    xvfb = openbox = chromium = None
    cleanup: list[dict[str, Any]] = []
    xvfb_log = output_dir / "xvfb.log"
    browser_log = output_dir / "chromium.log"
    before = output_dir / "before.png"
    after = output_dir / "after.png"
    try:
        xvfb = _start(
            ["Xvfb", display, "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
            env,
            xvfb_log,
        )
        socket_path = Path(f"/tmp/.X11-unix/X{display_number}")
        deadline = time.monotonic() + 10
        while not socket_path.exists():
            if xvfb.poll() is not None:
                stderr = xvfb_log.read_text(encoding="utf-8", errors="replace")[-4000:]
                raise RuntimeError(f"GUI_OR_BROWSER_RUNTIME: Xvfb exited: {stderr}")
            if time.monotonic() >= deadline:
                raise RuntimeError("GUI_OR_BROWSER_RUNTIME: Xvfb did not become ready")
            time.sleep(0.1)

        openbox = _start(["openbox"], env, output_dir / "openbox.log")
        url = f"http://127.0.0.1:{port}/"
        browser_env = env.copy()
        browser_env["HOME"] = browser_user.pw_dir
        chromium = _start(
            [
                "setpriv",
                "--reuid=pocbrowser",
                "--regid=pocbrowser",
                "--init-groups",
                "chromium",
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-background-networking",
                "--window-size=1200,700",
                url,
            ],
            browser_env,
            browser_log,
        )

        deadline = time.monotonic() + 20
        window = ""
        while not window:
            if chromium.poll() is not None:
                stderr = browser_log.read_text(encoding="utf-8", errors="replace")[-4000:]
                raise RuntimeError(
                    f"GUI_OR_BROWSER_RUNTIME: Chromium exited: {stderr}"
                )
            completed = subprocess.run(
                [
                    "xdotool",
                    "search",
                    "--onlyvisible",
                    "--name",
                    "FA Hosted Sandbox POC",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                window = completed.stdout.splitlines()[0]
                break
            if time.monotonic() >= deadline:
                stderr = browser_log.read_text(encoding="utf-8", errors="replace")[-4000:]
                raise RuntimeError(
                    f"GUI_OR_BROWSER_RUNTIME: Chromium window did not appear: {stderr}"
                )
            time.sleep(0.25)

        if not ready_event.wait(timeout=10):
            raise RuntimeError("GUI_OR_BROWSER_RUNTIME: loopback page did not become ready")
        subprocess.run(
            ["import", "-display", display, "-window", "root", str(before)],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        commands = _send_gui_input(window, nonce, env)
        (output_dir / "gui-commands.json").write_text(
            json.dumps(commands, indent=2), encoding="utf-8"
        )
        input_completed = completed_event.wait(timeout=15)
        subprocess.run(
            ["import", "-display", display, "-window", "root", str(after)],
            env=env, capture_output=True, text=True, timeout=15, check=True,
        )
        if not input_completed:
            raise RuntimeError(
                f"GUI_OR_BROWSER_RUNTIME: GUI input did not complete; evidence: {after}"
            )

        download = download_dir / "browser-artifact.txt"
        deadline = time.monotonic() + 15
        while not download.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not download.exists():
            raise RuntimeError("GUI_OR_BROWSER_RUNTIME: browser download was not created")
        _verify_completion(
            nonce, completed_value.get("value"), download.read_bytes(),
            artifact_text.encode("utf-8"),
        )

        subprocess.run(
            ["import", "-display", display, "-window", "root", str(after)],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )

        published: dict[str, Any] | None = None
        if publish_relative_path is not None:
            if (
                not isinstance(publish_relative_path, str)
                or "/" in publish_relative_path
                or "\\" in publish_relative_path
                or publish_relative_path in {"", ".", ".."}
            ):
                raise RuntimeError(
                    "INVALID_REQUEST: publish_relative_path must be a filename"
                )
            shared_run_root.mkdir(parents=True, exist_ok=True)
            destination = shared_run_root / publish_relative_path
            shutil.copyfile(download, destination)
            published = {
                "relative_path": publish_relative_path,
                "sha256": _sha256(destination),
                "size": destination.stat().st_size,
            }

        return {
            "display": display,
            "loopback_url": url,
            "page_ready": ready_event.is_set(),
            "completed_value": completed_value.get("value"),
            "browser_uid": browser_user.pw_uid,
            "browser_status": Path(f"/proc/{chromium.pid}/status").read_text(),
            "browser_command_line": Path(f"/proc/{chromium.pid}/cmdline").read_bytes()
                .decode("utf-8").split("\x00"),
            "gui_commands": commands,
            "cleanup": cleanup,
            "pids": {
                "xvfb": xvfb.pid,
                "openbox": openbox.pid,
                "chromium": chromium.pid,
                "server": os.getpid(),
            },
            "versions": {
                "xvfb": _version(["dpkg-query", "-W", "-f=${Version}", "xvfb"]),
                "openbox": _version(["openbox", "--version"]),
                "chromium": _version(["chromium", "--version"]),
            },
            "artifacts": {
                "before": {"path": str(before), "sha256": _sha256(before)},
                "after": {"path": str(after), "sha256": _sha256(after)},
                "browser_download": {
                    "path": str(download),
                    "sha256": _sha256(download),
                    "size": download.stat().st_size,
                },
                "published_shared": published,
            },
            "browser_sandbox_disabled": False,
        }
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"GUI_OR_BROWSER_RUNTIME: command failed: {exc}; evidence: {output_dir}"
        ) from exc
    finally:
        for process in (chromium, openbox, xvfb):
            result = _terminate(process)
            if result:
                cleanup.append(result)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        (output_dir / "cleanup.json").write_text(
            json.dumps(cleanup, indent=2), encoding="utf-8"
        )
