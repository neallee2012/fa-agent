"""Pins behavior_router's whole table: order, names, grants, body models and the exact runtime call.

The runtime and verifier are stand-ins: real flows live in test_behavior_api and test_behavior_native_api.
"""

import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr

from shared_context import behavior_api
from shared_context import behavior_wire as wire
from shared_context.behavior_api import PREFIX, _install_error_handlers, behavior_router
from shared_context.behavior_auth import BehaviorJwtVerifier
from shared_context.errors import Problem

W = object()  # the verified principal
C = SimpleNamespace(request_id="rid-1")  # the parsed command
BINDING = SimpleNamespace(job_id="job-1", child_jobs=(SimpleNamespace(job_id="child-1"),))
IDS = {"binding_id": "b-1", "source_id": "s-1", "version": "3", "snapshot_id": "n-1", "backup_id": "a" * 32,
       "claim_id": "c-1", "job_id": "job-1", "child_id": "child-1"}
AUTH = {"authorization": "Bearer stand-in"}


def route(method, path, grant, model, call, args, **options):
    return SimpleNamespace(method=method, path=path, grant=grant, model=model, call=call, args=args, options=options)


def stages(paths, grants, model, call, args):
    """A write stage and its reconcile of the original request, each with its own grant."""
    return [route("POST", paths[0], grants[0], model, call, args, reconcile=False),
            route("POST", paths[1], grants[1], model, call, args, reconcile=True)]


