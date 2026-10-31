"""What the API accepts from a Hosted answer: each refusal, and which one wins when two apply."""

import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from shared_context.assistant_image_wire import ImageEntry
from shared_context.behavior_native_turn_api import _RESPONSE_CONTRACTS, NativeTurnGateway
from shared_context.behavior_native_turn_wire import (
    NativeConversation, NativeConversationRead, NativeDelivered, NativeEnvironmentView, NativeHistory,
    NativeHistoryPage, NativeHitlHistoryRead, NativePublicTurn, NativeRead, NativeReceipt, NativeReceivedItem,
    NativeReceivedPage, NativeReceivedRead, NativeResult, NativeResultPage, NativeResultReference, NativeSessionFilesOmission,
    NativeSessionFollow, NativeSessionPreparation, NativeSessionPrepare, NativeSessionRetarget, NativeTurnStatus,
)
from shared_context.errors import Problem
from shared_context.native_append_wire import NativeAppendRead, NativeAppendReceipt, NativeAppends
from shared_context.native_continuation_wire import NativeContinuationRead, NativeContinuationView
from shared_context.native_files_wire import (
    NativeFilesManifest, NativeFilesProgress, NativeFilesView, NativeSessionFiles,
)
from shared_context.native_hitl_wire import NativeHitlReviewHistory
from shared_context.native_image_wire import NativeImageContent, NativeImageRead
from shared_context.native_progress_wire import NativeTurnStatusRead
from test_native_turn_wire import agent_receive, receipt_for, submit

AGENT, CURRENT, EARLIER, VERSION = "agent-1", "session-current", "session-earlier", "7"
RID, CID, DIGEST = "rid-1", "cid-1", "a" * 64
TEXT = "原始回覆"
RAW = TEXT.encode("utf-8")
TEXT_SHA = hashlib.sha256(RAW).hexdigest()


def client(answer):
    body = answer if isinstance(answer, bytes) else answer.model_dump_json().encode()
    response = SimpleNamespace(body=body, status_code=200, invocation_id="OFFLINE")
    return SimpleNamespace(invoke_native=lambda _command, **_options: response,
                           target=SimpleNamespace(agent_id=AGENT, session_id=CURRENT, agent_version=VERSION))


def refused(command, answer):
    with pytest.raises(Problem) as caught:
        NativeTurnGateway._invoke(client(answer), command)
    error = caught.value.body["error"]
    return caught.value.status, error["code"], error["message"]


def accepted(command, answer):
    return NativeTurnGateway._invoke(client(answer), command)[0]


def reference(turn=1, *, sha=TEXT_SHA, size=len(RAW), parts=1):
    return NativeResultReference(
        turn_number=turn, turn_key=f"turn:{turn}", result_digest=DIGEST, text_sha256=sha, text_bytes=size,
        text_parts=parts, checkpoint_sha256=DIGEST)


def receipt(status="started", *, request=None, session=CURRENT, **fields):
    request = request or submit(RID, CID)
    done = {"result": reference(), "turn_number": 1} if status == "completed" else {}
    return NativeReceipt.model_validate_json(json.dumps({
        **receipt_for(request, status=status).model_dump(mode="json"), "agent_id": AGENT, "hosted_session_id": session,
        "conversation_id": CID, **{name: value.model_dump(mode="json") if hasattr(value, "model_dump") else value
                                   for name, value in {**done, **fields}.items()}}))


def conversation(last_turn=3):
    capture = submit(RID, CID).capture
    return NativeConversation(agent_id=AGENT, hosted_session_id=CURRENT, conversation_id=CID, last_turn=last_turn,
                              last_request_id=RID, capture=capture, capture_digest=capture.sha256)


def turn(number, *, rid=None, cid=CID, result_turn=None):
    return NativePublicTurn(conversation_id=cid, request_id=rid or f"rid-{number}", turn_number=number, user_text="問",
                            result=reference(result_turn or number))


def history(*turns, next_after=None):
    return NativeHistoryPage(conversation=conversation(), turns=turns, next_after_turn=next_after)


def status(text=TEXT, *, size=len(RAW), sha=TEXT_SHA, **fields):
    value = receipt("completed", result=reference(size=size, sha=sha), **fields)
    return NativeTurnStatus(receipt=value, text=text)


