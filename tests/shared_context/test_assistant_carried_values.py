"""Which earlier turn a new assistant session inherits its values from."""

from types import SimpleNamespace

import pytest

from shared_context.behavior_assistant import _carried


def turn(**values):
    return SimpleNamespace(context_read=False, user_turn_sources=(), work_memory_source=None, **values)


@pytest.mark.parametrize("name, resumed, archived, default, expected", [
    ("context_read", True, False, False, True),
    ("context_read", False, True, True, False),
    ("user_turn_sources", (), ("archived",), ("default",), ()),
    ("work_memory_source", None, "archived", "default", None),
])
def test_a_resumed_session_decides_even_when_its_value_is_empty(name, resumed, archived, default, expected):
    previous = turn()
    setattr(previous, name, resumed)
    prior = turn()
    setattr(prior, name, archived)
    assert _carried(previous, prior, name, default) == expected


@pytest.mark.parametrize("name, archived, default", [
    ("context_read", True, False), ("context_read", False, True),
    ("user_turn_sources", ("archived",), ()), ("user_turn_sources", (), ("default",)),
    ("work_memory_source", "archived", None), ("work_memory_source", None, "default"),
])
def test_without_a_resumed_session_the_previous_instruction_turn_decides(name, archived, default):
    prior = turn()
    setattr(prior, name, archived)
    assert _carried(None, prior, name, default) == archived


def test_without_any_earlier_turn_the_default_applies():
    assert _carried(None, None, "context_read", False) is False
    assert _carried(None, None, "user_turn_sources", ()) == ()
    assert _carried(None, None, "work_memory_source", "default") == "default"