SOURCE = "/sources/{source_id}"
CLAIM = "/claims/{claim_id}"
BACKUP = "/backups/{backup_id}"
CHILD = "/children/{child_id}"
ROUTES = [
    *stages((SOURCE + "/commit", SOURCE + "/reconcile"), ("submit", "read"), wire.Command, "commit_source",
            ("b-1", "s-1", C, W)),
    route("POST", SOURCE + "/prepare", "submit", wire.SourcePrepare, "prepare_source", ("b-1", "s-1", C, W)),
    *stages((SOURCE + "/withdraw", SOURCE + "/withdrawal-reconcile"), ("approve", "approve"), wire.SourceWithdrawal,
            "withdraw_source", ("b-1", "s-1", C, W)),
    route("GET", SOURCE + "/versions/{version}", "read", None, "read_source", ("b-1", "s-1", 3, W)),
    route("POST", "/snapshots/{snapshot_id}/capture", "submit", wire.SnapshotCapture, "capture_snapshot",
          ("b-1", "n-1", C, W)),
    route("POST", "/snapshots/{snapshot_id}/verify", "read", wire.Command, "verify_snapshot", ("b-1", "n-1", C, W)),
    route("POST", BACKUP + "/prepare", "backup", wire.BackupPrepare, "prepare_backup", ("b-1", "a" * 32, C, W)),
    *stages((BACKUP + "/upload", BACKUP + "/reconcile"), ("backup", "backup"), wire.Command, "upload_backup",
            ("b-1", "a" * 32, C, W)),
    route("POST", CLAIM + "/review", "approve", wire.ClaimReview, "review_claim", ("b-1", "c-1", C, W)),
    route("POST", CLAIM + "/preview", "approve", wire.ClaimDraft, "preview_claim", ("b-1", "c-1", C, W)),
    *stages((CLAIM + "/commit", CLAIM + "/reconcile"), ("submit", "read"), wire.Command, "commit_claim",
            ("b-1", "c-1", C, W)),
    route("GET", CLAIM, "read", None, "current_claim", ("b-1", "c-1", W)),
    route("POST", "/context", "read", wire.ContextRequest, "context", ("b-1", C, W)),
    route("POST", "/operation/prepare", "read", wire.OperationPrepare, "prepare_operation", ("b-1", C, W)),
    route("GET", "/operation", "read", None, "operation_preview", ("b-1", W)),
    route("GET", "/operation/decision-state", "read", None, "operation_decision_state", ("b-1", W)),
    route("POST", "/operation/approve", "approve", wire.OperationApproval, "approve_operation", ("b-1", C, W)),
    route("POST", "/operation/approval-reconcile", "read", wire.OperationApproval, "reconcile_operation_approval",
          ("b-1", C, W)),
    route("POST", "/operation/revoke", "approve", wire.OperationRevocation, "revoke_operation", ("b-1", C, W)),
    route("POST", "/operation/revocation-reconcile", "read", wire.OperationRevocation,
          "reconcile_operation_revocation", ("b-1", C, W)),
    route("POST", "/operation/reject", "approve", wire.OperationRejection, "reject_operation", ("b-1", C, W)),
    route("POST", "/operation/rejection-reconcile", "read", wire.OperationRejection,
          "reconcile_operation_rejection", ("b-1", C, W)),
    *stages(("/operation/execute", "/operation/reconcile"), ("execute", "read"), wire.Command, "execute_operation",
            ("b-1", C, W)),
    *stages(("/work/submit", "/work/reconcile"), ("submit", "read"), wire.SubmitWork, "submit", ("b-1", C, W)),
    route("GET", "/jobs/{job_id}", "read", None, "job_status", ("b-1", W)),
    *stages((CHILD + "/enqueue", CHILD + "/reconcile"), ("submit", "read"), wire.Command, "enqueue_child",
            ("b-1", "child-1", C, W)),
    route("GET", CHILD, "read", None, "child_status", ("b-1", "child-1", W)),
    route("POST", "/memory/plan", "submit", wire.Command, "memory_projection", ("b-1", C, W), action="plan"),
    route("POST", "/memory/start", "submit", wire.MemoryProjectionCommand, "memory_projection", ("b-1", C, W),
          action="start"),
    route("POST", "/memory/poll", "submit", wire.MemoryProjectionCommand, "memory_projection", ("b-1", C, W),
          action="poll"),
    route("POST", "/memory/reconcile", "read", wire.MemoryProjectionCommand, "memory_projection", ("b-1", C, W),
          action="reconcile"),
    route("POST", "/memory/search", "read", wire.MemoryProjectionQuery, "memory_projection", ("b-1", C, W),
          action="search"),
    route("POST", "/memory/context", "read", wire.MemoryProjectionQuery, "memory_projection", ("b-1", C, W),
          action="context"),
    route("POST", "/delivery/pull", "read", wire.Command, "fetch", ("b-1", C, W)),
    route("POST", "/delivery/pull-continue", "read", wire.Command, "continue_fetch", ("b-1", C, W)),
    route("POST", "/delivery/pull-reconcile", "read", wire.Command, "reconcile_fetch", ("b-1", C, W)),
    route("POST", "/delivery/acknowledge", "acknowledge", wire.PullAcknowledgment, "acknowledge", ("b-1", C, W)),
]
CALLBACKS = [
    ("/hosted/launches/{launch_id}/claim", "assigned-native-claim"),
    ("/hosted/launches/{launch_id}/probes/{probe_id}/resolve", "assigned-native-resolve"),
    ("/hosted/launches/{launch_id}/probes/{probe_id}/report", "assigned-native-report"),
]


class PullBinding(BaseModel):
    payload_digest: str = "d" * 64


class Result(dict):
    """Satisfies every render: empty JSON for the plain routes, attributes for the shaped ones."""
    manifest, claims, originals = {"m": 1}, {"c": 2}, {"o": 3}
    original = b"exact"
    event = SimpleNamespace(media_type="text/plain", reference=SimpleNamespace(sha256="e" * 64, version=3))
    payload, exchange_id, acknowledgment_token, binding = b'{"p":1}', "x-1", "ack-1", PullBinding()


class Runtime:
    def __init__(self, *, backups=True, memory=True, launches=True):
        launch = SimpleNamespace(targets=(), team_id="team") if launches else None
        self.dep = SimpleNamespace(source_backups=object() if backups else None,
                                   memory_projections=("memory",) if memory else (), native_launches=launch)
        self.bindings, self.calls, self.mode, self.result = {}, [], "native", Result()

    def authorize(self, *args):
        self.calls.append(("authorize", args, {}))
        return BINDING, None, None

    def __getattr__(self, name):
        if name == "profiles":  # only AssistantRuntime has them
            raise AttributeError(name)

        def call(*args, **options):
            self.calls.append((name, args, options))
            if isinstance(self.result, Exception):
                raise self.result
            return self.result
        return call


