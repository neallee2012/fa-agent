"""HTTP behaviour of the work-policy routes against a recording control; no store, model or network.

Every expectation is literal so the same file holds for any internal arrangement of the routes.
Without error handlers a refused Problem reaches the client, which pins its code and message.
"""

import hashlib
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from shared_context import behavior_work_policy_api as api
from shared_context.behavior_work_policy import WorkSubmitObservation
from shared_context.errors import Problem


WP = "/work-policy/v1"
PRINCIPAL = SimpleNamespace(oid="human-oid", kind="human", actor_id="human")
JSON_HEADERS = {"Authorization": "Bearer stand-in", "Content-Type": "application/json"}
AUTH = {"Authorization": "Bearer stand-in"}
SHA = "a" * 64


class Receipt(dict):
    def __init__(self, result):
        super().__init__(result)
        self.payload = {"result": result}


class Control:
    """Records each call; a raised Problem or a return value is chosen per test."""

    def __init__(self):
        self.calls = []
        self.outcome = {"ok": True}
        self.records = {"b-1": SimpleNamespace(profile=SimpleNamespace(instruction="instruction"))}
        self.policy = SimpleNamespace(template=SimpleNamespace(team_id="team-1"))
        self.models = SimpleNamespace(public=lambda: {"models": ["m"]})
        self.turns = []
        self.status = SimpleNamespace(state="running", result_forwarded=False)
        self.cancel_result = {}
        self.delegation = {"delegated": True}

    def _answer(self, name, *args):
        self.calls.append((name, *args))
        if isinstance(self.outcome, Problem):
            raise self.outcome
        return self.outcome

    def refresh(self, *args):
        self.calls.append(("refresh", *args))

    def installed(self):
        self.calls.append(("installed",))
        return SimpleNamespace(active=True, read_enabled=True)

    def _human(self, principal, *args):
        self.calls.append(("human", *args))

    def accounting(self, principal):
        return self._answer("accounting")

    def preflight(self, principal, query):
        return self._answer("preflight", query.request_id)

    def submit(self, principal, command):
        self._answer("submit", command.request_id)
        return SimpleNamespace(binding_id="b-1"), self.status

    def submission_result(self, binding, status):
        return {"binding": binding.binding_id, "state": status.state}

    def submit_reconcile(self, principal, command):
        return self._answer("submit_reconcile", command.request_id)

    def conversation(self, principal, conversation_id):
        self._answer("conversation", conversation_id)
        return {"turns": [dict(turn) for turn in self.turns]}

    def read_delegation(self, principal, conversation_id, turn_number, authority):
        self._answer("read_delegation", conversation_id, turn_number)
        return self.delegation

    def read_reply(self, principal, conversation_id, turn_number):
        self._answer("read_reply", conversation_id, turn_number)
        return SimpleNamespace(model_dump_json=lambda: '{"reply":"saved"}')

    def cancel(self, principal, binding_id):
        self._answer("cancel", binding_id)
        return Receipt(self.cancel_result)

    def cancellation_receipt(self, principal, binding_id, generation):
        return self._answer("cancellation_receipt", binding_id, generation)

    def reset_conversation(self, principal, conversation_id, command):
        return self._answer("reset", conversation_id, command.request_id)

    def revoke(self, principal, decision_id):
        return self._answer("revoke", decision_id)


@pytest.fixture
def control():
    return Control()


@pytest.fixture
def composition(control):
    native = SimpleNamespace(
        pending_version_observation=lambda job: job == "version",
        pending_lost_observation=lambda job: job == "lost",
        pending_completion_observation=lambda job: job == "completion",
    )
    dep = SimpleNamespace(native_launches=native, workers=SimpleNamespace(inspect=lambda job_id: job_id),
                          delivery=SimpleNamespace(contract=SimpleNamespace(review_interval_seconds=10)))
    store = SimpleNamespace(consistent_read=lambda call, max_keys, row_scoped: call())
    return SimpleNamespace(
        verifier=lambda request: PRINCIPAL, agent_id="agent-a", assistant=SimpleNamespace(authority="authority"),
        runtime=SimpleNamespace(work_policy=control, mode="hosted", store=store, dep=dep),
        installation=lambda: None,
    )


def client_for(composition, *, start_work=None, wake_work=None, live=None):
    app = FastAPI()
    if live is not None:
        app.state.instruction_live_replies = live
    api.install_work_policy_routes(app, composition, start_work=start_work, wake_work=wake_work)
    return TestClient(app)


