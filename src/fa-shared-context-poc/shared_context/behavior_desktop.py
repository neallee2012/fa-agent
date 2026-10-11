"""One owned Linux desktop for one Agent's Hosted process; no SDK, model, cloud or API calls.

The object starts its own Xvfb display and window manager, opens files from the Agent's
private ``files`` directory in a fixed editor, returns real pixels and executes one validated
input action at a time on that display only.

Boundaries:
- This is not an OS sandbox. Desktop apps run as the Hosted UID; separate Hosted sessions,
  not this module, keep Agents apart. Path checks only stop this API from naming other roots.
- Every input action must name the latest observation, so a lost or replayed call cannot act on
  a screen the model has not seen. Nothing is replayed after a restart.
- An unowned window, a security-looking dialog, repeated no-change input or an exhausted budget
  halts the desktop; it stays halted until closed. Detection is conservative, not complete.
- ``display_env``/``adopt`` let a later owned app (for example a Playwright-launched Chromium)
  share this display while remaining subject to the same halt rules. A window whose process descends
  from this Hosted process (the SDK's own MCP children) is adopted the same way.
- The display lives as long as its Hosted process. ``renew`` starts a new turn on it: fresh budgets,
  no carried halt and no usable earlier observation; windows and files stay.
- ``browser`` starts this desktop's own Chromium once, with a persistent profile in the private root, and returns
  its local CDP endpoint. Each turn's Playwright MCP attaches to it, so tabs and logins stay across turns.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import itertools
import os
import re
import secrets
import shutil
import signal
import stat
import struct
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import Problem

WIDTH, HEIGHT = 1280, 800
EDITOR = "l3afpad"
REQUIRED_COMMANDS = ("Xvfb", "openbox", "xdotool", "xprop", "import", EDITOR)
MAX_SCREENSHOT_BYTES = 1024 * 1024
MAX_WINDOWS = 32
DESKTOP_LOCK = threading.Lock()
# Random per-process base: an id from another desktop, or from before a Hosted restart whose SDK
# session resumes, cannot match a new screen. Values stay below 2**53 for JSON clients.
_OBSERVATION_IDS = itertools.count(secrets.randbelow(2**40) * 4096 + 1)

_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_SECURITY_TITLE = re.compile(
    r"security|privacy error|not secure|certificate|dangerous|deceptive|unsafe|malware|phishing|"
    r"warning|authenticat|password|安全|憑證|凭证|警告|密碼|密码",
    re.IGNORECASE,
)
_MODIFIERS = {"ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt", "option": "alt"}
_NAMED_KEYS = {
    "enter": "Return", "return": "Return", "tab": "Tab", "esc": "Escape", "escape": "Escape",
    "backspace": "BackSpace", "delete": "Delete", "del": "Delete", "home": "Home", "end": "End",
    "pageup": "Page_Up", "page_up": "Page_Up", "pagedown": "Page_Down", "page_down": "Page_Down",
    "up": "Up", "arrowup": "Up", "down": "Down", "arrowdown": "Down", "left": "Left",
    "arrowleft": "Left", "right": "Right", "arrowright": "Right", "space": "space",
}
_BUTTONS = {"left": "1", "right": "3"}
_INPUT_ACTIONS = frozenset({"click", "double_click", "scroll", "type", "keypress"})
_ACTION_FIELDS = {
    "click": {"type", "x", "y", "button"}, "double_click": {"type", "x", "y"},
    "move": {"type", "x", "y"}, "scroll": {"type", "x", "y", "scroll_x", "scroll_y"},
    "type": {"type", "text"}, "keypress": {"type", "keys"}, "wait": {"type"},
}
_REQUIRED_FIELDS = {"click": {"type", "x", "y"}, "scroll": {"type", "x", "y", "scroll_x", "scroll_y"}}
# The attached browser runs as the Playwright-launched one did: Playwright starts Chrome for Testing on Linux without
# its sandbox (chromiumSandbox=false) and with these first-run/keyring switches (playwright-core chromiumSwitches).
BROWSER_ARGS = (
    "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check", "--disable-default-apps",
    "--disable-dev-shm-usage", "--password-store=basic", "--use-mock-keychain", "--disable-sync",
    "--disable-search-engine-choice-screen", "--disable-infobars", "--hide-crash-restore-bubble", "--no-sandbox",
    f"--window-size={WIDTH},{HEIGHT}", "--window-position=0,0",
)
# A profile left by a browser that no longer runs; only this desktop starts a browser on the profile.
_BROWSER_STALE = ("SingletonLock", "SingletonSocket", "SingletonCookie", "DevToolsActivePort")


@dataclass(frozen=True)
class DesktopLimits:
    max_actions: int = 60
    max_observations: int = 150
    max_seconds: float = 1200.0
    settle_seconds: float = 1.5
    settle_interval: float = 0.15
    max_unchanged_actions: int = 3
    max_editors: int = 2
    max_file_bytes: int = 64 * 1024
    max_text_chars: int = 1024


class SubprocessRunner:
    """The only place that touches the host; tests replace it with a scripted runner."""

    def which(self, name):
        return shutil.which(name)

    def euid(self):
        return os.geteuid() if hasattr(os, "geteuid") else None

    def is_linux(self):
        return os.name == "posix" and os.uname().sysname == "Linux"

    def spawn(self, args, env, log_path: Path):
        with open(log_path, "wb") as log:
            return subprocess.Popen(
                args, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                close_fds=True, start_new_session=True,
            )

    def run(self, args, env, timeout=10.0):
        return subprocess.run(args, env=env, capture_output=True, timeout=timeout, check=False)

    def stop(self, process):
        if process.poll() is not None:
            return True
        for sig, wait in ((signal.SIGTERM, 3), (signal.SIGKILL, 3)):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                return True
            try:
                process.wait(timeout=wait)
                return True
            except subprocess.TimeoutExpired:
                continue
        return False

    def sleep(self, seconds):
        time.sleep(seconds)

    def monotonic(self):
        return time.monotonic()

    def self_pid(self):
        return os.getpid()

    def parent(self, pid):
        """The parent process ID from /proc, or None; the command name may contain spaces and parentheses."""
        try:
            text = Path(f"/proc/{int(pid)}/stat").read_text(encoding="ascii", errors="replace")
            return int(text[text.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            return None


def prerequisites(runner=None) -> int:
    runner = runner or SubprocessRunner()
    if not runner.is_linux():
        raise Problem(503, "DESKTOP_UNAVAILABLE", "the owned desktop requires a Linux Hosted runtime")
    uid = runner.euid()
    if uid in (None, 0):
        raise Problem(503, "DESKTOP_UNAVAILABLE", "the owned desktop must run as a non-root user")
    missing = [name for name in REQUIRED_COMMANDS if runner.which(name) is None]
    if missing:
        raise Problem(503, "DESKTOP_UNAVAILABLE", "desktop commands missing: " + ", ".join(missing))
    return uid


def _png_size(data: bytes):
    if (not isinstance(data, bytes) or not 24 <= len(data) <= MAX_SCREENSHOT_BYTES
            or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR"):
        return None
    return struct.unpack(">II", data[16:24])


def _key_chord(keys) -> str:
    invalid = Problem(422, "DESKTOP_ACTION", "keys must be up to three modifiers and one allowed key")
    if not isinstance(keys, list) or not 1 <= len(keys) <= 4 or not all(isinstance(key, str) for key in keys):
        raise invalid
    *modifiers, last = [key.strip() for key in keys]
    chord = []
    for modifier in modifiers:
        name = _MODIFIERS.get(modifier.lower())
        if name is None or name in chord:
            raise invalid
        chord.append(name)
    if last.lower() in _NAMED_KEYS:
        chord.append(_NAMED_KEYS[last.lower()])
    elif re.fullmatch(r"[A-Za-z0-9]", last):
        chord.append(last.lower() if modifiers else last)
    else:
        raise invalid
    return "+".join(chord)


def _coordinate(action, name, limit):
    value = action[name]
    if type(value) is not int or not 0 <= value < limit:
        raise Problem(422, "DESKTOP_ACTION", f"{name} must be an integer pixel inside the screenshot")
    return str(value)


def action_commands(action, limits: DesktopLimits = DesktopLimits()):
    """Validate one action and return the exact argv lists; nothing is interpreted by a shell."""
    if not isinstance(action, dict) or action.get("type") not in _ACTION_FIELDS:
        raise Problem(422, "DESKTOP_ACTION", "unsupported desktop action")
    kind = action["type"]
    fields = set(action)
    required = _REQUIRED_FIELDS.get(kind, _ACTION_FIELDS[kind])
    if not required <= fields <= _ACTION_FIELDS[kind]:
        raise Problem(422, "DESKTOP_ACTION", f"{kind} accepts exactly: {', '.join(sorted(_ACTION_FIELDS[kind]))}")
    if kind in ("click", "double_click", "move", "scroll"):
        x, y = _coordinate(action, "x", WIDTH), _coordinate(action, "y", HEIGHT)
        # No --sync: xdotool 3.20160805.1 then waits ~15 s for the pointer to leave its old spot, so a move or
        # click at the current position (Xvfb starts at the centre) always hit the 10 s timeout. The warp and the
        # click are ordered on one X connection, and xdotool's XCloseDisplay syncs before the next command runs.
        move = ["xdotool", "mousemove", x, y]
    if kind == "click":
        button = _BUTTONS.get(action.get("button", "left"))
        if button is None:
            raise Problem(422, "DESKTOP_ACTION", "button must be left or right")
        return [move + ["click", button]]
    if kind == "double_click":
        return [move + ["click", "--repeat", "2", "--delay", "80", "1"]]
    if kind == "move":
        return [move]
    if kind == "scroll":
        commands = [move]
        for name, negative, positive in (("scroll_y", "4", "5"), ("scroll_x", "6", "7")):
            delta = action[name]
            if type(delta) is not int or abs(delta) > 2000:
                raise Problem(422, "DESKTOP_ACTION", "scroll deltas must be integers within 2000 pixels")
            if delta:
                clicks = str(min(20, max(1, round(abs(delta) / 100))))
                commands.append(["xdotool", "click", "--repeat", clicks, negative if delta < 0 else positive])
        return commands
    if kind == "type":
        text = action["text"]
        if (not isinstance(text, str) or not 1 <= len(text) <= limits.max_text_chars
                or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", text)):
            raise Problem(422, "DESKTOP_ACTION", "text must be 1-1024 characters without control characters")
        # xdotool's own newline/tab translation is unreliable in GTK text views; send real keys.
        commands = []
        for part in re.split(r"([\n\t])", text):
            if part in ("\n", "\t"):
                commands.append(["xdotool", "key", "--clearmodifiers", "Return" if part == "\n" else "Tab"])
            elif part:
                commands.append(["xdotool", "type", "--clearmodifiers", "--delay", "5", "--", part])
        return commands
    if kind == "keypress":
        return [["xdotool", "key", "--clearmodifiers", _key_chord(action["keys"])]]
    return []


def _serialized(method):
    """One public call at a time, so two tool calls cannot both consume one observation."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class OwnedDesktop:
    """One desktop per Hosted process for the caller's already-authorized private root."""

    def __init__(self, root: Path, *, runner=None, limits: DesktopLimits | None = None):
        if not isinstance(root, Path) or not root.is_absolute():
            raise Problem(422, "DESKTOP_INPUT", "root must be an absolute private directory")
        self.root = root
        self.runner = runner or SubprocessRunner()
        self.limits = limits or DesktopLimits()
        self.files = root / "files"
        self._env = {}
        self._runtime = None
        self._display = None
        self._processes = []
        self._core = []
        self._owners = {}
        self._editors = 0
        self._lock_held = self._started = self._closed = False
        self._started_at = 0.0
        self._actions = self._observations = self._unchanged = 0
        self._observation_id = None
        self._lock = threading.RLock()
        self._last_sha = None
        self._halt = None
        self._browser = self._cdp = None

    # Lifecycle -------------------------------------------------------------------------

    @_serialized
    def start(self):
        if self._started or self._closed:
            raise Problem(409, "DESKTOP_STATE", "a desktop object starts once")
        uid = prerequisites(self.runner)
        self._check_root(uid)
        if not DESKTOP_LOCK.acquire(blocking=False):
            raise Problem(409, "DESKTOP_BUSY", "this Hosted process already owns a desktop")
        self._lock_held = True
        try:
            if self.files.is_symlink():
                raise RuntimeError("the private files directory must not be a symlink")
            self.files.mkdir(mode=0o700, exist_ok=True)
            self._runtime = Path(tempfile.mkdtemp(prefix="fa-desktop-"))
            runtime = self._runtime / "rt"
            runtime.mkdir(mode=0o700)
            gtk = self._runtime / "config" / "gtk-3.0"
            gtk.mkdir(parents=True, mode=0o700)
            # Static carets make screenshot hashes meaningful for settle and no-progress checks.
            (gtk / "settings.ini").write_text("[Settings]\ngtk-cursor-blink = false\n", encoding="utf-8")
            self._env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
                "HOME": str(self._runtime), "TMPDIR": str(runtime), "XDG_RUNTIME_DIR": str(runtime),
                "XDG_CONFIG_HOME": str(self._runtime / "config"),
                "XDG_CACHE_HOME": str(self._runtime / "cache"),
                "XDG_DATA_HOME": str(self._runtime / "data"),
                "GTK_USE_PORTAL": "0", "NO_AT_BRIDGE": "1",
            }
            log = self._runtime / "xvfb.log"
            xvfb = self._spawn([
                "Xvfb", "-displayfd", "1", "-screen", "0", f"{WIDTH}x{HEIGHT}x24", "-nolisten", "tcp",
            ], log, owner=None)
            self._core.append(xvfb)
            deadline = self.runner.monotonic() + 10
            while True:
                text = log.read_text(errors="replace") if log.exists() else ""
                number = next((line for line in text.splitlines() if re.fullmatch(r"\d{1,5}", line.strip())), None)
                if number is not None:
                    self._display = ":" + number.strip()
                    self._env["DISPLAY"] = self._display
                    break
                if xvfb.poll() is not None or self.runner.monotonic() >= deadline:
                    raise RuntimeError("the owned Xvfb display did not become ready")
                self.runner.sleep(0.1)
            self._core.append(self._spawn(["openbox"], self._runtime / "openbox.log", owner="wm"))
            # Apps mapped before the WM owns the screen can stay unmapped; wait for EWMH readiness.
            deadline = self.runner.monotonic() + 10
            while "window id #" not in self._run(
                    ["xprop", "-root", "_NET_SUPPORTING_WM_CHECK"]).stdout.decode("ascii", "replace"):
                if self._core[-1].poll() is not None or self.runner.monotonic() >= deadline:
                    raise RuntimeError("the owned window manager did not become ready")
                self.runner.sleep(0.1)
            # openbox publishes that property before it reliably manages new windows: local image
            # runs left 2 of 12 immediate editor windows unmapped, and 0 of 16 after this pause.
            self.runner.sleep(1.0)
            self._started_at = self.runner.monotonic()
            self._started = True
            return self
        except Problem:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise Problem(503, "DESKTOP_START_FAILED", str(exc)) from exc

    @_serialized
    def close(self) -> dict:
        if self._closed:
            return {"closed": True}
        self._closed = True
        stopped = all([self.runner.stop(process) for process in reversed(self._processes)])
        removed = True
        if self._runtime is not None:
            shutil.rmtree(self._runtime, ignore_errors=True)
            removed = not self._runtime.exists()
        if self._lock_held:
            DESKTOP_LOCK.release()
            self._lock_held = False
        return {"closed": True, "processes_stopped": stopped, "runtime_removed": removed}

    @_serialized
    def renew(self) -> bool:
        """Start a new turn on this running display; False if it has exited and must be replaced.

        Budgets restart, a halt is cleared and no earlier observation can act. Open windows stay, so a
        warning that caused a halt halts again on the next observation; only editors still open count."""
        if not self._started or self._closed or any(process.poll() is not None for process in self._core):
            return False
        self._started_at = self.runner.monotonic()
        self._actions = self._observations = self._unchanged = 0
        self._observation_id = self._halt = None
        self._editors = self._live_editors()
        return True

    def _live_editors(self) -> int:
        return sum(1 for process in self._processes
                   if self._owners.get(process.pid) == "editor" and process.poll() is None)

    @_serialized
    def display_env(self) -> dict:
        """Environment for another owned GUI process on this display; secrets are never included."""
        self._require_running()
        return dict(self._env)

    @_serialized
    def browser(self, executable: str) -> str:
        """This desktop's own Chromium, started once on its display; returns its local CDP endpoint.

        The profile lives in the private root (``browser/``), so logins outlive the browser too. A browser that has
        exited is started again on the same profile. Playwright MCP attached over CDP only disconnects when it stops,
        so tabs stay open across turns (like Grokbot's browser)."""
        self._require_running()
        if self._browser is not None and self._browser.poll() is None:
            return self._cdp
        profile = self.root / "browser"
        if profile.is_symlink():
            raise Problem(503, "DESKTOP_BROWSER_FAILED", "the private browser profile must not be a symlink")
        profile.mkdir(mode=0o700, exist_ok=True)
        for name in _BROWSER_STALE:
            if os.path.lexists(profile / name):
                os.unlink(profile / name)
        process = self._spawn([executable, *BROWSER_ARGS, f"--user-data-dir={profile}", "about:blank"],
                              self._runtime / f"browser-{len(self._processes)}.log", owner="adopted")
        # Chromium writes the chosen port to DevToolsActivePort in the profile once it listens on 127.0.0.1.
        deadline = self.runner.monotonic() + 20
        while True:
            try:
                port = (profile / "DevToolsActivePort").read_text(encoding="ascii", errors="replace").split("\n")[0]
            except OSError:
                port = ""
            if re.fullmatch(r"[1-9]\d{0,4}", port.strip()) and int(port) < 65536:
                self._browser, self._cdp = process, f"http://127.0.0.1:{int(port)}"
                return self._cdp
            if process.poll() is not None or self.runner.monotonic() >= deadline:
                self.runner.stop(process)
                raise Problem(503, "DESKTOP_BROWSER_FAILED", "the desktop browser did not start")
            self.runner.sleep(0.1)

    @_serialized
    def adopt(self, pid: int):
        """Treat an app the caller launched with display_env as owned; its titles stay checked."""
        self._require_running()
        if type(pid) is not int or pid <= 1:
            raise Problem(422, "DESKTOP_INPUT", "pid must be a process identifier")
        self._owners[pid] = "adopted"

    # Files -----------------------------------------------------------------------------

    @_serialized
    def open_file(self, name: str, *, create: bool = False) -> dict:
        self._require_active()
        path = self._file_path(name)
        if create and not os.path.lexists(path):
            try:
                os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600))
            except FileExistsError:
                pass
        self._regular_file(path)
        # Editors stay open across turns; one closed in this turn frees its slot right away.
        self._editors = self._live_editors()
        if self._editors >= self.limits.max_editors:
            # Editors stay open across turns; the model can keep using one or close one, then retry.
            raise Problem(429, "DESKTOP_EDITORS_OPEN", "too many editor windows are open")
        before = self._capture()
        # The screen is about to change; the model must observe it before any further input.
        self._observation_id = None
        editor = self._spawn([EDITOR, str(path)], self._runtime / f"editor-{len(self._processes)}.log", owner="editor")
        self._editors += 1
        deadline = self.runner.monotonic() + 20
        while not any(window["pid"] == editor.pid and window["window_type"] == "normal"
                      for window in self._windows()):
            if editor.poll() is not None or self.runner.monotonic() >= deadline:
                raise Problem(503, "DESKTOP_APP_FAILED", "the editor window did not appear")
            self.runner.sleep(0.1)
        return self._observation(before=before)

    @_serialized
    def read_file(self, name: str) -> dict:
        self._require_running()
        path = self._file_path(name)
        data = self._read_regular(path)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Problem(422, "DESKTOP_FILE_ENCODING", "only UTF-8 text files can be read back") from exc
        return {"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "text": text}

    # Pixels and input ------------------------------------------------------------------

    @_serialized
    def observe(self) -> dict:
        self._require_running()
        return self._observation()

    @_serialized
    def act(self, observation_id: int, action: dict) -> dict:
        self._require_active()
        if type(observation_id) is not int or observation_id != self._observation_id:
            raise Problem(409, "DESKTOP_OBSERVATION_STALE",
                          "act only on the latest observation; observe again instead of resending")
        commands = action_commands(action, self.limits)
        if self._actions >= self.limits.max_actions:
            self._halt_with("action_budget")
            raise Problem(429, "DESKTOP_LIMIT", "desktop action budget exhausted")
        windows = self._windows()
        if self._check_halt(windows):
            raise Problem(409, "DESKTOP_HALTED", "the desktop stopped before input; a human must review it")
        before = self._capture()
        self._actions += 1
        # Consume the observation before input so a failure never permits a blind resend.
        self._observation_id = None
        for command in commands:
            try:
                failed = self._run(command).returncode != 0
            except Problem:
                failed = True
            if failed:
                self._halt_with("input_failed")
                raise Problem(503, "DESKTOP_ACTION_FAILED", "the input command failed; a human must review it")
        if action["type"] == "wait":
            self.runner.sleep(1.0)
        observation = self._observation(before=before)
        if action["type"] in _INPUT_ACTIONS:
            unchanged = observation["sha256"] == hashlib.sha256(before).hexdigest()
            self._unchanged = self._unchanged + 1 if unchanged else 0
            if self._unchanged >= self.limits.max_unchanged_actions and self._halt is None:
                self._halt_with("no_progress")
                observation["halted"] = dict(self._halt)
        return observation

    # Internals -------------------------------------------------------------------------

    def _check_root(self, uid):
        try:
            info = os.lstat(self.root)
        except FileNotFoundError as exc:
            raise Problem(422, "DESKTOP_INPUT", "private root must already exist") from exc
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or self.root.resolve() != self.root):
            raise Problem(403, "DESKTOP_ROOT_DENIED", "private root must be a real directory without symlinks")
        if os.name == "posix" and (info.st_uid != uid or info.st_mode & 0o077):
            raise Problem(403, "DESKTOP_ROOT_DENIED", "private root must belong only to this Hosted user")

    def _file_path(self, name) -> Path:
        if not isinstance(name, str) or not _FILE_NAME.fullmatch(name) or ".." in name:
            raise Problem(403, "DESKTOP_FILE_DENIED", "only a plain file name inside this Agent's files directory")
        return self.files / name

    def _regular_file(self, path: Path):
        try:
            info = os.lstat(path)
        except FileNotFoundError as exc:
            raise Problem(404, "DESKTOP_FILE_NOT_FOUND", "no such private file") from exc
        if not stat.S_ISREG(info.st_mode):
            raise Problem(403, "DESKTOP_FILE_DENIED", "private files must be regular files, not links")
        if info.st_size > self.limits.max_file_bytes:
            raise Problem(413, "DESKTOP_FILE_LIMIT", "private file exceeds the desktop read bound")
        return info

    def _read_regular(self, path: Path) -> bytes:
        self._regular_file(path)
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise Problem(403, "DESKTOP_FILE_DENIED", "private file could not be opened safely") from exc
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise Problem(403, "DESKTOP_FILE_DENIED", "private files must be regular files")
            data = handle.read(self.limits.max_file_bytes + 1)
        if len(data) > self.limits.max_file_bytes:
            raise Problem(413, "DESKTOP_FILE_LIMIT", "private file exceeds the desktop read bound")
        return data

    def _spawn(self, args, log: Path, *, owner):
        process = self.runner.spawn(args, dict(self._env), log)
        self._processes.append(process)
        if owner is not None:
            self._owners[process.pid] = owner
        return process

    def _require_running(self):
        if not self._started or self._closed:
            raise Problem(409, "DESKTOP_STATE", "the desktop is not running")
        if self.runner.monotonic() - self._started_at >= self.limits.max_seconds and self._halt is None:
            self._halt_with("time_budget")
        if any(process.poll() is not None for process in self._core) and self._halt is None:
            self._halt_with("display_exited")

    def _require_active(self):
        self._require_running()
        if self._halt is not None:
            raise Problem(409, "DESKTOP_HALTED", f"desktop halted ({self._halt['reason']}); a human must review it")

    def _halt_with(self, reason, title=None, cause=None):
        if self._halt is None:
            self._halt = {"reason": reason, **({"cause": cause} if cause else {}),
                          **({"title": title[:128]} if title else {})}

    def _check_halt(self, windows) -> bool:
        if self._halt is not None:
            return True
        for window in windows:
            owner = window["owner"]
            if owner is None:
                # A window this desktop did not start may be a warning from elsewhere; treat it as one.
                self._halt_with("security_warning", window["title"], "unowned_window")
            elif (owner == "adopted" or window["window_type"] == "dialog") and _SECURITY_TITLE.search(window["title"]):
                self._halt_with("security_warning", window["title"], "warning_title")
            if self._halt is not None:
                return True
        return False

    def _run(self, args):
        try:
            return self.runner.run(args, self._env, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            raise Problem(503, "DESKTOP_COMMAND_FAILED", "an owned desktop command could not complete") from exc

    def _capture(self) -> bytes:
        result = self._run(["import", "-display", self._display, "-window", "root", "-depth", "8", "png:-"])
        data = result.stdout
        if result.returncode != 0 or _png_size(data) != (WIDTH, HEIGHT):
            raise Problem(503, "DESKTOP_OBSERVE_FAILED", "the owned display returned no valid screenshot")
        return data

    def _observation(self, *, before: bytes | None = None) -> dict:
        """Without ``before`` return the current frame; otherwise wait for a changed, stable frame.

        A frame equal to the pre-action frame is not treated as settled until the settle
        window ends, so slow dialogs are not reported from a stale screenshot.
        """
        if self._observations >= self.limits.max_observations:
            self._halt_with("observation_budget")
            raise Problem(429, "DESKTOP_LIMIT", "desktop observation budget exhausted")
        data, settled = self._capture(), before is None
        if before is not None:
            deadline = self.runner.monotonic() + self.limits.settle_seconds
            while self.runner.monotonic() < deadline:
                self.runner.sleep(self.limits.settle_interval)
                latest = self._capture()
                stable, data = latest == data, latest
                if stable and data != before:
                    settled = True
                    break
            else:
                settled = data == self._capture()
        windows = self._windows()
        self._check_halt(windows)
        self._observations += 1
        self._observation_id = next(_OBSERVATION_IDS)
        self._last_sha = hashlib.sha256(data).hexdigest()
        return {
            "observation_id": self._observation_id, "png_base64": base64.b64encode(data).decode("ascii"),
            "mime_type": "image/png", "sha256": self._last_sha, "width": WIDTH, "height": HEIGHT,
            "settled": settled, "windows": windows, "halted": dict(self._halt) if self._halt else None,
            "actions_left": max(0, self.limits.max_actions - self._actions),
        }

    def _descendant(self, pid: int) -> bool:
        """True for a process started under this Hosted process, e.g. Chromium from the SDK's Playwright MCP."""
        parent = getattr(self.runner, "parent", None)
        if parent is None:
            return False
        own = self.runner.self_pid()
        for _ in range(64):
            pid = parent(pid)
            # Checked first: in the Hosted container this process is PID 1 (exec-form CMD, no init).
            if pid == own:
                return True
            if pid is None or pid <= 1:
                return False
        return False

    def _windows(self) -> list[dict]:
        found = self._run(["xdotool", "search", "--onlyvisible", "--name", "."])
        ids = [window_id for window_id in dict.fromkeys(found.stdout.decode("ascii", "replace").split())
               if re.fullmatch(r"\d{1,12}", window_id)]
        if len(ids) > MAX_WINDOWS:
            # Unchecked windows could hide a warning; keep the topmost ones and stop input.
            self._halt_with("too_many_windows")
            ids = ids[-MAX_WINDOWS:]
        windows = []
        for window_id in ids:
            title = self._run(["xdotool", "getwindowname", window_id]).stdout
            properties = self._run(
                ["xprop", "-id", window_id, "_NET_WM_PID", "_NET_WM_WINDOW_TYPE"],
            ).stdout.decode("utf-8", "replace")
            pid = re.search(r"_NET_WM_PID\(CARDINAL\)\s*=\s*(\d+)", properties)
            kind = ("dialog" if "_NET_WM_WINDOW_TYPE_DIALOG" in properties
                    else "normal" if "_NET_WM_WINDOW_TYPE_NORMAL" in properties else "other")
            number = int(pid[1]) if pid else None
            owner = None if number is None else self._owners.get(number)
            if owner is None and number is not None and self._descendant(number):
                owner = "adopted"
            windows.append({
                "id": window_id, "title": title.decode("utf-8", "replace").strip()[:256],
                "window_type": kind, "pid": number, "owner": owner,
            })
        return windows