class Verifier(BehaviorJwtVerifier):
    def __init__(self, actors=None):
        self.auth = SimpleNamespace(actors=actors or {}, tenant_id="tenant")
        self.settings = SimpleNamespace(human_oid="human", team="team")

    def __call__(self, request):
        return W


def serve(runtime, verifier=None):
    app = FastAPI()
    _install_error_handlers(app)
    app.include_router(behavior_router(runtime, verifier or Verifier()))
    return TestClient(app, raise_server_exceptions=False, headers=AUTH)


def url(path):
    return (PREFIX + path).format(**IDS)


@pytest.fixture
def body(monkeypatch):
    seen = []

    async def parsed(_request, model):
        seen.append(model)
        return C

    monkeypatch.setattr(behavior_api, "_body", parsed)
    return seen


def registered(router):
    return [(sorted(item.methods), item.path, item.name) for item in router.routes]


def expected(item):
    return ([item.method], PREFIX + item.path, "behavior-" + item.path.replace("/", "-") + item.method)


def callback_rows():
    return [(["POST"], PREFIX + path, name) for path, name in CALLBACKS]


def test_every_route_keeps_its_order_method_and_name():
    assert registered(behavior_router(Runtime(), Verifier())) == [*map(expected, ROUTES), *callback_rows()]


@pytest.mark.parametrize("optional", ["backups", "memory", "launches"])
def test_a_missing_optional_dependency_only_drops_its_own_routes(optional):
    rows = [*map(expected, ROUTES), *callback_rows()]
    gone = {"backups": lambda row: "/backups/" in row[1], "memory": lambda row: "/memory/" in row[1],
            "launches": lambda row: "/hosted/" in row[1]}[optional]
    assert registered(behavior_router(Runtime(**{optional: False}), Verifier())) == [
        row for row in rows if not gone(row)]


@pytest.mark.parametrize("item", ROUTES, ids=lambda item: item.method + " " + item.path)
def test_route_checks_its_grant_parses_its_model_and_calls_the_runtime_once(body, item):
    runtime = Runtime()
    response = serve(runtime).request(item.method, url(item.path))
    assert response.status_code == 200, response.text
    assert body == ([] if item.model is None else [item.model])
    assert runtime.calls == [("authorize", ("b-1", W, item.grant), {}), (item.call, item.args, item.options)]


def test_the_runtime_method_is_looked_up_at_request_time(body):
    runtime = Runtime()
    client = serve(runtime)
    runtime.job_status = lambda *_: {"late": True}
    assert client.get(url("/jobs/{job_id}")).json() == {"late": True}


@pytest.mark.parametrize("path", [
    "/sources/bad.id/commit", "/sources/s-1/versions/0", "/sources/s-1/versions/01", "/sources/s-1/versions/2147483648",
    "/sources/s-1/versions/12345678901", "/sources/s-1/versions/x", "/backups/" + "A" * 32 + "/upload",
    "/backups/s-1/upload", "/jobs/job-2", "/children/child-2", "/children/child-2/enqueue",
])
def test_a_path_outside_the_binding_is_denied_after_authorize_and_before_anything_else(body, path):
    runtime = Runtime()
    response = serve(runtime).request("GET" if path.count("/") < 3 or "versions" in path else "POST",
                                      PREFIX.format(binding_id="b-1") + path + "?also=query")
    assert (response.status_code, response.json()["error"]["code"]) == (403, "BEHAVIOR_ACCESS_DENIED")
    assert [call[0] for call in runtime.calls] == ["authorize"] and not body


def test_the_largest_source_version_is_accepted_as_an_integer(body):
    runtime = Runtime()
    assert serve(runtime).get(url(SOURCE) + "/versions/2147483647").status_code == 200
    assert runtime.calls[-1] == ("read_source", ("b-1", "s-1", 2147483647, W), {})


def test_authorize_runs_before_the_path_is_read():
    runtime = Runtime()

    def denied(*_):
        raise Problem(404, "BEHAVIOR_NOT_FOUND", "unknown binding")

    runtime.authorize = denied
    assert serve(runtime).get(url("/jobs/not.an.id")).status_code == 404


