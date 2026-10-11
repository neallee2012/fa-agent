import copy
import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / "controller" / "foundry_session_inventory.py"
SPEC = importlib.util.spec_from_file_location("foundry_session_inventory", PATH)
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


def page(ids, more=False):
    return {
        "data": [{"agent_session_id": identity} for identity in ids],
        "first_id": ids[0] if ids else None,
        "last_id": ids[-1] if ids else None,
        "has_more": more,
    }


def test_native_page_101_is_not_lost_and_explicit_terminal_page_stops_reads():
    first = page([f"old-{i}" for i in range(100)], True)
    last = page(["new-session"])
    calls = []

    def fetch(cursor):
        calls.append(cursor)
        return copy.deepcopy(first if cursor is None else last)

    result = inventory.collect_sessions(fetch)
    assert len(result) == 101
    assert result[-1]["agent_session_id"] == "new-session"
    assert calls == [None, "old-99"]


def test_exactly_100_with_explicit_has_more_false_is_complete():
    result = inventory.collect_sessions(lambda _: page([str(i) for i in range(100)]))
    assert len(result) == 100


@pytest.mark.parametrize("bad", [
    {"data": [{"agent_session_id": str(i)} for i in range(100)]},
    {**page(["a"]), "has_more": "false"},
    {**page(["a"]), "last_id": "other"},
    {**page(["a"]), "first_id": "other"},
    {**page(["a"]), "next_link": "https://example.invalid"},
    page([], True),
    {**page([]), "last_id": "a"},
    page(["a", "a"]),
    page([""]),
    page([str(i) for i in range(101)]),
])
def test_ambiguous_or_corrupt_pages_fail_closed(bad):
    with pytest.raises(inventory.InventoryError):
        inventory.collect_sessions(lambda _: bad)


def test_repeated_page_is_not_a_complete_inventory():
    with pytest.raises(inventory.InventoryError, match="repeated"):
        inventory.collect_sessions(lambda _: page(["a"], True))


def test_page_bound_is_not_silently_treated_as_completion():
    calls = []

    def fetch(cursor):
        calls.append(cursor)
        return page([str(len(calls))], True)

    with pytest.raises(inventory.InventoryError, match="bound"):
        inventory.collect_sessions(fetch, max_pages=2)
    assert len(calls) == 2


def test_empty_complete_inventory():
    assert inventory.collect_sessions(lambda _: page([])) == []


def test_transport_failure_is_not_an_empty_success():
    def fetch(_):
        raise TimeoutError("native request unknown")

    with pytest.raises(TimeoutError):
        inventory.collect_sessions(fetch)
