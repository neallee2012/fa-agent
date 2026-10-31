from __future__ import annotations

import os
from pathlib import Path

import pytest

import hosted_bootstrap as bootstrap


@pytest.fixture
def dropped_identity(monkeypatch):
    identity = {"Uid": [10001] * 4, "Gid": [10001] * 4, "Groups": [], "CapEff": "0", "CapPrm": "0", "CapAmb": "0"}
    order = []
    for name in ("setgroups", "setresgid", "setresuid"):
        monkeypatch.setattr(os, name, lambda *args, name=name: order.append((name, args)), raising=False)
    monkeypatch.setattr(bootstrap, "process_identity", lambda: identity)
    return identity, order


def test_drop_clears_groups_before_all_group_and_user_credentials(dropped_identity):
    identity, order = dropped_identity
    assert bootstrap.drop_privileges(10001, 10001) == identity
    assert order == [("setgroups", ([],)), ("setresgid", (10001, 10001, 10001)), ("setresuid", (10001, 10001, 10001))]


@pytest.mark.parametrize("field,value", [
    ("Uid", [10001, 10001, 0, 10001]), ("Gid", [10001, 10001, 0, 10001]), ("Groups", [0]),
    ("CapEff", "1"), ("CapPrm", "1"), ("CapAmb", "1"),
])
def test_incomplete_drop_is_not_accepted(dropped_identity, field, value):
    identity, _ = dropped_identity
    identity[field] = value
    with pytest.raises(RuntimeError, match="privilege drop"):
        bootstrap.drop_privileges(10001, 10001)


def test_proc_identity_parser_requires_all_observations(monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: "Uid:\t10001 10001 10001 10001\n")
    with pytest.raises(RuntimeError, match="unavailable"):
        bootstrap.process_identity()
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: (
        "Name:\tpython\nUid:\t10001 10001 10001 10001\nGid:\t10001 10001 10001 10001\n"
        "Groups:\t\nCapEff:\t00000000\nCapPrm:\t00000000\nCapAmb:\t00000000\n"
    ))
    assert bootstrap.process_identity() == {
        "Uid": [10001] * 4, "Gid": [10001] * 4, "Groups": [],
        "CapEff": "00000000", "CapPrm": "00000000", "CapAmb": "00000000",
    }
