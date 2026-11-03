"""The decisions of ``turn_order``, ``wait_turn`` and ``adopt_left`` on the paths the file-store harness does not reach.

Everything crossing the module (the store, the receipts, the chain, the API send) is a small fake, so these run with no
SDK and no Hosted; what is checked is what the caller gets and which writes it caused, in order."""

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest
from azure.ai.agentserver.core.tasks import SteeringQueueFull
from azure.core.exceptions import ServiceRequestError

from shared_context import native_left_receipts as left
from shared_context.errors import Problem
from shared_context.native_responses_wire import NATIVE_RESPONSES_PROFILE
from shared_context.native_turns import NativeTaskInput

ME = "session-me"
OLD = "session-old"
INVOCATIONS = "foundry-native-v2"


def versioned(rid, *, status="accepted", session=ME, profile=INVOCATIONS, adopted_from=None, cancel=False, etag="etag"):
    receipt = SimpleNamespace(
        request_id=rid, conversation_id="cid", status=status, hosted_session_id=session, state_profile=profile,
        cancel_requested=cancel, failure_code=None, command=SimpleNamespace(conversation_id="cid"))
    return SimpleNamespace(value=SimpleNamespace(receipt=receipt, adopted_from=adopted_from, call_id=None), etag=etag)


class Store:
    def __init__(self, items=None, pages=()):
        self.items, self.pages, self.reads, self.listed = dict(items or {}), list(pages), [], []

    async def get_item(self, key):
        self.reads.append(key)
        return self.items.get(key)

    async def list_keys(self, *, limit, after):
        self.listed.append((limit, after))
        return self.pages.pop(0)


class Scoped:
    def __init__(self, store, failure):
        self.store, self.failure = store, failure

    async def __aenter__(self):
        if self.failure is not None:
            raise self.failure
        return self.store

    async def __aexit__(self, *_):
        return False


class Runtime:
    """What these three functions touch of ``NativeTurnRuntime``; ``calls`` keeps the writes in order."""

    def __init__(self, *, store=None, receipts=(), failure=None):
        self.identity = SimpleNamespace(session_id=ME, agent_id="agent", agent_version="7")
        self.store, self.failure = store or Store(), failure
        self.receipts, self.calls = {item.value.receipt.request_id: item for item in receipts}, []
        self.reads = []
        self.chain = SimpleNamespace(start=self._chain_start)
        self.left_wait = SimpleNamespace(start=self._left_wait_start)
        self.on_slot_taken = None
        self.slot = SimpleNamespace(request_id="rid", status="accepted", dispatch=SimpleNamespace(state="needs_response"))

    def _store(self, name, *, create=False):
        return Scoped(self.store, self.failure)

    async def _rid(self, rid):
        self.reads.append(rid)
        found = self.receipts[rid]
        if isinstance(found, list):
            found = found.pop(0) if len(found) > 1 else found[0]
        if isinstance(found, Exception):
            raise found
        return found

    def _check_rid(self, item, rid):
        if item.original is None:
            raise Problem(403, "NATIVE_STATE_SCOPE", "OFFLINE another Agent's receipt")
        return item.original

    async def _chain_start(self, **kwargs):
        self.calls.append(("chain", kwargs["task_id"], kwargs["input_id"]))

    async def _left_wait_start(self, **kwargs):
        self.calls.append(("left_wait", kwargs["task_id"]))

    async def _require_original_session(self, receipt):
        return SimpleNamespace(task_id="task-" + receipt.request_id)

    async def _response_slot(self, original, *, wait):
        self.calls.append(("slot", wait))
        return self.slot

    async def _unknown(self, original, code):
        self.calls.append(("unknown", code))
        return original.value.receipt

    async def _notify_terminal(self, receipt):
        self.calls.append(("notice", receipt.request_id))

    def _close_cards_later(self, receipt):
        self.calls.append(("cards", receipt.request_id))


def queue_free(monkeypatch, runtime):
    """``dequeue`` and ``defer`` keep their own tests; here only their place in the order counts."""
    async def dequeue(_runtime, receipt):
        runtime.calls.append(("dequeue", receipt.request_id))

    async def defer(_runtime, original, behind, *, after=None):
        runtime.calls.append(("defer", behind, after))

    monkeypatch.setattr(left, "dequeue", dequeue)
    monkeypatch.setattr(left, "defer", defer)


