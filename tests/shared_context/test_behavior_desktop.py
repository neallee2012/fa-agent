"""CAP-05 offline cases for the owned desktop: private file round trip, Agent/path limits,
display/env isolation, security-warning halt, observation-bound input and no blind replay."""

import hashlib
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

import pytest

from shared_context.behavior_desktop import (
    DESKTOP_LOCK, EDITOR, HEIGHT, WIDTH, DesktopLimits, OwnedDesktop, SubprocessRunner, action_commands,
    prerequisites,
)
from shared_context.errors import Problem

ALLOWED_ENV = {"PATH", "LANG", "HOME", "TMPDIR", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
               "XDG_DATA_HOME", "GTK_USE_PORTAL", "NO_AT_BRIDGE", "DISPLAY"}
_FRAMES = {}


def png(seed: int) -> bytes:
    if seed not in _FRAMES:
        def chunk(kind, content):
            return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content))
        row = b"\x00" + bytes([seed % 251]) * (WIDTH * 3)
        _FRAMES[seed] = (b"\x89PNG\r\n\x1a\n"
                         + chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0))
                         + chunk(b"IDAT", zlib.compress(row * HEIGHT, 1)) + chunk(b"IEND", b""))
    return _FRAMES[seed]


class Result:
    def __init__(self, stdout=b"", returncode=0):
        self.stdout, self.returncode = stdout, returncode


class Process:
    def __init__(self, pid):
        self.pid, self.alive = pid, True

    def poll(self):
        return None if self.alive else 0