@pytest.fixture
def client(composition):
    return client_for(composition)


def error_of(client, method, path, **request):
    with pytest.raises(Problem) as refused:
        client.request(method, path, headers=request.pop("headers", AUTH), **request)
    return refused.value.status, refused.value.body["error"]["code"], str(refused.value)


READS_WITH_ONE_MESSAGE = [
    (WP + "/agents", "agent directory accepts no body or query"),
    (WP + "/bindings/b-1/agent-messages", "message projection accepts no body or query"),
    (WP + "/agent-template/system-prompt", "system prompt reads accept no body or query"),
    (WP + "/agent-template/options", "template options accept no body or query"),
    (WP + "/agent-template/selection", "profile selection reads accept no body or query"),
    (WP + "/attachment-sources", "attachment source catalog accepts no body or query"),
    (WP + "/conversations/c-1/attachment-sources", "conversation source catalog accepts no body or query"),
    (WP + "/conversations/c-1/user-text-file-capability", "file capability reads accept no body or query"),
]
READS_WITH_TWO_MESSAGES = [
    (WP + "/agent-template", "template export does not accept query parameters",
     "template export does not accept a body"),
    (WP + "/accounting", "accounting reads do not accept query parameters",
     "accounting reads do not accept a body"),
    (WP + "/conversations/c-1/environment", "environment preview does not accept query parameters",
     "environment preview does not accept a body"),
    (WP + "/conversations/c-1/turns/1/context", "turn metadata reads do not accept query parameters",
     "turn metadata reads do not accept a body"),
    (WP + "/conversations/c-1/turns/1/delegation", "delegation reads accept no query parameters",
     "delegation reads accept no body"),
    (WP + "/conversations/c-1/turns/1/files/a.txt", "original file reads accept no query parameters",
     "original file reads accept no body"),
    (WP + "/bindings/b-1/cancel-receipt/1", "cancellation receipt reads do not accept query parameters",
     "cancellation receipt reads do not accept a body"),
]


@pytest.mark.parametrize("path,message", READS_WITH_ONE_MESSAGE)
def test_reads_refuse_query_and_body_with_one_message(client, path, message):
    expected = (422, "BEHAVIOR_INVALID_REQUEST", message)
    assert error_of(client, "GET", path + "?x=1") == expected
    assert error_of(client, "GET", path, content=b"x") == expected


@pytest.mark.parametrize("path,query_message,body_message", READS_WITH_TWO_MESSAGES)
def test_reads_refuse_query_and_body_with_their_own_messages(client, path, query_message, body_message):
    assert error_of(client, "GET", path + "?x=1") == (422, "BEHAVIOR_INVALID_REQUEST", query_message)
    assert error_of(client, "GET", path, content=b"x") == (422, "BEHAVIOR_INVALID_REQUEST", body_message)


@pytest.mark.parametrize("method,path,body,message", [
    ("GET", WP + "/agent-template", None, "a configured standing scope is required"),
    ("GET", WP + "/agent-template/options", None, "a configured standing scope is required"),
    ("GET", WP + "/agent-template/system-prompt", None, "a configured standing scope is required"),
    ("GET", WP + "/accounting", None, "a configured standing accounting scope is required"),
    ("GET", WP + "/conversations/c-1/turns/1/context", None, "a configured standing scope is required"),
    ("GET", WP + "/conversations/c-1/turns/1/delegation", None, "a configured standing scope is required"),
    ("GET", WP + "/bindings/b-1/cancel-receipt/1", None, "a configured standing scope is required"),
    ("POST", WP + "/submit-reconcile", '{"request_id":"r-1","content":"x"}', "a configured standing scope is required"),
    ("POST", WP + "/routines/daily/trigger-reconcile", '{"scheduled_for":"2026-10-30T00:00:00Z"}',
     "a configured standing scope is required"),
])
def test_reads_without_a_policy_refuse_instead_of_installing_one(composition, client, method, path, body, message):
    installed = []
    composition.installation = lambda: installed.append(True)
    composition.runtime.work_policy = None
    assert error_of(client, method, path, content=body, headers=JSON_HEADERS) == (
        503, "WORK_POLICY_DISABLED", message)
    assert installed == []