def order_is(monkeypatch, *orders):
    """``turn_order`` answers these in turn (the last one repeats)."""
    answers, asked = list(orders), []

    async def turn_order(_runtime, original):
        asked.append(original.value.receipt.request_id)
        return answers.pop(0) if len(answers) > 1 else answers[0]

    monkeypatch.setattr(left, "turn_order", turn_order)
    return asked


def task(rid="rid", *, cancel_requested=False, timeout_exceeded=False, shutdown=False):
    ctx = SimpleNamespace(
        input=NativeTaskInput(request_id=rid, accepted_etag="etag"), shutdown=asyncio.Event(), cancel=asyncio.Event(),
        cancel_requested=cancel_requested, timeout_exceeded=timeout_exceeded, task_id="wait-task", exited=[])

    async def exit_for_recovery():
        ctx.exited.append(True)
        return "recovering"

    ctx.exit_for_recovery = exit_for_recovery
    if shutdown:
        ctx.shutdown.set()
    return ctx


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(left, "POLL_SECONDS", 0.01)


def wait(runtime, ctx):
    async def run():
        # A wait task that never ends is a failure here, not a hung run.
        return await asyncio.wait_for(left.wait_turn(runtime, ctx), 20)

    return asyncio.run(run())


# turn_order

def meta(*, rid=None, inflight_rid=None):
    return SimpleNamespace(rid=rid, inflight_rid=inflight_rid)


def order(runtime, original, value, **kwargs):
    return asyncio.run(left.turn_order(runtime, original, meta=value, **kwargs))


def test_a_conversation_with_no_store_yet_goes_on_and_is_not_queued():
    runtime = Runtime(failure=Problem(404, "NATIVE_STATE_STORE_MISSING", "OFFLINE no store"))
    assert order(runtime, versioned("rid"), meta(rid="holder")) == left.Order(True, False)
    assert runtime.reads == []


def test_any_other_store_failure_reaches_the_caller():
    runtime = Runtime(failure=Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "OFFLINE store"))
    with pytest.raises(Problem) as refused:
        order(runtime, versioned("rid"), meta(rid="holder"))
    assert refused.value.body["error"]["code"] == "NATIVE_STATE_STORE_UNKNOWN"


def test_a_caller_that_read_the_meta_pays_only_the_queue_read():
    runtime = Runtime()
    assert order(runtime, versioned("rid"), None) == left.Order(True, False)
    assert runtime.store.reads == [left.QUEUE_KEY]


def adoption(monkeypatch, runtime, adopted, *, failure=None):
    async def adopt(_runtime, held):
        runtime.calls.append(("adopt", held.value.receipt.request_id))
        return adopted

    async def dispatch(_runtime, value):
        runtime.calls.append(("dispatch", value.value.receipt.request_id))
        if failure is not None:
            raise failure

    monkeypatch.setattr(left, "adopt", adopt)
    monkeypatch.setattr(left, "_dispatch_adopted", dispatch)


def test_an_adopted_slot_holder_not_sent_yet_is_waited_for_like_a_left_one(monkeypatch):
    held = versioned("holder", session=OLD)
    runtime = Runtime(receipts=[held])
    adoption(monkeypatch, runtime, versioned("holder", adopted_from=OLD))
    decided = order(runtime, versioned("rid", profile=NATIVE_RESPONSES_PROFILE), meta(inflight_rid="holder"))
    assert decided == left.Order(False, False, "adopted:holder")
    assert runtime.calls == [("adopt", "holder"), ("dispatch", "holder")]


def test_an_adopted_invocations_holder_leaves_the_order_to_the_chain(monkeypatch):
    held = versioned("holder", session=OLD)
    runtime = Runtime(receipts=[held])
    adoption(monkeypatch, runtime, versioned("holder", adopted_from=OLD))
    assert order(runtime, versioned("rid"), meta(rid="holder")) == left.Order(True, False)
    assert runtime.calls == [("adopt", "holder"), ("dispatch", "holder")]


@pytest.mark.parametrize("failure", [Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "OFFLINE"), ServiceRequestError("OFFLINE")])
def test_a_committed_adoption_whose_wait_task_is_unconfirmed_still_decides(monkeypatch, caplog, failure):
    held = versioned("holder", session=OLD)
    runtime = Runtime(receipts=[held])
    adoption(monkeypatch, runtime, versioned("holder", adopted_from=OLD), failure=failure)
    with caplog.at_level(logging.WARNING, logger=left.__name__):
        decided = order(runtime, versioned("rid", profile=NATIVE_RESPONSES_PROFILE), meta(inflight_rid="holder"))
    assert decided == left.Order(False, False, "adopted:holder")
    assert "NATIVE_LEFT_RECEIPT_ADOPTION_WAIT_UNCONFIRMED exception_type=" + type(failure).__name__ in caplog.text


