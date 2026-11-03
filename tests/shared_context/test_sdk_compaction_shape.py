"""The pinned checkpoint shape contract: an unknown or malformed shape is refused, never repaired or guessed."""
import copy
import json
import logging

import pytest

from shared_context.behavior_sdk_compaction import NESTED_KEYS, _raw_shape, raw_shape, validate_native_files
from shared_context.errors import Problem

UNSUPPORTED = "SDK_STATE_UNSUPPORTED_FORMAT"
INCOMPLETE = "SDK_STATE_INCOMPLETE"
STAMP = "2026-09-24T00:00:00.000Z"


def call_event():
    data = {"kind": "model_call_success", "turn": 1, "modelCallDurationMs": 3, "ttftMs": 1.5, "outputTtftMs": 1,
            "interTokenLatencyMs": 2, "rte": False, "reasoningEffort": "low", "toolCount": 0, "quotaSnapshots": {},
            "requestMessages": json.dumps([{"role": "user", "content": "q"}])}
    for key, names in NESTED_KEYS.items():
        data[key] = {name: None for name in names}
    return {"type": "model.model_call_success", "id": "call", "timestamp": STAMP, "parentId": None, "data": data}


def snapshot_event():
    return {"type": "model.messages_snapshot", "id": "snap", "timestamp": STAMP, "parentId": None,
            "data": {"kind": "messages_snapshot", "messages": [{"role": "system", "content": "s"}, {"role": "user"}]}}


def code(error):
    return error.value.body["error"]["code"]


def reshape(event, path, value):
    """Set (or, for the sentinel DELETE, remove) one value inside event["data"]."""
    node = event["data"]
    for step in path[:-1]:
        node = node[step]
    if value is DELETE:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return event


DELETE = object()


def test_a_complete_call_and_a_snapshot_are_accepted_with_nothing_unknown():
    assert _raw_shape(call_event()) == {}
    assert _raw_shape(snapshot_event()) is None


def test_every_pinned_message_role_is_accepted_in_a_snapshot():
    event = snapshot_event()
    event["data"]["messages"] = [{"role": role} for role in ("system", "assistant", "tool", "user")]
    assert _raw_shape(event) is None


@pytest.mark.parametrize("path, value, unknown", [
    (("responseUsage", "extra_counter"), 5, {"responseUsage.extra_counter": "int"}),
    (("responseChunk", "weird"), 1.5, {"responseChunk.weird": "float"}),
    (("responseChunk", "latency_checkpoint"), {"p": 1}, {}),
    (("responseChunk", "routing"), {}, {}),
    (("responseChunk", "obfuscation"), "x", {}),
    (("responseChunk", "system_fingerprint"), "fp", {}),
    (("responseChunk", "service_tier"), None, {}),
    (("responseChunk", "prompt_filter_results"), [{"prompt_index": 0, "content_filter_results": {}}], {}),
    (("responseUsage", "prompt_tokens_details"), {"cached_tokens": None, "text_tokens": 3}, {}),
    (("responseUsage", "prompt_tokens_details"), {"cached_tokens": 0}, {}),
    (("responseUsage", "completion_tokens_details"), None, {}),
    (("responseUsage", "prompt_tokens_details"), {"vendor_counter": 1},
     {"responseUsage.prompt_tokens_details.vendor_counter": "int"}),
])
def test_known_and_tolerated_provider_metadata_is_accepted_and_only_unknown_names_are_reported(path, value, unknown):
    assert _raw_shape(reshape(call_event(), path, value)) == unknown


