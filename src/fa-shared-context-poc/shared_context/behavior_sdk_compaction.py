"""Pinned diagnostics projection and lineage checks; no SDK/runtime dependency."""
import copy
import hashlib
import json
import logging
import math
import re
from datetime import datetime

from .behavior_checkpoint_privacy import (
    PRIVATE_FIELDS, RESPONSES_PRIVATE_FIELDS, PRIVATE_EVENTS, CONTENT_FIELDS, public_events,
    validate_checkpoint_files, reject_private_copies, _reject_private_structures,
    checkpoint_events, jsonl_lines, structure_nodes,
)
from .errors import Problem

TARGETS = frozenset({"model.model_call_success", "model.messages_snapshot"})
COMPACTION_EVENTS = ("session.compaction_start", "session.compaction_complete")
# Triggers the auto profile never accepts as the runtime's own threshold compaction. ``model_switch`` comes only from the
# experimental ``session.model.switchTo`` preflight, which FA never calls; CTX-03 does not enable model-switch compaction.
NOT_AUTOMATIC_TRIGGERS = ("", "manual", "model_switch")
ENVELOPE = frozenset({"type", "id", "timestamp", "parentId", "data"})
CALL_KEYS = frozenset({
    "kind", "turn", "modelCallDurationMs", "ttftMs", "outputTtftMs", "interTokenLatencyMs",
    "modelCall", "responseChunk", "responseUsage", "requestMessages", "quotaSnapshots",
    "rte", "reasoningEffort", "toolCount",
})
NESTED_KEYS = {
    "modelCall": {"model", "api_id", "api_endpoint", "request_id", "client_request_id",
                  "service_request_id", "rte", "initiator", "transport"},
    "responseChunk": {"usage", "object", "id", "choices", "created", "model"},
    "responseUsage": {"prompt_tokens", "completion_tokens", "total_tokens",
                      "prompt_cache_frontier_source", "prompt_cache_details_reported"},
}
CHUNK_METADATA = frozenset({
    "system_fingerprint", "service_tier", "prompt_filter_results",
    "latency_checkpoint", "routing", "obfuscation",
})
OBJECT_METADATA = frozenset({"latency_checkpoint", "routing"})
USAGE_DETAILS = frozenset({"prompt_tokens_details", "completion_tokens_details"})
TOKEN_COUNTERS = frozenset({
    "cached_tokens", "cache_write_tokens", "audio_tokens", "text_tokens", "image_tokens",
    "reasoning_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens",
})
MAX_PROVIDER_METADATA_BYTES = 64 * 1024
MAX_PROVIDER_METADATA_DEPTH = 8
METADATA_CONTENT_FIELDS = frozenset(re.sub(r"[_-]", "", name).casefold() for name in (
    *PRIVATE_FIELDS, *CONTENT_FIELDS, "text", "message", "messages", "requestMessages",
    "tool_calls", "tool_call", "function_call", "function_calls", "arguments", "args",
    "prompt", "prompts", "instruction", "instructions", "body", "payload", "reasoning",
))


def unsupported():
    raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "checkpoint is outside the pinned diagnostics contract")


def unverified():
    raise Problem(409, "SDK_STATE_SOURCE_UNVERIFIED", "controlled compaction source could not be uniquely verified")


def incomplete():
    raise Problem(409, "SDK_STATE_INCOMPLETE", "required native checkpoint material differs or is missing")


def _object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        unsupported()
    return value


def _constant(_):
    unsupported()


def parse_json(text):
    try:
        return json.loads(text, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, TypeError, RecursionError):
        unsupported()


def events_from(files):
    text = files.get("events.jsonl")
    if not isinstance(text, str) or not text.strip():
        incomplete()
    result = checkpoint_events(text, parser=parse_json)
    if not all(isinstance(event, dict) and isinstance(event.get("type"), str) for event in result):
        unsupported()
    return result


