"""Deployment-facing behavior of ``agent-versions`` that other tests reach only through a whole sync.

Pins each refusal of ``_check_environment``, the override file and flag checks of ``sync``, and the
exit code and output the deployment reads.
"""

import json
from types import SimpleNamespace

import pytest

import shared_context.behavior_agent_versions as versions
from shared_context.behavior_agent_versions import sync_all
from shared_context.errors import Problem
from test_agent_versions import A, R, Clock, Foundry, _composition, REAL_CURRENT
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key


def refused_code(environment, idle=900):
    with pytest.raises(Problem) as refused:
        versions._check_environment(environment, idle)
    assert refused.value.status == 409
    return refused.value.body["error"]["code"], refused.value.body["error"]["message"]


SHAPE = "only bounded non-secret deployment settings are accepted"
VALUE = "a deployment flag value is invalid"


@pytest.mark.parametrize("environment,idle,message", [
    ([("FA_NATIVE_HITL_ENABLED", "1")], 900, SHAPE),
    ({"AZURE_CLIENT_SECRET": "x"}, 900, SHAPE),
    ({"FA_NATIVE_HITL_ENABLED": 1}, 900, SHAPE),
    ({"FA_NATIVE_HITL_ENABLED": ""}, 900, SHAPE),
    ({"TZ": "x" * 2049}, 900, SHAPE),
    ({"TZ": "\ud800"}, 900, SHAPE),  # cannot be UTF-8 encoded
    ({}, 119, SHAPE),
    ({}, 3601, SHAPE),
    ({}, True, SHAPE),
    ({}, "900", SHAPE),
    ({"FA_NATIVE_HITL_ENABLED": "2"}, 900, VALUE),
    ({"FA_TEAM_MEMORY_ENABLED": "yes"}, 900, VALUE),
    ({"FA_NATIVE_TURNS": "foundry-native-v4"}, 900, VALUE),
    ({"FA_MEMORY_BACKEND": "redis"}, 900, VALUE),
    ({"FA_NATIVE_HITL_TTL_SECONDS": "0"}, 900, VALUE),
    ({"FA_NATIVE_HITL_TTL_SECONDS": "1234567"}, 900, VALUE),
    ({"FA_NATIVE_HITL_TTL_SECONDS": "6x"}, 900, VALUE),
    ({"TZ": "UTC\x00"}, 900, VALUE),
    ({"TZ": "UTC\r"}, 900, VALUE),
    ({"TZ": "UTC\n"}, 900, VALUE),
])
def test_each_unbounded_or_invalid_deployment_setting_is_refused_with_its_own_message(environment, idle, message):
    code, text = refused_code(environment, idle)
    assert (code, text) == ("HOSTED_ENVIRONMENT_INVALID", message)


@pytest.mark.parametrize("environment,idle", [
    (None, 120), ({}, 3600), ({"FA_NATIVE_HITL_TTL_SECONDS": "999999", "TZ": "UTC"}, 900),
    ({"FA_MEMORY_BACKEND": "cosmos", "FA_NATIVE_HITL_ENABLED": "0"}, 900),
])
def test_a_bounded_deployment_setting_is_returned_as_a_copy(environment, idle):
    checked = versions._check_environment(environment, idle)
    assert checked == (environment or {})
    assert environment is None or checked is not environment


def test_an_override_that_is_not_an_object_is_refused_before_any_agent_is_touched(monkeypatch):
    foundry = Foundry(A, R)
    clock = Clock()
    composition = _composition(monkeypatch, (A, "configured"), (R, "registered"))
    with pytest.raises(Problem) as refused:
        sync_all(composition, lambda _: foundry, image="new:image", build="new",
                 hosted_environment={R.agent_id: "FA_NATIVE_HITL_ENABLED=1"}, sleep=clock.sleep, clock=clock)
    assert refused.value.body["error"]["message"] == "each Agent override must be an object"
    assert foundry.calls == []


class Built:
    def __init__(self, composition):
        self.composition = composition

    def __enter__(self):
        return self.composition

    def __exit__(self, *exc):
        return False