def result_page(*, part=0, parts=1, text=TEXT, size=len(RAW), sha=TEXT_SHA, rid=RID):
    return NativeResultPage(conversation_id=CID, request_id=rid, result=reference(size=size, sha=sha, parts=parts),
                            part=part, text=text, next_part=part + 1 if part + 1 < parts else None,
                            complete=parts == 1)


def environment(**fields):
    return NativeEnvironmentView(**{
        "agent_id": AGENT, "request_id": RID, "previous_hosted_session_id": EARLIER, "previous_agent_version": "6",
        "hosted_session_id": CURRENT, "hosted_agent_version": VERSION, "state": "pending", **fields})


def follow_command(**fields):
    return NativeSessionFollow(**{"request_id": RID, "expected_session_id": EARLIER, "expected_etag": "etag",
                                  "stage": "prepare", **fields})


def retarget_command():
    return NativeSessionRetarget(request_id=RID, expected_session_id=EARLIER, expected_etag="etag",
                                 expected_agent_version=VERSION, pending_request_id="rid-p", pending_session_id="sid-p")


def omission_command(stage="confirm"):
    return NativeSessionFilesOmission(request_id="confirm-1", follow=follow_command(), stage=stage,
                                      missing=None if stage == "read" else "source")


FILES = {"agent_id": AGENT, "request_id": RID, "source_session_id": EARLIER, "source_agent_version": "6",
         "target_session_id": CURRENT, "target_agent_version": VERSION}


def manifest(**fields):
    return NativeFilesManifest(**{**FILES, **fields}, root_mode=0o755, entries=())


def files_command(stage="begin", index=0, token="send-1"):
    return NativeSessionFiles(**FILES, stage=stage, index=index, send_token=token if stage == "begin" else None)


def files_view(progress=None, *, send_allowed=False, **fields):
    return NativeFilesView(**{**FILES, **fields}, progress=progress, send_allowed=send_allowed)


def image_content():
    from test_native_image_api import png
    data = png(5)
    sha = hashlib.sha256(data).hexdigest()
    return NativeImageContent(
        agent_id=AGENT, conversation_id=CID, request_id=RID, submit_sha256=DIGEST,
        entry=ImageEntry(version_id=sha[:32], number=1, sha256=sha, byte_length=len(data)),
        png_base64=base64.b64encode(data).decode()), sha[:32]


def review_page(next_after=None, entries=()):
    return NativeHitlReviewHistory(agent_id=AGENT, conversation_id=CID, request_id=RID, entries=entries,
                                   next_after=next_after)


def received_item(rid):
    return NativeReceivedItem(request_id=rid, conversation_id=CID, state_profile="foundry-native-v1", status="accepted",
                              sender_agent_id="sender", message_kind="request")


def received_page(*items, next_after=None):
    return NativeReceivedPage(agent_id=AGENT, items=items, next_after=next_after)


def append_receipt(append_id="append-1"):
    return NativeAppendReceipt(agent_id=AGENT, hosted_session_id=CURRENT, request_id=RID, conversation_id=CID,
                               append_id=append_id, submit_digest=DIGEST, sequence=1, content="補充", status="accepted")


def continuation_view(original=RID):
    return NativeContinuationView(agent_id=AGENT, conversation_id=CID, original_request_id=original,
                                  root_request_id=RID, state="prepared", source_hosted_session_id=EARLIER)


SCHEMA = (502, "NATIVE_STATE_INVALID", "the native response does not match its fixed schema")
SCOPE = (502, "NATIVE_STATE_SCOPE", "the native response belongs to another Agent or request")
HISTORY = (502, "NATIVE_STATE_INVALID", "native history does not match its original page")
INCOMPLETE = (502, "NATIVE_RESULT_INCOMPLETE", "the complete original result length or hash differs")
PART = (502, "NATIVE_RESULT_INCOMPLETE", "the original result part differs")
CAPTURE = (409, "NATIVE_CAPTURE_CHANGED", "the response differs from the original submitted material")
SESSION = (409, "NATIVE_SESSION_CHANGED", "the original native session differs; no replacement was submitted")
ENVIRONMENT = (502, "NATIVE_STATE_SCOPE", "the environment response differs from the approved source and target")
OMISSION = (502, "NATIVE_STATE_SCOPE", "the file omission differs from the original confirmed operation")
READ = NativeRead(request_id=RID)