@pytest.mark.parametrize("method,path", [("POST", "/work/submit"), ("GET", "/operation")])
def test_query_parameters_are_refused_before_the_body_or_handler(body, method, path):
    runtime = Runtime()
    response = serve(runtime).request(method, url(path) + "?x=1")
    assert (response.status_code, response.json()["error"]["code"]) == (422, "BEHAVIOR_INVALID_REQUEST")
    assert [call[0] for call in runtime.calls] == ["authorize"] and not body


@pytest.mark.parametrize("options", [{"content": b"x"}, {"headers": {"transfer-encoding": "chunked"}}])
def test_read_routes_refuse_a_body(options):
    runtime = Runtime()
    response = serve(runtime).request("GET", url("/operation"), **options)
    assert (response.status_code, response.json()["error"]["code"]) == (422, "BEHAVIOR_INVALID_REQUEST")
    assert [call[0] for call in runtime.calls] == ["authorize"]


def test_handler_failures_keep_their_problem_or_become_the_dependency_answer(body):
    runtime = Runtime()
    client = serve(runtime)
    runtime.result = Problem(409, "BEHAVIOR_CONFLICT", "kept")
    assert client.get(url("/operation")).status_code == 409
    runtime.result = RuntimeError("SYNTHETIC private detail")
    response = client.get(url("/operation"))
    assert response.status_code == 503 and "SYNTHETIC" not in response.text
    # Only the route's own wrapper answers with the shared Problem shape; the app-wide fallback has another message.
    assert response.json()["error"]["message"] == "The bounded operation could not be completed."


def test_a_failed_render_is_the_same_dependency_answer(body):
    runtime = Runtime()
    runtime.result = {}  # the verify render reads .manifest
    response = serve(runtime).post(url("/snapshots/{snapshot_id}/verify"))
    assert response.status_code == 503
    assert response.json()["error"]["message"] == "The bounded operation could not be completed."


def test_exact_source_returns_the_original_bytes_and_identity_headers():
    response = serve(Runtime()).get(url(SOURCE + "/versions/{version}"))
    assert response.content == b"exact" and response.headers["content-type"].startswith("text/plain")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-source-sha256"] == "e" * 64 and response.headers["x-source-version"] == "3"


@pytest.mark.parametrize("size,status", [(8192, 200), (8193, 413)])
def test_exact_source_has_its_own_output_bound(size, status):
    runtime = Runtime()
    runtime.result.original = b"x" * size
    response = serve(runtime).get(url(SOURCE + "/versions/{version}"))
    assert response.status_code == status
    if status == 413:
        assert response.json()["error"]["code"] == "BEHAVIOR_BODY_LIMIT"


@pytest.mark.parametrize("path,fields", [
    ("/snapshots/{snapshot_id}/verify", {"manifest": {"m": 1}, "originals": {"o": 3}}),
    ("/context", {"manifest": {"m": 1}, "claims": {"c": 2}, "originals": {"o": 3}}),
])
def test_shaped_routes_project_only_their_fields(body, path, fields):
    assert serve(Runtime()).post(url(path)).json() == fields


@pytest.mark.parametrize("path", ["/delivery/pull", "/delivery/pull-continue", "/delivery/pull-reconcile"])
def test_pulls_return_the_exact_payload_with_exchange_headers(body, path):
    response = serve(Runtime()).post(url(path))
    assert response.status_code == 200 and response.content == b'{"p":1}'
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-behavior-exchange"] == "x-1" and response.headers["x-behavior-ack-token"] == "ack-1"
    assert response.headers["x-behavior-payload-sha256"] == "d" * 64
    assert response.headers["x-behavior-confirmation-level"] == "client_received"
    assert json.loads(base64.urlsafe_b64decode(response.headers["x-behavior-binding"])) == {"payload_digest": "d" * 64}


def test_reconciling_a_pull_that_never_started_names_the_original_request(body):
    runtime = Runtime()
    runtime.result = Problem(409, "INBOX_FETCH_NOT_STARTED", "internal wording")
    response = serve(runtime).post(url("/delivery/pull-reconcile"))
    assert response.status_code == 409 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"error": {
        "code": "INBOX_FETCH_NOT_STARTED", "message": "No exchange exists for the continued original fetch.",
        "original_request_id": "rid-1", "automatic_retry": False,
    }}