@pytest.fixture
def cli(monkeypatch):
    import shared_context.behavior_server as server
    import shared_context.behavior_server_config as server_config

    seen = {"sync_all": [], "configs": []}
    composition = SimpleNamespace(hosted_versions=SimpleNamespace(_foundry_for=lambda endpoint: None),
                                  installation=lambda: None)
    monkeypatch.setattr(server_config, "load_configuration", lambda path: seen["configs"].append(path) or path)
    monkeypatch.setattr(server, "build_server", lambda config: Built(composition))
    seen["results"] = [{"agent_id": "a", "agent_name": "agent-a", "build": "b", "state": "ok", "route": "ok"}]

    def fake_sync_all(owner, foundry_for, **options):
        assert owner is composition
        seen["sync_all"].append(options)
        if isinstance(seen["results"], Exception):
            raise seen["results"]
        return seen["results"]

    monkeypatch.setattr(versions, "sync_all", fake_sync_all)
    return seen


@pytest.mark.parametrize("flags", [
    ["--defer-follow"],
    ["--verify-follow"],
    ["--route", "--defer-follow", "--verify-follow"],
    ["--verify-follow", "--defer-follow"],
    ["--route", "--follow-wait-seconds", "0"],
])
def test_inconsistent_follow_flags_stop_before_any_configuration_is_loaded(cli, capsys, flags):
    with pytest.raises(SystemExit) as stopped:
        versions.main(["sync", "--config", "x.json", *flags])
    assert stopped.value.code == 2
    assert "--defer-follow and --verify-follow require --route" in capsys.readouterr().err
    assert cli["configs"] == [] and cli["sync_all"] == []


def test_the_overlay_file_reaches_sync_as_parsed_json_and_no_file_means_none(cli, tmp_path, capsys):
    overlay = tmp_path / "overlay.json"
    overlay.write_text(json.dumps({"agent-1": {"FA_NATIVE_HITL_ENABLED": "1"}}), encoding="utf-8")
    assert versions.main(["sync", "--config", "x.json", "--hosted-environment", str(overlay),
                          "--idle-timeout-seconds", "1200", "--agent-id", "agent-1", "--image", "i:1"]) == 0
    [options] = cli["sync_all"]
    assert options["hosted_environment"] == {"agent-1": {"FA_NATIVE_HITL_ENABLED": "1"}}
    assert (options["idle_timeout_seconds"], options["agent_id"], options["image"], options["set_route"],
            options["native_v2_registered"]) == (1200, "agent-1", "i:1", False, False)
    assert options["build"] == versions.code_sha256()
    cli["sync_all"].clear()
    assert versions.main(["sync", "--config", "x.json"]) == 0
    assert cli["sync_all"][0]["hosted_environment"] is None
    assert capsys.readouterr().out.count('"state": "ok"') == 2


@pytest.mark.parametrize("content,reason", [
    (b"x" * 65537, "HOSTED_ENVIRONMENT_INVALID"),
    (b'{"a": 1, "a": 2}', "AGENT_VERSIONS_CONFIGURATION_INVALID"),
    (b'{"a": NaN}', "AGENT_VERSIONS_CONFIGURATION_INVALID"),
    (b"not json", "AGENT_VERSIONS_CONFIGURATION_INVALID"),
], ids=["over-64-kib", "duplicate-key", "non-finite-number", "not-json"])
def test_an_unusable_overlay_file_fails_before_the_server_is_built(cli, tmp_path, capsys, content, reason):
    overlay = tmp_path / "overlay.json"
    overlay.write_bytes(content)
    assert versions.main(["sync", "--config", "x.json", "--hosted-environment", str(overlay)]) == 1
    out = capsys.readouterr()
    assert out.out == "" and json.loads(out.err) == {"state": "failed", "reason": reason}
    assert cli["configs"] == [] and cli["sync_all"] == []


def test_a_missing_overlay_file_is_a_configuration_error(cli, tmp_path, capsys):
    assert versions.main(["sync", "--config", "x.json", "--hosted-environment", str(tmp_path / "none.json")]) == 1
    assert json.loads(capsys.readouterr().err) == {"state": "failed", "reason": "AGENT_VERSIONS_CONFIGURATION_INVALID"}


