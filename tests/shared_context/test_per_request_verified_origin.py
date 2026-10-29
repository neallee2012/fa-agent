"""Evidence-gated requests reuse the standing-policy origin this API process already verified.

Only ``installed_work_policy_origin`` reads the whole partition (``read_inventory(max_entities=None)``; dev 2026-10-28:
about 32k rows and 150 s), so only that kind is reused. Its real offline fixture needs ``test_channel_window``, which
is not in this repository, so these tests fake only that dispatch target and go through the real
``ServerComposition.installation``; the real standing-policy validator is not exercised here.
"""

import json
import threading

from fastapi.testclient import TestClient
import pytest

from shared_context import behavior_migration, behavior_server, behavior_work_policy
from shared_context import behavior_work_policy_installation
from shared_context.behavior_channels_migration import channel_key
from shared_context.behavior_migration import (
    MigrationStore, installation_record, installed_origin_pins, origin_pointer_keys, protection_keys,
)
from shared_context.behavior_server import application
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.behavior_work_policy import work_policy_key
from shared_context.errors import Problem
from test_behavior_assistant_server import assistant_server
from test_behavior_server import workspace
from test_fresh_installation import fresh_case, initialize
from test_m3_auth import jwk_client, signing_key

HISTORY = work_policy_key("history", "offline-standing-install")
POINTER = {"schema_version": "fa-standing-policy-current-v1", "history_key": HISTORY, "history_sha256": "0" * 64}


def external_write(h, writes, mode):
    # An unbound operator store, like the migration tests: it writes outside this process's FA writer fence.
    operator = MigrationStore(h.composition.clients[0], h.config.table.scope)
    operator.transact(
        ControlRequest(request_id="external-" + mode + "-" + str(len(writes)), request_digest="f" * 64,
                       read_set=tuple(key for key, _ in writes)),
        lambda _: ControlChanges(writes=tuple(ControlWrite(row_id=key, mode=mode, data=data) for key, data in writes),
                                 result={}))


@pytest.fixture
def standing(assistant_server, monkeypatch, caplog):
    """An activated installation whose pointers dispatch to the standing-policy origin, faked as one counted check."""
    h = assistant_server
    h.legacy()
    h.activate()  # creates the guard; it verified the legacy kind, before the standing pointer existed
    h.composition._verified_origin = None  # so startup verifies like a new API process
    h.pointer = work_policy_key("current", h.config.table.scope.authority_domain)
    external_write(h, [(HISTORY, {"kind": "offline-standing-history"}), (h.pointer, POINTER)], "create")
    h.origins, h.caplog = [], caplog

    def checked(store, config):
        h.origins.append(config.sha256)
        if store.read_snapshot((h.pointer,))[h.pointer].payload["data"] != POINTER:
            # The message the real check gives for a changed pointer.
            raise behavior_migration._problem("ROTATION_HISTORY_INVALID", "standing-policy pointer differs")
        store.bind_installation(installation_record(config)[0], config.sha256)
        return config

    monkeypatch.setattr(behavior_work_policy_installation, "installed_work_policy_origin", checked)
    return h


def gated(client, h, key):
    """The status and the code the API logged (it returns a generic code for most refusals)."""
    # Every /behavior/v1 route runs the current_native_evidence dependency, which calls installation(), first.
    h.caplog.clear()
    binding = h.config.bindings[0]
    response = client.get(f"/behavior/v1/bindings/{binding.binding_id}/jobs/{binding.job_id}", headers=h.headers(key))
    logged = [json.loads(record.getMessage().split(" ", 1)[1])["code"] for record in h.caplog.records
              if record.getMessage().startswith("BEHAVIOR_REQUEST_FAILED ")]
    return response.status_code, logged[-1] if logged else None


# The gate let the request through: no job was submitted, so the job-status handler itself refuses.
HANDLER_ANSWERED = (409, "WORKER_RECORD_MISSING")


def test_later_gated_requests_reuse_the_verified_standing_origin(standing, signing_key):
    h = standing
    with TestClient(application(h.composition)) as client:
        assert h.origins == [h.config.sha256]
        assert [gated(client, h, signing_key) for _ in range(3)] == [HANDLER_ANSWERED] * 3
    assert len(h.origins) == 1


def test_a_moved_pointer_clears_the_origin_and_the_full_check_refuses(standing, signing_key):
    h = standing
    with TestClient(application(h.composition)) as client:
        assert gated(client, h, signing_key) == HANDLER_ANSWERED
        external_write(h, [(h.pointer, {**POINTER, "history_sha256": "1" * 64})], "replace")
        assert gated(client, h, signing_key) == (409, "ROTATION_HISTORY_INVALID")
        assert h.composition._verified_origin is None
        assert gated(client, h, signing_key) == (409, "ROTATION_HISTORY_INVALID")
    assert len(h.origins) == 3


