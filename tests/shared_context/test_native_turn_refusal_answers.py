"""The exact JSON a native-turn route answers for a refusal, and the selector refusals of its read routes.

The shared handler (``_install_error_handlers``) replaces a route's ``Problem`` code and message, so a selector
refusal is visible as that handler's generic answer; FastAPI's own validation would answer another message."""

import logging

import pytest

from shared_context.behavior_native_sessions import _RetiredSession
from shared_context.behavior_native_turn_api import (
    _ATTACHED_SOURCE_ERRORS, _BeforeAdmission, _OmissionBeforeDispatch, PREFIX,
)
from shared_context.behavior_timing import timing_id
from shared_context.behavior_work_policy_api import _SAFE_WORK_ERRORS
from shared_context.errors import Problem
from test_native_turn_api import native_api, submit
from test_native_turns import local_env
from test_conversation_transfer_startup import synthetic_standing_v1
from test_m3_auth import jwk_client, signing_key
from test_instruction_flow import instruction_models

GATEWAY = "shared_context.behavior_native_turn_api.NativeTurnGateway."
SHARED_HANDLER = "The bounded operation could not be completed."
NOT_COMPLETED = "The original native command could not be completed; no automatic retry."
READ_AGAIN = "This read did not complete; reading again is safe."
MOVING = "The entire Agent is changing environments; no new work was accepted."
FOLLOWING = ("The Agent is moving to its current version; this message was not accepted yet. "
             "Send the same request ID and body again shortly.")
SAFE_CODE, SAFE_MESSAGE = next(iter(_SAFE_WORK_ERRORS.items()))
SOURCE_CODE = "SOURCE_SCOPE_DENIED"


def answer(message, *, retry=False, reconcile=False, **extra):
    return {"message": message, "automatic_retry": retry, "reconcile_original": reconcile, **extra}


def refused(case, monkeypatch, method, error):
    def refuse(*_args, **_kwargs):
        raise error
    monkeypatch.setattr(GATEWAY + method, refuse)
    if method == "read":
        return case.client.get(PREFIX + "/requests/native-rid", headers=case.headers)
    return submit(case)


def before(status, code, **options):
    return _BeforeAdmission(Problem(status, code, "private detail"), "native-rid", **options)


