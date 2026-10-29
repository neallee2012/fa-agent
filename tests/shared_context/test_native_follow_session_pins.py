"""``follow_session`` (ADR-0009 explicit follow): its early exits, its conflicts and its commit, pinned one by one.

The destination is a probe called in the source's loop (as ``test_native_session_cut`` does). Every case reads the
literal view or error the API sees; the stores are the real local ``fa/v1/turns`` store.
"""

import asyncio

import pytest
from azure.ai.agentserver.core.storage import FoundryStateStore

from shared_context.behavior_native_turn_wire import NativeSessionFollow, NativeSessionRead
from shared_context import native_open_rids
from shared_context.errors import Problem
from shared_context.native_environment import _Following, follow_session
from shared_context.native_environment_cut import withdraw_session
from shared_context.native_files_omission import _Omission, _key as omission_key
from shared_context.native_turns import TURNS_STORE, _json
from shared_context.native_workspace_files import NativeWorkspaceFiles
from test_native_open_rids import V2, V3, _inactive
from test_native_session_cut import (  # noqa: F401 - fixtures
    SOURCE, _withdraw, following_key, items, local_env, probe, source)

CONFLICT = "NATIVE_ENVIRONMENT_CONFLICT"
COMPARISON = "the original Agent environment comparison changed"


def follow(source_fixture, request_id="f1", *, stage="prepare", etag=None, **fields):
    return NativeSessionFollow(
        request_id=request_id, expected_session_id=fields.pop("expected_session_id", SOURCE),
        expected_etag=etag or source_fixture.invoke(NativeSessionRead()).json()["etag"], stage=stage,
        idle_only=fields.pop("idle_only", True), **fields)


def refused(source_fixture, destination, command):
    with pytest.raises(Problem) as error:
        source_fixture.call(follow_session, destination, command)
    return error.value.status, error.value.body["error"]["code"], error.value.body["error"]["message"]


def destination(**options):
    return probe(bootstrap_source_inactive=options.pop("inactive", _inactive), **options)


def edit(key, **fields):
    async def write():
        async with FoundryStateStore(TURNS_STORE) as store:
            item = await store.get_item(key)
            await store.set_item(key, {**item.value, **fields}, if_match=item.etag)
    asyncio.run(write())


def test_prepare_then_commit_makes_the_destination_the_agent_and_a_rerun_writes_nothing(source):
    command = follow(source)
    pending = source.call(follow_session, destination(), command)
    assert (pending.state, pending.reason, pending.request_id) == ("pending", "source_stop_required", "f1")
    assert (pending.previous_hosted_session_id, pending.hosted_session_id) == (SOURCE, V2.session_id)
    found = items()
    assert found[following_key(V2.session_id)][0]["phase"] == "stopping"
    assert found["agent"][0]["environment"]["state"] == "pending"
    ready = source.call(follow_session, destination(), command.model_copy(update={"stage": "commit"}))
    assert (ready.state, ready.reason, ready.files_profile) == ("ready", None, None)
    assert (ready.request_id, ready.hosted_session_id, ready.previous_hosted_session_id) == ("f1", V2.session_id, SOURCE)
    agent = items()["agent"][0]
    assert (agent["hosted_session_id"], agent["following_supported"]) == (V2.session_id, True)
    assert (agent["environment"]["state"], agent["environment"]["request_id"]) == ("ready", "f1")
    before = items()
    assert source.call(follow_session, destination(), command.model_copy(update={"stage": "commit"})) == ready
    assert items() == before


@pytest.mark.parametrize("why", ["commit_first", "stale_etag", "same_session", "other_source"])
def test_a_first_call_that_is_not_a_fresh_prepare_of_the_current_agent_is_refused_and_writes_nothing(source, why):
    before = items()
    command = follow(source, stage="commit" if why == "commit_first" else "prepare",
                     etag="OFFLINE-stale" if why == "stale_etag" else None,
                     expected_session_id="offline-other-source" if why == "other_source" else SOURCE)
    probed = probe(V2.model_copy(update={"session_id": SOURCE})) if why == "same_session" else destination()
    assert refused(source, probed, command) == (409, CONFLICT, COMPARISON)
    assert items() == before


def test_a_second_call_naming_another_operation_is_refused(source):
    command = follow(source)
    source.call(follow_session, destination(), command)
    other = "the named environment has another original operation"
    assert refused(source, destination(), follow(source, idle_only=False)) == (409, CONFLICT, other)
    assert refused(source, destination(), follow(source, expected_session_id="offline-other-source")) == (
        409, CONFLICT, other)
    older = destination()
    older.identity = V2.model_copy(update={"agent_version": "9"})
    assert refused(source, older, command) == (409, CONFLICT, other)