def test_a_refused_adoption_dispatch_reaches_the_caller(monkeypatch):
    held = versioned("holder", session=OLD)
    runtime = Runtime(receipts=[held])
    adoption(monkeypatch, runtime, versioned("holder", adopted_from=OLD), failure=Problem(409, "NATIVE_X", "OFFLINE"))
    with pytest.raises(Problem) as refused:
        order(runtime, versioned("rid"), meta(rid="holder"))
    assert refused.value.body["error"]["code"] == "NATIVE_X"


def test_a_holder_not_adopted_is_left_to_the_stall_rule(monkeypatch):
    held = versioned("holder", status="started", session=OLD)
    runtime = Runtime(receipts=[held])
    adoption(monkeypatch, runtime, None)

    async def settled(_runtime, value):
        assert value is held
        return False

    monkeypatch.setattr(left, "settled", settled)
    assert order(runtime, versioned("rid"), meta(rid="holder")) == left.Order(False, False, "left:holder")
    assert runtime.calls == [("adopt", "holder")]


def test_the_caller_read_holder_is_not_read_again_and_another_rids_is_ignored():
    held = versioned("holder", status="completed", profile=NATIVE_RESPONSES_PROFILE)
    runtime = Runtime(receipts=[held])
    responses = versioned("rid", profile=NATIVE_RESPONSES_PROFILE)
    assert order(runtime, responses, meta(inflight_rid="holder"), held=held) == left.Order(True, False)
    assert runtime.reads == []
    assert order(runtime, responses, meta(inflight_rid="holder"), held=versioned("other")) == left.Order(True, False)
    assert runtime.reads == ["holder"]


@pytest.mark.parametrize("status,behind", [("accepted", "slot:holder"), ("started", "slot:holder"),
                                           ("completed", ""), ("unknown", ""), ("busy_refused", "")])
def test_a_responses_holder_of_this_session_keeps_one_at_a_time_until_it_ends(status, behind):
    runtime = Runtime(receipts=[versioned("holder", status=status)])
    decided = order(runtime, versioned("rid", profile=NATIVE_RESPONSES_PROFILE), meta(inflight_rid="holder", rid="other"))
    assert decided == left.Order(not behind, False, behind)


@pytest.mark.parametrize("status", ["completed", "unknown", "busy_refused", "started"])
def test_only_an_adopted_holder_not_sent_yet_counts_as_adopted(status):
    held = versioned("holder", status=status, adopted_from=OLD)
    runtime = Runtime(receipts=[held])
    decided = order(runtime, versioned("rid", profile=NATIVE_RESPONSES_PROFILE), meta(inflight_rid="holder"))
    assert decided == (left.Order(False, False, "slot:holder") if status == "started" else left.Order(True, False))


def test_an_invocations_message_looks_at_the_numbered_holder_not_the_slot():
    runtime = Runtime(receipts=[versioned("holder", status="started")])
    assert order(runtime, versioned("rid"), meta(rid="holder", inflight_rid="slot-holder")) == left.Order(True, False)
    assert runtime.reads == ["holder"]


@pytest.mark.parametrize("value", [None, meta(rid="rid", inflight_rid="rid")])
@pytest.mark.parametrize("profile", [INVOCATIONS, NATIVE_RESPONSES_PROFILE])
def test_no_holder_or_itself_as_the_holder_goes_on(value, profile):
    runtime = Runtime()
    assert order(runtime, versioned("rid", profile=profile), value) == left.Order(True, False)
    assert runtime.reads == []


# wait_turn

def test_shutdown_hands_the_task_back_before_any_read():
    runtime = Runtime(receipts=[versioned("rid")])
    ctx = task(shutdown=True)
    assert wait(runtime, ctx) == "recovering"
    assert ctx.exited == [True] and runtime.reads == []


@pytest.mark.parametrize("status,session", [("started", ME), ("completed", ME), ("accepted", OLD)])
def test_a_message_no_longer_waiting_here_only_leaves_its_queue(monkeypatch, status, session):
    runtime = Runtime(receipts=[versioned("rid", status=status, session=session, cancel=True)])
    queue_free(monkeypatch, runtime)
    assert wait(runtime, task(cancel_requested=True, timeout_exceeded=True)) == "rid"
    assert runtime.calls == [("dequeue", "rid")]