@pytest.mark.parametrize("body", [
    b"not json", b'{"a":1,"a":2}', b'{"a":NaN}', b'{"agent_id":"only-this"}',
], ids=["not-json", "repeated-key", "nonfinite", "wrong-shape"])
def test_an_unparseable_answer_is_refused_by_its_fixed_schema(body):
    assert refused(READ, body) == SCHEMA


@pytest.mark.parametrize(("command", "answer", "model"), [
    (READ, receipt(), NativeReceipt),
    (SimpleNamespace(), receipt(), NativeReceipt),
    (NativeHistory(conversation_id=CID), history(turn(1)), NativeHistoryPage),
    (NativeResult(conversation_id=CID, request_id=RID), result_page(), NativeResultPage),
    (NativeTurnStatusRead(conversation_id=CID, request_id=RID), status(), NativeTurnStatus),
    (NativeAppendRead(request_id=RID, conversation_id=CID), NativeAppends(
        agent_id=AGENT, hosted_session_id=CURRENT, request_id=RID, conversation_id=CID, submit_digest=DIGEST,
        appends=()), NativeAppends),
    (NativeAppendRead(request_id=RID, conversation_id=CID, append_id="append-1"), append_receipt(), NativeAppendReceipt),
    (NativeContinuationRead(request_id=RID), continuation_view(), NativeContinuationView),
    (NativeConversationRead(conversation_id=CID), conversation(), NativeConversation),
    (NativeReceivedRead(), received_page(received_item("rid-a")), NativeReceivedPage),
    (NativeHitlHistoryRead(conversation_id=CID, request_id=RID), review_page(), NativeHitlReviewHistory),
    (omission_command("check"), environment(), NativeEnvironmentView),
    (follow_command(), environment(), NativeEnvironmentView),
    (retarget_command(), environment(), NativeEnvironmentView),
    (files_command("read", None), files_view(), NativeFilesView),
], ids=lambda value: type(value).__name__ if isinstance(value, type) else "")
def test_a_matching_answer_is_returned_as_its_commands_model(command, answer, model):
    value = accepted(command, answer)
    assert type(value) is model and value == answer


@pytest.mark.parametrize("answer", [
    history(turn(1), turn(1, rid="rid-a")),
    history(turn(2), turn(1)),
    history(turn(1), turn(2, rid="rid-1")),
    history(turn(1, cid="cid-2")),
    history(turn(1, result_turn=2)),
    history(turn(4)),
    history(next_after=1),
    history(turn(1), turn(2), next_after=1),
], ids=["repeated-turn", "unsorted", "repeated-request", "other-conversation", "other-result-turn", "past-last-turn",
        "cursor-without-turns", "cursor-not-last"])
def test_a_history_page_must_match_its_original_page(answer):
    assert refused(NativeHistory(conversation_id=CID, after_turn=0), answer) == HISTORY


def test_a_history_page_cannot_exceed_its_limit_or_repeat_turns_before_its_cursor():
    assert refused(NativeHistory(conversation_id=CID, limit=1), history(turn(1), turn(2))) == HISTORY
    assert refused(NativeHistory(conversation_id=CID, after_turn=1), history(turn(1))) == HISTORY


def test_history_is_scoped_by_its_conversation():
    other = history(turn(1)).model_copy(update={"conversation": conversation().model_copy(update={"agent_id": "agent-2"})})
    assert refused(NativeHistory(conversation_id=CID), other) == SCOPE


def test_history_keeps_a_cursor_on_its_last_turn():
    page = history(turn(1), turn(2), next_after=2)
    assert accepted(NativeHistory(conversation_id=CID), page) == page


def test_status_text_must_be_the_complete_original_result():
    read = NativeTurnStatusRead(conversation_id=CID, request_id=RID)
    assert refused(read, status(size=len(RAW) + 1)) == INCOMPLETE
    assert refused(read, status(sha="b" * 64)) == INCOMPLETE


