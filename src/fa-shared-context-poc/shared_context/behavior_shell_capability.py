"""Hosted handler for the FA builtin capability ``fa-shell-v1`` (TOOL-04, SP-09): one ``run_command`` tool.

The command runs as ``bash -c`` exactly as the model sent it (the SDK permission handler validated these same
arguments), only in this Agent's own Hosted sandbox, never in the API or on a user device. There is no
per-command approval, as on the Agent's own computer.

Boundaries:
- The working directory is the Agent's private ``files`` directory (shared with the desktop tools); HOME and
  TMPDIR are private directories beside it. This is not an OS sandbox: the command runs as the Hosted UID, so it
  can read and write this Agent's own Hosted directory (including its SDK session data), not another Agent's,
  which lives in another Hosted session. Image code is root-owned and read-only for this UID.
- Only an allowlisted environment reaches the command: no token, key, IDENTITY_* or other credential. The main
  process must already be non-dumpable, so the command cannot read its environ or memory. The turn is refused
  when this process's managed identity is reachable without a secret it alone holds (IMDS, a token file).
- FA sets no time limit. A command runs until it exits or its SDK call is cancelled; cancellation stops its whole
  process group and is never reported as completion. Processes the command leaves in its group are stopped when
  it exits. Output keeps the first and last 8 KiB.
- While a command runs, ``keep_active`` on the Hosted session re-sends its last saved public progress, so the
  API observer does not treat a busy turn as idle; the replay writes nothing.
- Outbound network is not restricted here (no VNet injection yet).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import stat
import sys
from pathlib import Path
from typing import Annotated

from pydantic import Field

from .behavior_capabilities import BUILTIN_CAPABILITIES
from .behavior_process_hardening import is_dumpable
from .behavior_wire import WireModel
from .errors import Problem
from .m2_copilot import ToolFailure, ToolOutput, ToolSpec

ENTRY_ID = "fa-shell-v1"
TOOL_NAME = "run_command"
MAX_COMMAND_CHARS = 8192
OUTPUT_HEAD_BYTES = OUTPUT_TAIL_BYTES = 8 * 1024
KEEPALIVE_SECONDS = 60.0
STOP_GRACE_SECONDS = 3.0
# After the command exits, output still buffered in the pipe is read for this long.
DRAIN_SECONDS = 2.0
POLL_SECONDS = 0.1
PATH = "/usr/local/bin:/usr/bin:/bin"
# Credential material a same-UID child could read or use without a secret only this process holds.
_CREDENTIAL_VARIABLES = ("AZURE_FEDERATED_TOKEN_FILE", "AZURE_CLIENT_SECRET", "AZURE_CLIENT_CERTIFICATE_PATH",
                         "AZURE_PASSWORD")


class RunCommandArguments(WireModel):
    command: Annotated[str, Field(min_length=1, max_length=MAX_COMMAND_CHARS,
                                  description="One bash command line; it runs with bash -c in your private files directory.")]


class CommandResult(WireModel):
    exit_code: int = Field(description="The command's exit status; negative means it ended by that signal.")
    output: str
    output_bytes: Annotated[int, Field(ge=0)]
    truncated: bool


def _identity_protected(environ) -> bool:
    """The Hosted managed identity needs a header secret held only in this non-dumpable process's environment."""
    if any(environ.get(name) for name in _CREDENTIAL_VARIABLES):
        return False
    return bool(environ.get("IDENTITY_ENDPOINT") and environ.get("IDENTITY_HEADER")
                or not environ.get("IDENTITY_ENDPOINT") and environ.get("MSI_ENDPOINT") and environ.get("MSI_SECRET"))


def prerequisites(environ=None) -> str:
    """Refuse the turn before any shell tool exists unless this Hosted process can keep its secrets (E-10)."""
    environ = os.environ if environ is None else environ
    bash = shutil.which("bash", path=PATH)
    if not sys.platform.startswith("linux") or bash is None:
        raise Problem(503, "SHELL_UNAVAILABLE", "the shell requires bash in a Linux Hosted runtime")
    if os.geteuid() == 0:
        raise Problem(503, "SHELL_UNAVAILABLE", "the shell must not run as root")
    if is_dumpable() is not False:
        raise Problem(503, "SHELL_UNAVAILABLE", "the Hosted main process is still dumpable")
    if not _identity_protected(environ):
        raise Problem(503, "SHELL_UNAVAILABLE", "this Hosted identity is reachable without a process-held secret")
    return bash


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
            or path.resolve() != path):
        raise Problem(503, "SHELL_UNAVAILABLE", "the shell directories must be private real directories")
    return path


def shell_directories(session) -> tuple[Path, Path, Path]:
    """Private files (shared with the desktop), HOME and TMPDIR beside this Agent's launch roots."""
    from .behavior_desktop_capability import desktop_root
    desktop = _private_directory(desktop_root(session))
    base = _private_directory(Path(session.root).parent / "shell")
    return tuple(_private_directory(path) for path in (desktop / "files", base / "home", base / "tmp"))


def command_environment(home: Path, temp: Path) -> dict[str, str]:
    return {"PATH": PATH, "HOME": str(home), "TMPDIR": str(temp), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "TERM": "dumb", "PAGER": "cat", "SHELL": "/bin/bash"}