def test_a_message_another_session_took_is_not_given_a_turn_here(monkeypatch):
    runtime = Runtime(receipts=[versioned("rid", session=OLD)])
    queue_free(monkeypatch, runtime)
    asked = order_is(monkeypatch, left.Order(True, False))
    assert wait(runtime, task()) == "rid"
    assert runtime.calls == [("dequeue", "rid")] and asked == []


@pytest.mark.parametrize("from_task", [True, False])
def test_a_cancel_ends_the_message_before_its_timeout_or_its_turn_counts(monkeypatch, from_task):
    runtime = Runtime(receipts=[versioned("rid", cancel=not from_task)])
    queue_free(monkeypatch, runtime)
    asked = order_is(monkeypatch, left.Order(True, False))
    assert wait(runtime, task(cancel_requested=from_task, timeout_exceeded=True)) == "rid"
    assert runtime.calls == [("unknown", "NATIVE_TURN_CANCELLED"), ("dequeue", "rid"), ("notice", "rid")]
    assert asked == []


def test_a_timed_out_task_hands_over_before_its_turn_counts(monkeypatch, caplog):
    runtime = Runtime(receipts=[versioned("rid")])
    queue_free(monkeypatch, runtime)
    asked = order_is(monkeypatch, left.Order(True, False))
    with caplog.at_level(logging.WARNING, logger=left.__name__):
        assert wait(runtime, task(timeout_exceeded=True)) == "rid"
    assert runtime.calls == [("defer", "renewed", "wait-task")] and asked == []
    assert "NATIVE_LEFT_WAIT_RENEWED rid_prefix=rid" in caplog.text


def test_an_invocations_message_goes_on_the_agents_chain_once_it_may(monkeypatch, quick):
    runtime = Runtime(receipts=[versioned("rid")])
    asked = order_is(monkeypatch, left.Order(False, True, "queue:older"), left.Order(True, True))
    assert wait(runtime, task()) == "rid"
    assert runtime.calls == [("chain", "task-rid", "rid")] and asked == ["rid", "rid"]


def responses_wait(monkeypatch):
    runtime = Runtime(receipts=[versioned("rid", profile=NATIVE_RESPONSES_PROFILE)])
    order_is(monkeypatch, left.Order(True, True))
    return runtime


@pytest.mark.parametrize("slot", [SimpleNamespace(request_id="rid", status="started", dispatch=None),
                                  SimpleNamespace(request_id="rid", status="accepted", dispatch=None),
                                  SimpleNamespace(request_id="rid", status="completed",
                                                  dispatch=SimpleNamespace(state="needs_response"))])
def test_a_slot_that_needs_no_send_ends_the_wait_task(monkeypatch, slot):
    runtime = responses_wait(monkeypatch)
    runtime.slot = slot
    sent = []

    async def on_slot_taken(taken):
        sent.append(taken)

    runtime.on_slot_taken = on_slot_taken
    assert wait(runtime, task()) == "rid"
    assert runtime.calls == [("slot", False)] and sent == []


def test_a_slot_with_no_api_to_ask_ends_the_wait_task(monkeypatch):
    runtime = responses_wait(monkeypatch)
    assert wait(runtime, task()) == "rid"
    assert runtime.calls == [("slot", False)]


def test_a_slot_not_taken_yet_is_looked_at_again_without_a_send(monkeypatch, quick):
    runtime = responses_wait(monkeypatch)
    queue_free(monkeypatch, runtime)
    runtime.receipts["rid"] = [versioned("rid", profile=NATIVE_RESPONSES_PROFILE),
                               versioned("rid", status="started", profile=NATIVE_RESPONSES_PROFILE)]
    runtime.slot = SimpleNamespace(request_id="rid", status="accepted", dispatch=SimpleNamespace(state="already_queued"))
    sent = []

    async def on_slot_taken(taken):
        sent.append(taken)

    runtime.on_slot_taken = on_slot_taken
    assert wait(runtime, task()) == "rid"
    assert runtime.calls == [("slot", False), ("dequeue", "rid")] and sent == []


@pytest.mark.parametrize("failure", [Problem(403, "WORK_POLICY_REVOKED", "OFFLINE"), Problem(400, "OFFLINE_REFUSED", "OFFLINE")])
def test_a_send_the_api_refuses_leaves_the_message_waiting_and_ends_the_task(monkeypatch, failure):
    runtime = responses_wait(monkeypatch)

    async def on_slot_taken(_taken):
        raise failure

    runtime.on_slot_taken = on_slot_taken
    assert wait(runtime, task()) == "rid"
    assert runtime.calls == [("slot", False)]