@pytest.mark.parametrize(("method", "error", "expected"), [
    ("read", Problem(404, "NATIVE_RID_MISSING", "private"), answer(NOT_COMPLETED)),
    ("read", Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "private"), answer(READ_AGAIN, retry=True, reconcile=True)),
    ("read", Problem(500, "NATIVE_RESULT_INCOMPLETE", "private"), answer(READ_AGAIN, retry=True)),
    ("read", Problem(503, "NATIVE_TURNS_DISABLED", "private"), answer(_SAFE_WORK_ERRORS["NATIVE_TURNS_DISABLED"])),
    ("read", _RetiredSession(503, "NATIVE_TURN_UNKNOWN", "private"), answer(NOT_COMPLETED, reconcile=True)),
    ("read", Problem(412, "BEHAVIOR_CONFLICT", "private"), answer(READ_AGAIN, retry=True)),
    ("read", Problem(503, "BEHAVIOR_READ_FAILED", "private"), answer(READ_AGAIN, retry=True)),
    ("read", Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "private"), answer(READ_AGAIN, retry=True, reconcile=True)),
    ("read", Problem(409, SAFE_CODE, "private"), answer(SAFE_MESSAGE)),
    ("read", Problem(503, SAFE_CODE, "private"), answer(SAFE_MESSAGE, retry=True)),
    ("read", Problem(503, "AGENT_VERSION_FOLLOWING", "private"), answer(MOVING, retry=True)),
    ("submit", Problem(503, "AGENT_VERSION_FOLLOWING", "private"), answer(MOVING)),
    ("submit", Problem(404, SOURCE_CODE, "private"), answer(_ATTACHED_SOURCE_ERRORS[SOURCE_CODE])),
    ("submit", Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "private"), answer(NOT_COMPLETED, reconcile=True)),
    ("submit", Problem(503, "FOUNDRY_INVOCATION_UNKNOWN", "private"), answer(NOT_COMPLETED, reconcile=True)),
    ("submit", Problem(503, "FOUNDRY_RESPONSE_INVALID", "private"), answer(NOT_COMPLETED)),
    ("submit", Problem(503, "FOUNDRY_NATIVE_BINDING_MISMATCH", "private"), answer(NOT_COMPLETED)),
    ("submit", Problem(503, "FOUNDRY_CREDENTIAL_DISABLED", "private"), answer(NOT_COMPLETED)),
    ("submit", Problem(503, "FOUNDRY_CREDENTIAL_UNAVAILABLE", "private"), answer(NOT_COMPLETED)),
    ("submit", Problem(503, "NATIVE_TURN_UNKNOWN", "private"), answer(NOT_COMPLETED, reconcile=True)),
    ("submit", Problem(503, "SDK_STATE_UPLOAD_UNKNOWN", "private"), answer(NOT_COMPLETED, reconcile=True)),
    ("submit", Problem(503, "NATIVE_BOOTSTRAP_UNKNOWN", "private"), answer(NOT_COMPLETED, reconcile=True)),
    ("submit", Problem(503, "NATIVE_RESPONSE_UNCONFIRMED", "private"),
     answer(_SAFE_WORK_ERRORS["NATIVE_RESPONSE_UNCONFIRMED"], reconcile=True)),
    ("submit", Problem(503, "NATIVE_APPEND_UNKNOWN", "private"), answer(NOT_COMPLETED, reconcile=True)),
    ("submit", before(409, "NATIVE_SESSION_CHANGED"),
     answer(NOT_COMPLETED, not_dispatched=True, request_id="native-rid")),
    ("submit", before(503, "AGENT_VERSION_FOLLOWING"),
     answer(MOVING, not_dispatched=True, request_id="native-rid")),
    ("submit", before(503, "NATIVE_TURN_UNKNOWN"),
     answer(NOT_COMPLETED, not_dispatched=True, request_id="native-rid")),
    ("submit", before(503, "AGENT_VERSION_FOLLOWING", server_following=True),
     answer(FOLLOWING, not_dispatched=True, request_id="native-rid", server_following=True)),
    ("submit", before(409, "UNLISTED_CODE"), answer(NOT_COMPLETED, not_dispatched=True, request_id="native-rid")),
    ("submit", _OmissionBeforeDispatch("native-rid", "NATIVE_SESSION_CHANGED", "private"),
     answer(NOT_COMPLETED, confirmation_not_dispatched=True, request_id="native-rid")),
    ("submit", _OmissionBeforeDispatch("native-rid", "NATIVE_SESSION_CHANGED", "private", files_check={"category": "x"}),
     answer(NOT_COMPLETED, confirmation_not_dispatched=True, request_id="native-rid", files_check={"category": "x"})),
    ("submit", Problem(409, "NATIVE_SESSION_CHANGED", "private", files_check={"category": "x"}),
     answer(NOT_COMPLETED, files_check={"category": "x"})),
])
def test_a_refusal_answers_exactly_this_json(native_api, monkeypatch, method, error, expected):
    response = refused(native_api, monkeypatch, method, error)
    assert response.status_code == error.status and response.headers["cache-control"] == "no-store"
    assert response.json() == {"error": {"code": error.body["error"]["code"], **expected}}


@pytest.mark.parametrize(("method", "error"), [
    ("read", Problem(409, "BEHAVIOR_CONFLICT", "unlisted while it is not a lost race")),
    ("read", Problem(403, "UNLISTED_CODE", "unlisted")),
    ("read", Problem(412, "UNLISTED_CODE", "only BEHAVIOR_CONFLICT is a lost race")),
    ("read", Problem(409, SOURCE_CODE, "an attached-source code is only answered on a write")),
    ("submit", Problem(412, "BEHAVIOR_CONFLICT", "only a read may invite another read")),
    ("submit", Problem(503, "BEHAVIOR_READ_FAILED", "only a read may invite another read")),
    ("submit", Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "only a read may invite another read")),
    ("submit", Problem(403, "UNLISTED_CODE", "unlisted")),
])
def test_a_refusal_outside_the_lists_is_left_to_the_shared_error_handler(native_api, monkeypatch, method, error):
    response = refused(native_api, monkeypatch, method, error)
    assert response.status_code == error.status
    assert response.json()["error"]["message"] == SHARED_HANDLER