def test_other_pull_reconcile_problems_stay_on_the_shared_path(body):
    runtime = Runtime()
    runtime.result = Problem(409, "BEHAVIOR_CONFLICT", "kept")
    response = serve(runtime).post(url("/delivery/pull-reconcile"))
    assert response.status_code == 409 and response.json()["error"]["code"] == "BEHAVIOR_CONFLICT"
    assert "original_request_id" not in response.json()["error"]


def test_a_verifier_of_another_type_is_refused():
    with pytest.raises(Problem) as error:
        behavior_router(Runtime(), object())
    assert error.value.body["error"] == {
        "code": "BEHAVIOR_CONFIGURATION", "message": "the explicit behavior JWT verifier is required"}


GOOD_BINDING = {"agent_oid": "agent", "tenant_id": "tenant", "human_oid": "human", "team_id": "team"}
ACTORS = {"agent": SimpleNamespace(team="team")}


def test_a_matching_binding_is_accepted():
    runtime = Runtime()
    runtime.bindings = {"b-1": SimpleNamespace(**GOOD_BINDING)}
    assert behavior_router(runtime, Verifier(ACTORS)).routes


@pytest.mark.parametrize("change,actors", [
    ({"tenant_id": "other"}, ACTORS), ({"human_oid": "other"}, ACTORS), ({"team_id": "other"}, ACTORS),
    ({"agent_oid": "unknown"}, ACTORS), ({}, {"agent": SimpleNamespace(team="other")}),
    # The binding and its actor agree with each other, not with the verifier's team.
    ({"team_id": "other"}, {"agent": SimpleNamespace(team="other")}),
])
def test_a_binding_that_differs_from_the_verifier_is_refused(change, actors):
    runtime = Runtime()
    runtime.bindings = {"b-1": SimpleNamespace(**{**GOOD_BINDING, **change})}
    with pytest.raises(Problem) as error:
        behavior_router(runtime, Verifier(actors))
    assert error.value.body["error"] == {
        "code": "BEHAVIOR_CONFIGURATION", "message": "JWT and runtime principal bindings differ"}


def test_a_listed_native_executor_is_accepted():
    runtime = Runtime()
    runtime.dep.native_launches.targets = (SimpleNamespace(agent_oid="agent"),)
    assert behavior_router(runtime, Verifier(ACTORS)).routes


@pytest.mark.parametrize("oid,actors", [("unknown", ACTORS), ("agent", {"agent": SimpleNamespace(team="other")})])
def test_a_native_executor_must_be_an_actor_of_the_launch_team(oid, actors):
    runtime = Runtime()
    runtime.dep.native_launches.targets = (SimpleNamespace(agent_oid=oid),)
    with pytest.raises(Problem) as error:
        behavior_router(runtime, Verifier(actors))
    assert error.value.body["error"] == {
        "code": "BEHAVIOR_CONFIGURATION", "message": "native executor MI must be explicitly allowlisted"}


def test_a_registered_actor_only_widens_this_router():
    runtime = Runtime()
    runtime.bindings = {"b-1": SimpleNamespace(**GOOD_BINDING)}
    verifier = Verifier({"other": SimpleNamespace(team="team")})
    with pytest.raises(Problem):
        behavior_router(runtime, verifier)
    assert behavior_router(runtime, verifier, registered_actor=SimpleNamespace(oid="agent", team="team")).routes
    assert "agent" not in verifier.auth.actors


def test_a_registered_actor_never_replaces_a_configured_one():
    runtime = Runtime()
    runtime.bindings = {"b-1": SimpleNamespace(**GOOD_BINDING)}
    with pytest.raises(Problem) as error:
        behavior_router(runtime, Verifier(ACTORS), registered_actor=SimpleNamespace(oid="agent", team="team"))
    assert error.value.body["error"] == {
        "code": "BEHAVIOR_CONFIGURATION", "message": "a registered agent cannot reuse a configured actor"}


class ClaimResult(BaseModel):
    claim_state: str = "fresh"
    report_token: SecretStr | None = None