def test_status_is_scoped_by_its_receipt():
    read = NativeTurnStatusRead(conversation_id=CID, request_id=RID)
    assert refused(read, status(agent_id="agent-2")) == SCOPE


@pytest.mark.parametrize(("command", "answer"), [
    (READ, receipt(agent_id="agent-2")),
    (NativeRead(request_id="rid-2"), receipt()),
    (NativeConversationRead(conversation_id="cid-2"), conversation()),
    (NativeResult(conversation_id="cid-2", request_id=RID), result_page()),
    (NativeResult(conversation_id=CID, request_id=RID), result_page(rid="rid-2")),
], ids=["agent", "request", "conversation-read", "result-conversation", "result-request"])
def test_an_answer_of_another_agent_conversation_or_request_is_refused(command, answer):
    assert refused(command, answer) == SCOPE


def test_commands_that_keep_their_own_request_id_are_not_held_to_the_answers():
    assert accepted(follow_command(request_id="rid-2"), environment(request_id="rid-other")).request_id == "rid-other"
    assert accepted(omission_command("check").model_copy(update={"request_id": "rid-2"}), environment()).request_id == RID


def test_a_continuation_view_keeps_its_original_request_not_its_own():
    read = NativeContinuationRead(request_id=RID)
    assert accepted(read, continuation_view()).original_request_id == RID
    assert refused(read, continuation_view("rid-2")) == (
        502, "NATIVE_STATE_SCOPE", "the continuation view belongs to another original request")


def _unchecked(answer, **changes):
    return json.dumps({**answer.model_dump(mode="json"), **changes}, ensure_ascii=False).encode()


def test_a_submit_answer_with_another_digest_is_a_capture_change():
    request = submit(RID, CID)
    other = submit(RID, CID, text="另一段")
    assert refused(request, receipt("completed", request=other)) == CAPTURE


def test_a_submit_answer_with_another_source_is_a_capture_change():
    request = agent_receive(RID, CID, recipient=AGENT)
    without_source = submit(RID, CID)
    assert refused(request, receipt("completed", request=without_source)) == CAPTURE


@pytest.mark.parametrize("state", ["accepted", "started"])
def test_a_live_answer_of_another_session_is_refused_so_nothing_is_replaced(state):
    assert refused(submit(RID, CID), receipt(state, session=EARLIER)) == SESSION


@pytest.mark.parametrize("state", ["completed", "unknown", "busy_refused"])
def test_a_finished_original_of_another_session_is_returned_as_it_is(state):
    value = accepted(submit(RID, CID), receipt(state, session=EARLIER))
    assert (value.request_id, value.status, value.hosted_session_id) == (RID, state, EARLIER)


def preparation(session=CURRENT):
    return NativeSessionPreparation(agent_id=AGENT, request_id="rid-p", previous_hosted_session_id=None,
                                    hosted_session_id=session, hosted_agent_version=VERSION)


def prepare_command():
    return NativeSessionPrepare(request_id="rid-q", expected_session_id=CURRENT, expected_etag="etag")


def test_a_preparation_belongs_to_this_session_and_may_keep_another_request_id():
    assert accepted(prepare_command(), preparation()).request_id == "rid-p"
    assert refused(prepare_command(), preparation(EARLIER)) == SESSION


def test_only_a_follow_can_answer_without_its_request_id_and_still_be_refused_as_a_transition():
    answer = environment(state="available", request_id=None)
    assert refused(follow_command(), answer) == ENVIRONMENT
    assert refused(retarget_command(), answer) == SCOPE


@pytest.mark.parametrize("command", [follow_command(), retarget_command()], ids=["follow", "retarget"])
@pytest.mark.parametrize("answer", [
    environment(state="available"),
    environment(previous_hosted_session_id="session-other"),
    environment(hosted_session_id="session-other"),
    environment(hosted_agent_version="8"),
], ids=["not-persisted", "other-source", "other-target", "other-version"])
def test_an_environment_answer_must_be_the_approved_transition(command, answer):
    assert refused(command, answer) == ENVIRONMENT


def test_an_environment_answer_may_be_ready():
    assert accepted(follow_command(), environment(state="ready")).state == "ready"


