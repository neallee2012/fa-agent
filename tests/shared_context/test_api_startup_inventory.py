"""The API verifies its installed origin once before it opens the port.

In work-policy installations that verification reads and validates the whole authority partition, so each extra
run before the port opens adds minutes to every new revision (dev 2026-10-25: about 5 to 6 minutes).
"""

from fastapi.testclient import TestClient

from shared_context.behavior_server import ServerComposition, application
from shared_context.behavior_work_policy import WorkPolicyControl
from test_behavior_assistant_server import assistant_server
from test_behavior_server import workspace
from test_fresh_installation import fresh_case, initialize
from test_m3_auth import jwk_client, signing_key


def counted_installation(monkeypatch):
    # The root's installation() verifies the origin; counting it, not installed_origin, also catches a call that
    # reuses one. Peers (agent_id set) only reuse the root's and are not counted.
    calls = []
    original = ServerComposition.installation

    def counted(composition):
        if composition.agent_id is None:
            calls.append(composition.config.sha256)
        return original(composition)

    monkeypatch.setattr(ServerComposition, "installation", counted)
    return calls


def test_startup_runs_installation_once_before_the_port_opens(assistant_server, monkeypatch):
    h = assistant_server
    h.legacy()
    h.activate()
    calls = counted_installation(monkeypatch)
    app = application(h.composition)
    built = len(calls)
    with TestClient(app) as client:
        started = len(calls)
        assert client.get("/healthz").status_code == 200
    assert (built, started - built) == (1, 0)


def test_work_policy_startup_runs_installation_once_before_the_port_opens(fresh_case, monkeypatch):
    # The lightest real work-policy installation offline; installed_origin dispatches it to installed_fresh_origin.
    h = fresh_case
    initialize(h)
    calls = counted_installation(monkeypatch)
    app = application(h.composition)
    built = len(calls)
    assert h.runtime.work_policy is not None
    with TestClient(app) as client:
        started = len(calls)
        assert client.get("/healthz").status_code == 200
    assert (built, started - built) == (1, 0)


def test_work_policy_lifespan_does_not_reread_the_whole_work_registry(fresh_case, monkeypatch):
    # A full refresh reads every registered work row one by one. dev 2026-10-28 spent about 104 s of its 121 s
    # lifespan before the first observer could run; that window held the lifespan's two full refreshes plus
    # pending(), the warm step and the resume loop's per-job reads, so 104 s is an upper bound for the refreshes.
    # The factory's refreshes must stay: they are the readiness check that runs before the port opens.
    h = fresh_case
    initialize(h)
    full = []
    original = WorkPolicyControl.refresh

    def counted(self, binding_id=None):
        if self is h.runtime.work_policy and binding_id is None:
            full.append(binding_id)
        return original(self, binding_id)

    monkeypatch.setattr(WorkPolicyControl, "refresh", counted)
    app = application(h.composition)
    built = len(full)
    assert h.runtime.work_policy is not None and app.state.instruction_runner
    with TestClient(app) as client:
        started = len(full)
        assert client.get("/healthz").status_code == 200
    # installation()'s attach_standing_policy and validate_startup() at build; none again in the lifespan.
    assert (built, started - built) == (2, 0)
