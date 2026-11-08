"""Which API refusals each assistant request keeps typed, and what each refusal carries; no network or SDK."""
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from shared_context import behavior_assistant_hosted as hosted
from shared_context import behavior_assistant_wire as aw
from shared_context import behavior_wire as wire
from shared_context.assistant_image_wire import IMAGE_DELIVERY_ERRORS
from shared_context.behavior_agent_message_wire import AGENT_MESSAGE_ERRORS
from shared_context.behavior_assistant_reply import ASSISTANT_REPLY_READ_ERRORS
from shared_context.behavior_attachments import ATTACHMENT_ERRORS
from shared_context.behavior_capabilities import CAPABILITY_ERRORS
from shared_context.behavior_delegation_wire import DELEGATION_SIZE_REFUSAL
from shared_context.behavior_model_capacity import MODEL_CAPACITY_ERRORS
from shared_context.behavior_sdk_state import SDK_STATE_ERRORS
from shared_context.behavior_skill_copy import SOP_COPY_ERRORS
from shared_context.behavior_text_files import USER_FILE_READ_ERRORS
from shared_context.behavior_work_memory import WORK_MEMORY_ERRORS
from shared_context.errors import Problem
from shared_context.memory_candidate_wire import MEMORY_CANDIDATE_ERRORS

CODE = "code"
STATUS = "status"
DIAGNOSTICS = "diagnostics"

# Written from the request contract, in the order an action's first matching refusal wins.
EXPECTED = [
    ({"copy-sop-draft"}, SOP_COPY_ERRORS,
     "original copy was refused or unresolved; never change the slot", DIAGNOSTICS),
    ({"save-skill-draft"}, hosted.SKILL_DRAFT_ERRORS,
     "original skill draft was refused or unresolved; never change the slot", DIAGNOSTICS),
    ({"sdk-state", "restore", "save-text-reply"}, SDK_STATE_ERRORS,
     "original checkpoint request was refused; no retry", CODE),
    ({"attached-text", "restore"}, ATTACHMENT_ERRORS,
     "original attachment access was refused; no replacement", STATUS),
    ({"skill-catalog-presentation"}, hosted.SKILL_ERRORS | hosted.CATALOG_BUDGET_ERRORS | MODEL_CAPACITY_ERRORS,
     "original catalog budget was refused; no unbounded fallback", STATUS),
    ({"skill-catalog", "read-skill", "read-skill-resource", "restore"}, hosted.SKILL_ERRORS,
     "original approved skill source was refused; no cached fallback", DIAGNOSTICS),
    ({"read-user-turn", "read-user-file", "read-assistant-reply", "restore"},
     USER_FILE_READ_ERRORS | ASSISTANT_REPLY_READ_ERRORS,
     "original user turn was refused; no substitute or retry", DIAGNOSTICS),
    ({"acquire", "restore"}, MODEL_CAPACITY_ERRORS,
     "original model capacity was refused; no retry or substitution", DIAGNOSTICS),
    ({"work-memory-catalog", "read-work-memory", "restore"}, WORK_MEMORY_ERRORS,
     "original reviewed work knowledge was refused; no substitute", DIAGNOSTICS),
    ({"capability-catalog", "capability-call"}, CAPABILITY_ERRORS,
     "the captured capability was refused or unresolved; no resend", STATUS),
    ({"stage-memory-candidate", "memory-candidate-reconcile"}, MEMORY_CANDIDATE_ERRORS,
     "the original candidate was refused or unconfirmed", STATUS),
    ({"deliver-image"}, IMAGE_DELIVERY_ERRORS,
     "the screenshot delivery was refused or unconfirmed", STATUS),
    ({"reviewed-note-catalog", "read-reviewed-note", "restore"}, hosted.REVIEWED_NOTE_ERRORS,
     "the selected reviewed note is unavailable; no substitute", STATUS),
]


def client(status, code=None, *, headers=(), binding=None):
    def answer(request):
        sent = dict(headers)
        if code is not None:
            sent["x-behavior-assistant-error-code"] = code
        return httpx.Response(status, headers=sent, content=b"{}")
    assignment = SimpleNamespace(binding_id="binding-1", session_id="session-1", sdk_start_binding=binding)
    return hosted.AssistantClient("https://api.example.test", assignment, SecretStr("t" * 64),
                                  token_provider=lambda: "OFFLINE", transport=httpx.MockTransport(answer))


def refused(action, status, code=None, **options):
    with pytest.raises(Problem) as error:
        client(status, code, **options)._post(action, aw.Empty(), aw.Empty)
    return error.value.status, error.value.body["error"]


def expected(action, status, code):
    for actions, codes, message, carries in EXPECTED:
        if action in actions and code in codes:
            body = {"code": code, "message": message}
            if carries != CODE:
                body.update(upstream_phase="response_status", upstream_status=status)
            if carries == DIAGNOSTICS:
                body["upstream_error_code"] = code
            return status, body
    return 503, {"code": "ASSISTANT_API_UNKNOWN", "message": "original typed request is unresolved; no retry",
                 "upstream_phase": "response_status", "upstream_status": status, "upstream_error_code": code}