def test_template_reads_need_the_configured_authority(composition, client, control):
    composition.assistant = None
    for path in ("/agent-template", "/agent-template/options"):
        response = client.get(WP + path, headers=AUTH)
        assert response.status_code == 503
        assert response.json()["error"]["message"] == "The configured assistant template source is unavailable."
        assert response.headers["cache-control"] == "no-store"
    assert control.calls == [("human",), ("human",)]


def test_template_export_is_a_digested_attachment(client, control, monkeypatch):
    seen = []
    def export_template(*args):
        seen.append(args)
        return {"template": 1}
    monkeypatch.setattr("shared_context.behavior_agent_template.export_template", export_template)
    response = client.get(WP + "/agent-template", headers=AUTH)
    assert response.json() == {"template": 1}
    assert seen == [(control, "authority", PRINCIPAL)]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == 'attachment; filename="fa-agent-template.json"'
    assert response.headers["x-content-sha256"] == hashlib.sha256(b'{"template":1}').hexdigest()


def test_refused_template_export_stays_uncacheable_without_attachment_headers(client, monkeypatch):
    def refuse(*args):
        raise Problem(503, "SYSTEM_PROMPT_UNRECONSTRUCTABLE", "internal")
    monkeypatch.setattr("shared_context.behavior_agent_template.export_template", refuse)
    response = client.get(WP + "/agent-template", headers=AUTH)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert "content-disposition" not in response.headers and "x-content-sha256" not in response.headers


def test_system_prompt_view_follows_the_native_turn_switch(client, monkeypatch):
    monkeypatch.delenv("FA_NATIVE_TURNS", raising=False)
    response = client.get(WP + "/agent-template/system-prompt", headers=AUTH)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "NATIVE_TURNS_DISABLED"
    assert response.headers["cache-control"] == "no-store"


def test_system_prompt_view_reads_the_current_prompt_uncached(client, control, monkeypatch):
    monkeypatch.setattr("shared_context.behavior_native_turn_api.configured_native_turns", lambda: True)
    monkeypatch.setattr("shared_context.behavior_native_capture.system_prompt_inputs",
                        lambda *args: (args,))
    monkeypatch.setattr("shared_context.behavior_system_prompt_view.current_view",
                        lambda inputs: {"control": inputs[0] is control, "authority": inputs[1]})
    response = client.get(WP + "/agent-template/system-prompt", headers=AUTH)
    assert response.json() == {"control": True, "authority": "authority"}
    assert response.headers["cache-control"] == "no-store"


def test_template_options_merge_responses_options(client, monkeypatch):
    monkeypatch.setattr("shared_context.behavior_agent_template.template_options", lambda *args: SimpleNamespace(
        model_copy=lambda update: update))
    monkeypatch.setattr("shared_context.behavior_native_responses_api.template_responses_options",
                        lambda composition, principal: ["responses"])
    response = client.get(WP + "/agent-template/options", headers=AUTH)
    assert response.json() == {"responses": ["responses"]}
    assert response.headers["cache-control"] == "no-store"


def test_revoked_profile_selection_is_refused(client, control):
    control.installed = lambda: SimpleNamespace(active=False, read_enabled=True)
    response = client.get(WP + "/agent-template/selection", headers=AUTH)
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "The current standing work/read authority is revoked."
    assert response.headers["cache-control"] == "no-store"


def test_unselected_profile_applies_only_to_new_conversations(client, control, monkeypatch):
    control.store = "store"
    monkeypatch.setattr("shared_context.behavior_profile_selection.current_selection", lambda store, policy: None)
    response = client.get(WP + "/agent-template/selection", headers=AUTH)
    assert response.json() == {"selection_id": None, "applies_to": "new-conversations-only",
                               "permissions_changed": False}
    assert response.headers["cache-control"] == "no-store"
    assert ("refresh",) not in control.calls


def test_accounting_reads_the_policy_without_refreshing(client, control):
    assert client.get(WP + "/accounting", headers=AUTH).json() == {"ok": True}
    assert control.calls == [("accounting",)]


def test_accounting_keeps_internal_problems_unfiltered(client, control):
    control.outcome = Problem(503, "WORK_SUBMIT_UNKNOWN", "internal")
    assert error_of(client, "GET", WP + "/accounting") == (503, "WORK_SUBMIT_UNKNOWN", "internal")


def test_models_need_an_installed_policy_and_the_human(client, control):
    assert client.get(WP + "/models", headers=AUTH).json() == {"models": ["m"]}
    assert control.calls == [("installed",), ("human",)]