@pytest.mark.parametrize("error,reason", [
    (Problem(409, "HOSTED_ENVIRONMENT_INVALID", "x"), "HOSTED_ENVIRONMENT_INVALID"),
    (ValueError("bad"), "AGENT_VERSIONS_CONFIGURATION_INVALID"),
    (OSError("gone"), "AGENT_VERSIONS_CONFIGURATION_INVALID"),
])
def test_a_failed_sync_prints_one_reason_on_stderr_and_nothing_on_stdout(cli, capsys, error, reason):
    cli["results"] = error
    assert versions.main(["sync", "--config", "x.json"]) == 1
    out = capsys.readouterr()
    assert out.out == "" and json.loads(out.err) == {"state": "failed", "reason": reason}


@pytest.mark.parametrize("results,code", [
    ([{"agent_id": "a", "state": "ok", "route": "ok"}], 0),
    ([{"agent_id": "a", "state": "ok"}], 0),
    ([{"agent_id": "a", "state": "ok", "route": "ROUTE_UNKNOWN"}], 1),
    ([{"agent_id": "a", "state": "pending", "reason": "VERSION_NOT_ACTIVE_YET"}], 1),
    ([{"agent_id": "a", "state": "ok", "route": "ok"}, {"agent_id": "b", "state": "failed", "reason": "X"}], 1),
])
def test_the_exit_code_is_zero_only_when_every_agent_is_ok_and_routed(cli, capsys, results, code):
    cli["results"] = results
    assert versions.main(["sync", "--config", "x.json"]) == code
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == results


def test_an_idle_timeout_outside_the_bound_is_refused_even_with_no_override(monkeypatch):
    composition = _composition(monkeypatch, (A, "configured"))
    foundry = Foundry(A)
    for idle in (60, 3601):
        with pytest.raises(Problem) as refused:
            sync_all(composition, lambda _: foundry, build="new", idle_timeout_seconds=idle)
        assert refused.value.body["error"]["code"] == "HOSTED_ENVIRONMENT_INVALID"
    assert foundry.calls == []


def test_an_overlay_file_of_exactly_64_kib_is_accepted(cli, tmp_path):
    overlay = tmp_path / "overlay.json"
    overlay.write_bytes(b"{}" + b" " * (65536 - 2))
    assert versions.main(["sync", "--config", "x.json", "--hosted-environment", str(overlay)]) == 0
    assert cli["sync_all"][0]["hosted_environment"] == {}


# ----- sync: what must read back before a version counts as published ----------------------------------------------

from test_agent_versions import _sync


def _readback_edits():
    return {
        "name": lambda item: item.update(name="other-agent"),
        "version": lambda item: item.update(version="99"),
        "guid": lambda item: item.update(agent_guid="00000000-0000-0000-0000-000000000000"),
        "principal": lambda item: item["instance_identity"].update(principal_id="someone-else"),
        "build": lambda item: item["metadata"].update(fa_build="another-build"),
        "definition": lambda item: item["definition"].update(image="registry.invalid/other:image"),
    }


@pytest.mark.parametrize("edit", list(_readback_edits()))
def test_an_active_version_whose_readback_differs_in_any_way_is_a_conflict_not_a_success(edit):
    foundry = Foundry(R)
    foundry.add(R.agent_name, 1, "new")
    real = foundry.get_version

    def altered(name, version):
        item = real(name, version)
        _readback_edits()[edit](item)
        return item

    foundry.get_version = altered
    result = _sync(foundry, R, "new")
    assert (result["state"], result["reason"]) == ("failed", "HOSTED_DEFINITION_READBACK_CONFLICT")
    assert "version" not in result and foundry.creates() == []


def test_the_same_readback_unaltered_is_a_success():
    foundry = Foundry(R)
    foundry.add(R.agent_name, 1, "new")
    result = _sync(foundry, R, "new")
    assert (result["state"], result["version"]) == ("ok", "1")


# ----- sync_all: the served version is confirmed before its route is written -----------------------------------------