class FakeRunner:
    """Scripted X display with a minimal editor: typed text is buffered and ctrl+s saves it."""

    def __init__(self):
        self.clock, self.next_pid = 1000.0, 100
        self.frame, self.pending_change, self.freeze, self.change_delay = 0, 0, False, 0
        self.spawned, self.runs, self.inputs = [], [], []
        self.windows = {}
        self.editor_path, self.buffer = None, ""
        self.fail_input = False
        self.editor_fails, self.unstoppable, self.stopped = False, set(), []
        self.search_delay = 0.0
        self.fail_capture_after_input = self.fail_capture_now = False
        self.browser_port, self.browser_title = 9222, "about:blank - Chrome for Testing"
        # Xvfb starts the pointer at the screen centre.
        self.pointer = (WIDTH // 2, HEIGHT // 2)

    def which(self, name):
        return "/usr/bin/" + name

    def euid(self):
        return os.geteuid() if hasattr(os, "geteuid") else 10001

    def is_linux(self):
        return True

    def spawn(self, args, env, log_path):
        self.next_pid += 1
        process = Process(self.next_pid)
        self.spawned.append((list(args), dict(env)))
        if args[0] == "Xvfb":
            Path(log_path).write_text("7\n")
        elif Path(args[0]).name == "chrome":
            # Chromium reports the debugging port it chose in the profile; a port of None never becomes ready.
            profile = Path(next(arg for arg in args if arg.startswith("--user-data-dir="))[len("--user-data-dir="):])
            if self.browser_port is not None:
                (profile / "DevToolsActivePort").write_text(f"{self.browser_port}\n/devtools/browser/OFFLINE\n")
                self.add_window(self.browser_title, process.pid, "NORMAL")
        elif args[0] == EDITOR and self.editor_fails:
            process.alive = False
        elif args[0] == EDITOR:
            self.editor_path = Path(args[1])
            self.buffer = self.editor_path.read_bytes().decode("utf-8")
            self.add_window(f"{self.editor_path.name} - L3afpad", process.pid, "NORMAL")
            self.frame += 1
        return process

    def add_window(self, title, pid, kind="NORMAL"):
        window_id = str(5000 + len(self.windows))
        self.windows[window_id] = (title, pid, kind)
        return window_id

    def _changed(self):
        if not self.freeze:
            self.pending_change = self.change_delay + 1

    def run(self, args, env, timeout=10):
        self.runs.append((list(args), dict(env)))
        if args[0] == "import":
            if self.fail_capture_now:
                self.fail_capture_now = False
                return Result(returncode=1)
            if self.pending_change:
                self.pending_change -= 1
                if not self.pending_change:
                    self.frame += 1
            return Result(png(self.frame))
        if args[:2] == ["xdotool", "search"]:
            if self.search_delay:
                time.sleep(self.search_delay)
            return Result(" ".join(self.windows).encode())
        if args[:2] == ["xdotool", "getwindowname"]:
            return Result(self.windows[args[2]][0].encode())
        if args[:2] == ["xprop", "-root"]:
            return Result(b"_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x400001\n")
        if args[0] == "xprop":
            title, pid, kind = self.windows[args[2]]
            pid_line = f"_NET_WM_PID(CARDINAL) = {pid}\n" if pid else ""
            return Result((pid_line + f"_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_{kind}\n").encode())
        self.inputs.append(list(args))
        if self.fail_capture_after_input:
            self.fail_capture_after_input, self.fail_capture_now = False, True
        if self.fail_input:
            return Result(returncode=1)
        if args[:2] == ["xdotool", "mousemove"]:
            sync = args[2] == "--sync"
            target = (int(args[2 + sync]), int(args[3 + sync]))
            if sync and target == self.pointer:
                # xdotool 3.20160805.1 cmd_mousemove.c: --sync then waits ~15 s for the pointer to leave its spot.
                raise subprocess.TimeoutExpired(args, timeout)
            self.pointer = target
        if args[:2] == ["xdotool", "type"]:
            self.buffer += args[-1]
        elif args[:2] == ["xdotool", "key"] and args[-1] in ("Return", "Tab"):
            self.buffer += "\n" if args[-1] == "Return" else "\t"
        elif args[:2] == ["xdotool", "key"] and args[-1] == "ctrl+s" and self.editor_path:
            self.editor_path.write_bytes(self.buffer.encode("utf-8"))
        self._changed()
        return Result()

    def stop(self, process):
        self.stopped.append(process.pid)
        if process.pid in self.unstoppable:
            return False
        process.alive = False
        return True

    def sleep(self, seconds):
        self.clock += seconds

    def monotonic(self):
        return self.clock


def private_root(base: Path, name="agent-a") -> Path:
    root = base / name
    root.mkdir(mode=0o700)
    (root / "files").mkdir(mode=0o700)
    return root.resolve()


@pytest.fixture
def desktop(tmp_path):
    root = private_root(tmp_path)
    (root / "files" / "notes.txt").write_bytes(b"line1\nline2\n")
    runner = FakeRunner()
    owned = OwnedDesktop(root, runner=runner).start()
    yield owned, runner, root
    owned.close()


def code(error):
    return error.value.body["error"]["code"]


# C1: open, edit, save and read back a private file through the same act interface.
def test_c1_private_file_round_trip(desktop):
    owned, runner, root = desktop
    observation = owned.open_file("notes.txt")
    assert observation["halted"] is None and observation["settled"]
    for action in ({"type": "click", "x": 640, "y": 500}, {"type": "type", "text": "line3\n"},
                   {"type": "keypress", "keys": ["ctrl", "s"]}):
        observation = owned.act(observation["observation_id"], action)
        assert observation["halted"] is None
    read = owned.read_file("notes.txt")
    expected = b"line1\nline2\nline3\n"
    assert read == {"name": "notes.txt", "bytes": len(expected),
                    "sha256": hashlib.sha256(expected).hexdigest(), "text": expected.decode()}
    assert runner.inputs == [
        ["xdotool", "mousemove", "640", "500", "click", "1"],
        ["xdotool", "type", "--clearmodifiers", "--delay", "5", "--", "line3"],
        ["xdotool", "key", "--clearmodifiers", "Return"],
        ["xdotool", "key", "--clearmodifiers", "ctrl+s"],
    ]
    runtime = Path(owned.display_env()["HOME"])
    assert owned.close() == {"closed": True, "processes_stopped": True, "runtime_removed": True}
    assert not runtime.exists() and (root / "files" / "notes.txt").read_bytes() == expected


@pytest.mark.skipif(not sys.platform.startswith("linux") or shutil.which(EDITOR) is None,
                    reason="real desktop round trip needs the Linux desktop image")
def test_c1_real_display_round_trip(tmp_path):
    prerequisites()
    root = private_root(tmp_path)
    (root / "files" / "notes.txt").write_bytes(b"line1\nline2\n")
    owned = OwnedDesktop(root).start()
    try:
        observation = owned.open_file("notes.txt")
        # openbox centres the first window; this point is below the text, so the caret lands on the last line.
        for action in ({"type": "click", "x": 640, "y": 500}, {"type": "type", "text": "line3\n"},
                       {"type": "keypress", "keys": ["ctrl", "s"]}, {"type": "wait"}):
            observation = owned.act(observation["observation_id"], action)
            assert observation["halted"] is None, observation["halted"]
        assert owned.read_file("notes.txt")["text"] == "line1\nline2\nline3\n"
    finally:
        cleanup = owned.close()
    assert cleanup["processes_stopped"] and cleanup["runtime_removed"]


# C2: only plain names inside this Agent's files directory; nothing is launched otherwise.
@pytest.mark.parametrize("name", ["../agent-b/files/notes.txt", "/etc/passwd", "..", "a/b", "C:\\x",
                                  "", ".hidden", "x" * 65, None])
def test_c2_names_outside_private_files_are_denied(desktop, name):
    owned, runner, _ = desktop
    spawned = len(runner.spawned)
    for call in (owned.open_file, owned.read_file):
        with pytest.raises(Problem) as error:
            call(name)
        assert code(error) == "DESKTOP_FILE_DENIED"
    assert len(runner.spawned) == spawned and runner.inputs == []


def test_c2_links_missing_files_and_other_agent_roots_are_denied(tmp_path):
    other = private_root(tmp_path, "agent-b")
    (other / "files" / "secret.txt").write_text("agent-b only", encoding="utf-8")
    root = private_root(tmp_path)
    runner = FakeRunner()
    owned = OwnedDesktop(root, runner=runner).start()
    try:
        with pytest.raises(Problem) as error:
            owned.open_file("missing.txt")
        assert code(error) == "DESKTOP_FILE_NOT_FOUND"
        try:
            os.symlink(other / "files" / "secret.txt", root / "files" / "link.txt")
        except OSError:
            pytest.skip("symlinks are not permitted on this host")
        for call in (owned.open_file, owned.read_file):
            with pytest.raises(Problem) as error:
                call("link.txt")
            assert code(error) == "DESKTOP_FILE_DENIED"
        assert not any(args[0] == EDITOR for args, _ in runner.spawned)
    finally:
        owned.close()
    linked_root = tmp_path / "linked-root"
    os.symlink(other, linked_root, target_is_directory=True)
    with pytest.raises(Problem) as error:
        OwnedDesktop(linked_root, runner=FakeRunner()).start()
    assert code(error) == "DESKTOP_ROOT_DENIED"


def test_c2_missing_or_shared_roots_are_denied(tmp_path):
    with pytest.raises(Problem) as error:
        OwnedDesktop(tmp_path / "absent", runner=FakeRunner()).start()
    assert code(error) == "DESKTOP_INPUT"
    with pytest.raises(Problem) as error:
        OwnedDesktop(Path("relative"), runner=FakeRunner())
    assert code(error) == "DESKTOP_INPUT"
    if os.name == "posix":
        shared = private_root(tmp_path, "shared")
        shared.chmod(0o770)
        with pytest.raises(Problem) as error:
            OwnedDesktop(shared, runner=FakeRunner()).start()
        assert code(error) == "DESKTOP_ROOT_DENIED"
    assert not DESKTOP_LOCK.locked()


# C3: one desktop per Hosted process; every child and command sees only its own display
# and an allowlisted environment.
def test_c3_one_display_per_process_and_allowlisted_child_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("FA_API_TICKET", "ticket-must-not-reach-gui")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "secret-must-not-reach-gui")
    first = private_root(tmp_path, "agent-a")
    (first / "files" / "notes.txt").write_text("x", encoding="utf-8")
    runner = FakeRunner()
    owned = OwnedDesktop(first, runner=runner).start()
    second = OwnedDesktop(private_root(tmp_path, "agent-b"), runner=FakeRunner())
    with pytest.raises(Problem) as error:
        second.start()
    assert code(error) == "DESKTOP_BUSY"
    observation = owned.open_file("notes.txt")
    owned.act(observation["observation_id"], {"type": "click", "x": 10, "y": 10})
    environments = [env for _, env in runner.spawned] + [env for _, env in runner.runs] + [owned.display_env()]
    for env in environments:
        assert set(env) <= ALLOWED_ENV
        assert "ticket-must-not-reach-gui" not in env.values() and "secret-must-not-reach-gui" not in env.values()
    assert all(env["DISPLAY"] == ":7" for _, env in runner.runs)
    assert all(args[2] == ":7" for args, _ in runner.runs if args[0] == "import")
    owned.close()
    assert not DESKTOP_LOCK.locked()
    second.start().close()