def test_agents_and_messages_read_their_projection(client, control, monkeypatch):
    monkeypatch.setattr("shared_context.behavior_agent_message_api.public_agents", lambda c, p: {"agents": []})
    monkeypatch.setattr("shared_context.behavior_agent_message_api.public_messages",
                        lambda c, p, binding: {"binding": binding})
    assert client.get(WP + "/agents", headers=AUTH).json() == {"agents": []}
    assert control.calls == [("refresh",)]
    assert client.get(WP + "/bindings/b-1/agent-messages", headers=AUTH).json() == {"binding": "b-1"}
    assert control.calls == [("refresh",), ("refresh", "b-1")]


@pytest.mark.parametrize("path,kwargs,refreshed", [
    (WP + "/attachment-sources", {}, True),
    (WP + "/conversations/c-1/attachment-sources", {"conversation_id": "c-1"}, False),
])
def test_attachment_catalogs_are_uncached(client, control, monkeypatch, path, kwargs, refreshed):
    seen = []
    def catalog(actual, principal, **given):
        seen.append(given)
        return {"sources": []}
    monkeypatch.setattr("shared_context.behavior_attachment_catalog.read_attachment_catalog", catalog)
    response = client.get(path, headers=AUTH)
    assert response.json() == {"sources": []} and seen == [kwargs]
    assert response.headers["cache-control"] == "no-store"
    assert (("refresh",) in control.calls) is refreshed


def test_file_capability_is_uncached(client, monkeypatch):
    monkeypatch.setattr("shared_context.behavior_text_files.read_user_text_file_capability",
                        lambda c, p, conversation_id: {"conversation": conversation_id})
    response = client.get(WP + "/conversations/c-1/user-text-file-capability", headers=AUTH)
    assert response.json() == {"conversation": "c-1"}
    assert response.headers["cache-control"] == "no-store"


PREFLIGHT = {"read_id": "read-1", "request_id": "r-1", "submit_body_sha256": SHA}


def test_preflight_shows_only_role_prompt_refusals(client, control):
    assert client.post(WP + "/preflight", json=PREFLIGHT, headers=AUTH).json() == {"ok": True}
    control.outcome = Problem(409, "AGENT_ROLE_PROMPT_MISSING", "internal")
    refused = client.post(WP + "/preflight", json=PREFLIGHT, headers=AUTH)
    assert refused.status_code == 409
    assert refused.json() == {"error": {
        "code": "AGENT_ROLE_PROMPT_MISSING",
        "message": "This agent's role profile has no saved role prompt yet. No work was accepted.",
        "reconcile_original": False, "automatic_retry": False}}
    control.outcome = Problem(409, "WORK_SUBMIT_UNKNOWN", "internal")
    assert error_of(client, "POST", WP + "/preflight", json=PREFLIGHT, headers=AUTH) == (
        409, "WORK_SUBMIT_UNKNOWN", "internal")


FINISHED = SimpleNamespace(state="succeeded", result_forwarded=True)
SAVED_NOT_FORWARDED = SimpleNamespace(state="failed", result_forwarded=False)
RUNNING = SimpleNamespace(state="running", result_forwarded=False)


@pytest.mark.parametrize("status,instruction,started", [
    (RUNNING, "instruction", ["b-1"]),
    (SAVED_NOT_FORWARDED, "instruction", ["b-1"]),
    (FINISHED, "instruction", []),
    (RUNNING, None, []),
])
def test_submit_starts_only_unfinished_instruction_work(composition, control, status, instruction, started):
    calls = []
    control.status = status
    control.records["b-1"].profile.instruction = instruction
    with client_for(composition, start_work=calls.append) as client:
        response = client.post(WP + "/submit", json={"request_id": "r-1", "content": "hello"}, headers=AUTH)
    assert response.json() == {"binding": "b-1", "state": status.state}
    assert calls == started
    assert control.calls == [("human", "submit"), ("submit", "r-1")]


def test_submit_without_a_starter_only_admits(client, control):
    response = client.post(WP + "/submit", json={"request_id": "r-1", "content": "hello"}, headers=AUTH)
    assert response.status_code == 200