@pytest.mark.parametrize("status,guid", [("failed", None), ("active", "00000000-0000-0000-0000-000000000000")])
def test_a_served_version_that_is_not_active_and_own_is_never_routed(monkeypatch, status, guid):
    foundry = Foundry(R)
    foundry.add(R.agent_name, 1, "old", status=status, guid=guid)
    composition = _composition(monkeypatch, (R, "registered"))
    clock = Clock()
    [result] = sync_all(composition, lambda _: foundry, image="new:image", build="new", set_route=True,
                        sleep=clock.sleep, clock=clock)
    assert (result["state"], result["reason"]) == ("failed", "SOURCE_VERSION_MISSING")
    assert foundry.routes == [] and foundry.creates() == []


def test_only_agents_with_a_published_version_and_route_are_followed(cli, monkeypatch, capsys):
    from shared_context import native_environment_sync

    cli["results"] = [{"agent_id": "a", "state": "ok", "route": "ok"},
                      {"agent_id": "b", "state": "ok", "route": "ROUTE_UNKNOWN"},
                      {"agent_id": "c", "state": "failed", "reason": "X"},
                      {"agent_id": "d", "state": "pending", "reason": "VERSION_NOT_ACTIVE_YET"}]
    followed = []
    monkeypatch.setattr(native_environment_sync, "follow_all", lambda owner, **options: followed.append(options) or [
        {"agent_id": "a", "state": "ok"}])
    assert versions.main(["sync", "--config", "x.json", "--route"]) == 1  # b, c and d are not ok
    assert [options["published"] for options in followed] == [{"a"}]
    lines = {json.loads(line)["agent_id"]: json.loads(line) for line in capsys.readouterr().out.splitlines()}
    assert lines["a"]["environment_follow"] == {"agent_id": "a", "state": "ok"}
    assert all("environment_follow" not in lines[name] for name in "bcd")


def test_nothing_is_followed_when_no_agent_was_published(cli, monkeypatch):
    from shared_context import native_environment_sync

    cli["results"] = [{"agent_id": "a", "state": "pending", "reason": "VERSION_NOT_ACTIVE_YET"}]
    monkeypatch.setattr(native_environment_sync, "follow_all", lambda *a, **k: pytest.fail("no Agent was published"))
    assert versions.main(["sync", "--config", "x.json", "--route"]) == 1


@pytest.fixture
def fence(cli, monkeypatch):
    import shared_context.native_cut as native_cut

    calls = []

    def fence_command(composition, agent_id, *, clear):
        calls.append((agent_id, clear))
        if isinstance(cli["results"], Exception):
            raise cli["results"]
        return {"agent_id": agent_id, "row": None}

    monkeypatch.setattr(native_cut, "fence_command", fence_command)
    return calls


@pytest.mark.parametrize("confirm", [[], ["--confirm", "agent-b"]])
def test_clearing_a_fence_needs_the_same_agent_id_again_before_any_configuration_is_loaded(cli, fence, capsys, confirm):
    with pytest.raises(SystemExit) as stopped:
        versions.main(["fence", "--config", "x.json", "--agent-id", "agent-a", "--clear", *confirm])
    assert stopped.value.code == 2
    assert "--clear needs --confirm with the same Agent ID" in capsys.readouterr().err
    assert (cli["configs"], fence, cli["sync_all"]) == ([], [], [])


@pytest.mark.parametrize("flags,clear", [([], False), (["--clear", "--confirm", "agent-a"], True)])
def test_fence_prints_what_the_fence_command_returns_and_never_syncs(cli, fence, capsys, flags, clear):
    assert versions.main(["fence", "--config", "x.json", "--agent-id", "agent-a", *flags]) == 0
    assert (cli["configs"], fence, cli["sync_all"]) == (["x.json"], [("agent-a", clear)], [])
    out, err = capsys.readouterr()
    assert (json.loads(out), err) == ({"agent_id": "agent-a", "row": None}, "")


def test_a_refused_fence_prints_one_reason_on_stderr_and_nothing_on_stdout(cli, fence, capsys):
    cli["results"] = Problem(409, "NATIVE_FENCE_REFUSED", "x")
    assert versions.main(["fence", "--config", "x.json", "--agent-id", "agent-a"]) == 1
    out, err = capsys.readouterr()
    assert (out, json.loads(err)) == ("", {"state": "failed", "reason": "NATIVE_FENCE_REFUSED"})
