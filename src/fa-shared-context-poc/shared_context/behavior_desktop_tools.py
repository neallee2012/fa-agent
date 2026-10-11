"""Hosted-side CAP-05 desktop tools over one OwnedDesktop; no SDK or API dependency.

D2 registers ``DESKTOP_TOOLS`` as custom tools only for a conversation whose captured
capability enables the desktop, and converts ``DesktopToolResult`` into the SDK tool result
(typed JSON text plus at most one PNG). Everything here runs inside that Agent's own Hosted
process against its private root; the model cannot choose another root, path or display.

The desktop starts on ``start`` (each granted turn) or the first desktop call, and only after the Hosted
process is non-dumpable, so a same-UID desktop app cannot read the main process's environment or memory.
It then stays for later turns of the same process; ``renew`` gives each turn fresh budgets.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .behavior_desktop import HEIGHT, WIDTH, OwnedDesktop
from .behavior_process_hardening import is_dumpable
from .errors import Problem


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


FileName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
X = Annotated[int, Field(ge=0, lt=WIDTH)]
Y = Annotated[int, Field(ge=0, lt=HEIGHT)]
Delta = Annotated[int, Field(ge=-2000, le=2000)]


class Click(_Strict):
    type: Literal["click"]
    x: X
    y: Y
    button: Literal["left", "right"] = "left"


class DoubleClick(_Strict):
    type: Literal["double_click"]
    x: X
    y: Y


class Move(_Strict):
    type: Literal["move"]
    x: X
    y: Y


class Scroll(_Strict):
    type: Literal["scroll"]
    x: X
    y: Y
    scroll_x: Delta
    scroll_y: Delta


class TypeText(_Strict):
    type: Literal["type"]
    # Same bound and control-character rule as behavior_desktop.action_commands; \t and \n become keys.
    text: Annotated[str, Field(min_length=1, max_length=1024, pattern=r"^[^\x00-\x08\x0b-\x1f\x7f]*$")]


NamedKey = Literal["enter", "tab", "escape", "backspace", "delete", "home", "end", "pageup", "pagedown",
                   "up", "down", "left", "right", "space"]


class Keypress(_Strict):
    """One key with distinct modifiers; the published schema admits exactly what the desktop accepts."""

    type: Literal["keypress"]
    key: NamedKey | Annotated[str, Field(pattern=r"^[A-Za-z0-9]$")]
    modifiers: Annotated[list[Literal["ctrl", "shift", "alt"]],
                         Field(max_length=3, json_schema_extra={"uniqueItems": True})] = []

    @field_validator("modifiers")
    @classmethod
    def distinct(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("modifiers must be distinct")
        return value


class Wait(_Strict):
    type: Literal["wait"]


DesktopAction = Annotated[Click | DoubleClick | Move | Scroll | TypeText | Keypress | Wait,
                          Field(discriminator="type")]


class NoArguments(_Strict):
    pass


class OpenFileArguments(_Strict):
    name: FileName
    create: bool = False


class ReadFileArguments(_Strict):
    name: FileName


class ActArguments(_Strict):
    observation_id: Annotated[int, Field(ge=1, lt=2 ** 53)]
    action: DesktopAction


class DesktopWindow(_Strict):
    id: str
    title: str
    window_type: Literal["dialog", "normal", "other"]
    pid: int | None
    owner: str | None


class DesktopHalt(_Strict):
    reason: str
    cause: str | None = None
    title: str | None = None


class DesktopObservation(_Strict):
    observation_id: int
    sha256: str
    width: int
    height: int
    settled: bool
    windows: list[DesktopWindow]
    halted: DesktopHalt | None
    actions_left: int


class DesktopFileRead(_Strict):
    name: str
    bytes: int
    sha256: str
    text: str


@dataclass(frozen=True)
class DesktopToolDefinition:
    name: str
    description: str
    arguments: type[_Strict]
    result: type[_Strict]


_SCREEN = f"The screenshot is {WIDTH}x{HEIGHT}; x/y are absolute pixels in it. "
DESKTOP_TOOLS = (
    DesktopToolDefinition(
        "desktop_open_file",
        "Open one plain file name from your own private files directory in the desktop text editor "
        "(create=true makes an empty file first). Returns a fresh screenshot and its observation_id.",
        OpenFileArguments, DesktopObservation),
    DesktopToolDefinition(
        "desktop_observe",
        "Take a fresh screenshot of your own Hosted desktop. Use it whenever the screen is unknown or an "
        "earlier desktop call failed.",
        NoArguments, DesktopObservation),
    DesktopToolDefinition(
        "desktop_act",
        "Perform exactly one input action on the screenshot named by observation_id, which must be the "
        "latest one you received. " + _SCREEN + "keypress takes one key plus optional distinct modifiers "
        "(ctrl, shift, alt), e.g. key \"s\" with modifiers [\"ctrl\"]. Returns the next screenshot; never repeat "
        "an action on an old observation_id. If the result is halted, stop desktop work and tell the user a human "
        "must review it.",
        ActArguments, DesktopObservation),
    DesktopToolDefinition(
        "desktop_read_file",
        "Read back one plain file name from your own private files directory as saved on disk (UTF-8 text "
        "and SHA-256). Use it to verify a save; screen content is not proof.",
        ReadFileArguments, DesktopFileRead),
)
_BY_NAME = {tool.name: tool for tool in DESKTOP_TOOLS}

DESKTOP_GUIDANCE = {
    "DESKTOP_OBSERVATION_STALE": "Call desktop_observe and decide from the new screenshot; do not resend the old action.",
    "DESKTOP_HALTED": "The desktop stopped for human review. Stop desktop work and tell the user what you saw.",
    "DESKTOP_ACTION": "Follow this tool's published schema exactly; desktop_act takes one action per call.",
    "DESKTOP_FILE_DENIED": "Only plain file names inside your own private files directory are allowed.",
    "DESKTOP_FILE_NOT_FOUND": "That private file does not exist; do not substitute another file.",
    "DESKTOP_FILE_LIMIT": "The file exceeds the desktop size bound; report it instead of truncating.",
    "DESKTOP_FILE_ENCODING": "Only UTF-8 text files can be read back.",
    "DESKTOP_LIMIT": "The desktop budget is exhausted; stop and report what was and was not completed.",
    "DESKTOP_EDITORS_OPEN": "Editor windows stay open across turns and the editor limit is reached. Call "
                            "desktop_observe; keep working in an open editor, or save, click the editor you no longer "
                            "need and press ctrl+q with desktop_act, then open the file again.",
    "DESKTOP_ACTION_FAILED": "Input failed and the desktop stopped; do not assume the action happened.",
    "DESKTOP_OBSERVE_FAILED": "No screenshot was taken; the last action may or may not have happened. Observe before acting.",
    "DESKTOP_APP_FAILED": "The editor did not open; observe the desktop before trying anything else.",
    "DESKTOP_UNAVAILABLE": "The desktop is not available in this environment; tell the user.",
    "DESKTOP_BUSY": "Another desktop is already running in this environment; tell the user.",
    "DESKTOP_CLOSED": "The desktop was closed because this Agent's current settings no longer include it; "
                      "stop desktop work and tell the user. Unsaved edits were not kept.",
}


def public_desktop_failure(code) -> str | None:
    """Safe model-facing guidance for known desktop refusals; None keeps the generic failure."""
    guidance = DESKTOP_GUIDANCE.get(code) if isinstance(code, str) else None
    if guidance is None:
        return None
    return f"Desktop refused the action [{code}]. {guidance} Do not claim completion."


# desktop_act raises these only before any input is sent (the observation is not even consumed), so the action
# certainly did not happen. DESKTOP_LIMIT and the *_FAILED codes can follow input and stay unknown.
ACT_REFUSED_BEFORE_INPUT = frozenset({"DESKTOP_OBSERVATION_STALE", "DESKTOP_HALTED", "DESKTOP_ACTION"})


@dataclass(frozen=True)
class DesktopToolResult:
    text: str
    png_base64: str | None = None


class DesktopToolbox:
    """The desktop tools of one Hosted process for one Agent-private root."""

    def __init__(self, root: Path, *, desktop_factory=OwnedDesktop, hardened=None):
        self.root = root
        self._factory = desktop_factory
        self._hardened = hardened or (lambda: is_dumpable() is False)
        self._desktop = None
        self._closed = False
        self._lock = threading.Lock()

    def call(self, name: str, arguments) -> DesktopToolResult:
        tool = _BY_NAME.get(name)
        if tool is None:
            raise Problem(403, "DESKTOP_TOOL_DENIED", "unknown desktop tool")
        try:
            args = tool.arguments.model_validate(arguments)
        except ValidationError as exc:
            raise Problem(422, "DESKTOP_ACTION", "arguments do not match the desktop tool schema") from exc
        with self._lock:
            desktop = self._running()
            if name == "desktop_read_file":
                return DesktopToolResult(DesktopFileRead.model_validate(desktop.read_file(args.name)).model_dump_json())
            if name == "desktop_open_file":
                raw = desktop.open_file(args.name, create=args.create)
            elif name == "desktop_observe":
                raw = desktop.observe()
            else:
                action = args.action.model_dump()
                if action["type"] == "keypress":
                    action = {"type": "keypress", "keys": [*action["modifiers"], action["key"]]}
                raw = desktop.act(args.observation_id, action)
        png = raw.pop("png_base64")
        raw.pop("mime_type", None)
        return DesktopToolResult(DesktopObservation.model_validate(raw).model_dump_json(), png)

    def start(self) -> dict | None:
        """Start (or keep) the desktop now; None when it cannot run here, and its tools then report why."""
        with self._lock:
            try:
                return self._running().display_env()
            except Problem:
                return None

    def browser(self, executable: str) -> str | None:
        """The running desktop's own browser CDP endpoint (started if needed), or None if it cannot run."""
        with self._lock:
            try:
                return self._running().browser(executable)
            except Problem:
                return None

    def renew(self):
        """A new granted turn on the running desktop; a display that has exited is closed and started again."""
        with self._lock:
            if self._closed or self._desktop is None or self._desktop.renew():
                return
            desktop, self._desktop = self._desktop, None
            desktop.close()

    def close(self) -> dict:
        """Final: a handler still holding this toolbox can never start an untracked desktop."""
        with self._lock:
            desktop, self._desktop, self._closed = self._desktop, None, True
        return desktop.close() if desktop is not None else {"closed": True}

    def running_env(self) -> dict | None:
        """A started desktop's display environment, or None; never starts one (it may wait for one action)."""
        desktop = None if self._closed else self._desktop
        if desktop is None:
            return None
        try:
            return desktop.display_env()
        except Problem:
            return None

    def _running(self):
        if self._closed:
            raise Problem(409, "DESKTOP_CLOSED", "this desktop toolbox was closed")
        if self._desktop is None:
            if not self._hardened():
                raise Problem(503, "DESKTOP_UNAVAILABLE",
                              "the Hosted process must be non-dumpable before a same-UID desktop starts")
            if not self.root.exists():
                self.root.mkdir(mode=0o700)
            self._desktop = self._factory(self.root).start()
        return self._desktop


def tool_schema(name: str) -> dict:
    """The exact JSON schema the SDK validates before the handler runs."""
    return json.loads(json.dumps(_BY_NAME[name].arguments.model_json_schema()))