def test_a_pointer_naming_no_row_key_gets_the_full_checks_refusal(standing, signing_key):
    # Before: the pins passed that name to read_snapshot, which refused it with 422 BEHAVIOR_INVALID_DATA.
    h = standing
    with TestClient(application(h.composition)) as client:
        assert gated(client, h, signing_key) == HANDLER_ANSWERED
        external_write(h, [(h.pointer, {**POINTER, "history_key": "not a row key"})], "replace")
        assert gated(client, h, signing_key) == (409, "ROTATION_HISTORY_INVALID")
    assert len(h.origins) == 2


def test_a_transient_conflict_after_the_origin_check_keeps_the_origin(standing, signing_key, monkeypatch):
    h = standing
    original = behavior_work_policy.attach_standing_policy
    raised = []

    def once(composition, **kwargs):
        if not raised:
            raised.append(True)
            raise Problem(412, "BEHAVIOR_CONFLICT", "authority changed while reading predicates")
        return original(composition, **kwargs)

    with TestClient(application(h.composition)) as client:
        kept = h.composition._verified_origin
        monkeypatch.setattr(behavior_work_policy, "attach_standing_policy", once)
        assert gated(client, h, signing_key) == (412, "BEHAVIOR_CONFLICT")
        assert h.composition._verified_origin is kept
        assert gated(client, h, signing_key) == HANDLER_ANSWERED
    assert (raised, len(h.origins)) == ([True], 1)


@pytest.mark.parametrize("first_fails", [False, True], ids=["first-verifies", "first-fails"])
def test_concurrent_requests_run_one_full_check_at_a_time(standing, monkeypatch, first_fails):
    # Routes call installation() from the threadpool; two full reads at once can hold twice the inventory in memory.
    h = standing
    checked, pins = behavior_work_policy_installation.installed_work_policy_origin, installed_origin_pins
    started, second_read_pins, release = threading.Event(), threading.Event(), threading.Event()
    checks, reads, results = [], [], {}

    def first_waits(store, config):
        checks.append(threading.current_thread().name)
        if len(checks) == 1:
            started.set()
            assert release.wait(10)
            if first_fails:
                raise behavior_migration._problem("ROTATION_HISTORY_INVALID", "injected full-check refusal")
        return checked(store, config)

    def read_pins(store, config):
        reads.append(1)
        if len(reads) == 2:
            second_read_pins.set()  # the second request saw no verified origin, so it also needs a full check
        return pins(store, config)

    def request(name):
        try:
            h.composition.installation()
            results[name] = None
        except Problem as error:
            results[name] = error.body["error"]["code"]

    monkeypatch.setattr(behavior_work_policy_installation, "installed_work_policy_origin", first_waits)
    monkeypatch.setattr(behavior_migration, "installed_origin_pins", read_pins)
    first = threading.Thread(target=request, args=("first",), name="first")
    second = threading.Thread(target=request, args=("second",), name="second")
    first.start()
    assert started.wait(10)
    second.start()
    assert second_read_pins.wait(10)
    release.set()
    for thread in (first, second):
        thread.join(10)
        assert not thread.is_alive()
    if first_fails:
        # The waiter finds the origin cleared, so it runs its own check rather than reusing a refused one.
        assert (checks, results) == (["first", "second"], {"first": "ROTATION_HISTORY_INVALID", "second": None})
    else:
        assert (checks, results) == (["first"], {"first": None, "second": None})
    assert isinstance(h.composition._verified_origin, behavior_server._VerifiedOrigin)


def fail_once(monkeypatch, status, code):
    """attach_standing_policy runs after the origin check for every kind; it raises once, then works."""
    original = behavior_work_policy.attach_standing_policy
    raised = []

    def once(composition, **kwargs):
        if not raised:
            raised.append(True)
            raise Problem(status, code, "injected after the origin check")
        return original(composition, **kwargs)

    monkeypatch.setattr(behavior_work_policy, "attach_standing_policy", once)


@pytest.mark.parametrize("status,code", [(403, "WORK_POLICY_SCOPE_CHANGED"), (409, "BEHAVIOR_CONFLICT")])
def test_any_other_error_after_the_origin_check_clears_the_standing_origin(
    standing, signing_key, monkeypatch, status, code,
):
    # As on main: a non-transient root failure leaves peers no origin, so they get 503 AGENT_ORIGIN_UNAVAILABLE.
    h = standing
    with TestClient(application(h.composition)) as client:
        fail_once(monkeypatch, status, code)
        assert gated(client, h, signing_key) == (status, code)
        assert h.composition._verified_origin is None
        assert gated(client, h, signing_key) == HANDLER_ANSWERED
    assert len(h.origins) == 2