def test_unknown_submit_reconciles_the_same_request_and_starts_nothing(composition, control):
    calls = []
    control.outcome = Problem(503, "WORK_SUBMIT_UNKNOWN", "internal")
    with client_for(composition, start_work=calls.append) as client:
        response = client.post(WP + "/submit", json={"request_id": "r-1", "content": "hello"}, headers=AUTH)
    assert response.status_code == 503
    assert response.json()["error"]["reconcile_original"] is True
    assert response.json()["error"]["automatic_retry"] is False
    assert calls == []


@pytest.mark.parametrize("status,started", [(RUNNING, ["b-1"]), (FINISHED, [])])
def test_routine_trigger_starts_only_unfinished_work(composition, control, monkeypatch, status, started):
    calls = []
    monkeypatch.setattr("shared_context.behavior_routine.trigger",
                        lambda c, p, routine_id, command: (SimpleNamespace(binding_id="b-1"), status))
    with client_for(composition, start_work=calls.append) as client:
        response = client.post(WP + "/routines/daily/trigger", json={"scheduled_for": "2026-10-30T00:00:00Z"},
                               headers=AUTH)
    assert response.json() == {"binding": "b-1", "state": status.state}
    assert calls == started


OBSERVATION = WorkSubmitObservation(schema_version="fa-work-submit-observation-v1", request_id="r-1",
                                    submit_sha256=SHA, state="not_admitted", retry_same_request=True)


def test_submit_reconcile_returns_the_observation_or_the_original_result(client, control):
    body = {"request_id": "r-1", "content": "hello"}
    control.outcome = OBSERVATION
    assert client.post(WP + "/submit-reconcile", json=body, headers=AUTH).json()["state"] == "not_admitted"
    control.outcome = (SimpleNamespace(binding_id="b-1"), RUNNING)
    assert client.post(WP + "/submit-reconcile", json=body, headers=AUTH).json() == {
        "binding": "b-1", "state": "running"}
    assert ("refresh",) not in control.calls


@pytest.mark.parametrize("original,expected", [
    (OBSERVATION, "not_admitted"), ((SimpleNamespace(binding_id="b-1"), RUNNING), "running"),
])
def test_routine_reconcile_returns_the_observation_or_the_original_result(client, monkeypatch, original, expected):
    monkeypatch.setattr("shared_context.behavior_routine.reconcile", lambda c, p, routine_id, command: original)
    response = client.post(WP + "/routines/daily/trigger-reconcile",
                           json={"scheduled_for": "2026-10-30T00:00:00Z"}, headers=AUTH)
    assert response.json()["state"] == expected


def test_conversation_adds_live_text_only_to_unfinished_turns(composition, control):
    control.turns = [
        {"job_id": "a", "state": "running"}, {"job_id": "b", "state": "running"},
        {"job_id": "c", "state": "succeeded"},
    ]
    live = SimpleNamespace(read=lambda job_id: "partial" if job_id in {"a", "c"} else None)
    response = client_for(composition, live=live).get(WP + "/conversations/c-1", headers=AUTH)
    assert response.json() == {"turns": [
        {"job_id": "a", "state": "running", "stream": "partial"}, {"job_id": "b", "state": "running"},
        {"job_id": "c", "state": "succeeded"},
    ]}


def test_conversation_read_restarts_version_and_lost_observers_every_time(composition, control):
    started, woken = [], []
    control.turns = [
        {"job_id": "version", "state": "starting", "binding_id": "b-1"},
        {"job_id": "lost", "state": "failed", "binding_id": "b-2"},
        {"job_id": "lost", "state": "running", "binding_id": "b-3"},
        {"job_id": "none", "state": "starting", "binding_id": "b-4"},
    ]
    client = client_for(composition, start_work=started.append, wake_work=woken.append)
    for _ in range(2):
        assert client.get(WP + "/conversations/c-1", headers=AUTH).status_code == 200
    assert started == ["b-1", "b-2", "b-1", "b-2"]
    assert woken == []


def test_conversation_read_wakes_completion_observers_twice_spaced_by_the_review_interval(
        composition, control, monkeypatch):
    clock, woken = [100.0], []
    monkeypatch.setattr(api, "monotonic", lambda: clock[0])
    control.turns = [{"job_id": "completion", "state": "capacity_uncertain", "binding_id": "b-1"}]
    client = client_for(composition, start_work=lambda binding_id: None, wake_work=woken.append)
    for now, expected in ((100.0, 1), (105.0, 1), (110.0, 2), (500.0, 2)):
        clock[0] = now
        assert client.get(WP + "/conversations/c-1", headers=AUTH).status_code == 200
        assert len(woken) == expected