SKIPPED = {"state": "ready", "files_migrated": False, "files_skip_request_id": "confirm-1"}


@pytest.mark.parametrize("stage", ["confirm", "read", "activate"])
def test_an_active_omission_is_accepted_at_each_applying_stage(stage):
    command = omission_command(stage)
    assert accepted(command, environment(**SKIPPED)).files_migrated is False


def test_an_omission_stage_accepts_only_its_own_saved_evidence():
    pending = environment(reason="accepted_or_started", files_confirmation_pending="confirm-1")
    saved = environment(files_confirmation_saved="confirm-1", files_confirmation_reason="source")
    absent = environment(files_confirmation_absent="confirm-1")
    assert accepted(omission_command("confirm"), pending) == pending
    assert accepted(omission_command("read"), saved) == saved
    assert accepted(omission_command("read"), absent) == absent
    assert refused(omission_command("read"), pending) == OMISSION
    assert refused(omission_command("confirm"), saved) == OMISSION
    assert refused(omission_command("activate"), pending) == OMISSION
    assert refused(omission_command("activate"), absent) == OMISSION


@pytest.mark.parametrize("answer", [
    environment(**{**SKIPPED, "files_skip_request_id": "confirm-other"}),
    environment(**SKIPPED, previous_hosted_session_id="session-other"),
    environment(**SKIPPED, hosted_session_id="session-other"),
    environment(**SKIPPED, hosted_agent_version="8"),
    environment(**SKIPPED, request_id="rid-other"),
], ids=["other-confirmation", "other-source", "other-target", "other-version", "other-follow"])
def test_an_omission_answer_must_be_the_original_confirmed_operation(answer):
    assert refused(omission_command("confirm"), answer) == OMISSION


def test_a_check_only_needs_the_original_transition():
    assert refused(omission_command("check"), environment(request_id="rid-other")) == OMISSION
    assert accepted(omission_command("check"), environment()).state == "pending"


SESSION_FIELDS = [name for name in FILES if name not in {"agent_id", "request_id"}]


@pytest.mark.parametrize("field", SESSION_FIELDS)
def test_file_progress_must_name_its_original_operation(field):
    other = "9" if "version" in field else "session-other"
    assert refused(files_command("read", None), _unchecked(files_view(), **{field: other})) == (
        502, "NATIVE_STATE_SCOPE", "the file progress differs from its original operation")


@pytest.mark.parametrize("field", list(FILES))
def test_a_file_manifest_must_name_its_original_operation(field):
    other = {**FILES, field: "9" if "version" in field else "session-other" if "session" in field else "other"}
    answer = files_view(NativeFilesProgress(manifest=manifest(**{field: other[field]})))
    assert refused(files_command("read", None), answer) == (
        502, "NATIVE_STATE_SCOPE", "the manifest differs from its original operation")


def test_only_a_first_send_can_authorize_an_upload():
    answer = files_view(NativeFilesProgress(manifest=manifest()), send_allowed=True)
    assert refused(files_command("confirm"), answer) == (
        502, "NATIVE_STATE_INVALID", "a readback cannot authorize a file upload")


@pytest.mark.parametrize("answer", [
    files_view(send_allowed=True),
    files_view(NativeFilesProgress(manifest=manifest(), next_index=0, sending=None), send_allowed=True),
], ids=["no-progress", "no-claim"])
def test_a_send_claim_must_read_back(answer):
    assert refused(files_command(), answer) == (502, "NATIVE_STATE_INVALID", "the file send claim did not read back")


def test_a_result_page_keeps_its_part_and_complete_hash():
    read = NativeResult(conversation_id=CID, request_id=RID, part=1)
    assert refused(read, result_page(part=0, parts=2)) == PART
    assert accepted(read, result_page(part=1, parts=2)).part == 1
    whole = NativeResult(conversation_id=CID, request_id=RID)
    assert refused(whole, result_page(sha="b" * 64)) == INCOMPLETE
    assert refused(whole, result_page(size=len(RAW) + 1)) == INCOMPLETE