class _Output:
    """The first and last bytes of a stream of any length."""

    def __init__(self):
        self.head, self.tail, self.total = bytearray(), bytearray(), 0

    def add(self, chunk: bytes):
        self.total += len(chunk)
        room = OUTPUT_HEAD_BYTES - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail = (self.tail + chunk)[-OUTPUT_TAIL_BYTES:]

    def result(self, exit_code: int) -> CommandResult:
        omitted = self.total - len(self.head) - len(self.tail)
        text = self.head.decode("utf-8", "replace")
        if omitted:
            text += f"\n[... {omitted} bytes omitted ...]\n"
        text += self.tail.decode("utf-8", "replace")
        return CommandResult(exit_code=exit_code, output=text, output_bytes=self.total, truncated=omitted > 0)


def _signal_group(pid: int, sig) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


async def _exited(process, timeout=None) -> bool:
    """The command itself exited. Process.wait() also waits for every pipe holder, which a background child can be."""
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    while process.returncode is None:
        if deadline is not None and loop.time() >= deadline:
            return False
        await asyncio.sleep(POLL_SECONDS)
    return True


async def _stop(process) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        _signal_group(process.pid, sig)
        if await _exited(process, STOP_GRACE_SECONDS):
            break
        if sig == signal.SIGKILL:
            logging.getLogger(__name__).warning("SHELL_STOP_UNCONFIRMED")
    # Members that ignored SIGTERM after the command itself exited.
    _signal_group(process.pid, signal.SIGKILL)


def _close(process) -> None:
    transport = getattr(process, "_transport", None)
    if transport is not None:
        transport.close()


def _output_unconfirmed():
    logging.getLogger(__name__).warning("SHELL_OUTPUT_UNCONFIRMED")
    return ToolFailure("The command output is unconfirmed; the command may have run. Do not rerun it unasked.",
                       public_code="CAPABILITY_UNAVAILABLE")


async def run_command(bash: str, command: str, *, cwd: Path, env: dict[str, str], keep_active=None) -> CommandResult:
    """Run until exit or cancellation; a cancelled command's group is stopped and the cancellation propagates."""
    process = await asyncio.create_subprocess_exec(
        bash, "--noprofile", "--norc", "-c", command, cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True,
    )
    output = _Output()

    async def read():
        try:
            while chunk := await process.stdout.read(64 * 1024):
                output.add(chunk)
        except OSError:
            raise _output_unconfirmed() from None

    async def beat():
        while True:
            await asyncio.sleep(KEEPALIVE_SECONDS)
            await keep_active()

    reader = asyncio.create_task(read())
    heartbeat = asyncio.create_task(beat()) if keep_active is not None else None
    exited = asyncio.create_task(_exited(process))
    try:
        done, _ = await asyncio.wait((reader, exited), return_when=asyncio.FIRST_COMPLETED)
        if reader in done:
            reader.result()
        await exited
        # A background member can hold the pipe open; it ends with the command.
        await asyncio.wait((reader,), timeout=DRAIN_SECONDS)
        _signal_group(process.pid, signal.SIGKILL)
        if not reader.done():
            await asyncio.wait((reader,), timeout=DRAIN_SECONDS)
        if not reader.done():
            raise _output_unconfirmed()
        reader.result()
        return output.result(process.returncode)
    except BaseException:
        if process.returncode is None:
            await _stop(process)
        else:
            _signal_group(process.pid, signal.SIGKILL)
        raise
    finally:
        for task in (reader, heartbeat, exited):
            if task is not None and not task.done():
                task.cancel()
        _close(process)


def shell_capability(session, grant):
    builtin = BUILTIN_CAPABILITIES[ENTRY_ID]
    if (grant.entry_id != ENTRY_ID or grant.kind != "fa-builtin"
            or grant.entry_sha256 != builtin.entry_sha256 or grant.tools != builtin.tools or grant.vision):
        raise Problem(503, "CAPABILITY_UNAVAILABLE", "the original shell capability differs")
    try:
        bash = prerequisites()
        files, home, temp = shell_directories(session)
    except (Problem, OSError) as error:
        reason = error.body["error"]["message"] if isinstance(error, Problem) else type(error).__name__
        logging.getLogger(__name__).warning("SHELL_UNAVAILABLE reason=%s", reason)
        raise Problem(503, "CAPABILITY_UNAVAILABLE", "this Hosted environment cannot run the shell",
                      capability=ENTRY_ID) from None
    env = command_environment(home, temp)

    async def handle(packet):
        try:
            arguments = RunCommandArguments.model_validate(packet["arguments"])
        except (KeyError, ValueError, TypeError):
            raise ToolFailure("run_command takes one command string.", public_code="CAPABILITY_ARGUMENTS") from None
        if "\x00" in arguments.command:
            raise ToolFailure("The command must not contain NUL characters.", public_code="CAPABILITY_ARGUMENTS")
        try:
            result = await run_command(bash, arguments.command, cwd=files, env=env,
                                       keep_active=getattr(session, "keep_active", None))
        except OSError:
            raise ToolFailure("The command could not be started; nothing ran.",
                              public_code="CAPABILITY_UNAVAILABLE") from None
        return ToolOutput(result.model_dump_json())

    tool = ToolSpec(
        TOOL_NAME,
        "Run one bash command on this Agent's own Hosted Linux sandbox (not the user's device) and wait for it to "
        "finish; there is no time limit and the user can cancel. The working directory is your private files "
        "directory, shared with the desktop tools. No credentials are available to commands. Output keeps the "
        "first and last 8 KiB; processes left in the background stop when the command exits. Report the actual "
        "output and exit_code; a cancelled or failed call is not a result and must not be rerun unasked.",
        RunCommandArguments.model_json_schema(), handle,
    )
    return (tool,), {tool.name: CommandResult}