@pytest.mark.parametrize("path, value", [
    (("modelCall", "extra"), 1),
    (("modelCall",), []), (("responseChunk",), None), (("responseUsage",), "x"),
    (("responseUsage", "prompt_tokens"), DELETE), (("modelCall", "model"), DELETE),
    (("responseUsage", "prompt_tokens_details"), []),
    (("responseUsage", "prompt_tokens_details"), {"cached_tokens": -1}),
    (("responseUsage", "prompt_tokens_details"), {"cached_tokens": True}),
    (("responseUsage", "prompt_tokens_details"), {"cached_tokens": 1.5}),
    (("responseUsage", "completion_tokens_details"), {"reasoning_tokens": "1"}),
    (("responseChunk", "system_fingerprint"), 1), (("responseChunk", "service_tier"), []),
    (("responseChunk", "obfuscation"), 1), (("responseChunk", "latency_checkpoint"), []),
    (("responseChunk", "routing"), "x"),
    (("responseChunk", "prompt_filter_results"), {}),
    (("responseChunk", "prompt_filter_results"), [1]),
    (("responseChunk", "prompt_filter_results"), [{"prompt_index": 0}]),
    (("responseChunk", "prompt_filter_results"), [{"prompt_index": 0, "content_filter_results": {}, "x": 1}]),
    (("responseChunk", "prompt_filter_results"), [{"prompt_index": -1, "content_filter_results": {}}]),
    (("responseChunk", "prompt_filter_results"), [{"prompt_index": True, "content_filter_results": {}}]),
    (("responseChunk", "prompt_filter_results"), [{"prompt_index": 0, "content_filter_results": []}]),
    (("responseChunk", "messages"), "private text"), (("responseUsage", "prompt"), 1),
    (("responseChunk", "vendor"), {"nested": {"body": 1}}),
    (("responseChunk", "vendor"), float("inf")),
    (("responseChunk", "vendor"), "x" * 70000),
    (("responseChunk", "vendor"), object()),
    (("extra",), 1), (("turn",), DELETE), (("kind",), "other"),
    (("reasoningSummary",), "concise"),
    (("turn",), True), (("modelCallDurationMs",), 1.5), (("toolCount",), "0"),
    (("ttftMs",), "1"), (("outputTtftMs",), float("inf")), (("interTokenLatencyMs",), float("nan")),
    (("rte",), 0), (("reasoningEffort",), 1), (("quotaSnapshots",), {"a": 1}),
    (("requestMessages",), []), (("requestMessages",), "{}"), (("requestMessages",), "not json"),
], ids=lambda value: "oversized-text" if isinstance(value, str) and len(value) > 1000 else None)
def test_a_malformed_or_unknown_call_shape_is_refused(path, value):
    with pytest.raises(Problem) as refused:
        _raw_shape(reshape(call_event(), path, value))
    assert (refused.value.status, code(refused)) == (409, UNSUPPORTED)


@pytest.mark.parametrize("summary", ["none", "concise", "detailed", None])
def test_a_reasoning_summary_is_accepted_only_for_the_responses_provider(summary):
    event = reshape(call_event(), ("reasoningSummary",), summary)
    assert _raw_shape(event, model_provider="openai-responses") == {}
    with pytest.raises(Problem) as refused:
        _raw_shape(event, model_provider="openai-chat")
    assert code(refused) == UNSUPPORTED


FLAGS = {"enabledFeatureFlags": ["QUEUED_COMMANDS", "SANDBOX"]}


@pytest.mark.parametrize("path, value", [
    (("customToolCallCount",), 0), (("orphanToolCallCount",), 2), (("unmatchedToolResultCount",), 0),
    (("maxOutputTokens",), 4096), (("modelDisplayName",), "gpt-4.1-mini"),
    (("requestSettings",), FLAGS), (("requestSettings",), {"enabledFeatureFlags": []}),
    (("modelCall", "request_body_bytes"), 12873), (("modelCall", "request_body_wire_bytes"), 0),
    (("modelCall", "request_body_encoding"), "identity"),
])
def test_the_newer_runtimes_call_metadata_is_accepted_with_nothing_unknown(path, value):
    assert _raw_shape(reshape(call_event(), path, value)) == {}


@pytest.mark.parametrize("path, value", [
    (("customToolCallCount",), -1), (("orphanToolCallCount",), True), (("unmatchedToolResultCount",), 1.0),
    (("maxOutputTokens",), None), (("modelDisplayName",), 1),
    (("requestSettings",), None), (("requestSettings",), {}), (("requestSettings",), {"enabledFeatureFlags": "x"}),
    (("requestSettings",), {"enabledFeatureFlags": [1]}), (("requestSettings",), FLAGS | {"verbosity": "low"}),
    (("modelCall", "request_body_bytes"), -1), (("modelCall", "request_body_wire_bytes"), "1"),
    (("modelCall", "request_body_encoding"), None),
])
def test_the_newer_runtimes_call_metadata_is_refused_when_its_shape_differs(path, value):
    with pytest.raises(Problem) as refused:
        _raw_shape(reshape(call_event(), path, value))
    assert code(refused) == UNSUPPORTED


def test_an_invalid_reasoning_summary_is_refused_even_for_the_responses_provider():
    with pytest.raises(Problem) as refused:
        _raw_shape(reshape(call_event(), ("reasoningSummary",), "verbose"), model_provider="openai-responses")
    assert code(refused) == UNSUPPORTED


@pytest.mark.parametrize("change", [
    lambda e: e["data"].update(extra=1), lambda e: e["data"].update(kind="other"),
    lambda e: e["data"].update(messages="x"), lambda e: e["data"].update(messages={}),
    lambda e: e["data"].update(messages=[1]),
    lambda e: e["data"].update(messages=[{"role": "developer"}]), lambda e: e["data"].update(messages=[{}]),
    lambda e: e["data"].pop("kind"), lambda e: e.update(type="model.other"),
    lambda e: e.update(timestamp="yesterday"), lambda e: e.update(id=""), lambda e: e.update(data=[]),
    lambda e: e.pop("parentId"),
])
def test_a_malformed_snapshot_or_envelope_is_refused(change):
    event = snapshot_event()
    change(event)
    with pytest.raises(Problem) as refused:
        _raw_shape(event)
    assert code(refused) == UNSUPPORTED


