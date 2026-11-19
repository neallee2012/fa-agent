"""CTX-03 codec rules for runtime 1.0.112's compaction events; offline, hand-written after what the real runtime wrote.

1.0.112 reports a background summarizer settling (``session.compaction_summarized``, ephemeral, never in events.jsonl)
between an automatic start and complete, and writes no ``model.*`` diagnostics for a manual compaction. Only events
that release wrote get these allowances, whichever release runs later; every other release keeps its own rules.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from shared_context import sdk_pin
from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.behavior_sdk_state import AUTO_PROFILE, DIAG_CODEC, CheckpointProfile, SdkState, local_platform
from shared_context.errors import Problem

SID = "OFFLINE-sid"
FIXTURES = Path(__file__).parent / "fixtures"
ATTEMPT = "attempt-background"
SUMMARY = ("<overview>Original public conversation.</overview><checkpoint_title>Public conversation</checkpoint_title>")
INDEX = ("# Checkpoint History\n\nCheckpoints are listed in chronological order. "
         "Checkpoint 1 is the oldest, higher numbers are more recent.\n\n"
         "| # | Title | File |\n|---|-------|------|\n| 1 | Public conversation | 001-public-conversation.md |\n")


def live(kind, identity, data, ephemeral=None):
    packet = {"type": kind, "id": identity, "timestamp": "2026-11-19T01:00:00.000Z", "parentId": None, "data": data}
    return packet if ephemeral is None else {**packet, "ephemeral": ephemeral}


def start(attempt=ATTEMPT):
    return live("session.compaction_start", "start", {"trigger": "threshold", "compactionAttemptId": attempt,
                                                      "execution": "background"})


def summarized(attempt=ATTEMPT, ephemeral=True, success=True):
    return live("session.compaction_summarized", "summarized", {
        "applyPending": True, "compactionAttemptId": attempt, "execution": "background", "outcome": "succeeded",
        "success": success}, ephemeral)


def complete():
    return live("session.compaction_complete", "complete", {
        "trigger": "threshold", "compactionAttemptId": ATTEMPT, "success": True, "summaryContent": SUMMARY,
        "checkpointNumber": 1})


def observed(*events, profile=AUTO_PROFILE):
    files = SdkSessionFiles(SID, None, checkpoint_profile=CheckpointProfile(name=profile, platform=local_platform()))
    for event in events:
        files.observe_compaction(event)
    return files.checkpoint_error


def test_the_auto_profile_takes_the_1_0_112_summarized_event_of_its_own_open_compaction():
    assert observed(start(), summarized(), complete()) is None


@pytest.mark.parametrize("events", [
    (summarized(), start(), complete()),
    (start(), complete(), summarized()),
    (start(), summarized(), summarized(), complete()),
    (start(), summarized(attempt="another-attempt"), complete()),
    (start(attempt=None), summarized(), complete()),
    (start(), summarized(ephemeral=False), complete()),
    (start(), summarized(ephemeral=None), complete()),
    (start(), summarized(success="yes"), complete()),
    (start(), summarized(success=False), complete()),
], ids=["before-start", "after-complete", "twice", "other-attempt", "start-without-attempt", "persisted",
        "no-ephemeral-flag", "success-not-bool", "not-succeeded"])
def test_any_other_summarized_event_leaves_the_auto_checkpoint_unverified(events):
    assert observed(*events) == "SDK_STATE_SOURCE_UNVERIFIED"


def test_a_runtime_without_the_summarized_event_still_refuses_it(monkeypatch):
    monkeypatch.setattr(sdk_pin, "RUNTIME_VERSION", "1.0.103")
    assert observed(start(), summarized(), complete()) == "SDK_STATE_SOURCE_UNVERIFIED"


def test_a_manual_compaction_still_refuses_the_summarized_event():
    files = SdkSessionFiles(SID, None, checkpoint_profile=CheckpointProfile(platform=local_platform()))
    files._compacting = True
    files.observe_compaction(live("session.compaction_start", "start", {"trigger": "manual"}))
    files.observe_compaction(summarized())
    assert files.checkpoint_error == "SDK_STATE_SOURCE_UNVERIFIED"


def _event(kind, identity, data, parent):
    return {"type": kind, "id": identity, "timestamp": "2026-11-19T01:00:00.000Z", "parentId": parent, "data": data}


def manual_files(*, complete_data=None, diagnostic=None, created="1.0.112"):
    """A manual compaction as runtime 1.0.112 writes it: no model.* event inside the window. ``created`` is the
    runtime ``session.start`` names."""
    data = {"trigger": "manual", "compactionAttemptId": ATTEMPT, "success": True, "summaryContent": SUMMARY,
            "checkpointNumber": 1, "checkpointPath": "/session-state/checkpoints/001-public-conversation.md",
            "messagesRemoved": 1, "tokensRemoved": 0, "execution": "direct", "clientRequestId": "client-request",
            "compactionTokensUsed": {"requestCount": 1, "inputTokens": 100, "outputTokens": 9}}
    data.update(complete_data or {})
    data = {key: value for key, value in data.items() if value is not None}
    start = {"sessionId": SID} if created is None else {"sessionId": SID, "copilotVersion": created}
    events = [_event("session.start", "session-start", start, None),
              _event("user.message", "user", {"content": "Public question."}, "session-start"),
              _event("assistant.message", "answer", {"content": "Public answer."}, "user"),
              _event("session.compaction_start", "compact-start",
                     {"trigger": "manual", "compactionAttemptId": ATTEMPT, "execution": "direct"}, "answer"),
              *([] if diagnostic is None else [diagnostic]),
              _event("session.compaction_complete", "compact-complete", data, "compact-start")]
    return {"events.jsonl": "".join(json.dumps(event) + "\n" for event in events),
            "workspace.yaml": "id: " + SID + "\ncwd: /\n", "checkpoints/index.md": INDEX,
            "checkpoints/001-public-conversation.md": "<overview>Original public conversation.</overview>"}


def diag_state(runtime, files, ids=()):
    sdk = next(sdk for sdk, known in sdk_pin.KNOWN_RELEASES if known == runtime)
    return SdkState(sdk_version=sdk, runtime_version=runtime, sdk_session_id=SID, codec=DIAG_CODEC, files=files,
                    projected_event_ids=ids)


def refused(build):
    with pytest.raises((Problem, ValueError)) as error:
        build()
    return error.value.body["error"]["code"] if isinstance(error.value, Problem) else "invalid"


def test_a_1_0_112_manual_checkpoint_is_attested_by_its_one_direct_request_without_diagnostics():
    state = diag_state("1.0.112", manual_files())
    assert state.projected_event_ids == ()


@pytest.mark.parametrize("change", [
    {"execution": None}, {"execution": "background"}, {"clientRequestId": None}, {"clientRequestId": ""},
    {"compactionTokensUsed": None}, {"compactionTokensUsed": {"requestCount": 2}},
    {"compactionTokensUsed": {"requestCount": True}},
], ids=["no-execution", "background", "no-client-request", "empty-client-request", "no-token-use", "two-requests",
        "bool-count"])
def test_a_1_0_112_manual_checkpoint_without_that_evidence_is_unverified(change):
    assert refused(lambda: diag_state("1.0.112", manual_files(complete_data=change))) == "SDK_STATE_SOURCE_UNVERIFIED"


@pytest.mark.parametrize("runtime", [runtime for _, runtime in sdk_pin.OLDER_RELEASES])
def test_a_state_saved_before_1_0_112_still_needs_its_diagnostics(runtime):
    assert refused(lambda: diag_state(runtime, manual_files(created=runtime))) == "SDK_STATE_SOURCE_UNVERIFIED"


def test_a_session_the_real_1_0_83_runtime_created_keeps_its_1_0_112_manual_checkpoint():
    # The real runtime 1.0.83 names itself 0.0.0 in session.start; the creator is not part of the rule.
    with sqlite3.connect(FIXTURES.joinpath("ctx03-runtime-1.0.83", "artifacts.sqlite3").as_uri() + "?mode=ro",
                         uri=True) as db:
        saved = b"".join(data for (data,) in db.execute("select data from versions"))
    assert b'copilotVersion\\": \\"0.0.0\\"' in saved
    assert diag_state("1.0.112", manual_files(created="0.0.0")).projected_event_ids == ()


# A later build whose pin is runtime 1.0.999 (not known to write a manual compaction without diagnostics) and which
# still knows every release this build knows; set before any model module imports the pin.
NEXT_BUILD = """
import json, sys
from typing import Literal
from shared_context import sdk_pin
sdk_pin.OLDER_RELEASES = (*sdk_pin.OLDER_RELEASES, (sdk_pin.SDK_VERSION, sdk_pin.RUNTIME_VERSION))
sdk_pin.SDK_VERSION, sdk_pin.RUNTIME_VERSION = "9.9.9", "1.0.999"
sdk_pin.KNOWN_RELEASES = ((sdk_pin.SDK_VERSION, sdk_pin.RUNTIME_VERSION), *sdk_pin.OLDER_RELEASES)
sdk_pin.KnownSdkVersion = Literal[tuple(sdk for sdk, _ in sdk_pin.KNOWN_RELEASES)]
sdk_pin.KnownRuntimeVersion = Literal[tuple(runtime for _, runtime in sdk_pin.KNOWN_RELEASES)]
from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.behavior_sdk_state import CheckpointProfile, SdkState, local_platform
previous = SdkState.model_validate_json(sys.stdin.read())
files = SdkSessionFiles(previous.sdk_session_id, previous, checkpoint_profile=CheckpointProfile(platform=local_platform()))
files.files["events.jsonl"] += "".join(json.dumps({"type": kind, "id": kind + "-next", "timestamp": "2026-11-20T01:00:00.000Z",
    "parentId": "compact-complete", "data": {"content": "Next turn."}}) + "\\n" for kind in ("user.message", "assistant.message"))