# C4: a security-looking dialog or any window this desktop did not start stops all input.
@pytest.mark.parametrize("title,owner,kind,cause", [
    ("Security Warning", "editor", "DIALOG", "warning_title"),
    ("憑證警告", "editor", "DIALOG", "warning_title"),
    ("xmessage", None, "NORMAL", "unowned_window"),
    ("Privacy error - Chromium", "adopted", "NORMAL", "warning_title"),
])
def test_c4_security_warning_or_unowned_window_halts(desktop, title, owner, kind, cause):
    owned, runner, _ = desktop
    observation = owned.open_file("notes.txt")
    editor_pid = next(pid for _, pid, _ in runner.windows.values())
    pid = {"editor": editor_pid, "adopted": 4242, None: 0}[owner]
    if owner == "adopted":
        owned.adopt(4242)
    runner.add_window(title, pid, kind)
    halted = owned.observe()
    assert halted["halted"] == {"reason": "security_warning", "cause": cause, "title": title}
    inputs = len(runner.inputs)
    for call in (lambda: owned.act(halted["observation_id"], {"type": "type", "text": "continue"}),
                 lambda: owned.open_file("notes.txt")):
        with pytest.raises(Problem) as error:
            call()
        assert code(error) == "DESKTOP_HALTED"
    assert len(runner.inputs) == inputs
    assert owned.observe()["halted"]["reason"] == "security_warning"
    assert observation["halted"] is None