class Native:
    def __init__(self, result=None):
        self.calls, self.result = [], result

    def claim(self, *args, **options):
        self.calls.append(("claim", args, options))
        return self.result

    def resolve_probe(self, *args):
        self.calls.append(("resolve", args, {}))
        return {"resolved": True}

    def report(self, *args):
        self.calls.append(("report", args, {}))
        return {"reported": True}


class CallbackBody:
    def model_dump(self):
        return {"request_id": "rid-1"}


@pytest.fixture
def callbacks(monkeypatch):
    """Real capability and path checks; the wire models are recorders, so the command's fields can be asserted."""
    async def parsed(_request, model):
        parsed.models.append(model)
        return CallbackBody()

    parsed.models = []
    monkeypatch.setattr(behavior_api, "_body", parsed)
    for name, label in (("HostedClaimRequest", "claim"), ("HostedProbeRequest", "resolve"), ("HostedReport", "report")):
        monkeypatch.setattr(wire, name, lambda label=label, **fields: {label: fields})
    runtime, native = Runtime(), Native(ClaimResult(report_token="report-1"))
    runtime.result = native
    return SimpleNamespace(runtime=runtime, native=native, client=serve(runtime), models=parsed.models)


LAUNCH = PREFIX.format(binding_id="b-1") + "/hosted/launches/launch-1"
PERMIT = {"x-behavior-launch-permit": "a" * 64}
REPORT = {"x-behavior-report-token": "b" * 64}


def test_claim_authorizes_reads_its_permit_and_returns_the_token_only_in_a_header(callbacks):
    response = callbacks.client.post(LAUNCH + "/claim", headers=PERMIT)
    assert response.status_code == 200 and response.json() == {"claim_state": "fresh"}
    assert response.headers["x-behavior-report-token"] == "report-1"
    assert callbacks.models == [wire.HostedClaimBody]
    assert callbacks.runtime.calls == [("executor_callback", ("b-1", "launch-1", W), {"claim": True})]
    (name, args, options), = callbacks.native.calls
    assert (name, args[:2], args[3], options) == ("claim", ("b-1", "launch-1"), W, {})
    fields = args[2]["claim"]
    assert fields["request_id"] == "rid-1" and fields["launch_permit"].get_secret_value() == "a" * 64


@pytest.mark.parametrize("result", [ClaimResult(claim_state="replayed", report_token="report-1"), ClaimResult()])
def test_only_a_fresh_claim_with_a_token_sets_the_header(callbacks, result):
    callbacks.native.result = result
    response = callbacks.client.post(LAUNCH + "/claim", headers=PERMIT)
    assert response.status_code == 200 and "x-behavior-report-token" not in response.headers


@pytest.mark.parametrize("suffix,name,model,reply", [
    ("resolve", "resolve", wire.HostedProbeBody, {"resolved": True}),
    ("report", "report", wire.HostedReportBody, {"reported": True}),
])
def test_probe_callbacks_use_the_report_token_and_their_own_operation(callbacks, suffix, name, model, reply):
    response = callbacks.client.post(LAUNCH + "/probes/probe-1/" + suffix, headers=REPORT)
    assert response.status_code == 200 and response.json() == reply
    assert callbacks.models == [model]
    assert callbacks.runtime.calls == [("executor_callback", ("b-1", "launch-1", W), {"claim": False})]
    (called, args, _), = callbacks.native.calls
    assert called == name and args[:3] == ("b-1", "launch-1", "probe-1") and args[4] is W
    assert args[3][name]["report_token"].get_secret_value() == "b" * 64


@pytest.mark.parametrize("headers", [{}, {"x-behavior-launch-permit": "A" * 64},
                                     {"x-behavior-launch-permit": "a" * 63}, REPORT])
def test_a_missing_or_malformed_permit_is_denied_before_the_body(callbacks, headers):
    response = callbacks.client.post(LAUNCH + "/claim", headers=headers)
    assert (response.status_code, response.json()["error"]["code"]) == (403, "BEHAVIOR_ACCESS_DENIED")
    assert not callbacks.models and not callbacks.native.calls


def test_a_repeated_permit_is_denied(callbacks):
    response = callbacks.client.post(LAUNCH + "/claim", headers=[
        ("authorization", "Bearer stand-in"), ("x-behavior-launch-permit", "a" * 64),
        ("x-behavior-launch-permit", "b" * 64)])
    assert response.status_code == 403 and not callbacks.models