def envelope(event):
    if (set(event) != ENVELOPE or any(not isinstance(event[k], str) or not event[k]
                                     for k in ("type", "id", "timestamp"))
            or event["parentId"] is not None and not isinstance(event["parentId"], str)
            or not isinstance(event["data"], dict)
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", event["timestamp"]) is None):
        unsupported()
    try:
        datetime.fromisoformat(event["timestamp"])
    except ValueError:
        unsupported()
    return {key: value for key, value in event.items() if key != "data"}


def _content_field(name):
    return re.sub(r"[_-]", "", name.strip()).casefold() in METADATA_CONTENT_FIELDS


def _provider_metadata(values):
    pending = []
    for path, value in values.items():
        if any(_content_field(name) for name in path.split(".")):
            unsupported()
        pending.append((value, 0))
    while pending:
        value, depth = pending.pop()
        if depth > MAX_PROVIDER_METADATA_DEPTH:
            unsupported()
        if isinstance(value, dict):
            if any(not isinstance(key, str) or _content_field(key) for key in value):
                unsupported()
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
        elif (value is not None and type(value) not in (str, int, float, bool)
              or type(value) is float and not math.isfinite(value)):
            unsupported()
    try:
        size = len(json.dumps(values, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    except (ValueError, TypeError, RecursionError):
        unsupported()
    if size > MAX_PROVIDER_METADATA_BYTES:
        unsupported()


def _shape_warning(message, event, unknown=None):
    def safe_key(key):
        return key if isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", key) else "<non-schema-key>"

    def names(keys):
        return sorted({safe_key(key) for key in keys})[:32]

    def describe(value, required, optional=()):
        if not isinstance(value, dict):
            return {"container_type": type(value).__name__, "missing": names(required), "extra": []}
        keys = list(value)
        return {
            "missing": names(set(required) - value.keys()),
            "extra": names(value.keys() - set(required) - set(optional)),
            "types": {safe_key(key): type(value[key]).__name__ for key in keys[:32]},
            "omitted_keys": max(0, len(keys) - 32),
        }

    kind = event.get("type") if isinstance(event, dict) else None
    kind = kind if isinstance(kind, str) and kind in TARGETS else "unknown"
    data = event.get("data") if isinstance(event, dict) else None
    report = {
        "event_type": kind, "unknown_metadata_keys": names(unknown or {}),
        "unknown_metadata_types": {safe_key(key): value for key, value in list((unknown or {}).items())[:32]},
        "envelope": describe(event, ENVELOPE),
        "data": describe(data, CALL_KEYS if kind == "model.model_call_success" else {"kind", "messages"}),
    }
    if kind == "model.model_call_success" and isinstance(data, dict):
        report["provider"] = {
            name: describe(data.get(name), required,
                           CHUNK_METADATA if name == "responseChunk" else USAGE_DETAILS if name == "responseUsage" else ())
            for name, required in NESTED_KEYS.items()
        }
    logging.getLogger(__name__).warning("%s %s", message, json.dumps(report, ensure_ascii=True, sort_keys=True))


def _provider_objects(data):
    metadata, unknown = {}, {}
    for name, required in NESTED_KEYS.items():
        value = data[name]
        if not isinstance(value, dict) or not required <= value.keys():
            unsupported()
        extras = value.keys() - required
        if name == "modelCall" and extras:
            unsupported()
        if name == "responseChunk":
            for key in extras - {"system_fingerprint", "service_tier", "prompt_filter_results"}:
                metadata[name + "." + key] = value[key]
                if key not in CHUNK_METADATA:
                    unknown[name + "." + key] = type(value[key]).__name__
        if name == "responseUsage":
            for key in extras:
                item = value[key]
                if key not in USAGE_DETAILS:
                    metadata[name + "." + key] = item
                    unknown[name + "." + key] = type(item).__name__
                elif item is not None:
                    if not isinstance(item, dict):
                        unsupported()
                    for counter, count in item.items():
                        if counter in TOKEN_COUNTERS:
                            if count is not None and (type(count) is not int or count < 0):
                                unsupported()
                        else:
                            path = name + "." + key + "." + counter
                            metadata[path] = count
                            unknown[path] = type(count).__name__
    chunk = data["responseChunk"]
    for key in ("system_fingerprint", "service_tier", "obfuscation"):
        if chunk.get(key) is not None and not isinstance(chunk[key], str):
            unsupported()
    for key in OBJECT_METADATA:
        if chunk.get(key) is not None and not isinstance(chunk[key], dict):
            unsupported()
    filters = chunk.get("prompt_filter_results")
    if filters is not None and (
        not isinstance(filters, list) or any(
            not isinstance(item, dict) or set(item) != {"prompt_index", "content_filter_results"}
            or type(item["prompt_index"]) is not int or item["prompt_index"] < 0
            or not isinstance(item["content_filter_results"], dict) for item in filters)
    ):
        unsupported()
    _provider_metadata(metadata)
    return unknown


def raw_shape(event, *, model_provider=None):
    try:
        unknown = _raw_shape(event, model_provider=model_provider)
    except Problem as error:
        if error.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT":
            _shape_warning("SDK_CHECKPOINT_SHAPE_REJECTED", event)
        raise
    if unknown:
        _shape_warning("SDK_CHECKPOINT_PROVIDER_METADATA", event, unknown)


def _raw_shape(event, *, model_provider=None):
    envelope(event)
    data = event["data"]
    if event["type"] == "model.messages_snapshot":
        if (set(data) != {"kind", "messages"} or data["kind"] != "messages_snapshot"
                or not isinstance(data["messages"], list)
                or any(not isinstance(m, dict) or m.get("role") not in {"system", "assistant", "tool", "user"}
                       for m in data["messages"])):
            unsupported()
    elif event["type"] == "model.model_call_success":
        optional = {"reasoningSummary"} if model_provider == "openai-responses" else set()
        if (not CALL_KEYS <= data.keys() or data.keys()-CALL_KEYS-optional
                or data.get("reasoningSummary") not in (None, "none", "concise", "detailed")
                or data["kind"] != "model_call_success"
                or any(type(data[key]) is not int for key in ("turn", "modelCallDurationMs", "toolCount"))
                or any(type(data[key]) not in (int, float) or not math.isfinite(data[key])
                       for key in ("ttftMs", "outputTtftMs", "interTokenLatencyMs"))
                or type(data["rte"]) is not bool or not isinstance(data["reasoningEffort"], str)
                or data["quotaSnapshots"] != {} or not isinstance(data["requestMessages"], str)
                or not isinstance(parse_json(data["requestMessages"]), list)):
            unsupported()
        return _provider_objects(data)
    else:
        unsupported()


def compaction_pair(events):
    found = [event for event in events if event["type"].startswith("session.compaction")]
    if len(found) != 2 or tuple(event["type"] for event in found) != COMPACTION_EVENTS:
        unverified()
    start, complete = found
    for event in found:
        data = event["data"]
        if data.get("trigger") != "manual":
            unverified()
        if data.get("customInstructions") not in (None, ""):
            raise Problem(409, "SDK_STATE_PRIVATE_MATERIAL", "custom compaction instructions are not supported")
    data = complete["data"]
    if (start["id"] == complete["id"] or data.get("success") is not True
            or type(data.get("checkpointNumber")) is not int or data["checkpointNumber"] <= 0
            or not isinstance(data.get("summaryContent"), str) or not data["summaryContent"].strip()
            or any(type(data.get(key)) is not int for key in ("messagesRemoved", "tokensRemoved"))
            or data["messagesRemoved"] < 0):
        unverified()
    return start, complete


def automatic_pairs(events):
    """auto profile: the runtime's own compactions, each a complete start/complete pair with its summary."""
    found = [event for event in events if event["type"].startswith("session.compaction")]
    if len(found) % 2 or any(event["type"] != COMPACTION_EVENTS[index % 2] for index, event in enumerate(found)):
        unverified()
    for start, complete in zip(found[::2], found[1::2]):
        for event in (start, complete):
            envelope(event)
            if not isinstance(event["data"].get("trigger"), str) or event["data"]["trigger"] in NOT_AUTOMATIC_TRIGGERS:
                unverified()
            if event["data"].get("customInstructions") not in (None, ""):
                raise Problem(409, "SDK_STATE_PRIVATE_MATERIAL", "custom compaction instructions are not supported")
        data = complete["data"]
        if (start["id"] == complete["id"] or start["data"]["trigger"] != data["trigger"]
                or data.get("success") is not True
                or not isinstance(data.get("summaryContent"), str) or not data["summaryContent"].strip()):
            unverified()
    return found


def native_pair(events):
    pair = compaction_pair(events)
    for event in pair:
        envelope(event)
    return pair


def validate_execution(events, execution):
    start, complete = native_pair(events)
    data = complete["data"]
    if ((start["id"], complete["id"], data["checkpointNumber"], data["messagesRemoved"], data["tokensRemoved"],
         hashlib.sha256(data["summaryContent"].encode("utf-8")).hexdigest())
            != (execution.start_event_id, execution.complete_event_id, execution.checkpoint_number,
                execution.messages_removed, execution.tokens_removed, execution.summary_sha256)):
        unverified()


def validate_inherited(events, previous):
    """The caller must obtain previous from its published, scope-checked authority."""
    prior = events_from(previous.files)
    if events[:len(prior)] != prior:
        unverified()
    return {event["id"]: event for event in prior if event["type"] in TARGETS}


def _private_values(value, *, model_provider=None, public_summaries=()):
    fields = RESPONSES_PRIVATE_FIELDS if model_provider == "openai-responses" else PRIVATE_FIELDS
    for node in structure_nodes(value):
        if isinstance(node, dict):
            if model_provider == "openai-responses":
                from .behavior_responses_checkpoint import REASONING_FIELDS, private_reasoning_values
                if REASONING_FIELDS & node.keys():
                    yield from private_reasoning_values(node, public_summaries=public_summaries)
            for key, item in node.items():
                if model_provider == "openai-responses" and key in REASONING_FIELDS:
                    continue
                if key in fields or node.get("role") == "system" and key == "content":
                    yield item


def _private_structure(value):
    _reject_private_structures(value)
    for node in structure_nodes(value):
        if not isinstance(node, dict):
            continue
        kind = node.get("type")
        private_event = isinstance(kind, str) and kind in PRIVATE_EVENTS
        data = node.get("data", {})
        if private_event and not isinstance(data, dict):
            unsupported()
        if (node.get("role") == "system" and node.get("content") not in (None, "", [], {})
                or private_event and any(data.get(key) not in (None, "") for key in CONTENT_FIELDS)):
            raise Problem(409, "SDK_STATE_PRIVATE_MATERIAL", "private message structure remains in checkpoint material")


def _private_sidecar(name, text):
    try:
        value = json.loads(text)
    except RecursionError:
        unsupported()
    except ValueError:
        value = None
    _private_structure(value)
    if not name.endswith((".yaml", ".yml")):
        return
    scalar_indent = None
    for line in jsonl_lines(text):
        indent = len(line) - len(line.lstrip(" "))
        if scalar_indent is not None:
            if not line.strip(" \t\r") or indent > scalar_indent:
                continue
            scalar_indent = None
        match = re.fullmatch(r' *["\']?(\w+)["\']? *: *(.*?) *', line)
        if match:
            if match[1] in PRIVATE_FIELDS and match[2] not in {"", "null", "~", '""', "''", "[]", "{}"}:
                raise Problem(409, "SDK_STATE_PRIVATE_MATERIAL", "private sidecar field remains in checkpoint material")
            if re.fullmatch(r"[|>][-+0-9]*", match[2]):
                scalar_indent = indent


def validate_projected_state(state):
    events = events_from(state.files)
    targets = [event for event in events if event["type"] in TARGETS]
    ids = tuple(event.get("id") for event in targets)
    if not ids or ids != state.projected_event_ids or len(set(ids)) != len(ids):
        unverified()
    event_ids = [event.get("id") for event in events]
    if any(not isinstance(identity, str) or not identity for identity in event_ids) or len(set(event_ids)) != len(event_ids):
        unverified()
    for event in targets:
        envelope(event)
        if event["data"] != {}:
            raise Problem(409, "SDK_STATE_PRIVATE_MATERIAL", "diagnostic data must be projected before upload")
    start, complete = native_pair(events)
    first, last = events.index(start), events.index(complete)
    if any(not first < index < last for index, event in enumerate(events) if event["type"] in TARGETS):
        unverified()
    validate_native_files(state.files, events, model_provider=state.model_provider)
    public_sidecars = {complete["data"]["checkpointPath"].removeprefix("/session-state/"), "checkpoints/index.md"}
    validate_checkpoint_files(state.files, projected_event_ids=set(ids), public_sidecars=public_sidecars,
                              model_provider=state.model_provider)
    _private_structure(events)
    for name, text in state.files.items():
        if name != "events.jsonl" and name not in public_sidecars:
            _private_sidecar(name, text)


def validate_native_files(files, events, *, model_provider=None):
    from .behavior_sdk_state import checked_path
    _, complete = native_pair(events)
    path = complete["data"].get("checkpointPath")
    if not isinstance(path, str) or not path.startswith("/session-state/checkpoints/"):
        incomplete()
    name = checked_path(path.removeprefix("/session-state/"))
    if not files.get(name, "").strip() or not files.get("workspace.yaml", "").strip():
        incomplete()
    header = ("# Checkpoint History\n\nCheckpoints are listed in chronological order. "
              "Checkpoint 1 is the oldest, higher numbers are more recent.\n\n"
              "| # | Title | File |\n|---|-------|------|\n")
    index = files.get("checkpoints/index.md", "")
    if not index.startswith(header):
        incomplete()
    rows = jsonl_lines(index[len(header):])
    if len(rows) != 1:
        incomplete()
    match = re.fullmatch(r"\| ([1-9][0-9]*) \| ([^|\r\n]+) \| ([^|/\r\n]+) \|", rows[0])
    number = complete["data"]["checkpointNumber"]
    if (match is None or int(match[1]) != number or not match[2].strip()
            or "checkpoints/" + match[3] != name or not match[3].startswith(f"{number:03d}-")
            or not name.endswith(".md")):
        incomplete()
    summary = complete["data"]["summaryContent"]
    titles = list(re.finditer(r"<checkpoint_title>(.*?)</checkpoint_title>", summary, re.DOTALL))
    # Runtime 1.0.83's fallback title uses the first eight SDK-session characters, not the summary title.
    starts = [event for event in events if event["type"] == "session.start"]
    start = starts[0]["data"] if len(starts) == 1 else {}
    session_id = start.get("sessionId", start.get("session_id"))
    prefix = session_id[:8] if isinstance(session_id, str) else ""
    fallback_titles = {("Session response", f"{number:03d}-session-response.md")}
    if re.fullmatch(r"[A-Za-z0-9-]{1,8}", prefix):
        fallback_titles.add((f"Session {prefix}", f"{number:03d}-session-{prefix.lower()}.md"))
    responses_title = model_provider == "openai-responses" and (match[2], match[3]) in fallback_titles
    if len(titles) != 1 or titles[0][1].strip() != match[2] and not responses_title:
        incomplete()
    body = (summary[:titles[0].start()] + summary[titles[0].end():]).strip()
    if files[name] != body:
        incomplete()


def project_files(original, *, previous, forbidden_values=(), directories=(), model_provider=None):
    raw = events_from(original)
    events, removed = public_events(original["events.jsonl"], model_provider=model_provider)
    public_summaries = ()
    if model_provider == "openai-responses":
        from .behavior_responses_checkpoint import public_reasoning_summaries
        start, complete = native_pair(raw)
        public_summaries = public_reasoning_summaries(raw[raw.index(start):raw.index(complete) + 1])
    inherited = {} if previous is None else validate_inherited(events, previous)
    ids = []
    for original_event, event in zip(raw, events, strict=True):
        if event["type"] not in TARGETS:
            continue
        identity = original_event.get("id")
        if identity in inherited:
            if original_event != inherited[identity] or original_event["data"] != {}:
                unverified()
        else:
            raw_shape(original_event, model_provider=model_provider)
            removed.extend(_private_values(original_event["data"], model_provider=model_provider,
                                           public_summaries=public_summaries))
            if original_event["type"] == "model.model_call_success":
                removed.extend(_private_values(parse_json(original_event["data"]["requestMessages"]), model_provider=model_provider,
                                               public_summaries=public_summaries))
        event["data"] = {}
        ids.append(identity)
    if len(set(ids)) != len(ids) or not set(inherited) <= set(ids):
        unverified()
    files = copy.copy(original)
    files["events.jsonl"] = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)
    reject_private_copies(files, removed, forbidden_values, paths=(*files, *directories), nested_json=True)
    return files, tuple(ids)
