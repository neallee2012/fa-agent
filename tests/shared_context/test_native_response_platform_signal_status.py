"""A GET while the platform's signal is set must not replace the handler's own terminal with the host's "cancelled".

agentserver-responses 2.2.0 does replace it. The pin test below is the one that breaks on an SDK upgrade; the strict
xfail beside it states the right outcome and only xfails on an assertion."""

import asyncio
import threading
import time

import pytest
from azure.ai.agentserver.responses.hosting import _orchestrator

from test_native_response_streaming import sse_events
from test_native_responses import response_submit, responses
from test_native_turns import IDENTITY, local_env

HEADERS = {"x-agent-session-id": IDENTITY.session_id}
STREAMS = pytest.mark.parametrize("stream", [False, True], ids=["background", "background-stream"])


def until(predicate, message):
    deadline = time.monotonic() + 8
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not predicate():
        pytest.fail(message)  # a real FAIL even under the xfail below, which only accepts AssertionError


def signalled_get(responses, monkeypatch, stream):
    """A started RID, then a platform signal (no client cancel): the handler fences the SDK turn and ends it as
    failed/NATIVE_TURN_UNKNOWN. One GET lands between the signal and that terminal.

    Returns that GET, a GET afterwards and, with stream=true, the live stream's events and the replay's response.
    The API creates an owner turn with stream=true (its live preview replays it) and other turns without."""
    # Kept local: test_native_response_handle_paths.py builds the same wrapper inside signal_during_admission, which
    # also patches the session check.
    resolve, resolved = _orchestrator._bg_resolve_terminal_status, set()

    def resolved_here(record, *args, **options):
        resolve(record, *args, **options)
        resolved.add(options["response_id"])

    monkeypatch.setattr(_orchestrator, "_bg_resolve_terminal_status", resolved_here)
    notify, entered, release = responses.runtime._notify_terminal, threading.Event(), threading.Event()

    async def held(receipt, **options):
        """Holds the handler just before its terminal event, so the GET lands inside the window every run."""
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return await notify(receipt, **options)

    monkeypatch.setattr(responses.runtime, "_notify_terminal", held)
    responses.sdk.block = True
    assert responses.invoke(response_submit()).status_code == 202
    accepted, posted = responses.call(responses.runtime.read, "rid-1"), []
    # A stream=true POST streams until the response ends, so it runs beside the reads.
    poster = threading.Thread(target=lambda: posted.append(responses.respond(accepted, stream=stream)))
    poster.start()
    try:
        responses.wait("rid-1", "started")
        context, signal = responses.contexts["rid-1"][-1]
        response_id = context.response_id
        responses.call(signal.set)  # the platform's signal: the host set it, no client cancel named it
        until(entered.is_set, "the handler did not reach its terminal notice")
        assert not context.client_cancelled
        fenced = responses.call(responses.runtime.read, "rid-1")
        assert (fenced.status, fenced.failure_code) == ("unknown", "NATIVE_TURN_UNKNOWN")  # FA's own /status answer
        during = responses.client.get("/responses/" + response_id, headers=HEADERS).json()
    finally:
        release.set()  # a failed step above must not leave the non-daemon poster held
    poster.join(8)
    assert not poster.is_alive() and posted and posted[0].status_code == 200
    if not stream:
        until(lambda: response_id in resolved, "the host did not resolve the handler's terminal")
    after = responses.client.get("/responses/" + response_id, headers=HEADERS).json()
    if not stream:
        return during, after, None, None
    with responses.client.stream("GET", "/responses/" + response_id + "?stream=true", headers=HEADERS) as replay:
        replay.read()
    return during, after, sse_events(posted[0]), replay


@STREAMS
def test_pin_a_get_during_a_platform_signal_replaces_the_handlers_terminal_in_2_2_0(responses, monkeypatch, stream):
    """Today's agentserver-responses 2.2.0 behaviour, pinned as literals: hosting/_orchestrator.py:1256-1257
    _refresh_background_status sets record.status='cancelled' on the GET; _bg_resolve_terminal_status (:638) keeps it,
    _bg_persist_terminal (:856) skips 'cancelled', and the stream path's transition_to (:1839) raises
    cancelled -> failed. This test failing after an upgrade is the signal to re-check the README note."""
    during, after, live, replay = signalled_get(responses, monkeypatch, stream)

    assert during["status"] == "cancelled"
    assert (after["status"], after["output"]) == ("in_progress", [])
    if stream:
        assert live[-1]["type"] == "response.in_progress"
        assert not any(event["type"] in {"response.completed", "response.failed", "response.incomplete",
                                         "response.cancelled"} for event in live)
        assert replay.status_code == 400


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "agentserver-responses 2.2.0 (same function in 2.3.0 and 2.4.0b2) replaces the handler's terminal; see the pin "
    "test above and the README note."))
@STREAMS
def test_a_get_during_a_platform_signal_keeps_the_handlers_unknown_terminal(responses, monkeypatch, stream):
    during, after, live, replay = signalled_get(responses, monkeypatch, stream)

    assert during["status"] == "in_progress"
    assert (after["status"], after["error"]["code"]) == ("failed", "NATIVE_TURN_UNKNOWN")
    if stream:
        replayed = sse_events(replay)
        for events in (live, replayed):
            assert (events[-1]["type"], events[-1]["response"]["error"]["code"]) == (
                "response.failed", "NATIVE_TURN_UNKNOWN")
