"""Rejected Responses state exposes schema metadata only, never the original values."""
import json
import logging

import pytest

from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.errors import Problem

PREFIX = "RESPONSES_CHECKPOINT_SHAPE_REJECTED "


def state_event(variant):
    block = {"id":"PRIVATE_ITEM_ID","type":"reasoning","encrypted_content":"PRIVATE_ENCRYPTED",
             "summary":[{"type":"summary_text","text":"PRIVATE_SUMMARY"}]}
    data = {"content":"PRIVATE_ANSWER","reasoning_text":"PRIVATE_RAW",
            "reasoningBlocks":{"provider":"openai-responses","blocks":[block]}}
    event = {"type":"assistant.message","id":"PRIVATE_EVENT_ID","parentId":"start",
             "timestamp":"2026-09-30T12:00:00Z","data":data}
    if variant == "fields":
        data["future_private_field"] = {"secret":"PRIVATE_UNKNOWN"}
    elif variant == "scalar":
        data["reasoning_text"] = 987654321
    elif variant == "container":
        data["reasoningBlocks"]["blocks"] = "PRIVATE_NOT_AN_ARRAY"
    elif variant == "block":
        block["future_metadata"] = "PRIVATE_FUTURE"
    elif variant == "content":
        block["content"] = [{"type":"reasoning_text","text":"PRIVATE_CONTENT","extra":"PRIVATE_EXTRA"}]
    elif variant == "summary":
        block["summary"][0]["extra"] = "PRIVATE_EXTRA"
    else:
        event["type"] = "model.response"
        event["data"] = {"kind":"response","turn":1,"modelCall":{"model":"PRIVATE_MODEL","api_id":"PRIVATE_API"},
                         "response":{**data,"role":"assistant"}}
        if variant == "call":
            event["data"]["modelCall"]["unexpected"] = "PRIVATE_CALL"
        elif variant == "message":
            event["data"]["response"]["unexpected"] = "PRIVATE_MESSAGE"
        elif variant == "server-items":
            event["data"]["response"]["serverTools"] = {"provider":"openai-responses",
                                                       "items":[{"future":"PRIVATE_TOOL"}]}
        elif variant == "envelope":
            event["private-extra"] = "PRIVATE_ENVELOPE"
    return event


@pytest.mark.parametrize("variant,location", [
    ("fields","reasoning.fields"), ("scalar","reasoning.scalar"), ("container","reasoning.container"),
    ("block","reasoning.block"), ("content","reasoning.content_part"), ("summary","reasoning.summary_part"),
    ("call","model.call"), ("message","model.message"), ("server-items","model.server_tool_items"),
    ("envelope","model.event_envelope"),
])
def test_actual_snapshot_refusal_logs_bounded_types_without_payload_values(caplog, variant, location):
    event = state_event(variant)
    files = SdkSessionFiles("diagnostic-session", model_provider="openai-responses")
    original = {
        "events.jsonl": json.dumps({"type":"session.start","id":"start","data":{"sessionId":"diagnostic-session"}})
                        + "\n" + json.dumps(event) + "\n",
        "workspace.yaml": "id: diagnostic-session\n",
    }
    files.files = dict(original)
    with caplog.at_level(logging.WARNING), pytest.raises(Problem) as refused:
        files.snapshot()
    assert refused.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"
    lines = [record.getMessage() for record in caplog.records if record.getMessage().startswith(PREFIX)]
    assert len(lines) == 1
    assert len(lines[0].encode()) <= 4096
    assert "PRIVATE_" not in lines[0] and "987654321" not in lines[0]
    metadata = json.loads(lines[0][len(PREFIX):])
    assert metadata["location"] == location and metadata["event_type"] == event["type"]
    assert set(metadata) == {"location","event_type","shape"}
    assert files.files == original
    assert not files.frozen
    if variant == "call":
        assert metadata["shape"]["fields"]["unexpected"] == "string"
    elif variant == "content":
        assert metadata["shape"]["fields"]["extra"] == "string"
    elif variant == "server-items":
        assert metadata["shape"]["sample_shapes"][0]["type"] == "object"
    elif variant == "envelope":
        from shared_context.behavior_sdk_compaction import envelope
        with pytest.raises(Problem) as original_error:
            envelope(event)
        assert refused.value.status == original_error.value.status
        assert refused.value.body == original_error.value.body


