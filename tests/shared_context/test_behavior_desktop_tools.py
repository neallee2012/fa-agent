"""D2 add-only desktop tools: schemas match the handler, the desktop needs a hardened process,
results are typed JSON plus one PNG, and known refusals give safe model guidance."""

import base64
import json

import pytest
from jsonschema import Draft202012Validator

from shared_context.behavior_desktop import DESKTOP_LOCK, OwnedDesktop
from shared_context.behavior_desktop_tools import (
    DESKTOP_TOOLS, DesktopFileRead, DesktopObservation, DesktopToolbox, public_desktop_failure, tool_schema,
)
from shared_context.errors import Problem
from test_behavior_desktop import FakeRunner, private_root


def code(error):
    return error.value.body["error"]["code"]


@pytest.fixture
def toolbox(tmp_path):
    runner = FakeRunner()
    root = private_root(tmp_path)
    tools = DesktopToolbox(root, desktop_factory=lambda path: OwnedDesktop(path, runner=runner),
                           hardened=lambda: True)
    yield tools, runner, root
    tools.close()


def test_desktop_does_not_start_in_a_dumpable_process(tmp_path):
    started = []
    tools = DesktopToolbox(private_root(tmp_path), desktop_factory=lambda path: started.append(path),
                           hardened=lambda: False)
    with pytest.raises(Problem) as error:
        tools.call("desktop_observe", {})
    assert code(error) == "DESKTOP_UNAVAILABLE" and started == [] and not DESKTOP_LOCK.locked()


def test_default_gate_uses_the_real_process_flag(tmp_path):
    # The test process is dumpable on Linux and has no flag elsewhere, so the default gate must refuse.
    started = []
    tools = DesktopToolbox(private_root(tmp_path), desktop_factory=lambda path: started.append(path))
    with pytest.raises(Problem) as error:
        tools.call("desktop_observe", {})
    assert code(error) == "DESKTOP_UNAVAILABLE" and started == [] and not DESKTOP_LOCK.locked()


def test_tool_round_trip_returns_typed_text_and_one_png(toolbox):
    tools, runner, root = toolbox
    opened = tools.call("desktop_open_file", {"name": "notes.txt", "create": True})
    assert (root / "files" / "notes.txt").read_bytes() == b""
    observation = DesktopObservation.model_validate_json(opened.text)
    assert "png_base64" not in json.loads(opened.text)
    assert base64.b64decode(opened.png_base64).startswith(b"\x89PNG")
    for action in ({"type": "click", "x": 640, "y": 400}, {"type": "type", "text": "line1\nline2\n"},
                   {"type": "keypress", "key": "s", "modifiers": ["ctrl"]}):
        result = tools.call("desktop_act", {"observation_id": observation.observation_id, "action": action})
        observation = DesktopObservation.model_validate_json(result.text)
        assert observation.halted is None and result.png_base64
    read = DesktopFileRead.model_validate_json(tools.call("desktop_read_file", {"name": "notes.txt"}).text)
    assert read.text == "line1\nline2\n" and read.bytes == 12
    assert tools.call("desktop_read_file", {"name": "notes.txt"}).png_base64 is None


@pytest.mark.parametrize("name,arguments", [
    ("desktop_act", {"observation_id": 1, "action": {"type": "click", "x": 1, "y": 1, "text": "mixed"}}),
    ("desktop_act", {"observation_id": 1, "action": {"type": "type", "text": "x", "x": 1}}),
    ("desktop_act", {"observation_id": 1, "action": {"type": "drag", "path": [[0, 0], [1, 1]]}}),
    ("desktop_act", {"observation_id": 1, "action": {"type": "click", "x": 1280, "y": 0}}),
    ("desktop_act", {"observation_id": True, "action": {"type": "wait"}}),
    ("desktop_act", {"action": {"type": "wait"}}),
    ("desktop_act", {"observation_id": 1, "action": {"type": "keypress", "keys": ["ctrl", "s"]}}),
    ("desktop_act", {"observation_id": 1, "action": {"type": "keypress", "key": "F5"}}),
    ("desktop_act", {"observation_id": 1, "action": {"type": "keypress", "key": "s", "modifiers": ["cmd"]}}),
    ("desktop_act", {"observation_id": 1, "action": {"type": "keypress", "key": "ab"}}),
    ("desktop_act", {"observation_id": 1, "action": {"type": "keypress", "key": "s", "modifiers": ["ctrl", "ctrl"]}}),
    ("desktop_act", {"observation_id": 1, "action": {"type": "keypress", "key": "ctrl"}}),
    ("desktop_act", {"observation_id": 1, "action": {"type": "type", "text": "line1\r\nline2"}}),
    ("desktop_act", {"observation_id": 1, "action": {"type": "type", "text": "\x1b"}}),
    ("desktop_open_file", {"name": "../agent-b/files/notes.txt"}),
    ("desktop_open_file", {"name": "notes.txt", "path": "/etc/passwd"}),
    ("desktop_read_file", {"name": "/etc/passwd"}),
    ("desktop_observe", {"root": "/home/other"}),
])
def test_published_schema_and_handler_reject_the_same_arguments(toolbox, name, arguments):
    tools, runner, _ = toolbox
    schema = tool_schema(name)
    Draft202012Validator.check_schema(schema)
    assert schema["type"] == "object"
    assert not Draft202012Validator(schema).is_valid(arguments)
    with pytest.raises(Problem) as error:
        tools.call(name, arguments)
    assert code(error) == "DESKTOP_ACTION" and runner.inputs == []


