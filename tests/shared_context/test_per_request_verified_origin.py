"""The origin check runs on every gated request; its result is kept only for peers and cleared on error."""

import json

from fastapi.testclient import TestClient
import pytest

from shared_context import behavior_migration, behavior_server, behavior_work_policy
from shared_context.behavior_migration import MigrationStore
from shared_context.behavior_server import application
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.behavior_work_policy import work_policy_key
from shared_context.errors import Problem
from test_behavior_assistant_server import assistant_server
from test_behavior_server import workspace
from test_fresh_installation import fresh_case, initialize
from test_m3_auth import jwk_client, signing_key



def external_write(h, writes, mode):
    # An unbound operator store, like the migration tests: it writes outside this process's FA writer fence.
    operator = MigrationStore(h.composition.clients[0], h.config.table.scope)
    operator.transact(
        ControlRequest(request_id="external-" + mode + "-" + str(len(writes)), request_digest="f" * 64,
                       read_set=tuple(key for key, _ in writes)),
        lambda _: ControlChanges(writes=tuple(ControlWrite(row_id=key, mode=mode, data=data) for key, data in writes),
                                 result={}))


def gated(client, h, key):
    """The status and the code the API logged (it returns a generic code for most refusals)."""
    # Every /behavior/v1 route runs the current_native_evidence dependency, which calls installation(), first.
    h.caplog.clear()
    binding = h.config.bindings[0]
    response = client.get(f"/behavior/v1/bindings/{binding.binding_id}/jobs/{binding.job_id}", headers=h.headers(key))
    logged = [json.loads(record.getMessage().split(" ", 1)[1])["code"] for record in h.caplog.records
              if record.getMessage().startswith("BEHAVIOR_REQUEST_FAILED ")]
    return response.status_code, logged[-1] if logged else None


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


def test_the_fresh_origin_is_still_checked_on_every_request(fresh_case, signing_key, monkeypatch, caplog):
    # Stage's kind: already key reads only, so it keeps main's per-request cost-ledger and receipt checks.
    h = fresh_case
    h.caplog = caplog
    initialize(h)
    calls = []
    checked = behavior_migration.installed_origin
    monkeypatch.setattr(behavior_migration, "installed_origin",
                        lambda store, config: (calls.append(1), checked(store, config))[1])
    app = application(h.composition)
    monkeypatch.setattr(h.composition.catalog, "require_activation", lambda: None)
    with TestClient(app) as client:
        for _ in range(2):
            # The fresh installation retired this legacy binding, so the gate passes and the handler refuses.
            assert gated(client, h, signing_key) == (403, "CHANNELS_RETIRED_BINDING")
    assert len(calls) == 3


def test_a_retired_standing_policy_pointer_is_refused_explicitly(assistant_server):
    # Without this refusal the row would fall through to the legacy kinds and start as if it were not there.
    h = assistant_server
    h.legacy()
    h.activate()
    domain = h.config.table.scope.authority_domain
    external_write(h, [(work_policy_key("current", domain), {"schema_version": "fa-standing-policy-current-v1"})],
                   "create")
    with pytest.raises(Problem) as refused:
        behavior_migration.installed_origin(h.runtime.store, h.config)
    assert (refused.value.status, refused.value.body["error"]["code"]) == (503, "BEHAVIOR_CONFIGURATION")
    assert "retired standing-policy installation" in str(refused.value)


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
