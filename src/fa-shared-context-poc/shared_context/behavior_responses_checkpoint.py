"""Exact Responses-only private-field projection for the versioned SDK codec."""
import json
import logging
import re
from itertools import islice

from .behavior_checkpoint_privacy import RESPONSES_PRIVATE_FIELDS, checkpoint_events, filter_private_event
from .errors import Problem

_NESTED = {"model.message": "message", "model.response": "response"}
_MESSAGE_FIELDS = {
    "role", "content", "refusal", "responses_message_status", "serverTools", "tool_calls", "phase",
    "apiCallId", "outputTokens", *RESPONSES_PRIVATE_FIELDS,
}
REASONING_FIELDS = frozenset({
    "reasoningBlocks", "reasoning_blocks", "reasoningText", "reasoning_text",
    "encryptedContent", "encrypted_content", "reasoningOpaque", "reasoning_opaque",
})


def _shape(value, *, sample=True):
    names = {dict: "object", list: "array", str: "string", int: "integer",
             float: "number", bool: "boolean", type(None): "null"}
    result = {"type": names.get(type(value), "unsupported")}
    if isinstance(value, dict):
        result["fields"] = {
            key if isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key) else "<non-schema-key>":
            names.get(type(item), "unsupported")
            for key, item in islice(value.items(), 16)
        }
        result["omitted_fields"] = max(0, len(value) - 16)
    elif isinstance(value, list):
        result["item_types"] = [names.get(type(item), "unsupported") for item in value[:8]]
        result["omitted_items"] = max(0, len(value) - 8)
        if sample:
            result["sample_shapes"] = [_shape(item, sample=False) for item in value[:2]]
    return result


def _shape_warning(location, value, event_type=None):
    known_events = {"assistant.message", "model.message", "model.response",
                    "model.model_call_success", "model.messages_snapshot"}
    logging.getLogger(__name__).warning("RESPONSES_CHECKPOINT_SHAPE_REJECTED %s", json.dumps({
        "location": location, "event_type": event_type if event_type in known_events else "unavailable",
        "shape": _shape(value),
    }, ensure_ascii=True, sort_keys=True))


def _unsupported(location, value, event_type=None):
    _shape_warning(location, value, event_type)
    raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "Responses private material has an unrecognized shape")


def _reasoning_fields(value, event_type=None):
    known = RESPONSES_PRIVATE_FIELDS | {"reasoningWireField", "reasoning_wire_field"}
    if any(any(marker in key.casefold() for marker in ("reasoning", "encrypted", "private")) and key not in known
           for key in value):
        _unsupported("reasoning.fields", value, event_type)
    for name in ("encryptedContent", "encrypted_content", "reasoningOpaque", "reasoning_opaque",
                 "reasoningText", "reasoning_text"):
        if value.get(name) is not None and not isinstance(value[name], str):
            _unsupported("reasoning.scalar", {name: value[name]}, event_type)
    for name in ("reasoningBlocks", "reasoning_blocks"):
        container = value.get(name)
        if container is None:
            continue
        if (not isinstance(container, dict) or set(container) != {"provider", "blocks"}
                or container.get("provider") != "openai-responses" or not isinstance(container.get("blocks"), list)):
            _unsupported("reasoning.container", container, event_type)
        for block in container["blocks"]:
            if (not isinstance(block, dict) or block.get("type") != "reasoning"
                    or set(block)-{"type", "id", "summary", "encrypted_content", "content", "status", "response_id"}
                    or not isinstance(block.get("summary"), list)
                    or block.get("id") is not None and not isinstance(block["id"], str)
                    or "response_id" in block and not isinstance(block["response_id"], str)
                    or block.get("status") not in (None, "completed", "in_progress", "incomplete")
                    or block.get("encrypted_content") is not None and not isinstance(block["encrypted_content"], str)):
                _unsupported("reasoning.block", block, event_type)
            content = block.get("content")
            if content is not None and not isinstance(content, list):
                _unsupported("reasoning.content", content, event_type)
            for part in content or []:
                if (not isinstance(part, dict) or set(part) != {"type", "text"}
                        or part.get("type") != "reasoning_text" or not isinstance(part.get("text"), str)):
                    _unsupported("reasoning.content_part", part, event_type)
            for part in block["summary"]:
                if (not isinstance(part, dict) or set(part) != {"type", "text"}
                        or part.get("type") != "summary_text" or not isinstance(part.get("text"), str)):
                    _unsupported("reasoning.summary_part", part, event_type)