def test_a_sent_message_is_not_asked_for_again_before_its_resend_time(monkeypatch, quick):
    runtime = responses_wait(monkeypatch)
    queue_free(monkeypatch, runtime)
    monkeypatch.setattr(left, "RESEND_SECONDS", 60)
    accepted = versioned("rid", profile=NATIVE_RESPONSES_PROFILE)
    runtime.receipts["rid"] = [accepted, accepted, accepted, versioned("rid", status="started")]
    sent = []

    async def on_slot_taken(taken):
        sent.append(taken)

    runtime.on_slot_taken = on_slot_taken
    assert wait(runtime, task()) == "rid"
    assert sent == [runtime.slot] and [name for name, *_ in runtime.calls] == ["slot", "slot", "slot", "dequeue"]


def test_failed_sends_stop_at_the_attempt_budget_with_the_terminal_notice(monkeypatch, quick):
    runtime = responses_wait(monkeypatch)
    queue_free(monkeypatch, runtime)
    monkeypatch.setattr(left, "RESEND_SECONDS", 0)
    monkeypatch.setattr(left, "SEND_ATTEMPTS", 2)
    sent = []

    async def on_slot_taken(taken):
        sent.append(taken)
        raise Problem(404, "NOT_THERE", "OFFLINE")

    runtime.on_slot_taken = on_slot_taken
    assert wait(runtime, task()) == "rid"
    assert len(sent) == 2
    assert runtime.calls[-3:] == [("unknown", "NATIVE_TURN_TIMEOUT"), ("dequeue", "rid"), ("notice", "rid")]


def test_an_answered_send_clears_the_early_backoff_and_a_failure_does_too(monkeypatch, quick):
    delays = []
    monkeypatch.setattr(left, "early_delay", lambda count: delays.append(count) or 0)
    monkeypatch.setattr(left, "RESEND_SECONDS", 0)
    runtime = responses_wait(monkeypatch)
    queue_free(monkeypatch, runtime)
    answers = [Problem(409, "NATIVE_SESSION_CHANGED", "OFFLINE"), Problem(409, "NATIVE_SESSION_CHANGED", "OFFLINE"), None,
               Problem(409, "NATIVE_SESSION_CHANGED", "OFFLINE"), Problem(503, "OFFLINE_DOWN", "OFFLINE"),
               Problem(409, "NATIVE_SESSION_CHANGED", "OFFLINE"), Problem(400, "OFFLINE_REFUSED", "OFFLINE")]

    async def on_slot_taken(_taken):
        answer = answers.pop(0)
        if answer is not None:
            raise answer

    runtime.on_slot_taken = on_slot_taken
    assert wait(runtime, task()) == "rid"
    assert delays == [1, 2, 1, 1] and answers == []


@pytest.mark.parametrize("failure", [ServiceRequestError("OFFLINE"), SteeringQueueFull("OFFLINE"),
                                     Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "OFFLINE")])
def test_a_transient_failure_of_a_look_looks_again(monkeypatch, quick, caplog, failure):
    runtime = Runtime(receipts=[versioned("rid")])
    runtime.receipts["rid"] = [failure, versioned("rid")]
    order_is(monkeypatch, left.Order(True, False))
    with caplog.at_level(logging.INFO, logger=left.__name__):
        assert wait(runtime, task()) == "rid"
    assert runtime.calls == [("chain", "task-rid", "rid")]
    assert "NATIVE_LEFT_WAIT_RETRY" in caplog.text


def test_a_refused_look_fails_the_task(monkeypatch):
    runtime = Runtime(receipts=[versioned("rid")])
    runtime.receipts["rid"] = [Problem(403, "NATIVE_STATE_SCOPE", "OFFLINE")]
    with pytest.raises(Problem) as refused:
        wait(runtime, task())
    assert refused.value.body["error"]["code"] == "NATIVE_STATE_SCOPE"