def test_refused_conversation_read_wakes_nothing(composition, control):
    started = []
    control.outcome = Problem(404, "CONVERSATION_NOT_FOUND", "internal")
    control.turns = [{"job_id": "version", "state": "starting", "binding_id": "b-1"}]
    response = client_for(composition, start_work=started.append).get(WP + "/conversations/c-1", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "The original conversation was not found."
    assert started == []


def test_a_retried_conversation_read_only_acts_on_its_last_attempt(composition, control):
    started, attempts = [], []
    control.turns = [{"job_id": "version", "state": "starting", "binding_id": "b-1"}]
    def conflict_once(call, max_keys, row_scoped):
        value = call()
        attempts.append(True)
        if len(attempts) == 1:
            raise Problem(412, "BEHAVIOR_CONFLICT", "changed")
        return value
    composition.runtime.store.consistent_read = conflict_once
    response = client_for(composition, start_work=started.append).get(WP + "/conversations/c-1", headers=AUTH)
    assert response.status_code == 200
    assert started == ["b-1"]


def test_conversation_without_a_native_launcher_observes_nothing(composition, control):
    started = []
    composition.runtime.dep.native_launches = None
    control.turns = [{"job_id": "version", "state": "starting", "binding_id": "b-1"}]
    assert client_for(composition, start_work=started.append).get(
        WP + "/conversations/c-1", headers=AUTH).status_code == 200
    assert started == []


def test_environment_preview_and_prepare_share_one_reconcile_switch(client, control, monkeypatch):
    seen = []
    monkeypatch.setattr("shared_context.behavior_conversation_transfer.preview",
                        lambda c, authority, p, conversation_id: {"preview": conversation_id, "authority": authority})
    def prepare(c, authority, p, conversation_id, command, reconcile):
        seen.append((authority, conversation_id, command.request_id, reconcile))
        return {"ok": True}
    monkeypatch.setattr("shared_context.behavior_conversation_transfer.prepare", prepare)
    assert client.get(WP + "/conversations/c-1/environment", headers=AUTH).json() == {
        "preview": "c-1", "authority": "authority"}
    assert ("refresh",) not in control.calls
    body = {"request_id": "r-1", "failed_job_id": "j-1", "expected_agent_version": "2"}
    assert client.post(WP + "/conversations/c-1/environment", json=body, headers=AUTH).status_code == 200
    assert client.post(WP + "/conversations/c-1/environment-reconcile", json=body, headers=AUTH).status_code == 200
    assert seen == [("authority", "c-1", "r-1", False), ("authority", "c-1", "r-1", True)]
    assert control.calls.count(("refresh",)) == 2


def test_transfer_refusal_keeps_only_its_bounded_detail(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise Problem(409, "CONVERSATION_TRANSFER_CHANGED", "internal", transfer_refusal={
            "stage": "before-reservation", "reserved": False})
    monkeypatch.setattr("shared_context.behavior_conversation_transfer.prepare", refuse)
    body = {"request_id": "r-1", "failed_job_id": "j-1", "expected_agent_version": "2"}
    response = client.post(WP + "/conversations/c-1/environment", json=body, headers=AUTH)
    assert response.json()["error"]["transfer_refusal"] == {"stage": "before-reservation", "reserved": False}


def test_turn_context_digests_only_a_successful_read(client, control, monkeypatch):
    monkeypatch.setattr("shared_context.behavior_turn_context.read_saved_turn_context",
                        lambda c, p, conversation_id, turn_number: {"turn": turn_number})
    response = client.get(WP + "/conversations/c-1/turns/2/context", headers=AUTH)
    assert response.json() == {"turn": 2}
    assert response.headers["x-content-sha256"] == hashlib.sha256(b'{"turn":2}').hexdigest()
    assert control.calls == [("human",)]
    def refuse(*args):
        raise Problem(409, "TURN_CONTEXT_NOT_READY", "internal")
    monkeypatch.setattr("shared_context.behavior_turn_context.read_saved_turn_context", refuse)
    refused = client.get(WP + "/conversations/c-1/turns/2/context", headers=AUTH)
    assert refused.status_code == 409 and "x-content-sha256" not in refused.headers


def test_delegation_reads_digest_empty_and_refused_answers(client, control):
    response = client.get(WP + "/conversations/c-1/turns/2/delegation", headers=AUTH)
    assert response.json() == {"delegated": True}
    assert response.headers["x-content-sha256"] == hashlib.sha256(b'{"delegated":true}').hexdigest()
    control.delegation = None
    empty = client.get(WP + "/conversations/c-1/turns/2/delegation", headers=AUTH)
    assert empty.status_code == 204 and empty.headers["cache-control"] == "no-store"
    control.outcome = Problem(503, "DELEGATION_UNAVAILABLE", "internal")
    refused = client.get(WP + "/conversations/c-1/turns/2/delegation", headers=AUTH)
    assert refused.status_code == 503
    assert refused.json()["error"]["code"] == "DELEGATION_UNAVAILABLE"
    control.outcome = Problem(503, "NOT_A_SAFE_CODE", "internal")
    assert error_of(client, "GET", WP + "/conversations/c-1/turns/2/delegation")[1] == "NOT_A_SAFE_CODE"


def test_reply_is_the_exact_saved_bytes_with_their_digest(client, control):
    response = client.get(WP + "/conversations/c-1/turns/2/reply", headers=AUTH)
    assert response.content == b'{"reply":"saved"}'
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-sha256"] == hashlib.sha256(b'{"reply":"saved"}').hexdigest()
    control.outcome = Problem(409, "INSTRUCTION_REPLY_NOT_READY", "internal")
    refused = client.get(WP + "/conversations/c-1/turns/2/reply", headers=AUTH)
    assert refused.json()["error"]["message"] == "No complete original SDK reply has been saved yet."


def test_original_file_downloads_as_inert_text(client, monkeypatch):
    monkeypatch.setattr("shared_context.behavior_text_files.read_original_file",
                        lambda c, p, conversation_id, turn_number, filename: SimpleNamespace(text="內容", name=filename))
    response = client.get(WP + "/conversations/c-1/turns/2/files/a.txt", headers=AUTH)
    assert response.content == "內容".encode()
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == 'attachment; filename="a.txt"'
    assert response.headers["x-content-sha256"] == hashlib.sha256("內容".encode()).hexdigest()
    def refuse(*args):
        raise Problem(404, "USER_FILE_NOT_FOUND", "internal")
    monkeypatch.setattr("shared_context.behavior_text_files.read_original_file", refuse)
    refused = client.get(WP + "/conversations/c-1/turns/2/files/a.txt", headers=AUTH)
    assert refused.json()["error"]["code"] == "USER_FILE_NOT_FOUND"


@pytest.mark.parametrize("result,instruction,woken", [
    ({"cancel_accepted": True}, "instruction", ["b-1"]),
    ({"cancel_accepted": True, "executor_stopped": True}, "instruction", []),
    ({"cancel_accepted": False}, "instruction", []),
    ({"cancel_accepted": True}, None, []),
])
def test_cancel_wakes_only_a_still_running_instruction(composition, control, result, instruction, woken):
    calls = []
    control.cancel_result = result
    control.records["b-1"].profile.instruction = instruction
    with client_for(composition, start_work=calls.append) as client:
        client.post(WP + "/bindings/b-1/cancel", json={"request_id": "r-1"}, headers=AUTH)
    assert calls == woken


def test_cancel_prefers_the_explicit_wake_hook(composition, control):
    started, woken = [], []
    control.cancel_result = {"cancel_accepted": True}
    with client_for(composition, start_work=started.append, wake_work=woken.append) as client:
        client.post(WP + "/bindings/b-1/cancel", json={"request_id": "r-1"}, headers=AUTH)
    assert woken == ["b-1"] and started == []


def test_cancellation_receipt_digests_only_a_successful_read(client, control):
    response = client.get(WP + "/bindings/b-1/cancel-receipt/3", headers=AUTH)
    assert response.headers["x-content-sha256"] == hashlib.sha256(b'{"ok":true}').hexdigest()
    assert control.calls == [("cancellation_receipt", "b-1", 3)]
    control.outcome = Problem(409, "CANCELLATION_RECEIPT_UNKNOWN", "internal")
    refused = client.get(WP + "/bindings/b-1/cancel-receipt/3", headers=AUTH)
    assert refused.status_code == 409 and "x-content-sha256" not in refused.headers


def test_reset_revoke_and_budget_refresh_their_control(client, control, monkeypatch):
    monkeypatch.setattr("shared_context.behavior_work_budget.set_budget_enforcement",
                        lambda actual, principal, command: {"enforced": command.enforced})
    assert client.post(WP + "/conversations/c-1/reset", json={"request_id": "r-1"}, headers=AUTH).status_code == 200
    assert client.post("/operator/v1/standing-work-policy/revoke", json={"decision_id": "d-1"},
                       headers=AUTH).status_code == 200
    budget = client.post("/operator/v1/standing-work-policy/budget-enforcement", json={
        "request_id": "r-2", "policy_sha256": SHA, "enforced": False}, headers=AUTH)
    assert budget.json() == {"enforced": False}
    assert control.calls == [("refresh",), ("reset", "c-1", "r-1"), ("refresh",), ("revoke", "d-1"), ("refresh",)]


def test_revoke_keeps_internal_problems_unfiltered(client, control):
    control.outcome = Problem(409, "WORK_SUBMIT_UNKNOWN", "internal")
    assert error_of(client, "POST", "/operator/v1/standing-work-policy/revoke", json={"decision_id": "d-1"})[1] == (
        "WORK_SUBMIT_UNKNOWN")


def test_a_missing_policy_is_installed_once_by_routes_that_may_install(composition, control, client):
    def install():
        composition.runtime.work_policy = control
    composition.runtime.work_policy = None
    composition.installation = install
    assert client.post(WP + "/conversations/c-1/reset", json={"request_id": "r-1"}, headers=AUTH).status_code == 200
    assert control.calls == [("reset", "c-1", "r-1")]


def test_native_mode_activates_the_target_on_each_control_read(composition, client, control, monkeypatch):
    seen = []
    composition.runtime.mode = "native"
    monkeypatch.setattr("shared_context.behavior_work_target.activate_target", seen.append)
    assert client.post(WP + "/conversations/c-1/reset", json={"request_id": "r-1"}, headers=AUTH).status_code == 200
    assert seen == [composition]


def usage_app(composition, monkeypatch, publisher):
    monkeypatch.setattr("shared_context.realtime_push.realtime_publisher", lambda composition: publisher)
    monkeypatch.setattr("shared_context.behavior_instruction_cost.agent_usage_scope", lambda c, p: None)
    monkeypatch.setattr("shared_context.behavior_instruction_cost.record_agent_turn_usage",
                        lambda c, rid, usage, source: SimpleNamespace(payload={"result": {"source": source}}))
    return client_for(composition)


@pytest.mark.parametrize("source,notices", [("model", 1), ("memory", 0)])
def test_turn_usage_hints_the_final_state_only_for_a_model_turn(composition, monkeypatch, source, notices):
    sent = []
    publisher = SimpleNamespace(notify=lambda team, event, **fields: sent.append((team, event, fields)))
    client = usage_app(composition, monkeypatch, publisher)
    body = {"request_id": "r-1", "usage": None, **({} if source == "model" else {"source": source})}
    response = client.post(WP + "/turn-usage", json=body, headers=AUTH)
    assert response.json() == {"request_id": "r-1", "source": source}
    assert sent == [("team-1", "turn.finished", {"agent_id": None, "request_id": "r-1"})][:notices]


def test_turn_usage_without_a_publisher_only_records(composition, monkeypatch):
    client = usage_app(composition, monkeypatch, None)
    response = client.post(WP + "/turn-usage", json={"request_id": "r-1", "usage": None}, headers=AUTH)
    assert response.status_code == 200


def test_connector_routes_read_and_save_the_agent_choice(composition, control, monkeypatch):
    monkeypatch.setenv("FA_TEAM_CONNECTORS_ENABLED", "1")
    monkeypatch.setattr("shared_context.team_connector_narrowing.connector_view",
                        lambda c, p: {"connectors": []})
    monkeypatch.setattr("shared_context.team_connector_narrowing.save_narrowing",
                        lambda c, p, command: {"saved": command.request_id})
    client = client_for(composition)
    assert client.get(WP + "/agent-connectors", headers=AUTH).json() == {"connectors": []}
    assert ("refresh",) not in control.calls
    assert error_of(client, "GET", WP + "/agent-connectors?x=1")[2] == "connector choice reads accept no body or query"
    saved = client.post(WP + "/agent-connectors", json={
        "request_id": "r-1", "schema_version": "fa-agent-connectors-command-v1", "expected_revision": 0,
        "disabled_connector_ids": []}, headers=AUTH)
    assert saved.json() == {"saved": "r-1"}
    assert control.calls == [("refresh",)]