def test_a_refused_read_logs_only_a_hashed_rid_or_none(native_api, monkeypatch, caplog):
    def refuse(*_args, **_kwargs):
        raise Problem(404, "NATIVE_RID_MISSING", "private")
    monkeypatch.setattr(GATEWAY + "read", refuse)
    caplog.set_level(logging.INFO, logger="shared_context.behavior_native_turn_api")
    native_api.client.get(PREFIX + "/requests/native-rid", headers=native_api.headers)
    native_api.client.get(PREFIX + "/conversations/native-cid", headers=native_api.headers)
    logged = [(r.levelno, r.getMessage()) for r in caplog.records if "NATIVE_TURN_REFUSED" in r.getMessage()]
    # Not there yet is an answer while the workbench polls for a peer's RID: INFO, below what the API prints.
    assert logged == [(logging.INFO, "NATIVE_TURN_REFUSED code=NATIVE_RID_MISSING rid=" + timing_id("native-rid")),
                      (logging.INFO, "NATIVE_TURN_REFUSED code=NATIVE_RID_MISSING rid=-")]


@pytest.mark.parametrize(("method", "code"), [("read", "BEHAVIOR_READ_FAILED"), ("submit", "NATIVE_RID_MISSING")])
def test_other_refusals_still_log_a_warning(native_api, monkeypatch, caplog, method, code):
    refused(native_api, monkeypatch, method, Problem(503 if method == "read" else 404, code, "private"))
    assert [(r.levelno, r.getMessage().split(" rid=")[0]) for r in caplog.records
            if "NATIVE_TURN_REFUSED" in r.getMessage()] == [(logging.WARNING, "NATIVE_TURN_REFUSED code=" + code)]


@pytest.mark.parametrize("path", [
    "/conversations/a%20b/attachment-sources",
    "/conversations/a%20b/requests/rid/status",
    "/conversations/cid/requests/a%20b/status",
    "/conversations/a%20b/requests/rid/hitl-reviews",
    "/requests/a%20b",
    "/requests/a%20b/continuation",
    "/requests/a%20b/system-prompt",
    "/conversations/a%20b",
    "/received?after=" + "x" * 8192,
    "/conversations/a%20b/history",
    "/conversations/a%20b/requests/rid/result",
    "/conversations/a%20b/requests/rid/memory-candidate",
    "/conversations/a%20b/requests/rid/images",
    "/conversations/a%20b/requests/rid/images/version",
])
def test_a_read_route_refuses_its_own_invalid_selectors(native_api, path):
    response = native_api.client.get(PREFIX + path, headers=native_api.headers)
    assert response.status_code == 422, response.text
    assert response.json() == {"error": {"code": "BEHAVIOR_INVALID_REQUEST", "message": SHARED_HANDLER,
                                         "automatic_retry": False, "reconcile_original": False}}
    assert not native_api.remote.calls


@pytest.mark.parametrize("path", ["/requests/rid?extra=1", "/received?after=a&after=b", "/received?unknown=1"])
def test_a_read_route_refuses_selectors_it_does_not_take(native_api, path):
    response = native_api.client.get(PREFIX + path, headers=native_api.headers)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["message"] == SHARED_HANDLER
    assert not native_api.remote.calls


def test_a_read_route_refuses_a_body(native_api):
    response = native_api.client.request("GET", PREFIX + "/requests/rid", headers=native_api.headers, content=b"{}")
    assert response.status_code == 422
    assert response.json()["error"]["message"] == SHARED_HANDLER