def test_a_changed_installed_configuration_refuses_requests_and_writes(standing, signing_key):
    h = standing
    with TestClient(application(h.composition)) as client:
        assert gated(client, h, signing_key) == HANDLER_ANSWERED
        key, installed = installation_record(h.config)
        external_write(h, [(key, {**installed, "configuration_sha256": "f" * 64})], "replace")
        assert gated(client, h, signing_key) == (409, "MIGRATION_INSTALLATION_CHANGED")
    with pytest.raises(Problem) as denied:
        h.runtime.store.transact(ControlRequest(request_id="after-replacement", request_digest="d" * 64),
                                 lambda _: ControlChanges(writes=(), result={}))
    assert denied.value.status == 409
    assert denied.value.body["error"]["code"] == "MIGRATION_INSTALLATION_CHANGED"


@pytest.mark.parametrize("mismatch", ["process", "store"])
def test_another_process_or_store_does_not_reuse_the_verified_origin(standing, signing_key, monkeypatch, mismatch):
    h = standing
    with TestClient(application(h.composition)) as client:
        assert gated(client, h, signing_key) == HANDLER_ANSWERED
        if mismatch == "process":
            pid = behavior_server.getpid()
            monkeypatch.setattr(behavior_server, "getpid", lambda: pid + 1)
        else:
            from dataclasses import replace
            h.composition._verified_origin = replace(
                h.composition._verified_origin, store=MigrationStore(h.composition.clients[0], h.config.table.scope))
        assert gated(client, h, signing_key) == HANDLER_ANSWERED
    assert len(h.origins) == 2


def test_the_fresh_origin_is_still_checked_on_every_request(fresh_case, signing_key, monkeypatch, caplog):
    # Stage's kind: already key reads only, so it keeps main's per-request cost-ledger and receipt checks.
    h = fresh_case
    h.caplog = caplog
    initialize(h)
    calls = {"origin": 0, "pins": 0}
    for name in ("installed_origin", "installed_origin_pins"):
        def counted(store, config, _name=name, _original=getattr(behavior_migration, name)):
            calls["pins" if _name.endswith("pins") else "origin"] += 1
            return _original(store, config)
        monkeypatch.setattr(behavior_migration, name, counted)
    app = application(h.composition)
    monkeypatch.setattr(h.composition.catalog, "require_activation", lambda: None)
    with TestClient(app) as client:
        for _ in range(2):
            # The fresh installation retired this legacy binding, so the gate passes and the handler refuses.
            assert gated(client, h, signing_key) == (403, "CHANNELS_RETIRED_BINDING")
    assert calls == {"origin": 3, "pins": 1}


def test_the_pins_cover_every_pointer_installed_origin_dispatches_on(assistant_server, monkeypatch):
    h = assistant_server
    h.legacy()
    h.activate()
    store = h.runtime.store
    read = []
    original = store.read_snapshot
    monkeypatch.setattr(store, "read_snapshot", lambda keys, *a, **k: (read.extend(keys), original(keys, *a, **k))[1])
    behavior_migration.installed_origin(store, h.config)
    pointers = set(read) - {installation_record(h.config)[0], *protection_keys(h.config)}
    domain = h.config.table.scope.authority_domain
    assert pointers == set(origin_pointer_keys(domain))
    external_write(h, [(HISTORY, {"kind": "offline-standing-history"}),
                       (work_policy_key("current", domain), POINTER)], "create")
    assert set(installed_origin_pins(store, h.config)) == pointers | {channel_key("current", domain), HISTORY}


@pytest.mark.parametrize("status,code", [(412, "BEHAVIOR_CONFLICT"), (403, "WORK_POLICY_SCOPE_CHANGED")])
def test_any_error_after_the_origin_check_clears_the_fresh_origin(
    fresh_case, signing_key, monkeypatch, caplog, status, code,
):
    # Stage's kind keeps main's rule even for a transient 412: peers then get 503 AGENT_ORIGIN_UNAVAILABLE.
    h = fresh_case
    h.caplog = caplog
    initialize(h)
    app = application(h.composition)
    monkeypatch.setattr(h.composition.catalog, "require_activation", lambda: None)
    with TestClient(app) as client:
        assert isinstance(h.composition._verified_origin, behavior_server._VerifiedOrigin)
        fail_once(monkeypatch, status, code)
        assert gated(client, h, signing_key) == (status, code)
        assert h.composition._verified_origin is None
        assert gated(client, h, signing_key) == (403, "CHANNELS_RETIRED_BINDING")
        assert isinstance(h.composition._verified_origin, behavior_server._VerifiedOrigin)
