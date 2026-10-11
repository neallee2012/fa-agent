from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import hosted_bootstrap as bootstrap


@pytest.fixture
def filesystem(monkeypatch):
    home = SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.S_IFDIR | 0o755)
    private = SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.S_IFDIR | 0o700)
    operations = []
    monkeypatch.setattr(Path, "is_symlink", lambda _: False)
    for flag, value in (("O_DIRECTORY", 0x10000), ("O_NOFOLLOW", 0x20000), ("O_CLOEXEC", 0x80000)):
        monkeypatch.setattr(os, flag, value, raising=False)

    def opened(path, flags, **kwargs):
        assert flags & os.O_NOFOLLOW and flags & os.O_DIRECTORY
        if path == bootstrap.PLATFORM_HOME:
            assert kwargs == {}
            return 10
        assert path == ".fa-m1" and kwargs == {"dir_fd": 10}
        return 11

    def chown(fd, uid, gid):
        assert fd == 11
        operations.append(("chown", fd))
        private.st_uid, private.st_gid = uid, gid

    def chmod(fd, mode):
        assert fd == 11
        operations.append(("chmod", fd))
        private.st_mode = stat.S_IFDIR | mode

    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "fstat", lambda fd: home if fd == 10 else private)
    monkeypatch.setattr(os, "mkdir", Mock())
    monkeypatch.setattr(os, "fchown", chown, raising=False)
    monkeypatch.setattr(os, "fchmod", chmod, raising=False)
    monkeypatch.setattr(os, "close", lambda fd: operations.append(("close", fd)))
    return home, private, operations


def test_bootstrap_only_prepares_new_private_child(filesystem):
    home, private, operations = filesystem
    result = bootstrap.prepare_private_directory(bootstrap.PLATFORM_HOME, 10001, 10001)
    os.mkdir.assert_called_once_with(".fa-m1", mode=0o700, dir_fd=10)
    assert result["created"] and not result["existing_data_modified"]
    assert result["home_attributes"] == {"uid": 0, "gid": 0, "mode": "0o755"}
    assert result["private_attributes"] == {"uid": 10001, "gid": 10001, "mode": "0o700"}
    assert operations == [("chown", 11), ("chmod", 11), ("close", 11), ("close", 10)]
    assert home.st_uid == 0 and stat.S_IMODE(home.st_mode) == 0o755


def test_valid_existing_private_directory_is_not_modified(filesystem):
    _, private, operations = filesystem
    private.st_uid = private.st_gid = 10001
    os.mkdir.side_effect = FileExistsError()
    result = bootstrap.prepare_private_directory(bootstrap.PLATFORM_HOME, 10001, 10001)
    assert result["created"] is False
    assert operations == [("close", 11), ("close", 10)]


@pytest.mark.parametrize("uid,mode", [(0, 0o700), (10001, 0o755)])
def test_wrong_existing_metadata_is_not_repaired(filesystem, uid, mode):
    _, private, operations = filesystem
    private.st_uid, private.st_gid, private.st_mode = uid, 10001, stat.S_IFDIR | mode
    os.mkdir.side_effect = FileExistsError()
    with pytest.raises(RuntimeError, match="no repair"):
        bootstrap.prepare_private_directory(bootstrap.PLATFORM_HOME, 10001, 10001)
    assert operations == [("close", 11), ("close", 10)]


def test_chown_failure_closes_descriptors_and_does_not_continue(filesystem, monkeypatch):
    _, _, operations = filesystem
    monkeypatch.setattr(os, "fchown", Mock(side_effect=PermissionError(1, "synthetic chown refusal")))
    with pytest.raises(PermissionError):
        bootstrap.prepare_private_directory(bootstrap.PLATFORM_HOME, 10001, 10001)
    assert operations == [("close", 11), ("close", 10)]


@pytest.mark.parametrize("home,symlink", [(Path("/tmp/elsewhere"), False), (bootstrap.PLATFORM_HOME, True)])
def test_arbitrary_home_or_symlink_is_rejected_before_privileged_io(monkeypatch, home, symlink):
    monkeypatch.setattr(Path, "is_symlink", lambda _: symlink)
    monkeypatch.setattr(os, "open", lambda *a, **kw: pytest.fail("no filesystem mutation"))
    with pytest.raises(RuntimeError):
        bootstrap.prepare_private_directory(home, 10001, 10001)


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


def test_exec_occurs_only_after_initialization_and_drop(monkeypatch):
    order = []
    monkeypatch.setattr(bootstrap.platform, "system", lambda: "Linux")
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setenv("HOME", str(bootstrap.PLATFORM_HOME))
    monkeypatch.setattr(bootstrap, "_account", lambda: (10001, 10001))
    monkeypatch.setattr(bootstrap, "prepare_private_directory", lambda *args: order.append("prepare") or {"created": True})
    monkeypatch.setattr(bootstrap, "drop_privileges", lambda *args: order.append("drop") or {"Uid": [10001] * 4})
    monkeypatch.setattr(os, "execv", lambda *args: order.append(("exec", args)))
    monkeypatch.setenv(bootstrap.RECEIPT_ENV, "caller-value-must-be-replaced")
    monkeypatch.setenv("USER", "root")
    monkeypatch.setenv("LOGNAME", "root")
    bootstrap.bootstrap()
    assert order[:2] == ["prepare", "drop"] and order[2][0] == "exec"
    assert Path(order[2][1][1][1]).name == "hosted_main.py"
    assert json.loads(os.environ[bootstrap.RECEIPT_ENV])["initial_uid"] == 0
    assert os.environ["USER"] == os.environ["LOGNAME"] == "app"


def test_failed_drop_never_executes_agent(monkeypatch):
    monkeypatch.setattr(bootstrap.platform, "system", lambda: "Linux")
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setenv("HOME", str(bootstrap.PLATFORM_HOME))
    monkeypatch.setattr(bootstrap, "_account", lambda: (10001, 10001))
    monkeypatch.setattr(bootstrap, "prepare_private_directory", lambda *args: {})
    monkeypatch.setattr(bootstrap, "drop_privileges", Mock(side_effect=PermissionError(1, "synthetic drop refusal")))
    monkeypatch.setattr(os, "execv", lambda *args: pytest.fail("must not run as root"))
    with pytest.raises(PermissionError):
        bootstrap.bootstrap()


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


@pytest.mark.parametrize("system,uid,home", [
    ("Windows", 0, "/home/session"), ("Linux", 10001, "/home/session"),
    ("Linux", 0, "/tmp/elsewhere"),
])
def test_bootstrap_rejects_wrong_starting_context(monkeypatch, system, uid, home):
    monkeypatch.setattr(bootstrap.platform, "system", lambda: system)
    monkeypatch.setattr(os, "geteuid", lambda: uid, raising=False)
    monkeypatch.setenv("HOME", home)
    monkeypatch.setattr(bootstrap, "_account", lambda: pytest.fail("must stop before setup"))
    with pytest.raises(RuntimeError):
        bootstrap.bootstrap()