def test_every_published_action_shape_is_accepted_by_the_handler(toolbox):
    tools, runner, _ = toolbox
    observation = DesktopObservation.model_validate_json(tools.call("desktop_observe", {}).text)
    schema = Draft202012Validator(tool_schema("desktop_act"))
    for action in ({"type": "click", "x": 1, "y": 2, "button": "right"}, {"type": "double_click", "x": 1, "y": 2},
                   {"type": "move", "x": 1, "y": 2}, {"type": "scroll", "x": 1, "y": 2, "scroll_x": 0, "scroll_y": 300},
                   {"type": "type", "text": "a"}, {"type": "keypress", "key": "s", "modifiers": ["ctrl"]},
                   {"type": "keypress", "key": "pagedown", "modifiers": ["alt", "shift", "ctrl"]},
                   {"type": "keypress", "key": "Z"}, {"type": "type", "text": "tab\tand line\n"}, {"type": "wait"}):
        arguments = {"observation_id": observation.observation_id, "action": action}
        assert schema.is_valid(arguments), action
        observation = DesktopObservation.model_validate_json(tools.call("desktop_act", arguments).text)
    assert len(runner.inputs) >= 6


def test_tool_definitions_are_unique_and_objects():
    names = [tool.name for tool in DESKTOP_TOOLS]
    assert names == ["desktop_open_file", "desktop_observe", "desktop_act", "desktop_read_file"]
    for tool in DESKTOP_TOOLS:
        assert tool_schema(tool.name)["type"] == "object" and tool.description


def test_known_refusals_give_guidance_and_unknown_codes_stay_generic(toolbox):
    tools, _, _ = toolbox
    first = DesktopObservation.model_validate_json(tools.call("desktop_observe", {}).text)
    tools.call("desktop_act", {"observation_id": first.observation_id, "action": {"type": "wait"}})
    with pytest.raises(Problem) as error:
        tools.call("desktop_act", {"observation_id": first.observation_id, "action": {"type": "wait"}})
    message = public_desktop_failure(code(error))
    assert code(error) == "DESKTOP_OBSERVATION_STALE" and "desktop_observe" in message
    assert "do not resend" in message
    assert public_desktop_failure("DESKTOP_HALTED").startswith("Desktop refused the action [DESKTOP_HALTED]")
    assert public_desktop_failure("SOMETHING_ELSE") is None and public_desktop_failure(None) is None
    with pytest.raises(Problem) as error:
        tools.call("desktop_shell", {})
    assert code(error) == "DESKTOP_TOOL_DENIED"


def test_close_is_final_and_nothing_restarts_or_replays(toolbox):
    tools, runner, _ = toolbox
    old = DesktopObservation.model_validate_json(tools.call("desktop_observe", {}).text)
    assert tools.close()["closed"] and not DESKTOP_LOCK.locked()
    for name, arguments in (("desktop_act", {"observation_id": old.observation_id,
                                               "action": {"type": "type", "text": "x"}}),
                            ("desktop_observe", {}), ("desktop_open_file", {"name": "notes.txt", "create": True})):
        with pytest.raises(Problem) as error:
            tools.call(name, arguments)
        assert code(error) == "DESKTOP_CLOSED"
    assert runner.inputs == [] and not DESKTOP_LOCK.locked()
    assert "stop desktop work" in public_desktop_failure("DESKTOP_CLOSED")


def test_halt_is_reported_and_blocks_further_input(toolbox):
    tools, runner, _ = toolbox
    observation = DesktopObservation.model_validate_json(tools.call("desktop_observe", {}).text)
    runner.add_window("Security Warning", 0, "DIALOG")
    halted = DesktopObservation.model_validate_json(tools.call("desktop_observe", {}).text)
    assert halted.halted.reason == "security_warning" and halted.halted.cause == "unowned_window"
    with pytest.raises(Problem) as error:
        tools.call("desktop_act", {"observation_id": halted.observation_id, "action": {"type": "wait"}})
    assert code(error) == "DESKTOP_HALTED" and observation.halted is None and runner.inputs == []


def test_every_schema_valid_keypress_is_accepted_by_the_desktop():
    from typing import get_args
    from shared_context.behavior_desktop import action_commands
    from shared_context.behavior_desktop_tools import NamedKey
    schema = Draft202012Validator(tool_schema("desktop_act"))
    for key in (*get_args(NamedKey), "a", "Z", "0"):
        for modifiers in ([], ["ctrl"], ["alt", "shift"], ["ctrl", "shift", "alt"]):
            action = {"type": "keypress", "key": key, "modifiers": modifiers}
            assert schema.is_valid({"observation_id": 1, "action": action}), action
            assert action_commands({"type": "keypress", "keys": [*modifiers, key]})