def test_a_cancel_wakes_the_wait_at_once_and_is_seen_at_the_next_look(monkeypatch):
    runtime = Runtime(receipts=[versioned("rid")])
    queue_free(monkeypatch, runtime)
    monkeypatch.setattr(left, "POLL_SECONDS", 30)
    ctx = task()

    async def order_then_cancel(_runtime, _original):
        asyncio.get_running_loop().call_soon(lambda: (ctx.__setattr__("cancel_requested", True), ctx.cancel.set()))
        return left.Order(False, True, "queue:older")

    monkeypatch.setattr(left, "turn_order", order_then_cancel)
    started = time.monotonic()
    assert wait(runtime, ctx) == "rid"
    assert time.monotonic() - started < 10
    assert runtime.calls[0] == ("unknown", "NATIVE_TURN_CANCELLED")


def test_a_cancel_being_retried_still_waits_a_whole_look(monkeypatch):
    runtime = Runtime(receipts=[versioned("rid")])
    queue_free(monkeypatch, runtime)
    monkeypatch.setattr(left, "POLL_SECONDS", 0.2)
    runtime.receipts["rid"] = [ServiceRequestError("OFFLINE"), versioned("rid")]
    ctx = task(cancel_requested=True)
    ctx.cancel.set()
    started = time.monotonic()
    assert wait(runtime, ctx) == "rid"
    # Windows timers may fire one 15.6 ms tick early; 0.15 still proves one whole look was waited.
    assert time.monotonic() - started >= 0.15
    assert runtime.calls[0] == ("unknown", "NATIVE_TURN_CANCELLED")


def test_a_shutdown_cuts_the_wait_short_and_no_signal_task_is_left():
    async def run():
        runtime = Runtime(receipts=[versioned("rid")])
        runtime.receipts["rid"] = [ServiceRequestError("OFFLINE")]
        ctx = task()
        left_POLL = left.POLL_SECONDS
        left.POLL_SECONDS = 30
        try:
            waiting = asyncio.create_task(left.wait_turn(runtime, ctx))
            await asyncio.sleep(0.01)
            ctx.shutdown.set()
            assert await asyncio.wait_for(waiting, 10) == "recovering"
        finally:
            left.POLL_SECONDS = left_POLL
        others = [item for item in asyncio.all_tasks() if item is not asyncio.current_task()]
        assert others == []

    asyncio.run(run())


# adopt_left

def row(rid, *, status="accepted", session=OLD, created_at=None, valid=True):
    return SimpleNamespace(created_at=created_at, original=versioned(rid, status=status, session=session) if valid else None)


def adopt_all(monkeypatch, runtime, *, refused=(), dispatch_failure=None, completes=lambda rid: True):
    async def adopt(_runtime, original):
        rid = original.value.receipt.request_id
        runtime.calls.append(("adopt", rid))
        if rid in refused:
            raise refused[rid]
        return original

    async def dispatch(_runtime, value):
        runtime.calls.append(("dispatch", value.value.receipt.request_id))
        if dispatch_failure is not None:
            raise dispatch_failure

    async def can_complete(_runtime, original):
        answer = completes(original.value.receipt.request_id)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(left, "adopt", adopt)
    monkeypatch.setattr(left, "_dispatch_adopted", dispatch)
    monkeypatch.setattr(left, "can_complete", can_complete)


def run_adopt_left(runtime, caplog, **kwargs):
    with caplog.at_level(logging.INFO, logger=left.__name__):
        asyncio.run(left.adopt_left(runtime, **kwargs))


def page(*keys, has_more=False, last_id=None):
    return SimpleNamespace(keys=[SimpleNamespace(key=key) for key in keys], has_more=has_more, last_id=last_id)


def test_receipts_are_adopted_oldest_first_then_by_key(monkeypatch, caplog):
    store = Store({"rid:late": row("late", created_at=30), "rid:b": row("b", created_at=20), "rid:a": row("a", created_at=20),
                   "rid:bool": row("bool", created_at=True), "rid:none": row("none")}, pages=[page(
                       "rid:late", "rid:b", "rid:a", "rid:bool", "rid:none", "meta", "stall:x")])
    runtime = Runtime(store=store)
    adopt_all(monkeypatch, runtime)
    run_adopt_left(runtime, caplog)
    assert [rid for name, rid in runtime.calls if name == "adopt"] == ["bool", "none", "a", "b", "late"]
    assert runtime.calls[:2] == [("adopt", "bool"), ("dispatch", "bool")]
    assert "meta" not in store.reads and store.listed == [(100, None)]
    assert "NATIVE_LEFT_RECEIPTS_ADOPTED count=5 complete=True" in caplog.text