def test_c4_adopted_normal_windows_and_editor_titles_do_not_halt(desktop):
    owned, runner, _ = desktop
    owned.adopt(4242)
    runner.add_window("Example Domain - Chromium", 4242, "NORMAL")
    (owned.files / "warning-notes.txt").write_text("", encoding="utf-8")
    observation = owned.open_file("warning-notes.txt")
    assert observation["halted"] is None


# C5: input binds to the latest observation, is validated into exact argv, and a lost or
# repeated call is never replayed.
def test_c5_input_requires_latest_observation_and_is_never_replayed(desktop):
    owned, runner, _ = desktop
    first = owned.observe()
    with pytest.raises(Problem) as error:
        owned.act(first["observation_id"] - 1, {"type": "click", "x": 1, "y": 1})
    assert code(error) == "DESKTOP_OBSERVATION_STALE" and runner.inputs == []
    second = owned.act(first["observation_id"], {"type": "type", "text": "; rm -rf / $(id)"})
    assert runner.inputs == [["xdotool", "type", "--clearmodifiers", "--delay", "5", "--", "; rm -rf / $(id)"]]
    with pytest.raises(Problem) as error:
        owned.act(first["observation_id"], {"type": "type", "text": "; rm -rf / $(id)"})
    assert code(error) == "DESKTOP_OBSERVATION_STALE" and len(runner.inputs) == 1
    assert second["observation_id"] > first["observation_id"]