def test_the_executor_is_checked_before_the_capability(callbacks):
    def denied(*_, **__):
        raise Problem(404, "BEHAVIOR_NOT_FOUND", "unknown launch")

    callbacks.runtime.executor_callback = denied
    assert callbacks.client.post(LAUNCH + "/claim").status_code == 404


def test_an_invalid_probe_id_is_a_dependency_answer_and_runs_no_operation(callbacks):
    response = callbacks.client.post(LAUNCH + "/probes/bad.id/report", headers=REPORT)
    assert response.status_code == 503 and not callbacks.native.calls
    assert response.json()["error"]["message"] == "The bounded operation could not be completed."


def test_callback_failures_stay_opaque(callbacks):
    def broken(*_, **__):
        raise RuntimeError("SYNTHETIC private detail")

    callbacks.runtime.executor_callback = broken
    response = callbacks.client.post(LAUNCH + "/claim", headers=PERMIT)
    assert response.status_code == 503 and "SYNTHETIC" not in response.text
    assert response.json()["error"]["message"] == "The bounded operation could not be completed."


def test_a_plain_runtime_never_takes_the_assistant_claim_lane(callbacks, monkeypatch):
    """Native mode without a profile for this binding keeps the ordinary claim path and its Problem body."""
    from shared_context import behavior_assistant, behavior_assistant_api
    monkeypatch.setattr(behavior_assistant, "_read_conflicts", lambda *_a, **_k: pytest.fail("assistant lane"))
    monkeypatch.setattr(behavior_assistant_api, "acquire_http_error", lambda *_a, **_k: pytest.fail("assistant lane"))
    assert callbacks.client.post(LAUNCH + "/claim", headers=PERMIT).status_code == 200


def test_a_profiled_binding_claims_through_the_assistant_conflict_read(callbacks, monkeypatch):
    from shared_context import behavior_assistant
    phases = []

    def conflicts(read, *, phase):
        phases.append(phase)
        return read()

    monkeypatch.setattr(behavior_assistant, "_read_conflicts", conflicts)
    callbacks.runtime.profiles = {"b-1": object()}
    assert callbacks.client.post(LAUNCH + "/claim", headers=PERMIT).status_code == 200
    assert phases == ["hosted_claim_authority"]
    assert callbacks.runtime.calls == [("executor_callback", ("b-1", "launch-1", W), {"claim": True})]
    assert callbacks.native.calls[0][2] == {"assistant_conflicts": True}


def test_a_profiled_binding_outside_native_mode_stays_ordinary(callbacks, monkeypatch):
    from shared_context import behavior_assistant
    monkeypatch.setattr(behavior_assistant, "_read_conflicts", lambda *_a, **_k: pytest.fail("assistant lane"))
    callbacks.runtime.profiles, callbacks.runtime.mode = {"b-1": object()}, "offline"
    assert callbacks.client.post(LAUNCH + "/claim", headers=PERMIT).status_code == 200
    assert callbacks.native.calls[0][2] == {}


def test_only_the_claim_uses_the_assistant_lane(callbacks, monkeypatch):
    from shared_context import behavior_assistant
    monkeypatch.setattr(behavior_assistant, "_read_conflicts", lambda *_a, **_k: pytest.fail("assistant lane"))
    callbacks.runtime.profiles = {"b-1": object()}
    assert callbacks.client.post(LAUNCH + "/probes/probe-1/report", headers=REPORT).status_code == 200


def test_assistant_claim_problems_use_the_assistant_answer(callbacks, monkeypatch):
    from shared_context import behavior_assistant_api
    seen = []

    def translated(error, phase):
        seen.append((error.body["error"]["code"], phase))
        return Problem(409, "BEHAVIOR_CONFLICT", "translated")

    monkeypatch.setattr(behavior_assistant_api, "acquire_http_error", translated)
    callbacks.runtime.profiles = {"b-1": object()}
    response = callbacks.client.post(LAUNCH + "/claim")  # no permit
    assert response.status_code == 409 and seen == [("BEHAVIOR_EXECUTOR_DENIED", "hosted_claim")]
