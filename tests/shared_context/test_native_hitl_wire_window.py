"""The three saved waits keep their bytes and their refusals: hashes and messages here are literals from the code
before ``window`` had one implementation, so sharing it can never change a stored record or a refusal text."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib

from pydantic import ValidationError
import pytest

from shared_context.native_hitl_wire import (
    ConnectorConsent, ConnectorConsentCreate, ConnectorConsentPending, NativeHitlAction, NativeHitlCall,
    NativeHitlPending, NativeHitlPendingCreate, NativeHitlQuestion, NativeHitlQuestionCreate,
    NativeHitlQuestionPending, NativeHitlReview,
)

START = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
END = START + timedelta(seconds=600)
TAIPEI = timezone(timedelta(hours=8))
SCOPE = {"tenant_id": "tenant", "team_id": "team", "agent_oid": "agent-oid"}
WAIT_ID = "a" * 64


def _call():
    return NativeHitlCall(agent_id="agent", conversation_id="conversation", request_id="request",
                          sdk_session_id="session", tool_call_id="tool-call")


def _pending(**times):
    action = NativeHitlAction(call=_call(), category="shell", tool="bash", target="ls", arguments={"path": "."})
    review = NativeHitlReview(rules_revision=0, source="default", outcome="ask")
    return NativeHitlPending(**SCOPE, pending_id=WAIT_ID, **times, command=NativeHitlPendingCreate(
        request_id="create", action=action, action_digest=action.sha256, review=review))


def _question(**times):
    asked = NativeHitlQuestion(call=_call(), question="Continue?", choices=("yes", "no"))
    return NativeHitlQuestionPending(**SCOPE, question_id=WAIT_ID, **times, command=NativeHitlQuestionCreate(
        request_id="create", question=asked, question_digest=asked.sha256))


def _consent(**times):
    needed = ConnectorConsent(call=_call(), need="reconnect", connector_id=None, toolbox_name="toolbox",
                              server_label="server", step="tools/list")
    return ConnectorConsentPending(**SCOPE, consent_id=WAIT_ID, **times, command=ConnectorConsentCreate(
        request_id="create", consent=needed, consent_digest=needed.sha256))


WAITS = {
    "pending": (_pending, "pending_id", "pending timestamps must be explicit UTC",
                "the pending call needs a positive wait window"),
    "question": (_question, "question_id", "question timestamps must be explicit UTC",
                 "the question needs a positive wait window"),
    "consent": (_consent, "consent_id", "consent timestamps must be explicit UTC",
                "the consent needs a positive wait window"),
}
SAVED = {
    "pending": ("269dde64e3ceb80247357cbd27d1e6435c8cc90cc347df011257d283e2029360",
                "01a5810350b85f11d34016cb903ce2ff3c31ba98c7786411e9016b4b47869525"),
    "question": ("ce2c85cc593379956e0ba914df3f92de7a6d3deb33b5cf72d46d035e955b16a4",
                 "c9be7e6505187a651ebd2a7887e4e2de752538a2a851769cdcbc753b1fe95334"),
    "consent": ("5a5ec7081befa7dc9c04356fd8a42d37dd808082c8e4ea160fce9f8f83a12cb6",
                "7fa0a0d1313813d7ab61aadc298a40eb3e3d8ce9c7860fcd837676d6da24bc12"),
}


def _refusal(build, **times):
    with pytest.raises(ValidationError) as caught:
        build(**times)
    return [(error["type"], error["loc"], error["msg"]) for error in caught.value.errors(include_url=False)]


@pytest.mark.parametrize("wait", WAITS)
def test_a_valid_wait_keeps_its_key_order_and_exact_bytes(wait):
    build, own_id, _, _ = WAITS[wait]
    value = build(created_at=START, expires_at=END)
    raw = value.model_dump_json().encode("utf-8")
    assert list(value.model_dump()) == [
        "kind", "tenant_id", "team_id", own_id, "agent_oid", "command", "created_at", "expires_at"]
    assert (hashlib.sha256(raw).hexdigest(), value.sha256) == SAVED[wait]
    assert type(value).model_validate_json(raw) == value


@pytest.mark.parametrize("wait", WAITS)
@pytest.mark.parametrize("times", [
    {"created_at": START.replace(tzinfo=None), "expires_at": END},
    {"created_at": START, "expires_at": END.replace(tzinfo=None)},
    {"created_at": START.astimezone(TAIPEI), "expires_at": END},
    {"created_at": START, "expires_at": END.astimezone(TAIPEI)},
    # UTC is checked first: a window that is also empty still reports the timestamp.
    {"created_at": END.astimezone(TAIPEI), "expires_at": START},
], ids=["naive-start", "naive-end", "offset-start", "offset-end", "offset-and-empty"])
def test_a_wait_needs_explicit_utc_times(wait, times):
    build, _, message, _ = WAITS[wait]
    assert _refusal(build, **times) == [("value_error", (), "Value error, " + message)]


@pytest.mark.parametrize("wait", WAITS)
@pytest.mark.parametrize("expires_at", [START, START - timedelta(seconds=1)], ids=["equal", "reversed"])
def test_a_wait_needs_a_positive_window(wait, expires_at):
    build, _, _, message = WAITS[wait]
    assert _refusal(build, created_at=START, expires_at=expires_at) == [("value_error", (), "Value error, " + message)]
