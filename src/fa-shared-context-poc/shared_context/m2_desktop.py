"""Bounded, pixel-driven desktop tools; no SDK, cloud calls, or scripted task steps.

Browser navigation policies and the trusted fixture's CSP are not sandbox network isolation.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import html
import http.server
import importlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Literal, get_args
from uuid import uuid4

from . import gui
from .errors import Problem

WIDTH, HEIGHT = 1280, 800
MAX_ACTIONS, MAX_OBSERVATIONS, MAX_SECONDS = 120, 180, 600
SYNTHETIC_UPLOAD = b"FA M2 synthetic upload only.\n"
DesktopKeyName = Literal[
    "Tab", "shift+Tab", "Return", "Left", "Right", "Up", "Down", "BackSpace", "Escape",
    "ctrl+a", "ctrl+l",
]
KEYS = frozenset(get_args(DesktopKeyName))


class DesktopWorkspace:
    """One bounded run, owned by one caller; not a general desktop/remote-control API.

    ``root`` must already be authorized beneath the stable agent's private M2 root.
    Profiles remain at root/profile; run artifacts remain at root/runs/run_id.
    A run ID cannot be reused, preventing stale download bytes from becoming proof.
    """

    def __init__(self, root: Path, run_id: str, variant: int = 0):
        if not isinstance(root, Path) or not root.is_absolute() or root.is_symlink():
            raise Problem(422, "M2_DESKTOP_INPUT", "root must be an authorized absolute private directory")
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
            raise Problem(422, "M2_DESKTOP_INPUT", "run_id must be a bounded identifier")
        if type(variant) is not int or variant not in (0, 1):
            raise Problem(422, "M2_DESKTOP_INPUT", "variant must be 0 or 1")
        self.root = root
        self.run_id = run_id
        self.variant = variant
        self._run_root = root / "runs" / run_id
        self._profile = root / "profile"
        self._runtime = None
        self._helpers = None
        self._processes = []
        self._env = {}
        self._server = self._thread = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._fixture_url = ""
        self._route = f"/{uuid4().hex}/"
        self._started = self._closed = False
        self._owns_run = False
        self._gui_lock_held = False
        self._cleanup = None
        self._started_at = 0.0
        self._actions = self._observations = 0
        self._observed = False
        self._submission = None
        self._download_requests = []
        self._dialog_observations = []
        self._receipt = None
        self._browser_pid = None
        self._request_count = self._submission_count = 0
        self._download_name = f"m2-receipt-{run_id}.json"

    @property
    def fixture_url(self) -> str:
        return self._fixture_url

    @property
    def upload_path(self) -> Path:
        return self._run_root / "synthetic-upload.txt"

    @property
    def task_description(self) -> str:
        return (
            f"Use only the visible desktop at {self.fixture_url}. Enter the caller's synthetic "
            "Work label (1-128 ASCII letters/digits/spaces/hyphens/underscores, starting with a letter "
            "or digit) and Unit count (integer 1-999) in the matching labeled fields. Open the native file chooser "
            f"and select only {self.upload_path}. Submit, then click Download receipt. "
            "Observe pixels before every individual action; field positions/order vary. "
            "Do not use a terminal, devtools, websites, or any other files."
        )

    def start(self):
        if self._started or self._closed:
            raise Problem(409, "M2_DESKTOP_STATE", "workspace cannot be started twice")
        try:
            if sys.platform != "linux":
                raise RuntimeError("M2 desktop requires a Linux Hosted runtime")
            uid = gui.prerequisites()
            if shutil.which("xprop") is None:
                raise RuntimeError("M2 desktop requires xprop for native dialog evidence")
            self._helpers = importlib.import_module("desktop_smoke")
        except (RuntimeError, ImportError) as exc:
            raise Problem(503, "M2_DESKTOP_UNAVAILABLE", str(exc)) from exc
        if not gui.GUI_LOCK.acquire(blocking=False):
            raise Problem(409, "M2_DESKTOP_BUSY", "another owned desktop is already running")
        self._gui_lock_held = True
        try:
            if self.root.resolve() != self.root or not self.root.is_dir():
                raise RuntimeError("private root must exist and contain no symlink ancestors")
            singleton = self._profile / "SingletonLock"
            if singleton.exists() or singleton.is_symlink():
                raise RuntimeError("browser profile is already locked; refusing to control another browser")
            for directory in (self.root / "runs", self._profile, self._profile / "Default"):
                if directory.is_symlink():
                    raise RuntimeError("workspace directories must not be symlinks")
                directory.mkdir(mode=0o700, exist_ok=True)
            self._run_root.mkdir(mode=0o700)
            self._owns_run = True
            downloads = self._run_root / "downloads"
            downloads.mkdir(mode=0o700)
            self.upload_path.write_bytes(SYNTHETIC_UPLOAD)
            preference_path = self._profile / "Default" / "Preferences"
            if preference_path.is_symlink():
                raise RuntimeError("browser preferences must not be a symlink")
            preferences = json.loads(preference_path.read_text()) if preference_path.exists() else {}
            preferences["download"] = {
                **preferences.get("download", {}), "default_directory": str(downloads),
                "prompt_for_download": False, "directory_upgrade": True,
            }
            gui._write_preferences(preference_path, preferences)
            # Chromium's SingletonSocket has a Unix-path limit independent of the persistent profile path.
            self._runtime = Path(tempfile.mkdtemp(prefix="fam2-", dir="/tmp"))
            runtime = self._runtime / "rt"
            runtime.mkdir(mode=0o700)
            self._env = {
                "PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8",
                "HOME": str(self._runtime), "TMPDIR": str(runtime), "XDG_RUNTIME_DIR": str(runtime),
                "XDG_CONFIG_HOME": str(self._runtime / "config"),
                "XDG_CACHE_HOME": str(self._runtime / "cache"),
                "GTK_USE_PORTAL": "0",
            }
            self._start_fixture()
            xvfb_log = self._runtime / "xvfb.log"
            self._spawn([
                "Xvfb", "-displayfd", "1", "-screen", "0", f"{WIDTH}x{HEIGHT}x24", "-nolisten", "tcp",
            ], xvfb_log)
            deadline = time.monotonic() + 10
            while True:
                text = xvfb_log.read_text(errors="replace")
                number = next((line for line in text.splitlines() if re.fullmatch(r"\d{1,5}", line)), None)
                if number:
                    self._env["DISPLAY"] = f":{number}"
                    break
                if self._processes[0].poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("owned Xvfb did not become ready")
                time.sleep(0.1)
            self._spawn(["openbox"], self._runtime / "openbox.log")
            args = gui.browser_command(self._profile, self.fixture_url)
            args = [f"--window-size={WIDTH},{HEIGHT}" if arg.startswith("--window-size=") else arg for arg in args]
            # DNS rules alone do not block literal external IPs. The same loopback server
            # also acts as a deny-only proxy; only its exact fixture address bypasses it.
            port = self._server.server_address[1]
            args[-1:-1] = [
                f"--proxy-server=http://127.0.0.1:{port}",
                f"--proxy-bypass-list=<-loopback>;127.0.0.1:{port}", "--disable-quic",
                "--disable-extensions", "--window-position=0,0",
            ]
            self._spawn(args, self._runtime / "chromium.log")
            self._browser_pid = self._processes[-1].pid
            if gui.browser_uids(self._browser_pid) != [uid] * 4:
                raise RuntimeError("could not verify Chromium has the expected non-root UID")
            deadline = time.monotonic() + 25
            while True:
                if any(process.poll() is not None for process in self._processes):
                    raise RuntimeError("owned desktop process exited during startup")
                found = self._command([
                    "xdotool", "search", "--onlyvisible", "--name", f"FA M2 Desktop {self.run_id}",
                ], check=False)
                if self._ready.is_set() and found.returncode == 0 and found.stdout.strip():
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("headed Chromium fixture did not become ready")
                time.sleep(0.1)
            self._started_at = time.monotonic()
            self._started = True
            return self
        except Exception as exc:
            self.close()
            raise Problem(503, "M2_DESKTOP_START_FAILED", str(exc)) from exc

    def _spawn(self, args, log):
        self._processes.append(self._helpers._start(args, self._env.copy(), log))

    def _command(self, args, *, check=True):
        return subprocess.run(
            args, env=self._env, capture_output=True, text=True, timeout=10, check=check,
        )

    def _require_running(self):
        if not self._started or self._closed:
            raise Problem(409, "M2_DESKTOP_STATE", "desktop is not running")
        if time.monotonic() - self._started_at >= MAX_SECONDS:
            self.close()
            raise Problem(429, "M2_DESKTOP_LIMIT", "desktop time budget exhausted")
        if any(process.poll() is not None for process in self._processes):
            self.close()
            raise Problem(503, "M2_DESKTOP_PROCESS_EXITED", "an owned desktop process exited")

    def _windows(self):
        found = self._command(["xdotool", "search", "--onlyvisible", "--name", "."], check=False)
        windows = []
        for window_id in dict.fromkeys(found.stdout.splitlines()):
            if not re.fullmatch(r"\d{1,12}", window_id) or len(windows) >= 32:
                continue
            title = self._command(["xdotool", "getwindowname", window_id], check=False)
            properties = self._command([
                "xprop", "-id", window_id, "_NET_WM_WINDOW_TYPE", "WM_TRANSIENT_FOR", "WM_CLASS", "_NET_WM_PID",
            ], check=False).stdout
            pid = re.search(r"_NET_WM_PID\(CARDINAL\)\s*=\s*(\d+)", properties)
            transient = re.search(r"WM_TRANSIENT_FOR.*?#\s*(0x[0-9a-fA-F]+)", properties)
            browser_owned = bool(
                pid and int(pid[1]) == self._browser_pid
                and re.search(r'WM_CLASS.*"[Cc]hromium[^"]*"', properties)
            )
            kind = "dialog" if "_NET_WM_WINDOW_TYPE_DIALOG" in properties else (
                "normal" if "_NET_WM_WINDOW_TYPE_NORMAL" in properties else "other"
            )
            windows.append({
                "id": window_id, "title": title.stdout.strip()[:256], "window_type": kind,
                "transient_for": str(int(transient[1], 16)) if transient else None,
                "owned_browser": browser_owned, "native_file_dialog": False,
            })
        browser_ids = {
            window["id"] for window in windows
            if window["owned_browser"] and window["window_type"] == "normal"
        }
        for window in windows:
            window["native_file_dialog"] = bool(
                window["owned_browser"] and window["window_type"] == "dialog"
                and window["transient_for"] in browser_ids
                and re.search(r"\b(open|upload|choose|select)\b", window["title"], re.IGNORECASE)
            )
        active_id = self._command(["xdotool", "getactivewindow"], check=False).stdout.strip()
        active = next((window for window in windows if window["id"] == active_id), None)
        return windows, active

    def observe(self) -> dict:
        """Capture this X display's real pixels and bounded native window metadata.

        Returns png_base64/mime_type/width/height, windows and active_window. No DOM,
        accessibility tree, recommended coordinates or task answers are provided.
        """
        self._require_running()
        if self._observations >= MAX_OBSERVATIONS:
            self.close()
            raise Problem(429, "M2_DESKTOP_LIMIT", "desktop observation budget exhausted")
        self._observed = False
        try:
            path = self._runtime / "observation.png"
            self._command([
                "import", "-display", self._env["DISPLAY"], "-window", "root", "-depth", "8", str(path),
            ])
            image = gui.screenshot(path, True, self._helpers)
            header = path.read_bytes()[:24]
            if len(header) != 24 or header[12:16] != b"IHDR":
                raise RuntimeError("screenshot has no PNG dimensions")
            width, height = struct.unpack(">II", header[16:24])
            if (width, height) != (WIDTH, HEIGHT):
                raise RuntimeError("screenshot does not match the owned display dimensions")
            windows, active = self._windows()
            self._observations += 1
            for window in windows:
                if window["native_file_dialog"] and len(self._dialog_observations) < MAX_OBSERVATIONS:
                    self._dialog_observations.append({
                        **window, "observation_id": self._observations, "screenshot_sha256": image["sha256"],
                    })
            self._observed = True
            return {
                **image, "mime_type": "image/png", "width": width, "height": height,
                "windows": windows, "active_window": active, "observation_id": self._observations,
            }
        except Exception as exc:
            self.close()
            raise Problem(503, "M2_DESKTOP_OBSERVE_FAILED", str(exc)) from exc

    def act(self, action: dict) -> dict:
        """Exactly one ``{type: click|type|key, ...}`` action after each observe().

        Click uses x/y (integers, left button only), type uses text, key uses one
        member of KEYS. Labels are bounded synthetic ASCII; URLs/paths must exactly
        match fixture_url/upload_path, and the path requires a native file chooser.
        """
        self._require_running()
        invalid = Problem(422, "M2_DESKTOP_ACTION", "invalid or out-of-scope single desktop action")
        if not isinstance(action, dict):
            raise invalid
        kind = action.get("type")
        if kind == "click" and set(action) == {"type", "x", "y"}:
            x, y = action["x"], action["y"]
            if type(x) is not int or type(y) is not int or not (0 <= x < WIDTH and 0 <= y < HEIGHT):
                raise invalid
            command = ["xdotool", "mousemove", "--sync", str(x), str(y), "click", "1"]
        elif kind == "key" and set(action) == {"type", "key"}:
            if not isinstance(action["key"], str) or action["key"] not in KEYS:
                raise invalid
            command = ["xdotool", "key", "--clearmodifiers", action["key"]]
        elif kind == "type" and set(action) == {"type", "text"}:
            text = action["text"]
            if not isinstance(text, str) or not text or len(text) > 1024:
                raise invalid
            if text not in (self.fixture_url, str(self.upload_path)) and not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9 _-]{0,127}", text,
            ):
                raise invalid
            command = ["xdotool", "type", "--clearmodifiers", "--delay", "1", "--", text]
        else:
            raise invalid
        if not self._observed:
            raise Problem(409, "M2_DESKTOP_OBSERVE_REQUIRED", "observe pixels before each action")
        if self._actions >= MAX_ACTIONS:
            self.close()
            raise Problem(429, "M2_DESKTOP_LIMIT", "desktop action budget exhausted")
        try:
            windows, active = self._windows()
            can_input = bool(active and active["owned_browser"] and active["window_type"] in ("normal", "dialog"))
            if (kind != "click" and not can_input) or not any(window["owned_browser"] for window in windows):
                raise Problem(409, "M2_DESKTOP_FOCUS", "active window is not the owned Chromium window")
            if kind == "type" and action["text"] == str(self.upload_path) and not active["native_file_dialog"]:
                raise invalid
            self._observed = False
            self._actions += 1
            self._command(command)
        except Problem:
            raise
        except Exception as exc:
            self.close()
            raise Problem(503, "M2_DESKTOP_ACTION_FAILED", str(exc)) from exc
        return {"action": kind, "action_index": self._actions, "requires_observation": True}

    def _form(self) -> bytes:
        label = '<label>Work label<input name="label" type="text" maxlength="128" required></label>'
        count = '<label>Unit count<input name="count" type="number" min="1" max="999" required></label>'
        upload = '<label>Synthetic file<input name="upload" type="file" accept=".txt" required></label>'
        fields = (label, count, upload) if self.variant == 0 else (count, upload, label)
        content = (
            '<h1>Synthetic work request</h1><p>Fill both fields and select the synthetic file.</p>'
            f'<form action="{self._route}submit" method="post" enctype="multipart/form-data">'
            + "".join(fields) + '<button type="submit">Submit request</button></form>'
        )
        return self._html(content)

    def _html(self, content: str) -> bytes:
        """Wrap module-owned markup only; submitted labels are never rendered as HTML."""
        margin = "40px 70px" if self.variant == 0 else "95px 280px"
        return (
            '<!doctype html><html><head><meta charset="utf-8">'
            f"<title>FA M2 Desktop {html.escape(self.run_id)}</title>"
            "<style>body{font:20px sans-serif;background:#eef3f9;color:#15243a;"
            f"margin:{margin}" + "}label{display:block;margin:22px 0}"
            "input{display:block;margin-top:8px;font:20px sans-serif;padding:8px;width:440px}"
            "button,a{font:20px sans-serif;padding:12px}h1{font-size:30px}</style>"
            f"</head><body>{content}</body></html>"
        ).encode("utf-8")

    def _start_fixture(self):
        workspace = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(2)

            def log_message(self, *_args):
                pass

            def send_body(self, status, content, content_type="text/html; charset=utf-8", *, attachment=False):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'none'; connect-src 'self'; "
                    "img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                    "object-src 'none'; base-uri 'none'; frame-src 'none'; "
                    "frame-ancestors 'none'; form-action 'self'",
                )
                self.send_header("X-Content-Type-Options", "nosniff")
                if attachment:
                    self.send_header("Content-Disposition", f'attachment; filename="{workspace._download_name}"')
                self.end_headers()
                self.wfile.write(content)

            def allowed(self):
                workspace._request_count += 1
                return (
                    workspace._request_count <= 128
                    and self.headers.get("Host") == workspace._host
                    and self.path.startswith(workspace._route)
                )

            def do_GET(self):
                if not self.allowed():
                    self.send_body(403, b"Only the synthetic fixture is permitted.")
                elif self.path == workspace._route:
                    self.send_body(200, workspace._form())
                    workspace._ready.set()
                elif self.path == workspace._route + "download":
                    with workspace._lock:
                        receipt = workspace._receipt
                        if receipt is not None and len(workspace._download_requests) < 16:
                            workspace._download_requests.append({
                                "path": self.path, "size": len(receipt),
                                "sha256": hashlib.sha256(receipt).hexdigest(),
                            })
                        else:
                            receipt = None
                    if receipt is None:
                        self.send_body(404, b"No accepted request to download.")
                    else:
                        self.send_body(200, receipt, "application/json", attachment=True)
                else:
                    self.send_body(404, b"Unknown fixture path.")

            def do_POST(self):
                if (
                    not self.allowed() or self.path != workspace._route + "submit"
                    or self.headers.get("Origin") != f"http://{workspace._host}"
                ):
                    self.send_body(403, b"Only same-origin synthetic submissions are permitted.")
                    return
                length = self.headers.get("Content-Length", "")
                content_type = self.headers.get("Content-Type", "")
                if (
                    self.headers.get("Transfer-Encoding") or not re.fullmatch(r"\d{1,5}", length)
                    or not 0 < int(length) <= 16384 or len(content_type) > 256
                ):
                    self.send_body(413, b"Submission must be bounded multipart data.")
                    return
                try:
                    body = self.rfile.read(int(length))
                    if len(body) != int(length):
                        raise ValueError("incomplete body")
                    accepted = workspace._accept_submission(content_type, body)
                except (ValueError, UnicodeError):
                    self.send_body(422, b"Use both synthetic fields and only the supplied synthetic file.")
                    return
                if not accepted:
                    self.send_body(422, b"Uploaded bytes do not match the synthetic input.")
                    return
                self.send_body(200, workspace._html(
                    '<h1>Request received</h1><p>Your synthetic request has been recorded.</p>'
                    f'<p><a href="{workspace._route}download">Download receipt</a></p>'
                ))

            def do_CONNECT(self):
                self.send_body(403, b"External destinations are not permitted.")

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self._host = f"127.0.0.1:{self._server.server_address[1]}"
        self._fixture_url = f"http://{self._host}{self._route}"
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.1), daemon=True,
        )
        try:
            self._thread.start()
        except Exception:
            self._server.server_close()
            self._server = self._thread = None
            raise

    def _accept_submission(self, content_type: str, body: bytes) -> bool:
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + content_type.encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body,
        )
        if message.get_content_type() != "multipart/form-data" or not message.is_multipart():
            raise ValueError("multipart form required")
        parts = list(message.iter_parts())
        if len(parts) != 3 or message.defects:
            raise ValueError("three bounded form parts required")
        values, uploaded, filename = {}, None, None
        for part in parts:
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True)
            if (
                part.get_content_disposition() != "form-data" or part.is_multipart() or part.defects
                or name not in ("label", "count", "upload") or not isinstance(payload, bytes)
                or name in values
            ):
                raise ValueError("invalid or duplicate field")
            values[name] = payload
            if name == "upload":
                uploaded, filename = payload, part.get_filename()
            elif part.get_filename() is not None:
                raise ValueError("unexpected file field")
        label = values["label"].decode("utf-8")
        count = values["count"].decode("ascii")
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,127}", label)
            or not re.fullmatch(r"[1-9]\d{0,2}", count)
            or uploaded is None or len(uploaded) > 4096
        ):
            raise ValueError("invalid synthetic fields")
        upload = {
            "filename": str(filename)[:128], "size": len(uploaded),
            "sha256": hashlib.sha256(uploaded).hexdigest(),
            "synthetic_match": uploaded == SYNTHETIC_UPLOAD and filename == self.upload_path.name,
        }
        if upload["synthetic_match"]:
            upload["content_base64"] = base64.b64encode(uploaded).decode("ascii")
        with self._lock:
            if self._receipt is not None or self._submission_count >= 8:
                raise ValueError("submission already accepted or submission budget exhausted")
            self._submission_count += 1
            self._submission = {
                "fields": {"label": label, "count": int(count)}, "upload": upload,
                "accepted": upload["synthetic_match"],
                "native_file_dialog_seen": bool(self._dialog_observations),
            }
            if self._submission["accepted"]:
                self._receipt = (json.dumps({
                    "run_id": self.run_id, "variant": self.variant,
                    "fields": self._submission["fields"], "upload": upload,
                }, sort_keys=True, indent=2) + "\n").encode("utf-8")
            return self._submission["accepted"]

    def result(self) -> dict:
        """Independent evidence only; no model assertion can set a success flag."""
        with self._lock:
            receipt = self._receipt
            result = {
                "run_id": self.run_id, "variant": self.variant,
                "submission": copy.deepcopy(self._submission),
                "download": {"verified": False, "requests": copy.deepcopy(self._download_requests)},
                "native_file_dialog_seen": bool(self._dialog_observations),
                "native_file_dialog_observations": copy.deepcopy(self._dialog_observations),
                "actions": self._actions, "observations": self._observations,
                "cleanup": copy.deepcopy(self._cleanup),
                "limits": {"actions": MAX_ACTIONS, "observations": MAX_OBSERVATIONS, "seconds": MAX_SECONDS},
            }
        path = self._run_root / "downloads" / self._download_name
        result["download"]["path"] = str(path)
        try:
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError("download is not an owned regular file")
            if path.exists():
                if not path.is_file() or path.stat().st_size > 16384:
                    raise ValueError("download is not a bounded regular file")
                with path.open("rb") as stream:
                    downloaded = stream.read(16385)
                result["download"].update({
                    "size": len(downloaded), "sha256": hashlib.sha256(downloaded).hexdigest(),
                    "verified": bool(receipt is not None and result["download"]["requests"] and downloaded == receipt),
                })
        except (OSError, ValueError) as exc:
            result["download"]["validation_error"] = str(exc)[:256]
        result["completed"] = bool(
            result["submission"] and result["submission"]["accepted"]
            and result["submission"]["native_file_dialog_seen"]
            and result["download"]["verified"] and self._actions and self._observations
            and (self._cleanup is None or self._cleanup["success"])
        )
        if self._owns_run:
            try:
                gui._write_preferences(self._run_root / "evidence.json", result)
            except OSError as exc:
                raise Problem(500, "M2_DESKTOP_EVIDENCE_FAILED", "could not persist desktop evidence") from exc
        return result

    def close(self) -> dict:
        if self._cleanup is not None:
            return copy.deepcopy(self._cleanup)
        self._closed = True
        try:
            if self._runtime is not None:
                self._cleanup = gui.cleanup(
                    self._processes, self._server, self._thread, self._runtime, self._helpers,
                )
            else:
                self._cleanup = {"success": True, "processes": [], "server_stopped": True, "files_removed": True}
            self.result()
            if not self._cleanup["success"]:
                raise Problem(500, "M2_DESKTOP_CLEANUP_FAILED", "owned desktop cleanup did not complete",
                              cleanup=self._cleanup)
            return copy.deepcopy(self._cleanup)
        finally:
            if self._gui_lock_held:
                gui.GUI_LOCK.release()
                self._gui_lock_held = False

    def __enter__(self):
        return self.start()

    def __exit__(self, *_args):
        self.close()