def test_an_open_rid_set_is_read_sorted_instead_of_every_receipt(monkeypatch, caplog):
    store = Store({"rid:a": row("a"), "rid:b": row("b")})
    runtime = Runtime(store=store)
    adopt_all(monkeypatch, runtime)
    run_adopt_left(runtime, caplog, rids={"b", "a"})
    assert store.reads == ["rid:a", "rid:b"] and store.listed == []
    assert "NATIVE_LEFT_RECEIPTS_ADOPTED count=2 complete=True" in caplog.text


def test_an_empty_open_rid_set_reads_nothing(monkeypatch, caplog):
    store = Store()
    runtime = Runtime(store=store)
    adopt_all(monkeypatch, runtime)
    run_adopt_left(runtime, caplog, rids=set())
    assert store.reads == [] and store.listed == []
    assert "NATIVE_LEFT_RECEIPTS_ADOPTED count=0 complete=True" in caplog.text


def test_every_page_is_read(monkeypatch, caplog):
    store = Store({"rid:a": row("a"), "rid:b": row("b")}, pages=[
        page("rid:a", has_more=True, last_id="rid:a"), page("rid:b")])
    runtime = Runtime(store=store)
    adopt_all(monkeypatch, runtime)
    run_adopt_left(runtime, caplog)
    assert store.listed == [(100, None), (100, "rid:a")]
    assert "NATIVE_LEFT_RECEIPTS_ADOPTED count=2 complete=True" in caplog.text


@pytest.mark.parametrize("second", [page("rid:b", has_more=True, last_id=None),
                                    page("rid:b", has_more=True, last_id="rid:a"),
                                    page("rid:b", has_more=True, last_id="")])
def test_a_page_with_no_way_forward_ends_the_read_incomplete(monkeypatch, caplog, second):
    store = Store({"rid:a": row("a"), "rid:b": row("b")}, pages=[page("rid:a", has_more=True, last_id="rid:a"), second])
    runtime = Runtime(store=store)
    adopt_all(monkeypatch, runtime)
    run_adopt_left(runtime, caplog)
    assert len(store.listed) == 2
    assert "NATIVE_LEFT_RECEIPTS_ADOPTED count=2 complete=False" in caplog.text


def test_the_deadline_ends_the_read_incomplete_and_what_it_found_is_still_adopted(monkeypatch, caplog):
    class Slow(Store):
        async def get_item(self, key):
            value = await super().get_item(key)
            await asyncio.sleep(0.05)
            return value

    monkeypatch.setattr(left, "_ADOPT_SCAN_SECONDS", 0.02)
    store = Slow({"rid:a": row("a"), "rid:b": row("b")}, pages=[page("rid:a", "rid:b", has_more=True, last_id="rid:b")])
    runtime = Runtime(store=store)
    adopt_all(monkeypatch, runtime)
    run_adopt_left(runtime, caplog)
    assert store.reads == ["rid:a"] and len(store.listed) == 1
    assert [rid for name, rid in runtime.calls if name == "adopt"] == ["a"]
    assert "NATIVE_LEFT_RECEIPTS_ADOPTED count=1 complete=False" in caplog.text


def test_a_receipt_that_does_not_read_is_skipped(monkeypatch, caplog):
    store = Store({"rid:bad": row("bad", valid=False), "rid:good": row("good")}, pages=[page("rid:bad", "rid:good")])
    runtime = Runtime(store=store)
    adopt_all(monkeypatch, runtime)
    run_adopt_left(runtime, caplog)
    assert [rid for name, rid in runtime.calls if name == "adopt"] == ["good"]
    assert "NATIVE_LEFT_RECEIPTS_ADOPTED count=1 complete=True" in caplog.text


@pytest.mark.parametrize("failure", [ServiceRequestError("OFFLINE"), Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "OFFLINE")])
def test_a_failed_read_never_fails_the_switch_and_what_it_found_is_kept(monkeypatch, caplog, failure):
    class Failing(Store):
        async def list_keys(self, *, limit, after):
            if after is not None:
                raise failure
            return await super().list_keys(limit=limit, after=after)

    store = Failing({"rid:a": row("a"), "rid:s": row("s", status="started")},
                    pages=[page("rid:a", "rid:s", has_more=True, last_id="rid:s")])
    runtime = Runtime(store=store)
    adopt_all(monkeypatch, runtime, completes=lambda rid: False)
    run_adopt_left(runtime, caplog, source=OLD)
    assert runtime.calls == [("adopt", "a"), ("dispatch", "a"), ("cards", "s")]
    assert "NATIVE_LEFT_RECEIPTS_ADOPTION_READ_UNCONFIRMED exception_type=" + type(failure).__name__ in caplog.text
    assert "NATIVE_LEFT_RECEIPTS_ADOPTED count=1 complete=False" in caplog.text