def test_a_refused_shape_and_unknown_provider_metadata_are_each_logged_once(caplog):
    caplog.set_level(logging.WARNING)
    with pytest.raises(Problem):
        raw_shape(reshape(call_event(), ("modelCall", "extra"), 1))
    assert caplog.text.count("SDK_CHECKPOINT_SHAPE_REJECTED") == 1
    caplog.clear()
    raw_shape(reshape(call_event(), ("responseUsage", "extra_counter"), 5))
    assert caplog.text.count("SDK_CHECKPOINT_PROVIDER_METADATA") == 1 and "extra_counter" in caplog.text
    caplog.clear()
    raw_shape(call_event())
    assert caplog.text == ""


def test_a_refusal_log_does_not_list_the_newer_runtimes_known_metadata_as_extra(caplog):
    caplog.set_level(logging.WARNING)
    event = reshape(reshape(call_event(), ("modelDisplayName",), 1), ("modelCall", "request_body_bytes"), 1)
    with pytest.raises(Problem):
        raw_shape(event)
    report = json.loads(caplog.text.split("SDK_CHECKPOINT_SHAPE_REJECTED ", 1)[1])
    assert report["data"]["extra"] == [] and report["provider"]["modelCall"]["extra"] == []
    assert report["data"]["types"]["modelDisplayName"] == "int"


SUMMARY = "<overview>o</overview><checkpoint_title>Inventory</checkpoint_title>"
HEADER = ("# Checkpoint History\n\nCheckpoints are listed in chronological order. "
          "Checkpoint 1 is the oldest, higher numbers are more recent.\n\n| # | Title | File |\n|---|-------|------|\n")
ROW = "| 1 | Inventory | 001-inventory.md |\n"


def native(*, start=None, summary=SUMMARY, index=HEADER + ROW, number=1, path="/session-state/checkpoints/001-inventory.md",
           body="<overview>o</overview>", workspace="id: x\n", name="checkpoints/001-inventory.md"):
    def event(kind, identity, data):
        return {"type": kind, "id": identity, "timestamp": STAMP, "parentId": None, "data": data}

    events = [event("session.start", "s", {"sessionId": "abcdef12345"} if start is None else start),
              event("session.compaction_start", "cs", {"trigger": "manual"}),
              event("session.compaction_complete", "cc", {
                  "trigger": "manual", "success": True, "summaryContent": summary, "checkpointNumber": number,
                  "checkpointPath": path, "messagesRemoved": 2, "tokensRemoved": 20})]
    files = {name: body, "checkpoints/index.md": index}
    if workspace is not None:
        files["workspace.yaml"] = workspace
    return files, events


def test_matching_checkpoint_files_are_accepted():
    files, events = native()
    assert validate_native_files(files, events) is None


FALLBACK_SUMMARY = "<overview>o</overview><checkpoint_title>Something else</checkpoint_title>"


@pytest.mark.parametrize("start, title, file", [
    ({"sessionId": "abcdef12345"}, "Session abcdef12", "001-session-abcdef12.md"),
    ({"session_id": "abcdef12345"}, "Session abcdef12", "001-session-abcdef12.md"),
    ({"sessionId": "ABCDEF12xyz"}, "Session ABCDEF12", "001-session-abcdef12.md"),
    ({"sessionId": "abc"}, "Session abc", "001-session-abc.md"),
    ({}, "Session response", "001-session-response.md"),
    ({"sessionId": "abcdef12345"}, "Session response", "001-session-response.md"),
    ({"sessionId": "ab cd"}, "Session response", "001-session-response.md"),
    ({"sessionId": 5}, "Session response", "001-session-response.md"),
])
def test_the_responses_provider_may_keep_the_runtimes_fallback_title(start, title, file):
    files, events = native(start=start, summary=FALLBACK_SUMMARY, index=HEADER + f"| 1 | {title} | {file} |\n",
                           name="checkpoints/" + file, path="/session-state/checkpoints/" + file)
    assert validate_native_files(files, events, model_provider="openai-responses") is None
    with pytest.raises(Problem) as refused:
        validate_native_files(files, events, model_provider="openai-chat")
    assert code(refused) == INCOMPLETE