CASES = sorted({(action, code) for actions, codes, _, _ in EXPECTED for action in actions for code in codes})


@pytest.mark.parametrize(("action", "code"), CASES)
def test_each_action_keeps_only_its_own_typed_refusal(action, code):
    assert refused(action, 409, code) == expected(action, 409, code)


@pytest.mark.parametrize("action", sorted({action for actions, *_ in EXPECTED for action in actions}))
def test_a_code_another_action_owns_stays_unknown(action):
    foreign = next(code for _, codes, _, _ in EXPECTED for code in sorted(codes)
                   if expected(action, 409, code)[0] == 503)
    assert refused(action, 409, foreign) == expected(action, 409, foreign)


def test_an_encoded_success_is_unknown_without_a_code():
    assert refused("acquire", 200, headers={"content-encoding": "x-unsupported"}) == (503, {
        "code": "ASSISTANT_API_UNKNOWN", "message": "original typed request is unresolved; no retry",
        "upstream_phase": "response_status", "upstream_status": 200})


def test_work_memory_refusals_widen_only_under_the_v2_start_binding():
    code = sorted(wire.SDK_MEMORY_REFUSAL_CODES - WORK_MEMORY_ERRORS)[0]
    v2 = SimpleNamespace(profile="fa-sdk-start-gate-v2")
    assert refused("read-work-memory", 409, code, binding=v2) == (409, {
        "code": code, "message": "original reviewed work knowledge was refused; no substitute",
        "upstream_phase": "response_status", "upstream_status": 409, "upstream_error_code": code})
    assert refused("read-work-memory", 409, code, binding=SimpleNamespace(profile="other"))[1]["code"] == (
        "ASSISTANT_API_UNKNOWN")


@pytest.mark.parametrize("action", ["agent-message-send", "agent-message-read"])
def test_peer_messages_keep_their_own_code_only_on_an_error_status(action, caplog):
    code = sorted(AGENT_MESSAGE_ERRORS)[0]
    message = "the original peer operation was refused or unconfirmed; do not resend"
    assert refused(action, 409, code) == (409, {
        "code": code, "message": message, "upstream_phase": "response_status", "upstream_status": 409})
    assert refused(action, 409, "OTHER") == (409, {
        "code": "AGENT_MESSAGE_UNKNOWN", "message": message, "upstream_phase": "response_status",
        "upstream_status": 409})
    assert refused(action, 200, code, headers={"content-encoding": "x-unsupported"}) == (503, {
        "code": "AGENT_MESSAGE_UNKNOWN", "message": message, "upstream_phase": "response_status",
        "upstream_status": 200})
    assert f'AGENT_MESSAGE_REQUEST_FAILED {{"action":"{action}","code":"{code}","upstream_status":409}}' in caplog.text


def test_delegation_size_refusal_needs_its_status_code_and_stage():
    stage = {"x-behavior-delegation-stage": "before-reservation"}
    assert refused("delegation-prepare", 413, DELEGATION_SIZE_REFUSAL, headers=stage) == (413, {
        "code": DELEGATION_SIZE_REFUSAL, "message": "Delegation material size exceeds 192 KiB; no intent was reserved."})
    for status, headers, action in ((409, stage, "delegation-prepare"), (413, {}, "delegation-prepare"),
                                    (413, stage, "delegation-record")):
        assert refused(action, status, DELEGATION_SIZE_REFUSAL, headers=headers)[1]["code"] == "ASSISTANT_API_UNKNOWN"


def test_an_oversized_answer_is_unknown_at_the_body_phase():
    with pytest.raises(Problem) as error:
        client(200)._post("acquire", aw.Empty(), aw.Empty, max_bytes=1)
    assert error.value.body["error"] == {"code": "ASSISTANT_API_UNKNOWN",
        "message": "typed authority response unavailable; no retry", "upstream_phase": "response_body",
        "upstream_status": 200, "exception_type": "ValueError"}
    assert client(200)._post("acquire", aw.Empty(), aw.Empty, max_bytes=len(b"{}")) == aw.Empty()


def test_an_sdk_state_envelope_over_its_bound_is_refused_before_sending():
    sent = []
    assignment = SimpleNamespace(binding_id="binding-1", session_id="session-1", sdk_start_binding=None)
    api = hosted.AssistantClient("https://api.example.test", assignment, SecretStr("t" * 64),
                                 token_provider=lambda: "OFFLINE",
                                 transport=httpx.MockTransport(lambda request: sent.append(request)
                                                               or httpx.Response(200, content=b"{}")))
    envelope = lambda size: SimpleNamespace(model_dump_json=lambda: "x" * size)
    with pytest.raises(Problem) as error:
        api._post("sdk-state", envelope(hosted.SDK_STATE_HTTP_BYTES + 1), aw.Empty)
    assert error.value.status == 413 and error.value.body["error"] == {
        "code": "SDK_STATE_INCOMPLETE", "message": "complete SDK state HTTP envelope exceeds its bound"}
    assert sent == []
    api._post("sdk-state", envelope(hosted.SDK_STATE_HTTP_BYTES), aw.Empty)
    api._post("restore", envelope(hosted.SDK_STATE_HTTP_BYTES + 1), aw.Empty)
    assert len(sent) == 2
