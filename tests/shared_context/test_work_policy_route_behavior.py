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
from shared_context.errors import Problem


WP = "/work-policy/v1"
PRINCIPAL = SimpleNamespace(oid="human-oid", kind="human", actor_id="human")
JSON_HEADERS = {"Authorization": "Bearer stand-in", "Content-Type": "application/json"}
AUTH = {"Authorization": "Bearer stand-in"}
SHA = "a" * 64


class Control:
    """Records each call; a raised Problem or a return value is chosen per test."""

    def __init__(self):
        self.calls = []
        self.outcome = {"ok": True}
        self.policy = SimpleNamespace(template=SimpleNamespace(team_id="team-1"))
        self.models = SimpleNamespace(public=lambda: {"models": ["m"]})
        self.turns = []
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

    def conversation(self, principal, conversation_id):
        self._answer("conversation", conversation_id)
        return {"turns": [dict(turn) for turn in self.turns]}

    def read_delegation(self, principal, conversation_id, turn_number, authority):
        self._answer("read_delegation", conversation_id, turn_number)
        return self.delegation

    def read_reply(self, principal, conversation_id, turn_number):
        self._answer("read_reply", conversation_id, turn_number)
        return SimpleNamespace(model_dump_json=lambda: '{"reply":"saved"}')

    def cancellation_receipt(self, principal, binding_id, generation):
        return self._answer("cancellation_receipt", binding_id, generation)

    def revoke(self, principal, decision_id):
        return self._answer("revoke", decision_id)


@pytest.fixture
def control():
    return Control()


@pytest.fixture
def composition(control):
    native = SimpleNamespace()
    dep = SimpleNamespace(native_launches=native, workers=SimpleNamespace(inspect=lambda job_id: job_id),
                          delivery=SimpleNamespace(contract=SimpleNamespace(review_interval_seconds=10)))
    store = SimpleNamespace(consistent_read=lambda call, max_keys, row_scoped: call())
    return SimpleNamespace(
        verifier=lambda request: PRINCIPAL, agent_id="agent-a", assistant=SimpleNamespace(authority="authority"),
        runtime=SimpleNamespace(work_policy=control, mode="hosted", store=store, dep=dep),
        installation=lambda: None,
    )


def client_for(composition, *, live=None):
    app = FastAPI()
    if live is not None:
        app.state.instruction_live_replies = live
    api.install_work_policy_routes(app, composition)
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


RETIRED = [
    WP + "/preflight", WP + "/submit", WP + "/submit-reconcile", WP + "/routines/daily/trigger",
    WP + "/routines/daily/trigger-reconcile", WP + "/conversations/c-1/environment",
    WP + "/conversations/c-1/environment-reconcile", WP + "/bindings/b-1/cancel", WP + "/conversations/c-1/reset",
]


@pytest.mark.parametrize("path", RETIRED)
def test_a_retired_job_entry_answers_410_without_reading_the_body_or_calling_the_control(composition, control, path):
    with client_for(composition) as client:
        response = client.post(path, content=b"{not json", headers=JSON_HEADERS)
    assert response.status_code == 410
    assert response.json() == {"error": {
        "code": "JOB_LANE_RETIRED", "message": "The legacy job lane is retired; this request was not processed.",
        "reconcile_original": False, "automatic_retry": False}}
    assert control.calls == []


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


def test_environment_preview_reads_without_refreshing(client, control, monkeypatch):
    monkeypatch.setattr("shared_context.behavior_conversation_transfer.preview",
                        lambda c, authority, p, conversation_id: {"preview": conversation_id, "authority": authority})
    assert client.get(WP + "/conversations/c-1/environment", headers=AUTH).json() == {
        "preview": "c-1", "authority": "authority"}
    assert ("refresh",) not in control.calls


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


def test_cancellation_receipt_digests_only_a_successful_read(client, control):
    response = client.get(WP + "/bindings/b-1/cancel-receipt/3", headers=AUTH)
    assert response.headers["x-content-sha256"] == hashlib.sha256(b'{"ok":true}').hexdigest()
    assert control.calls == [("cancellation_receipt", "b-1", 3)]
    control.outcome = Problem(409, "CANCELLATION_RECEIPT_UNKNOWN", "internal")
    refused = client.get(WP + "/bindings/b-1/cancel-receipt/3", headers=AUTH)
    assert refused.status_code == 409 and "x-content-sha256" not in refused.headers


def test_revoke_and_budget_refresh_their_control(client, control, monkeypatch):
    monkeypatch.setattr("shared_context.behavior_work_budget.set_budget_enforcement",
                        lambda actual, principal, command: {"enforced": command.enforced})
    assert client.post("/operator/v1/standing-work-policy/revoke", json={"decision_id": "d-1"},
                       headers=AUTH).status_code == 200
    budget = client.post("/operator/v1/standing-work-policy/budget-enforcement", json={
        "request_id": "r-2", "policy_sha256": SHA, "enforced": False}, headers=AUTH)
    assert budget.json() == {"enforced": False}
    assert control.calls == [("refresh",), ("revoke", "d-1"), ("refresh",)]


def test_revoke_keeps_internal_problems_unfiltered(client, control):
    control.outcome = Problem(409, "WORK_SUBMIT_UNKNOWN", "internal")
    assert error_of(client, "POST", "/operator/v1/standing-work-policy/revoke", json={"decision_id": "d-1"})[1] == (
        "WORK_SUBMIT_UNKNOWN")


def test_a_missing_policy_is_installed_once_by_routes_that_may_install(composition, control, client):
    def install():
        composition.runtime.work_policy = control
    composition.runtime.work_policy = None
    composition.installation = install
    assert client.post("/operator/v1/standing-work-policy/revoke", json={"decision_id": "d-1"},
                       headers=AUTH).status_code == 200
    assert control.calls == [("revoke", "d-1")]


def test_native_mode_activates_the_target_on_each_control_read(composition, client, control, monkeypatch):
    seen = []
    composition.runtime.mode = "native"
    monkeypatch.setattr("shared_context.behavior_work_target.activate_target", seen.append)
    assert client.post("/operator/v1/standing-work-policy/revoke", json={"decision_id": "d-1"},
                       headers=AUTH).status_code == 200
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