@pytest.mark.parametrize("start, title, file", [
    ({"sessionId": "abcdef12345"}, "Session abcdef1", "001-session-abcdef1.md"),
    ({"sessionId": "abcdef12345"}, "Session abcdef12", "001-session-ABCDEF12.md"),
    ({"sessionId": "other"}, "Session abcdef12", "001-session-abcdef12.md"),
    ({"sessionId": "abcdef12345"}, "Session response", "001-other.md"),
])
def test_a_fallback_title_that_does_not_match_the_start_event_is_refused_for_every_provider(start, title, file):
    files, events = native(start=start, summary=FALLBACK_SUMMARY, index=HEADER + f"| 1 | {title} | {file} |\n",
                           name="checkpoints/" + file, path="/session-state/checkpoints/" + file)
    for provider in ("openai-responses", None):
        with pytest.raises(Problem) as refused:
            validate_native_files(files, events, model_provider=provider)
        assert code(refused) == INCOMPLETE


@pytest.mark.parametrize("start", ["text", ["sessionId"], None, "missing"])
def test_a_session_start_without_object_data_is_refused_for_every_provider(start):
    files, events = native()
    if start == "missing":
        del events[0]["data"]
    else:
        events[0]["data"] = start
    for provider in ("openai-responses", None):
        with pytest.raises(Problem) as refused:
            validate_native_files(files, events, model_provider=provider)
        assert code(refused) == UNSUPPORTED


def test_two_session_start_events_leave_only_the_generic_fallback_title():
    files, events = native(summary=FALLBACK_SUMMARY, index=HEADER + "| 1 | Session abcdef12 | 001-session-abcdef12.md |\n",
                           name="checkpoints/001-session-abcdef12.md", path="/session-state/checkpoints/001-session-abcdef12.md")
    events.append(copy.deepcopy(events[0]) | {"id": "s2"})
    with pytest.raises(Problem) as refused:
        validate_native_files(files, events, model_provider="openai-responses")
    assert code(refused) == INCOMPLETE


@pytest.mark.parametrize("case", [
    "path_missing", "path_not_text", "path_outside", "name_blank", "workspace_missing", "workspace_blank",
    "header_wrong", "no_rows", "two_rows", "row_malformed", "row_zero_number", "number_differs", "title_blank",
    "file_differs", "file_prefix", "not_markdown", "no_title", "two_titles", "title_differs", "body_differs",
])
def test_checkpoint_files_that_differ_from_the_compaction_event_are_incomplete(case):
    kwargs = {}
    if case == "path_missing":
        kwargs["path"] = None
    elif case == "path_not_text":
        kwargs["path"] = 5
    elif case == "path_outside":
        kwargs["path"] = "/session-state/other/001-inventory.md"
    elif case == "name_blank":
        kwargs["body"] = "  \n"
    elif case == "workspace_missing":
        kwargs["workspace"] = None
    elif case == "workspace_blank":
        kwargs["workspace"] = " "
    elif case == "header_wrong":
        kwargs["index"] = "# Checkpoint History\n" + ROW
    elif case == "no_rows":
        kwargs["index"] = HEADER
    elif case == "two_rows":
        kwargs["index"] = HEADER + ROW + "| 2 | Other | 002-other.md |\n"
    elif case == "row_malformed":
        kwargs["index"] = HEADER + "| 1 | Inventory |\n"
    elif case == "row_zero_number":
        kwargs["index"] = HEADER + "| 01 | Inventory | 001-inventory.md |\n"
    elif case == "number_differs":
        kwargs["index"] = HEADER + "| 2 | Inventory | 001-inventory.md |\n"
    elif case == "title_blank":
        kwargs["index"] = HEADER + "| 1 |   | 001-inventory.md |\n"
    elif case == "file_differs":
        kwargs["index"] = HEADER + "| 1 | Inventory | 001-other.md |\n"
    elif case == "file_prefix":
        kwargs.update(index=HEADER + "| 1 | Inventory | 002-inventory.md |\n", path="/session-state/checkpoints/002-inventory.md",
                      name="checkpoints/002-inventory.md")
    elif case == "not_markdown":
        kwargs.update(index=HEADER + "| 1 | Inventory | 001-inventory.txt |\n", path="/session-state/checkpoints/001-inventory.txt",
                      name="checkpoints/001-inventory.txt")
    elif case == "no_title":
        kwargs["summary"] = "<overview>o</overview>"
    elif case == "two_titles":
        kwargs["summary"] = SUMMARY + "<checkpoint_title>Inventory</checkpoint_title>"
    elif case == "title_differs":
        kwargs["index"] = HEADER + "| 1 | Other | 001-inventory.md |\n"
    elif case == "body_differs":
        kwargs["body"] = "<overview>different</overview>"
    files, events = native(**kwargs)
    with pytest.raises(Problem) as refused:
        validate_native_files(files, events)
    assert (refused.value.status, code(refused)) == (409, INCOMPLETE)


def test_a_title_with_surrounding_spaces_matches_its_index_row():
    files, events = native(summary="<overview>o</overview><checkpoint_title>  Inventory </checkpoint_title>")
    assert validate_native_files(files, events) is None