def test_an_image_must_be_the_requested_original_version():
    content, version = image_content()
    read = NativeImageRead(conversation_id=CID, request_id=RID, version_id=version)
    assert accepted(read, content) == content
    assert refused(read.model_copy(update={"version_id": "b" * 32}), content) == (
        502, "IMAGE_DELIVERY_CONFLICT", "the image differs from the requested original version")


def test_a_review_page_must_advance():
    read = NativeHitlHistoryRead(conversation_id=CID, request_id=RID, after="cursor-1")
    assert refused(read, review_page(next_after="cursor-1")) == (
        502, "NATIVE_STATE_INVALID", "the original review page did not advance")
    assert accepted(read, review_page(next_after="cursor-2")).next_after == "cursor-2"


def test_a_received_page_must_stay_within_its_limit_be_unique_and_advance():
    message = (502, "NATIVE_STATE_INVALID", "the original received page did not advance")
    read = NativeReceivedRead(after="cursor-1", limit=1)
    assert refused(read, received_page(received_item("a"), received_item("b"))) == message
    assert refused(NativeReceivedRead(), received_page(received_item("a"), received_item("a"))) == message
    assert refused(read, received_page(next_after="cursor-1")) == message


def test_an_append_answer_must_be_the_original_control():
    read = NativeAppendRead(request_id=RID, conversation_id=CID, append_id="append-2")
    assert refused(read, append_receipt()) == (
        502, "NATIVE_STATE_SCOPE", "the append response differs from the original control")


def delivered_command(digest=DIGEST):
    return NativeDelivered(conversation_id=CID, request_id=RID, result_digest=digest)


def test_delivery_needs_the_original_result_acknowledged():
    message = (409, "NATIVE_DELIVERY_CONFLICT", "the original completed result was not acknowledged")
    assert refused(delivered_command(), receipt("started")) == message
    assert refused(delivered_command(), receipt("completed")) == message
    assert refused(delivered_command("b" * 64), receipt("completed", delivered=True)) == message
    assert accepted(delivered_command(), receipt("completed", delivered=True)).delivered


def test_when_two_rules_fail_the_earlier_one_answers():
    bad_history = history(turn(1, cid="cid-2")).model_copy(
        update={"conversation": conversation().model_copy(update={"agent_id": "agent-2"})})
    assert refused(NativeHistory(conversation_id=CID), bad_history) == HISTORY, "history page before its scope"
    read = NativeTurnStatusRead(conversation_id=CID, request_id=RID)
    assert refused(read, status(sha="b" * 64, agent_id="agent-2")) == INCOMPLETE, "status text before its scope"
    other = submit(RID, CID, text="另一段")
    assert refused(submit(RID, CID), receipt("started", request=other, session=EARLIER)) == CAPTURE, (
        "changed material before the session check")
    assert refused(NativeRead(request_id="rid-2"), receipt(agent_id="agent-2")) == SCOPE
    assert refused(NativeResult(conversation_id=CID, request_id="rid-2", part=1), result_page(part=0, parts=2)) == SCOPE, (
        "scope before the part")
    assert refused(NativeResult(conversation_id=CID, request_id=RID, part=1), result_page(sha="b" * 64)) == PART, (
        "part before the hash")
    assert refused(follow_command(), environment(agent_id="agent-2", state="available")) == SCOPE, (
        "scope before the transition")
    assert refused(files_command("read", None), _unchecked(files_view(), agent_id="agent-2")) == SCOPE, (
        "scope before file progress")
    assert refused(omission_command("confirm"), environment(request_id="rid-other", agent_id="agent-2")) == SCOPE, (
        "scope before the omission")
    assert refused(files_command("confirm"), files_view(
        NativeFilesProgress(manifest=manifest(request_id="other")), send_allowed=True)) == (
            502, "NATIVE_STATE_SCOPE", "the manifest differs from its original operation"), "manifest before the readback"
    assert refused(files_command(), files_view(send_allowed=True, request_id="other")) == SCOPE, (
        "request scope before the send claim")


def test_at_most_one_contract_applies_to_a_command():
    commands = [command for row in _RESPONSE_CONTRACTS for command in row[0]]
    assert len(commands) == len(set(commands))
    assert not [(a, b) for a in commands for b in commands if a is not b and issubclass(a, b)]
