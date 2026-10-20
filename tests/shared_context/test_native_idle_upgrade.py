"""ADR-0009 phase 4b-1: the binding row's idle-upgrade sub-object (B1) and the project-level veto row.

Offline: the stores run over the in-memory Table double; nothing here calls a session."""

from datetime import datetime, timedelta, timezone

import pytest

from shared_context.errors import Problem
from shared_context import native_binding
from shared_context.native_binding import NativeBindingStore
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401  (fixture)
from test_instruction_flow import instruction_models  # noqa: F401  (fixture)
from test_m3_auth import jwk_client, signing_key  # noqa: F401  (fixtures)
from test_native_binding import CountingRepository
from test_native_turn_api import native_api  # noqa: F401  (fixture)

AGENT, SID = "agent-a", "sid-41"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def table():
    repository, clock = CountingRepository(), Clock()
    store = NativeBindingStore(repository, tenant_id="tenant", team_id="team", clock=clock)
    store.claim(AGENT, hosted_session_id=SID, created_agent_version="41")
    return repository, clock, store


def test_a_row_without_the_sub_object_keeps_its_bytes(table):
    repository, _, store = table
    row = store.read(AGENT)
    assert row.idle_upgrade is None and "idle_upgrade" not in row.model_dump(mode="json")


def test_the_deferral_start_is_written_once_per_target(table):
    repository, clock, store = table
    writes = len(repository.writes)
    first = store.record_due(AGENT, hosted_session_id=SID, target_version="42").idle_upgrade
    assert (first.target_version, first.deferred_since) == ("42", clock.now.isoformat())
    clock.now += timedelta(hours=2)
    again = store.record_due(AGENT, hosted_session_id=SID, target_version="42").idle_upgrade
    assert again == first and len(repository.writes) == writes + 1, "the same target writes nothing more"
    # A selector move restarts U0 for the new target; a veto a later step wrote stays.
    row = store.read(AGENT)
    repository.replace(store.partition_key, store.row_key(AGENT), row.model_copy(update={
        "idle_upgrade": first.model_copy(update={"vetoed_versions": ("40",), "health_failures": 2})}).model_dump(
            mode="json"), repository.get(store.partition_key, store.row_key(AGENT)).etag)
    moved = store.record_due(AGENT, hosted_session_id=SID, target_version="43").idle_upgrade
    assert (moved.target_version, moved.deferred_since) == ("43", clock.now.isoformat())
    assert moved.vetoed_versions == ("40",) and moved.health_failures == 0


def test_another_sids_row_is_left_alone(table):
    repository, _, store = table
    writes = len(repository.writes)
    assert store.record_due(AGENT, hosted_session_id="sid-other", target_version="42").idle_upgrade is None
    assert len(repository.writes) == writes


def test_the_veto_row_is_insert_only_and_a_409_is_read_back():
    from shared_context.native_version_veto import VETO_PARTITION, NativeVersionVetoStore

    repository, clock = CountingRepository(), Clock()
    vetoes = NativeVersionVetoStore(repository, clock=clock)
    assert vetoes.read("fa-agent-a", "42") is None
    first = vetoes.veto("fa-agent-a", "42")
    clock.now += timedelta(minutes=5)
    again = vetoes.veto("fa-agent-a", "42")
    assert again == first and again.vetoed_at == "2026-10-18T09:00:00+00:00"
    assert [kind for kind, _ in repository.writes] == ["create"], "no replace, ever"
    assert list(repository.data) == [(VETO_PARTITION, "version:fa-agent-a:42")]
    assert vetoes.read("fa-agent-a", "42") == first and vetoes.read("fa-agent-a", "43") is None


def test_an_unreadable_or_foreign_veto_row_is_refused():
    from shared_context.native_version_veto import VETO_PARTITION, NativeVersionVetoStore

    repository = CountingRepository()
    vetoes = NativeVersionVetoStore(repository, clock=Clock())
    repository.create(VETO_PARTITION, vetoes.row_key("fa-agent-a", "42"), {"agent_name": "fa-agent-b"})
    with pytest.raises(Problem) as refused:
        vetoes.read("fa-agent-a", "42")
    assert refused.value.body["error"]["code"] == "NATIVE_VERSION_VETO_INVALID"


@pytest.mark.parametrize(("idle", "guard", "enabled"), [
    (None, None, False), ("0", "1", False), ("yes", "1", False), ("1", "1", True)])
def test_the_flag_is_off_unless_one_with_the_guard(idle, guard, enabled):
    environ = {key: value for key, value in (("FA_NATIVE_IDLE_UPGRADE", idle),
                                             ("FA_NATIVE_SESSION_GUARD_ENFORCE", guard)) if value is not None}
    assert native_binding.idle_upgrade_enabled(environ) is enabled


@pytest.mark.parametrize("guard", [None, "0"])
def test_the_api_refuses_to_start_with_the_idle_upgrade_but_not_the_guard(native_api, monkeypatch, guard):
    from shared_context.behavior_server import application

    monkeypatch.setenv("FA_NATIVE_IDLE_UPGRADE", "1")
    if guard is None:
        monkeypatch.delenv("FA_NATIVE_SESSION_GUARD_ENFORCE", raising=False)
    else:
        monkeypatch.setenv("FA_NATIVE_SESSION_GUARD_ENFORCE", guard)
    composition = native_api.composition
    monkeypatch.setattr(composition, "installation", lambda: None)
    monkeypatch.setattr(composition.runtime, "validate_startup", lambda: None)
    with pytest.raises(ValueError, match="FA_NATIVE_IDLE_UPGRADE=1 needs FA_NATIVE_SESSION_GUARD_ENFORCE=1"):
        application(composition)
    monkeypatch.setenv("FA_NATIVE_SESSION_GUARD_ENFORCE", "1")
    assert application(composition).state.composition is composition