@pytest.mark.parametrize("fault", ["unset", "problem", "active"])
def test_the_commit_waits_for_the_source_to_stop(source, fault):
    command = follow(source)
    source.call(follow_session, destination(), command)

    async def raising(sid):
        raise Problem(503, "OFFLINE", "unreachable")

    async def active(sid):
        return False

    probed = {"unset": probe(), "problem": probe(bootstrap_source_inactive=raising),
              "active": probe(bootstrap_source_inactive=active)}[fault]
    view = source.call(follow_session, probed, command.model_copy(update={"stage": "commit"}))
    assert (view.state, view.reason) == ("pending", "source_active" if fault == "active" else "source_unavailable")
    assert items()["agent"][0]["hosted_session_id"] == SOURCE


@pytest.mark.parametrize("stage", ["prepare", "commit"])
def test_an_admission_in_flight_holds_either_stage(source, stage):
    command = follow(source)
    if stage == "commit":
        source.call(follow_session, destination(), command)
        command = command.model_copy(update={"stage": "commit"})
    edit("agent", admitting={"request_id": "admitting-rid", "submit_digest": source.base.submit_digest})
    if stage == "prepare":
        command = follow(source)
    view = source.call(follow_session, destination(), command)
    assert (view.state, view.reason) == ("pending", "admission_pending")
    assert items()["agent"][0]["hosted_session_id"] == SOURCE


def test_a_withdrawn_pending_is_never_committed_over(source):
    command = follow(source)
    source.call(follow_session, destination(), command)
    source.call(withdraw_session, destination(), _withdraw(items()["agent"][1], "f1"))
    assert refused(source, destination(), command.model_copy(update={"stage": "commit"})) == (
        409, CONFLICT, "the original pending transition was withdrawn")


@pytest.mark.parametrize("first, second", [
    ("demand", "plain"), ("plain", "demand"), ("plain", "files"), ("files", "plain")])
def test_a_file_demand_and_the_destinations_files_must_agree(source, tmp_path, first, second):
    def call(kind):
        options = {"files": {"workspace_files": NativeWorkspaceFiles(tmp_path / "files")}}.get(kind, {})
        command = follow(source, files_required=kind == "demand")
        return source.call(follow_session, destination(**options), command)

    # A destination with files and no demand still carries the files (``runtime.workspace_files``): pending, not refused.
    expected = "files_pending" if first == "files" else "source_stop_required"
    if first == "demand":
        expected = "files_unsupported"
    assert call(first).reason == expected
    assert call(second).reason == "files_unsupported"
    assert items()[following_key(V2.session_id)][0].get("files_required", False) is (first in {"demand", "files"})


def test_a_follow_handing_off_to_a_retarget_is_refused(source):
    source.call(follow_session, destination(), follow(source))
    edit(following_key(V2.session_id), retarget_request_id="retarget-one")
    assert refused(source, destination(), follow(source)) == (
        409, CONFLICT, "the original file operation is handing off to an explicit retarget")


def test_a_destination_prepared_before_another_one_committed_cannot_commit_over_it(source):
    """V3 prepares and is withdrawn; V2 then takes the Agent; V3's old commit finds the binding moved."""
    third = probe(V3, bootstrap_source_inactive=_inactive)
    command = follow(source, "f3")
    source.call(follow_session, third, command)
    source.call(withdraw_session, third, _withdraw(items()["agent"][1], "f3", V3.session_id))
    taken = follow(source, "f2")
    source.call(follow_session, destination(), taken)
    source.call(follow_session, destination(), taken.model_copy(update={"stage": "commit"}))
    assert refused(source, third, command.model_copy(update={"stage": "commit"})) == (
        409, CONFLICT, "the original execution binding changed")


def test_a_pending_of_another_request_is_refused(source):
    command = follow(source)
    source.call(follow_session, destination(), command)
    pending = dict(items()["agent"][0]["environment"], request_id="someone-else")
    edit("agent", environment=pending)
    assert refused(source, destination(), command) == (409, CONFLICT, "another original version transition is pending")


