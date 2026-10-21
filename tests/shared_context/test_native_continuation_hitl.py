"""A proven unstarted operation gets a fresh review; its old once grant stays with the old call."""

import asyncio
from dataclasses import replace
from contextlib import asynccontextmanager
from types import SimpleNamespace

from shared_context.behavior_native_turn_wire import (
    NativeContinuationSubmit, NativeReceipt, continuation_request_id,
)
from shared_context.m2_copilot import ToolOutput
from shared_context.native_continuation_wire import NativeContinuation
from shared_context.native_effects import NativeEffects
from shared_context.native_hitl_runtime import NativeAutoReview, NativeHitlRuntime
from shared_context.native_operations import NativeOperationGuard
from test_native_hitl_h2 import (
    EffectRuntime, EffectStore, hitl, human_decide, jwk_client, pending_view, permit, setup_gate, signing_key,
)


def test_unstarted_continuation_requires_new_review_and_owner_once_without_transferring_the_old_grant(hitl, tmp_path):
    first = setup_gate(hitl, tmp_path)
    original = first.context.receipt
    calls = []

    async def scenario():
        guard = NativeOperationGuard(first.context.effects, {"run_command": "a" * 64})
        await guard.start()
        await guard.prepare(first.packet)
        permission = asyncio.create_task(permit(first))
        old_pending = await pending_view(first)
        human_decide(first, old_pending)
        assert await permission == (True, None)
        old_view = await first.client.read(old_pending.approval.pending_id)
        assert old_view.authorization is None
        prior = await first.context.effects.operation_records()
        assert len(prior) == 1 and prior[0].dispatch_started is False

        first.runtime.receipt = NativeReceipt.model_validate_json(
            original.model_copy(update={"status": "unknown", "failure_code": "NATIVE_TURN_UNKNOWN"}).model_dump_json())
        rid = continuation_request_id(original.agent_id, original.conversation_id, original.request_id, 1)
        ref = NativeContinuation(
            continuation_of=original.request_id, root_request_id=original.request_id, attempt=1,
            source_submit_digest=original.submit_digest, checkpoint_sha256="b" * 64)
        submitted = NativeContinuationSubmit(
            command=original.command.model_copy(update={"request_id": rid, "conversation_id": original.conversation_id}),
            capture=original.capture, continuation=ref)
        receipt = NativeReceipt(state_profile=submitted.state_profile,
            agent_id=original.agent_id, conversation_id=original.conversation_id,
            hosted_session_id=original.hosted_session_id, request_id=rid,
            command=submitted.command, capture=submitted.capture, continuation=ref,
            submit_digest=submitted.sha256, capture_digest=submitted.capture.sha256, status="started", turn_number=2)
        class ContinuingRuntime(EffectRuntime):
            @asynccontextmanager
            async def _store(self, name):
                if name == "fa/v1/turns":
                    yield self.turns
                else:
                    async with super()._store(name) as store:
                        yield store

        runtime = ContinuingRuntime(receipt)
        runtime.turns = EffectStore()
        runtime.store = first.runtime.store
        context = replace(first.context, receipt=receipt, runtime=runtime, hitl=None)
        gate = NativeHitlRuntime(context, first.client, NativeAutoReview(first.sender),
                                 clock=lambda: hitl.now, poll_seconds=0.005)
        context.hitl = gate
        packet = {**first.packet, "tool_call_id": "continued-call"}
        resumed = SimpleNamespace(**{**vars(first), "runtime": runtime, "context": context,
                                     "gate": gate, "packet": packet})
        guard = NativeOperationGuard(context.effects, {"run_command": "a" * 64}, prior=prior)
        await guard.start()
        assert await guard.prepare(packet) == "execute"
        waiting = asyncio.create_task(permit(resumed))
        new_pending = await pending_view(resumed)
        assert new_pending.approval.pending_id != old_pending.approval.pending_id
        assert not waiting.done() and calls == []
        assert len(first.sender.observed) == 2
        assert (await first.client.read(old_pending.approval.pending_id)).authorization is None
        human_decide(resumed, new_pending, request_id="human-for-continuation")
        assert await waiting == (True, None)

        async def effect(_packet):
            calls.append("one new effect")
            return ToolOutput("original operation completed")

        async def reviewed(value):
            return await gate.invoke(value, effect)

        assert (await guard.invoke(packet, reviewed)).text == "original operation completed"
        assert (await guard.invoke(packet, reviewed)).text == "original operation completed"
        assert calls == ["one new effect"]
        assert await NativeEffects(first.runtime, first.runtime.receipt).operation_records() == prior
        assert (await first.client.read(old_pending.approval.pending_id)).authorization is None

    asyncio.run(scenario())


def test_recovery_reads_original_human_answer_and_deadline_without_reopening_a_wait(hitl, tmp_path):
    from shared_context.native_continuations import read_human_context
    from shared_context.native_hitl_wire import NativeHitlAnswer, NativeHitlCall, NativeHitlQuestion, NativeHitlQuestionCreate
    from test_native_hitl import post

    case = setup_gate(hitl, tmp_path)

    async def scenario():
        question = NativeHitlQuestion(
            call=NativeHitlCall(**case.gate.turn.model_dump(), sdk_session_id=case.context.scope.sdk_session_id,
                               tool_call_id="original-human-question"),
            question="Choose the synthetic option.", choices=("alpha", "beta"), allow_freeform=False)
        created = await case.client.write("/questions", NativeHitlQuestionCreate(
            request_id="original-question-create", question=question, question_digest=question.sha256))
        before = await case.client.read_question(created["question_id"])
        answered = post(hitl, "/answers", NativeHitlAnswer(
            request_id="original-human-answer", question_id=before.reference.question_id,
            question_digest=before.reference.question_digest, answer="beta", was_freeform=False))
        assert answered.status_code == 200
        count = len(case.client.observed)
        data = await read_human_context(case.client, (case.context.receipt,))
        assert len(data) == 1 and data[0]["type"] == "question"
        assert data[0]["answer"]["answer"] == "beta"
        assert data[0]["reference"] == before.reference.model_dump(mode="json")
        assert data[0]["expires_at"] == before.question.expires_at.isoformat()
        assert data[0]["authorization"] is False
        assert all(method == "GET" or path.endswith(("/turn-questions", "/turn-pending"))
                   for method, path, _body in case.client.observed[count:])
        assert (await case.client.read_question(created["question_id"])).reference == before.reference

    asyncio.run(scenario())