def test_diagnostics_bound_field_count_and_hide_non_schema_keys(caplog):
    event = state_event("fields")
    event["data"] = {"not a schema key PRIVATE_KEY": "PRIVATE_VALUE",
                     **{f"field_{index}": "PRIVATE_LARGE_VALUE"*4 for index in range(1000)},
                     "future_private_field":"PRIVATE_END"}
    files = SdkSessionFiles("diagnostic-session", model_provider="openai-responses")
    files.files = {"events.jsonl": json.dumps({"type":"session.start","data":{"sessionId":"diagnostic-session"}})
                   + "\n" + json.dumps(event) + "\n", "workspace.yaml":"id: diagnostic-session\n"}
    with caplog.at_level(logging.WARNING), pytest.raises(Problem):
        files.snapshot()
    line = next(record.getMessage() for record in caplog.records if record.getMessage().startswith(PREFIX))
    metadata = json.loads(line[len(PREFIX):])
    assert len(metadata["shape"]["fields"]) <= 16 and metadata["shape"]["omitted_fields"] > 0
    assert "<non-schema-key>" in metadata["shape"]["fields"]
    assert "PRIVATE_" not in line and len(line.encode()) <= 4096


def test_unidentified_event_is_explicitly_unavailable_without_logging_a_guessed_type(caplog):
    from shared_context.behavior_responses_checkpoint import private_reasoning_values
    with caplog.at_level(logging.WARNING), pytest.raises(Problem) as refused:
        private_reasoning_values(state_event("fields")["data"])
    assert refused.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"
    line = next(record.getMessage() for record in caplog.records if record.getMessage().startswith(PREFIX))
    assert json.loads(line[len(PREFIX):])["event_type"] == "unavailable"
    assert "PRIVATE_" not in line


def test_diagnostics_bound_array_samples_and_long_field_names(caplog):
    event = state_event("server-items")
    item = {f"field_{index:02}_" + "x"*55: "PRIVATE_ARRAY_VALUE" for index in range(32)}
    event["data"]["response"]["serverTools"]["items"] = [item]*100
    files = SdkSessionFiles("diagnostic-session", model_provider="openai-responses")
    files.files = {"events.jsonl": json.dumps({"type":"session.start","data":{"sessionId":"diagnostic-session"}})
                   + "\n" + json.dumps(event) + "\n", "workspace.yaml":"id: diagnostic-session\n"}
    with caplog.at_level(logging.WARNING), pytest.raises(Problem):
        files.snapshot()
    line = next(record.getMessage() for record in caplog.records if record.getMessage().startswith(PREFIX))
    shape = json.loads(line[len(PREFIX):])["shape"]
    assert len(shape["sample_shapes"]) == 2 and len(shape["item_types"]) == 8
    assert shape["omitted_items"] == 92
    assert all(len(sample["fields"]) == 16 for sample in shape["sample_shapes"])
    assert "PRIVATE_" not in line and len(line.encode()) <= 4096


def test_content_diagnostic_identifies_the_rejected_part_after_valid_entries(caplog):
    event = state_event("content")
    event["data"]["reasoningBlocks"]["blocks"][0]["content"] = [
        {"type":"reasoning_text","text":"PRIVATE_FIRST"},
        {"type":"reasoning_text","text":"PRIVATE_SECOND"},
        {"type":"reasoning_text","text":"PRIVATE_THIRD","future_content_detail":"PRIVATE_EXTRA"},
    ]
    files = SdkSessionFiles("diagnostic-session", model_provider="openai-responses")
    files.files = {"events.jsonl": json.dumps({"type":"session.start","data":{"sessionId":"diagnostic-session"}})
                   + "\n" + json.dumps(event) + "\n", "workspace.yaml":"id: diagnostic-session\n"}
    original = dict(files.files)
    with caplog.at_level(logging.WARNING), pytest.raises(Problem) as refused:
        files.snapshot()
    assert refused.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"
    line = next(record.getMessage() for record in caplog.records if record.getMessage().startswith(PREFIX))
    assert "future_content_detail" in line
    assert "PRIVATE_" not in line and len(line.encode()) <= 4096
    assert files.files == original and not files.frozen


def test_valid_responses_checkpoint_does_not_emit_a_rejection(caplog):
    event = state_event("fields")
    del event["data"]["future_private_field"]
    event["id"] = "public-event"
    event["data"]["content"] = "Public answer."
    files = SdkSessionFiles("diagnostic-session", model_provider="openai-responses")
    files.files = {"events.jsonl": json.dumps({"type":"session.start","data":{"sessionId":"diagnostic-session"}})
                   + "\n" + json.dumps(event) + "\n", "workspace.yaml":"id: diagnostic-session\n"}
    original = dict(files.files)
    with caplog.at_level(logging.WARNING):
        saved = files.snapshot()
    assert not any(record.getMessage().startswith(PREFIX) for record in caplog.records)
    assert "PRIVATE_" not in saved.encoded().decode()
    assert "Public answer." in saved.files["events.jsonl"]
    assert files.files == original and files.frozen