def test_a_follow_that_names_a_session_the_agent_never_prepared_has_no_source(source):
    edit("agent", preparation=None, environment=None)
    before = items()
    assert refused(source, destination(), follow(source)) == (
        409, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the Agent has no original prepared environment")
    assert items() == before


def test_a_committed_follow_is_not_repeated_for_another_expected_source(source):
    command = follow(source)
    source.call(follow_session, destination(), command)
    commit = command.model_copy(update={"stage": "commit"})
    ready = source.call(follow_session, destination(), commit)
    other = commit.model_copy(update={"expected_session_id": "offline-other-source"})
    assert refused(source, destination(), other) == (
        409, CONFLICT, "the named environment has another original operation")
    assert source.call(follow_session, destination(), commit) == ready


def test_a_prepare_by_another_destination_while_one_is_pending_is_refused(source):
    source.call(follow_session, destination(), follow(source))
    third = probe(V3, bootstrap_source_inactive=_inactive)
    assert refused(source, third, follow(source, "f3")) == (409, CONFLICT, COMPARISON)


def test_a_prepare_writes_the_cursor_profile_into_the_following_record(source):
    source.call(follow_session, destination(), follow(source))
    record = items()[following_key(V2.session_id)][0]
    assert (record["cursor_profile"], record["phase"], record["responses"]) == ("page-last-id-v1", "stopping", False)


def test_a_source_refusal_on_the_pending_view_is_carried_into_the_answer(source):
    source.call(follow_session, destination(), follow(source))
    pending = dict(items()["agent"][0]["environment"], source_invocation_refused=True)
    edit("agent", environment=pending)
    view = source.call(follow_session, destination(), follow(source))
    assert (view.reason, view.source_invocation_refused) == ("source_stop_required", True)


@pytest.mark.parametrize("idle_only", [True, False])
def test_only_a_follow_that_is_not_idle_only_settles_the_admission_first(source, idle_only):
    calls, runtime = [], destination()
    settle = runtime._settle_admission

    async def spy():
        calls.append(1)
        return await settle()

    runtime._settle_admission = spy
    source.call(follow_session, runtime, follow(source, idle_only=idle_only))
    assert calls == ([] if idle_only else [1])


@pytest.mark.parametrize("supported", [False, True])
def test_a_held_prepare_leaves_the_pending_view_only_for_a_following_capable_source(source, supported):
    before = items()["agent"][0]
    edit("agent", following_supported=supported,
         admitting={"request_id": "admitting-rid", "submit_digest": source.base.submit_digest})
    view = source.call(follow_session, destination(), follow(source))
    assert view.reason == "admission_pending"
    environment = items()["agent"][0].get("environment")
    assert (environment is not None and environment["state"] == "pending") is supported
    if not supported:
        assert environment == before.get("environment")


@pytest.mark.parametrize("started", [False, True])
def test_a_commit_marks_the_open_set_complete_only_for_an_admission_that_started(source, started):
    edit("agent", admission_started=started)
    runtime = destination()
    command = follow(source)
    source.call(follow_session, runtime, command)
    source.call(follow_session, runtime, command.model_copy(update={"stage": "commit"}))
    marked = source.call(native_open_rids.read, runtime)
    assert (marked.complete_for, marked.complete_since, marked.responses) == (
        (V2.session_id, "f1", False) if started else (None, None, False))


@pytest.mark.parametrize("files, phase, reason", [
    (False, "stopping", "source_stop_required"), (True, "files", "files_pending")])
def test_the_pending_follow_keeps_the_reason_of_the_phase_it_waits_in(source, tmp_path, files, phase, reason):
    options = {"workspace_files": NativeWorkspaceFiles(tmp_path / "files")} if files else {}
    held = source.call(follow_session, destination(**options), follow(source))
    stored = items()[following_key(V2.session_id)][0]
    assert (held.reason, stored["phase"], stored["view"]["reason"]) == (reason, phase, reason)


def test_a_drain_with_a_recorded_omission_binds_the_pending_view_without_the_decision(source):
    """The Agent's pending view leaves the owner's file decision out until the commit; the answer carries it."""
    source.call(follow_session, destination(), follow(source))
    key = following_key(V2.session_id)
    view = dict(items()[key][0]["view"], files_migrated=False, files_skip_request_id="skip-once")
    edit(key, phase="draining", view=view)
    record = _Following.model_validate(items()[key][0])
    omission = _Omission(request_id="skip-once", missing="source_not_migratable", decision_basis="system_policy",
                         original=record)

    async def write():
        async with FoundryStateStore(TURNS_STORE) as store:
            await store.create_item(omission_key(record.view), _json(omission))
    asyncio.run(write())
    held = source.call(follow_session, destination(), follow(source))
    assert (held.reason, held.files_migrated, held.files_skip_request_id, held.files_omission_basis) == (
        "source_stop_required", False, "skip-once", "system_policy")
    bound = items()["agent"][0]["environment"]
    assert (bound["state"], bound["request_id"]) == ("pending", "f1")
    assert [bound.get(name) for name in ("files_migrated", "files_skip_request_id", "files_omission_basis")] == [
        None, None, None]


def test_a_commit_without_the_files_record_waits_for_files(source, tmp_path):
    runtime = destination(workspace_files=NativeWorkspaceFiles(tmp_path / "files"))
    command = follow(source)
    assert source.call(follow_session, runtime, command).reason == "files_pending"
    edit(following_key(V2.session_id), phase="stopping")
    view = source.call(follow_session, runtime, command.model_copy(update={"stage": "commit"}))
    assert (view.state, view.reason) == ("pending", "files_pending")
    assert items()["agent"][0]["hosted_session_id"] == SOURCE