@pytest.mark.parametrize("action", [
    {"type": "click", "x": WIDTH, "y": 0}, {"type": "click", "x": True, "y": 0},
    {"type": "click", "x": 1, "y": 1, "button": "middle"}, {"type": "click", "x": 1},
    {"type": "type", "text": ""}, {"type": "type", "text": "a\x1b[2J"}, {"type": "type", "text": "x" * 1025},
    {"type": "keypress", "keys": ["super", "l"]}, {"type": "keypress", "keys": ["ctrl", "ctrl", "x"]},
    {"type": "keypress", "keys": ["ctrl", "F13"]}, {"type": "keypress", "keys": "ctrl+s"},
    {"type": "scroll", "x": 1, "y": 1, "scroll_x": 0, "scroll_y": 5000}, {"type": "drag", "path": []},
    {"type": "wait", "x": 1}, {"type": "shell", "command": "id"}, "click",
])
def test_c5_invalid_actions_are_rejected_without_consuming_the_observation(desktop, action):
    owned, runner, _ = desktop
    observation = owned.observe()
    with pytest.raises(Problem) as error:
        owned.act(observation["observation_id"], action)
    assert code(error) == "DESKTOP_ACTION" and runner.inputs == []
    owned.act(observation["observation_id"], {"type": "wait"})


def test_c5_official_actions_map_to_exact_commands():
    assert action_commands({"type": "double_click", "x": 3, "y": 4}) == [
        ["xdotool", "mousemove", "3", "4", "click", "--repeat", "2", "--delay", "80", "1"]]
    assert action_commands({"type": "click", "x": 3, "y": 4, "button": "right"}) == [
        ["xdotool", "mousemove", "3", "4", "click", "3"]]
    assert action_commands({"type": "scroll", "x": 3, "y": 4, "scroll_x": 0, "scroll_y": -250}) == [
        ["xdotool", "mousemove", "3", "4"], ["xdotool", "click", "--repeat", "2", "4"]]
    assert action_commands({"type": "keypress", "keys": ["CTRL", "SHIFT", "Page_Down"]}) == [
        ["xdotool", "key", "--clearmodifiers", "ctrl+shift+Page_Down"]]
    assert action_commands({"type": "keypress", "keys": ["A"]}) == [["xdotool", "key", "--clearmodifiers", "A"]]
    assert action_commands({"type": "wait"}) == []
    assert action_commands({"type": "type", "text": "a\tb\n"}) == [
        ["xdotool", "type", "--clearmodifiers", "--delay", "5", "--", "a"],
        ["xdotool", "key", "--clearmodifiers", "Tab"],
        ["xdotool", "type", "--clearmodifiers", "--delay", "5", "--", "b"],
        ["xdotool", "key", "--clearmodifiers", "Return"]]