def test_handoff_closes_the_original_undecided_card_once_and_keeps_the_owners_choice(hitl, tmp_path, monkeypatch):
    from shared_context import native_continuations
    from shared_context.native_sdk import NativeSdkExecutor
    from test_native_hitl import decision, error, post, proposal

    case = setup_gate(hitl, tmp_path)
    created = {}
    for name in ("undecided", "chosen"):
        command = proposal(hitl, call_id=name + "-call", request_id=name).model_copy(update={"wait": "interactive"})
        response = post(hitl, "/pending", command, hitl.agent)
        assert response.status_code == 200, response.text
        created[name] = response.json()["result"]
    assert post(hitl, "/decisions", decision(hitl, created["chosen"])).status_code == 200

    async def handed_off(_reader, _receipt):
        return True
    monkeypatch.setattr(native_continuations, "has_handoff", handed_off)
    executor = SimpleNamespace(reader=case.runtime, hitl_client=case.client)

    async def scenario():
        await NativeSdkExecutor.notify_terminal(executor, case.context.receipt)
        assert (await case.client.read(created["undecided"]["pending_id"])).status == "interrupted"
        assert (await case.client.read(created["chosen"]["pending_id"])).status == "allow"
        closes = lambda: [path for _method, path, _body in case.client.observed if path.startswith("/native-hitl/v1/observations")]
        assert closes() == ["/native-hitl/v1/observations"]
        await NativeSdkExecutor.notify_terminal(executor, case.context.receipt)
        assert closes() == ["/native-hitl/v1/observations"]
        assert (await case.client.read(created["undecided"]["pending_id"])).status == "interrupted"

    asyncio.run(scenario())
    error(post(hitl, "/decisions", decision(hitl, created["undecided"], request_id="too-late")), 409,
          "NATIVE_HITL_STATE_CONFLICT")


def test_handoff_ends_the_original_turn_so_its_question_and_connect_card_close_and_another_turns_stay(
        hitl, tmp_path, monkeypatch):
    """ADR-0009 gate 6: the handed-off turn's ``/end`` closes its question and Connect card (a later reply is refused,
    never saved unread); a recorded answer stays; a card of another turn (the continuation's) is untouched."""
    from shared_context import native_continuations
    from shared_context.native_hitl_wire import ConnectorConsent, ConnectorConsentCreate, ConnectorConsentDecision, NativeHitlCall
    from shared_context.native_sdk import NativeSdkExecutor
    from test_native_hitl import error, post
    from test_native_hitl_questions import answer, create_question

    case = setup_gate(hitl, tmp_path)
    _, open_question = create_question(hitl)
    _, answered = create_question(hitl, call_id="ask-answered", request_id="question-answered")
    assert post(hitl, "/answers", answer(answered, request_id="answer-before")).status_code == 200
    _, other = create_question(hitl, call_id="ask-other", request_id="question-other", rid="resume-rid")
    consent = ConnectorConsent(
        call=NativeHitlCall(agent_id="native-a", conversation_id="cid-one", request_id="rid-one",
                            sdk_session_id="sdk-original", tool_call_id="connect-original"),
        need="consent", connector_id="teams", toolbox_name="fa-connector-team-one-teams", server_label="teams_read",
        step="tools/list", consent_urls=("https://consent.example.test/login?state=x",))
    connect = post(hitl, "/consents", ConnectorConsentCreate(
        request_id="create-connect", consent=consent, consent_digest=consent.sha256), hitl.agent)
    assert connect.status_code == 200, connect.text

    async def handed_off(_reader, _receipt):
        return True
    monkeypatch.setattr(native_continuations, "has_handoff", handed_off)
    executor = SimpleNamespace(reader=case.runtime, hitl_client=case.client)

    async def scenario():
        for _ in range(2):
            await NativeSdkExecutor.notify_terminal(executor, case.context.receipt)
        assert [path for _method, path, _body in case.client.observed
                if path.startswith("/native-hitl/v1/end")] == ["/native-hitl/v1/end"] * 2  # the second replays
        assert (await case.client.read_question(open_question["question_id"])).turn_closed
        assert (await case.client.read_question(answered["question_id"])).answer.answer == "blue"
        assert not (await case.client.read_question(other["question_id"])).turn_closed

    asyncio.run(scenario())
    error(post(hitl, "/answers", answer(open_question, request_id="answer-late")), 409, "NATIVE_HITL_TURN_CLOSED")
    error(post(hitl, "/consent-decisions", ConnectorConsentDecision(
        request_id="click-late", consent_id=connect.json()["result"]["consent_id"],
        consent_digest=connect.json()["result"]["consent_digest"], choice="resume"), read_only=True),
        409, "NATIVE_HITL_TURN_CLOSED")
    assert post(hitl, "/answers", answer(other, request_id="answer-other")).status_code == 200