saved = files.snapshot()
assert (saved.runtime_version, saved.projected_event_ids) == ("1.0.999", ())
SdkState.model_validate_json(saved.model_dump_json())
"""


def test_a_manual_compaction_1_0_112_wrote_is_saved_and_loaded_again_by_a_later_runtime():
    source = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"
    result = subprocess.run([sys.executable, "-c", NEXT_BUILD], input=diag_state("1.0.112", manual_files()).model_dump_json(),
                            capture_output=True, text=True, encoding="utf-8", timeout=120,
                            env={**os.environ, "PYTHONPATH": str(source)})
    assert result.returncode == 0, result.stderr


def manual_snapshot():
    """The manual profile saving the turn whose own manual compaction wrote no diagnostics."""
    import hashlib
    from shared_context.behavior_sdk_state import CompactionExecution
    files = manual_files()
    head = "".join(files["events.jsonl"].splitlines(keepends=True)[:3])
    previous = SdkState(sdk_session_id=SID, files={"events.jsonl": head, "workspace.yaml": files["workspace.yaml"]})
    session = SdkSessionFiles(SID, previous, checkpoint_profile=CheckpointProfile(platform=local_platform()))
    session.files = dict(files)
    session.compaction = CompactionExecution(
        job_id="job-compact", conversation_id="conversation", sdk_session_id=SID, profile_digest="c" * 64,
        base_state_sha256=previous.sha256, runtime_pid=1, rpc_success=True, trigger="manual",
        model_request_sha256="d" * 64, start_event_id="compact-start", complete_event_id="compact-complete",
        checkpoint_number=1, summary_sha256=hashlib.sha256(SUMMARY.encode()).hexdigest(), messages_removed=1,
        tokens_removed=0)
    return session.snapshot()


def test_the_turn_whose_1_0_112_compaction_wrote_no_diagnostics_is_saved():
    assert manual_snapshot().projected_event_ids == ()


def test_a_runtime_not_known_to_skip_the_diagnostics_cannot_save_its_own_compaction_without_them(monkeypatch):
    monkeypatch.setattr(sdk_pin, "RUNTIME_VERSION", "1.0.999")
    assert refused(manual_snapshot) == "SDK_STATE_SOURCE_UNVERIFIED"


def test_a_1_0_112_diagnostic_outside_the_window_is_still_unverified():
    files = manual_files()
    stray = _event("model.messages_snapshot", "stray", {}, "compact-complete")
    files["events.jsonl"] += json.dumps(stray) + "\n"
    assert refused(lambda: diag_state("1.0.112", files, ("stray",))) == "SDK_STATE_SOURCE_UNVERIFIED"


def test_an_uncontrolled_compaction_is_never_published_as_an_ordinary_checkpoint():
    previous = SdkState(sdk_session_id=SID, files={
        "events.jsonl": "".join(json.dumps(_event(*row)) + "\n" for row in (
            ("session.start", "session-start", {"sessionId": SID}, None),
            ("user.message", "user", {"content": "Public question."}, "session-start"))),
        "workspace.yaml": "id: " + SID + "\ncwd: /\n"})
    files = SdkSessionFiles(SID, previous)
    files.files = manual_files()
    with pytest.raises(Problem) as error:
        files.snapshot()
    assert error.value.body["error"]["code"] == "SDK_STATE_SOURCE_UNVERIFIED"


FAILED = "attempt-turn-blocking"


def failed_pair(attempt=FAILED, **change):
    """A turn-blocking compaction whose summarizer could not start, as runtime 1.0.112 writes it; it then truncates."""
    begin = _event("session.compaction_start", "failed-start", {
        "trigger": "threshold", "compactionAttemptId": attempt, "execution": "turn_blocking",
        "retainedInputMessages": 1}, "answer")
    data = {"trigger": "threshold", "compactionAttemptId": attempt, "execution": "turn_blocking", "success": False,
            "outcome": "failed", "failureReason": "context_limit", "model": "OFFLINE-model", "tokenLimit": 5000,
            "error": "compaction model call failed: Request exceeds the model's prompt budget after reserving output."}
    data.update(change)
    data = {key: value for key, value in data.items() if value is not None}
    return [begin, _event("session.compaction_complete", "failed-complete", data, "failed-start")]


def applied_pair(suffix):
    return [_event("session.compaction_start", "start-" + suffix, {"trigger": "threshold", "execution": "background"},
                   "answer"),
            _event("session.compaction_complete", "complete-" + suffix, {
                "trigger": "threshold", "execution": "background", "success": True, "summaryContent": SUMMARY},
                   "start-" + suffix)]


def pairs(events):
    from shared_context.behavior_sdk_compaction import automatic_pairs
    return automatic_pairs(events)


def test_the_auto_profile_keeps_a_1_0_112_turn_blocking_compaction_that_failed_before_summarizing():
    events = [*applied_pair("1"), *failed_pair(), *applied_pair("2")]
    assert pairs(events) == events


@pytest.mark.parametrize("change", [
    {"execution": "background"}, {"execution": "direct"}, {"execution": None}, {"outcome": "applied"},
    {"outcome": None}, {"failureReason": "timeout"}, {"failureReason": None}, {"success": None},
    {"compactionAttemptId": "another-attempt"}, {"summaryContent": SUMMARY}, {"checkpointNumber": 1},
    {"checkpointPath": "/session-state/checkpoints/001-x.md"}, {"messagesRemoved": 1},
], ids=["background", "direct", "no-execution", "applied", "no-outcome", "other-reason", "no-reason", "no-success",
        "other-attempt", "summary", "checkpoint", "checkpoint-path", "removed-messages"])
def test_any_other_failed_compaction_stays_unverified(change):
    with pytest.raises(Problem) as error:
        pairs(failed_pair(**change))
    assert error.value.body["error"]["code"] == "SDK_STATE_SOURCE_UNVERIFIED"


def test_a_failed_compaction_without_an_attempt_id_stays_unverified():
    with pytest.raises(Problem):
        pairs(failed_pair(attempt=None))


def jsonl(events):
    return "".join(json.dumps(event) + "\n" for event in events)


BASE = [_event("session.start", "session-start", {"sessionId": SID}, None),
        _event("user.message", "user", {"content": "Public question."}, "session-start"),
        _event("assistant.message", "answer", {"content": "Public answer."}, "user")]
NEXT_TURN = [_event("user.message", "user-2", {"content": "Next question."}, "failed-complete"),
             _event("assistant.message", "answer-2", {"content": "Next answer."}, "user-2")]


def auto_snapshot(saved, written_now, rewritten=None):
    """The auto profile saving after a turn that appended ``written_now`` to the predecessor that holds ``saved``, or
    whose events.jsonl ends up ``rewritten``."""
    previous = SdkState(sdk_session_id=SID, files={"events.jsonl": jsonl(BASE + saved),
                                                   "workspace.yaml": "id: " + SID + "\ncwd: /\n"})
    files = SdkSessionFiles(SID, previous, checkpoint_profile=CheckpointProfile(name=AUTO_PROFILE,
                                                                                platform=local_platform()))
    files.files["events.jsonl"] = jsonl(rewritten) if rewritten is not None else (
        files.files["events.jsonl"] + jsonl(written_now))
    return files.snapshot()


def test_a_failed_pair_1_0_112_wrote_is_still_recognised_once_a_later_runtime_runs(monkeypatch):
    monkeypatch.setattr(sdk_pin, "RUNTIME_VERSION", "1.0.999")  # a later runtime not known to write the failed pair
    state = auto_snapshot(failed_pair(), NEXT_TURN)
    assert "failed-complete" in state.files["events.jsonl"]


@pytest.mark.parametrize("rewritten", [
    [BASE[0], BASE[2], BASE[1], *failed_pair(), *NEXT_TURN], BASE + NEXT_TURN + failed_pair(),
], ids=["earlier-events-swapped", "pair-moved-after-the-turn"])
def test_a_predecessor_whose_events_were_reordered_inherits_no_failed_pair(monkeypatch, rewritten):
    monkeypatch.setattr(sdk_pin, "RUNTIME_VERSION", "1.0.999")
    with pytest.raises(Problem) as error:
        auto_snapshot(failed_pair(), [], rewritten=rewritten)
    assert error.value.body["error"]["code"] == "SDK_STATE_SOURCE_UNVERIFIED"


@pytest.mark.parametrize("saved, written_now", [
    ([], failed_pair() + NEXT_TURN), (failed_pair()[:1], failed_pair()[1:] + NEXT_TURN),
], ids=["written-this-turn", "completed-this-turn"])
def test_a_runtime_not_known_to_write_the_failed_pair_still_refuses_one_it_wrote(monkeypatch, saved, written_now):
    monkeypatch.setattr(sdk_pin, "RUNTIME_VERSION", "1.0.103")
    with pytest.raises(Problem) as error:
        auto_snapshot(saved, written_now)
    assert error.value.body["error"]["code"] == "SDK_STATE_SOURCE_UNVERIFIED"


# A build pinned to SDK 1.0.26 / runtime 1.0.103 (the image before this upgrade), set before any model module imports
# the pin (the Literal types are built at import).
OLDER_BUILD = """
import sys
from typing import Literal
from shared_context import sdk_pin
sdk_pin.SDK_VERSION, sdk_pin.RUNTIME_VERSION = "1.0.26", "1.0.103"
sdk_pin.OLDER_RELEASES = (("1.0.13", "1.0.83"),)
sdk_pin.KNOWN_RELEASES = ((sdk_pin.SDK_VERSION, sdk_pin.RUNTIME_VERSION), *sdk_pin.OLDER_RELEASES)
sdk_pin.KnownSdkVersion = Literal[tuple(sdk for sdk, _ in sdk_pin.KNOWN_RELEASES)]
sdk_pin.KnownRuntimeVersion = Literal[tuple(runtime for _, runtime in sdk_pin.KNOWN_RELEASES)]
from shared_context.behavior_sdk_state import SdkState
SdkState.model_validate_json(sys.stdin.read())
"""


@pytest.mark.parametrize("state", [
    lambda: diag_state("1.0.112", manual_files()),
    lambda: SdkState(sdk_session_id=SID, files={"events.jsonl": jsonl(BASE + failed_pair()),
                                                "workspace.yaml": "id: " + SID + "\ncwd: /\n"}),
], ids=["manual-without-diagnostics", "failed-pair"])
def test_a_build_that_only_knows_1_0_103_refuses_a_1_0_112_state_before_reading_its_events(state):
    saved = state()
    assert saved.runtime_version == "1.0.112"
    source = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"
    result = subprocess.run([sys.executable, "-c", OLDER_BUILD], input=saved.model_dump_json(), capture_output=True,
                            text=True, encoding="utf-8", timeout=120, env={**os.environ, "PYTHONPATH": str(source)})
    assert result.returncode != 0
    assert "runtime_version" in result.stderr and "literal_error" in result.stderr, result.stderr