# 2026-10-05 dev: a move to the screen centre (where Xvfb starts the pointer) timed out and halted the desktop.
def test_c5_input_at_the_current_pointer_position_does_not_wait_for_it_to_move(desktop):
    owned, runner, _ = desktop
    observation = owned.observe()
    centre = {"x": WIDTH // 2, "y": HEIGHT // 2}
    for action in ({"type": "move", **centre}, {"type": "click", **centre}, {"type": "click", **centre}):
        observation = owned.act(observation["observation_id"], action)
        assert observation["halted"] is None
    assert runner.pointer == (WIDTH // 2, HEIGHT // 2) and observation["actions_left"] == 57


@pytest.mark.skipif(not sys.platform.startswith("linux") or shutil.which(EDITOR) is None,
                    reason="the real pointer needs the Linux desktop image")
def test_c5_real_display_input_at_the_current_pointer_position(tmp_path):
    prerequisites()
    owned = OwnedDesktop(private_root(tmp_path)).start()
    try:
        observation = owned.observe()
        centre = {"x": WIDTH // 2, "y": HEIGHT // 2}
        for action in ({"type": "move", **centre}, {"type": "click", **centre}):
            started = time.monotonic()
            observation = owned.act(observation["observation_id"], action)
            assert observation["halted"] is None, observation["halted"]
            assert time.monotonic() - started < 5
    finally:
        cleanup = owned.close()
    assert cleanup["processes_stopped"] and cleanup["runtime_removed"]


def test_c5_no_progress_and_failed_input_halt(tmp_path):
    root = private_root(tmp_path)
    runner = FakeRunner()
    owned = OwnedDesktop(root, runner=runner, limits=DesktopLimits(max_unchanged_actions=3)).start()
    try:
        runner.freeze = True
        observation = owned.observe()
        for _ in range(3):
            observation = owned.act(observation["observation_id"], {"type": "click", "x": 5, "y": 5})
        assert observation["halted"] == {"reason": "no_progress"}
        with pytest.raises(Problem) as error:
            owned.act(observation["observation_id"], {"type": "click", "x": 5, "y": 5})
        assert code(error) == "DESKTOP_HALTED" and len(runner.inputs) == 3
    finally:
        owned.close()
    runner = FakeRunner()
    owned = OwnedDesktop(root, runner=runner).start()
    try:
        runner.fail_input = True
        observation = owned.observe()
        with pytest.raises(Problem) as error:
            owned.act(observation["observation_id"], {"type": "click", "x": 5, "y": 5})
        assert code(error) == "DESKTOP_ACTION_FAILED"
        with pytest.raises(Problem) as error:
            owned.act(observation["observation_id"] + 1, {"type": "click", "x": 5, "y": 5})
        assert code(error) == "DESKTOP_HALTED" and len(runner.inputs) == 1
    finally:
        owned.close()


def test_c5_restart_starts_fresh_and_waits_for_a_changed_frame(tmp_path):
    root = private_root(tmp_path)
    runner = FakeRunner()
    old = OwnedDesktop(root, runner=runner).start()
    stale = old.observe()["observation_id"]
    old.close()
    runner = FakeRunner()
    runner.change_delay = 3
    fresh = OwnedDesktop(root, runner=runner).start()
    try:
        observation = fresh.observe()
        with pytest.raises(Problem) as error:
            fresh.act(stale, {"type": "click", "x": 1, "y": 1})
        assert code(error) == "DESKTOP_OBSERVATION_STALE" and runner.inputs == []
        before = observation["sha256"]
        after = fresh.act(observation["observation_id"], {"type": "click", "x": 1, "y": 1})
        assert after["settled"] and after["sha256"] != before
    finally:
        fresh.close()


def test_prerequisites_refuse_root_non_linux_and_missing_commands():
    class Host(FakeRunner):
        def __init__(self, linux=True, uid=10001, missing=()):
            super().__init__()
            self.linux, self.uid, self.missing = linux, uid, missing

        def is_linux(self):
            return self.linux

        def euid(self):
            return self.uid

        def which(self, name):
            return None if name in self.missing else "/usr/bin/" + name

    assert prerequisites(Host()) == 10001
    for host in (Host(linux=False), Host(uid=0), Host(missing=(EDITOR,))):
        with pytest.raises(Problem) as error:
            prerequisites(host)
        assert code(error) == "DESKTOP_UNAVAILABLE"
    assert isinstance(SubprocessRunner().which("definitely-not-a-command-fa"), type(None))


def test_c5_concurrent_calls_cannot_both_consume_one_observation(desktop):
    owned, runner, _ = desktop
    observation = owned.observe()
    runner.search_delay = 0.05
    outcomes = []

    def attempt():
        try:
            owned.act(observation["observation_id"], {"type": "type", "text": "once"})
            outcomes.append("ok")
        except Problem as error:
            outcomes.append(error.body["error"]["code"])

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["DESKTOP_OBSERVATION_STALE", "ok"]
    assert runner.inputs == [["xdotool", "type", "--clearmodifiers", "--delay", "5", "--", "once"]]


def test_c1_second_editor_must_show_its_own_window(desktop):
    owned, runner, root = desktop
    seen = owned.open_file("notes.txt")["observation_id"]
    (root / "files" / "b.txt").write_bytes(b"b")
    runner.editor_fails = True
    with pytest.raises(Problem) as error:
        owned.open_file("b.txt")
    assert code(error) == "DESKTOP_APP_FAILED"
    with pytest.raises(Problem) as error:
        owned.act(seen, {"type": "type", "text": "meant for notes"})
    assert code(error) == "DESKTOP_OBSERVATION_STALE" and runner.inputs == []


def test_close_attempts_every_process_and_too_many_windows_halts(tmp_path):
    runner = FakeRunner()
    owned = OwnedDesktop(private_root(tmp_path), runner=runner).start()
    for index in range(33):
        runner.add_window(f"window {index}", 0)
    assert owned.observe()["halted"] == {"reason": "too_many_windows"}
    runner.unstoppable = {runner.next_pid}
    cleanup = owned.close()
    assert cleanup["processes_stopped"] is False
    assert sorted(runner.stopped) == sorted(process.pid for process in owned._processes)
    assert not DESKTOP_LOCK.locked()

def test_c5_observation_ids_differ_across_hosted_process_restarts():
    import subprocess
    source = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"
    probe = "from shared_context import behavior_desktop as d; print(next(d._OBSERVATION_IDS))"
    env = {**os.environ, "PYTHONPATH": str(source)}
    first, second = (int(subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True,
                                        text=True, check=True).stdout) for _ in range(2))
    assert first != second and max(first, second) < 2 ** 53

def test_c5_failed_screenshot_after_input_leaves_no_usable_observation(desktop):
    owned, runner, _ = desktop
    observation = owned.observe()
    runner.fail_capture_after_input = True
    with pytest.raises(Problem) as error:
        owned.act(observation["observation_id"], {"type": "type", "text": "once"})
    assert code(error) == "DESKTOP_OBSERVE_FAILED"
    for stale in (observation["observation_id"], observation["observation_id"] + 1):
        with pytest.raises(Problem) as error:
            owned.act(stale, {"type": "type", "text": "once"})
        assert code(error) == "DESKTOP_OBSERVATION_STALE"
    assert len(runner.inputs) == 1
    fresh = owned.observe()
    owned.act(fresh["observation_id"], {"type": "wait"})


def _xvfb_starts(runner):
    return sum(1 for args, _ in runner.spawned if args[0] == "Xvfb")


def test_renew_keeps_the_display_and_windows_but_gives_the_next_turn_fresh_budgets(desktop):
    owned, runner, _ = desktop
    first = owned.open_file("notes.txt")
    owned.act(first["observation_id"], {"type": "wait"})
    warning = runner.add_window("Security Warning", 0, "DIALOG")
    old = owned.observe()
    assert old["halted"]["reason"] == "security_warning"
    assert owned.renew() is True
    with pytest.raises(Problem) as stale:
        owned.act(old["observation_id"], {"type": "wait"})
    assert stale.value.body["error"]["code"] == "DESKTOP_OBSERVATION_STALE"
    # The warning is still on screen, so the new turn halts again on its first look.
    assert owned.observe()["halted"]["reason"] == "security_warning"
    del runner.windows[warning]
    assert owned.renew() is True
    seen = owned.observe()
    assert seen["halted"] is None and seen["actions_left"] == owned.limits.max_actions
    assert [window["title"] for window in seen["windows"]] == ["notes.txt - L3afpad"] and _xvfb_starts(runner) == 1


def test_renew_counts_only_editors_still_open(desktop):
    owned, runner, _ = desktop
    owned.open_file("notes.txt")
    owned.open_file("second.txt", create=True)
    with pytest.raises(Problem) as full:
        owned.open_file("third.txt", create=True)
    assert full.value.body["error"]["code"] == "DESKTOP_EDITORS_OPEN"
    from shared_context.behavior_desktop_tools import public_desktop_failure
    assert "ctrl+q" in public_desktop_failure("DESKTOP_EDITORS_OPEN")
    assert owned.renew() is True
    with pytest.raises(Problem):
        owned.open_file("third.txt", create=True)
    # Closing an editor (ctrl+q) frees its slot in the same turn, as the guidance says.
    editor = next(process for process in owned._processes if owned._owners.get(process.pid) == "editor")
    editor.alive = False
    assert owned.open_file("third.txt", create=True)["halted"] is None
    assert len({args[1] for args, _ in runner.spawned if args[0] == EDITOR}) == 3


def test_renew_reports_an_exited_display_so_it_is_replaced(desktop):
    owned, _, _ = desktop
    owned._core[0].alive = False
    assert owned.renew() is False
    owned.close()
    assert owned.renew() is False


class FamilyRunner(FakeRunner):
    """Scripted process tree: this Hosted process is 50; Chromium 900 runs under its Playwright MCP 800."""

    def __init__(self, parents, own=50):
        super().__init__()
        self.parents, self.own = parents, own

    def self_pid(self):
        return self.own

    def parent(self, pid):
        return self.parents.get(pid)


@pytest.mark.parametrize("own,pid,adopted", [(50, 900, True), (50, 950, False), (50, 970, False),
                                             (1, 900, True), (1, 970, False)])
def test_windows_of_this_hosted_process_tree_are_adopted_and_still_title_checked(tmp_path, own, pid, adopted):
    # own=1: the Hosted container runs this process as PID 1 (exec-form CMD without an init).
    runner = FamilyRunner({900: 800, 800: own, 950: 1, 970: 960}, own)
    owned = OwnedDesktop(private_root(tmp_path), runner=runner).start()
    try:
        runner.add_window("Google Maps - Chromium", pid)
        seen = owned.observe()
        if adopted:
            assert seen["halted"] is None and seen["windows"][0]["owner"] == "adopted"
            runner.add_window("Privacy error - Chromium", pid)
            assert owned.observe()["halted"]["cause"] == "warning_title"
        else:
            assert seen["halted"]["cause"] == "unowned_window" and seen["windows"][0]["owner"] is None
    finally:
        owned.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_the_real_runner_reads_the_parent_process():
    from shared_context.behavior_desktop import SubprocessRunner
    runner = SubprocessRunner()
    assert runner.parent(os.getpid()) == os.getppid() and runner.self_pid() == os.getpid()
    assert runner.parent(2**22 + 12345) is None


def test_the_real_runner_has_no_parent_without_proc():
    from shared_context.behavior_desktop import SubprocessRunner
    if not sys.platform.startswith("linux"):
        assert SubprocessRunner().parent(os.getpid()) is None


CHROME = "/opt/fa-tools/ms-playwright/chromium-1246/chrome-linux64/chrome"


def _browsers(runner):
    return [args for args, _ in runner.spawned if Path(args[0]).name == "chrome"]


def test_the_desktop_browser_starts_once_on_a_persistent_profile_and_stays_across_turns(desktop):
    owned, runner, root = desktop
    profile = root / "browser"
    profile.mkdir(mode=0o700)
    for name in ("SingletonLock", "SingletonCookie", "DevToolsActivePort"):
        (profile / name).write_text("left by a browser that no longer runs")
    assert owned.browser(CHROME) == "http://127.0.0.1:9222"
    args, = _browsers(runner)
    assert args[0] == CHROME and f"--user-data-dir={profile}" in args and "--remote-debugging-port=0" in args
    assert args[-1] == "about:blank" and "--no-sandbox" in args and not (profile / "SingletonLock").exists()
    # Its window is the desktop's own, so it never halts the desktop tools; titles stay checked.
    seen = owned.observe()
    assert seen["halted"] is None and seen["windows"][0]["owner"] == "adopted"
    assert owned.renew() is True and owned.browser(CHROME) == "http://127.0.0.1:9222" and len(_browsers(runner)) == 1


def test_an_exited_desktop_browser_is_started_again_on_the_same_profile(desktop):
    owned, runner, root = desktop
    owned.browser(CHROME)
    next(process for process in owned._processes if owned._owners.get(process.pid) == "adopted").alive = False
    runner.browser_port = 9333
    assert owned.browser(CHROME) == "http://127.0.0.1:9333"
    assert [arg for arg in _browsers(runner)[1] if arg.startswith("--user-data-dir=")] == [f"--user-data-dir={root / 'browser'}"]


def test_a_browser_that_never_listens_is_stopped_and_reported(desktop):
    owned, runner, _ = desktop
    runner.browser_port = None
    with pytest.raises(Problem) as failed:
        owned.browser(CHROME)
    assert failed.value.body["error"]["code"] == "DESKTOP_BROWSER_FAILED"
    browser = next(process for process in owned._processes if owned._owners.get(process.pid) == "adopted")
    assert browser.pid in runner.stopped


def test_closing_the_desktop_stops_its_browser_but_keeps_the_profile(desktop):
    owned, runner, root = desktop
    owned.browser(CHROME)
    browser = next(process for process in owned._processes if owned._owners.get(process.pid) == "adopted")
    owned.close()
    assert browser.pid in runner.stopped and (root / "browser").is_dir()
    with pytest.raises(Problem):
        owned.browser(CHROME)