def test_a_store_that_does_not_open_adopts_nothing_and_logs_incomplete(monkeypatch, caplog):
    runtime = Runtime(failure=ServiceRequestError("OFFLINE"))
    adopt_all(monkeypatch, runtime)
    run_adopt_left(runtime, caplog)
    assert runtime.calls == []
    assert "NATIVE_LEFT_RECEIPTS_ADOPTED count=0 complete=False" in caplog.text


def test_a_failed_adoption_never_stops_the_next_one(monkeypatch, caplog):
    store = Store({"rid:a": row("a", created_at=1), "rid:b": row("b", created_at=2), "rid:c": row("c", created_at=3)},
                  pages=[page("rid:a", "rid:b", "rid:c")])
    runtime = Runtime(store=store)
    adopt_all(monkeypatch, runtime, refused={"a": Problem(409, "NATIVE_X", "OFFLINE"), "b": ServiceRequestError("OFFLINE")})
    run_adopt_left(runtime, caplog)
    assert [rid for name, rid in runtime.calls if name == "adopt"] == ["a", "b", "c"]
    assert "NATIVE_LEFT_RECEIPT_ADOPTION_UNCONFIRMED exception_type=Problem code=NATIVE_X" in caplog.text
    assert "NATIVE_LEFT_RECEIPT_ADOPTION_UNCONFIRMED exception_type=ServiceRequestError code=None" in caplog.text
    assert "NATIVE_LEFT_RECEIPTS_ADOPTED count=1 complete=True" in caplog.text


def test_an_adoption_that_stays_put_is_not_counted(monkeypatch, caplog):
    store = Store({"rid:a": row("a")}, pages=[page("rid:a")])
    runtime = Runtime(store=store)
    adopt_all(monkeypatch, runtime)

    async def adopt(_runtime, _original):
        return None

    monkeypatch.setattr(left, "adopt", adopt)
    run_adopt_left(runtime, caplog)
    assert runtime.calls == []
    assert "NATIVE_LEFT_RECEIPTS_ADOPTED count=0 complete=True" in caplog.text


def test_a_dispatch_that_fails_after_the_adoption_still_counts_it(monkeypatch, caplog):
    store = Store({"rid:a": row("a")}, pages=[page("rid:a")])
    runtime = Runtime(store=store)
    adopt_all(monkeypatch, runtime, dispatch_failure=ServiceRequestError("OFFLINE"))
    run_adopt_left(runtime, caplog)
    assert "NATIVE_LEFT_RECEIPTS_ADOPTED count=1 complete=True" in caplog.text


def test_only_the_abandoned_sources_started_turns_that_can_no_longer_complete_close_their_cards(monkeypatch, caplog):
    store = Store({
        "rid:mine": row("mine", status="started", session=ME), "rid:other": row("other", status="started", session="session-x"),
        "rid:live": row("live", status="started"), "rid:gone": row("gone", status="started"),
        "rid:failed": row("failed", status="started"), "rid:adopt": row("adopt")},
        pages=[page("rid:mine", "rid:other", "rid:live", "rid:gone", "rid:failed", "rid:adopt")])
    runtime = Runtime(store=store)
    adopt_all(monkeypatch, runtime, completes=lambda rid: {
        "live": True, "gone": False, "failed": ServiceRequestError("OFFLINE")}[rid])
    run_adopt_left(runtime, caplog, source=OLD)
    # The adoptions come first; the log line stands between them and the cards.
    assert runtime.calls == [("adopt", "adopt"), ("dispatch", "adopt"), ("cards", "gone")]
    assert "NATIVE_LEFT_RECEIPT_CARDS_UNCONFIRMED exception_type=ServiceRequestError" in caplog.text
    assert caplog.text.index("NATIVE_LEFT_RECEIPTS_ADOPTED") < caplog.text.index("NATIVE_LEFT_RECEIPT_CARDS_UNCONFIRMED")


def test_with_no_source_no_card_is_closed(monkeypatch, caplog):
    store = Store({"rid:s": row("s", status="started")}, pages=[page("rid:s")])
    runtime = Runtime(store=store)
    adopt_all(monkeypatch, runtime, completes=lambda rid: False)
    run_adopt_left(runtime, caplog)
    assert runtime.calls == []