def _blocks(value, event_type=None):
    _reasoning_fields(value, event_type)
    return [block for name in ("reasoningBlocks", "reasoning_blocks") if value.get(name) is not None
            for block in value[name]["blocks"]]


def _public_texts(blocks):
    summaries = [part["text"] for block in blocks for part in block["summary"]]
    public = set(summaries)
    public.update(separator.join(summaries) for separator in ("", "\n", "\n\n"))
    return public


def private_reasoning_values(value, *, public_summaries=(), event_type=None):
    blocks = _blocks(value, event_type)
    identifiers, private = set(), []
    for block in blocks:
        if block.get("id"):
            identifiers.add(block["id"])
        if block.get("encrypted_content"):
            private.append(block["encrypted_content"])
        private.extend(part["text"] for part in block.get("content") or [])
    public = _public_texts(blocks) | set(public_summaries)
    for name in ("reasoningText", "reasoning_text"):
        text = value.get(name)
        if text and text not in public:
            private.append(text)
    for name in ("reasoningOpaque", "reasoning_opaque"):
        opaque = value.get(name)
        if opaque and opaque not in identifiers:
            private.append(opaque)
    for name in ("encryptedContent", "encrypted_content"):
        if value.get(name):
            private.append(value[name])
    return private


def public_reasoning_summaries(events):
    public = set()
    for event in events:
        if event["type"] in _NESTED:
            _, message = _nested_message(event)
            public.update(_public_texts(_blocks(message, event["type"])))
    return public


def _nested_message(event):
    from .behavior_sdk_compaction import envelope, NESTED_KEYS
    try:
        envelope(event)
    except Problem as error:
        if error.body["error"]["code"] != "SDK_STATE_UNSUPPORTED_FORMAT":
            raise
        _shape_warning("model.event_envelope", event, event.get("type"))
        raise
    key = _NESTED[event["type"]]
    data = event["data"]
    call_fields = NESTED_KEYS["modelCall"] if key == "message" else {"model", "api_id"}
    if (set(data) != {"kind", "turn", "modelCall", key} or data.get("kind") != key
            or type(data.get("turn")) is not int or data["turn"] < 0):
        _unsupported("model.envelope", data, event["type"])
    if not isinstance(data.get("modelCall"), dict) or set(data["modelCall"]) != call_fields:
        _unsupported("model.call", data.get("modelCall"), event["type"])
    message = data[key]
    if (not isinstance(message, dict) or set(message)-_MESSAGE_FIELDS
            or message.get("role") != "assistant" or not isinstance(message.get("content"), str)
            or message.get("refusal") is not None and not isinstance(message["refusal"], str)
            or message.get("responses_message_status") not in (None, "completed", "in_progress", "incomplete")
            or message.get("phase") not in (None, "commentary", "final_answer")
            or message.get("tool_calls") is not None and not isinstance(message["tool_calls"], list)):
        _unsupported("model.message", message, event["type"])
    server = message.get("serverTools")
    if server is not None and (not isinstance(server, dict) or set(server)-{"provider", "items"}
                               or server.get("provider") != "openai-responses"):
        _unsupported("model.server_tools", server, event["type"])
    if server is not None and server.get("items", []) != []:
        _unsupported("model.server_tool_items", server["items"], event["type"])
    return key, message


def project_public_events(text):
    events = checkpoint_events(text)
    removed = []
    for event in events:
        if event["type"] in _NESTED:
            key, _ = _nested_message(event)
            target = event["data"][key]
        elif event["type"] == "assistant.message" and isinstance(event.get("data"), dict):
            target = event["data"]
        else:
            target = None
        if target is not None:
            removed.extend(private_reasoning_values(target, event_type=event["type"]))
            for field in RESPONSES_PRIVATE_FIELDS:
                if field in target:
                    value = target.pop(field)
                    if field not in REASONING_FIELDS:
                        removed.append(value)
        filter_private_event(event, removed)
    return events, removed
