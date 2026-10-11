"""The existing public-only event policy, plus refusal of unapproved raw sidecars."""
import copy
import json

from .errors import Problem

PRIVATE_FIELDS = frozenset({
    "reasoning_text", "reasoningText", "reasoning_blocks", "reasoningBlocks",
    "system_message", "systemMessage", "system_prompt", "systemPrompt",
})
RESPONSES_PRIVATE_FIELDS = PRIVATE_FIELDS | {
    "encrypted_content", "encryptedContent", "reasoning_opaque", "reasoningOpaque",
}
PRIVATE_EVENTS = frozenset({"assistant.reasoning", "assistant.reasoning_delta", "system.message"})
CONTENT_FIELDS = ("content", "delta_content", "deltaContent")
MAX_STRUCTURE_DEPTH = 128


def json_string_values(text):
    """Scan scalar fragments once; invalid prefixes never restart at earlier quotes."""
    start, escaped, hex_left = None, False, 0
    for index, char in enumerate(text):
        if start is None:
            if char == '"':
                start = index
        elif hex_left:
            if char in "0123456789abcdefABCDEF":
                hex_left -= 1
            else:
                start = index if char == '"' else None
                hex_left = 0
        elif escaped:
            escaped = False
            if char == "u":
                hex_left = 4
            elif char not in '"\\/bfnrt':
                start = None
        elif char == '"':
            yield json.loads(text[start:index + 1])
            start = None
        elif char == "\\":
            escaped = True
        elif char < " ":
            start = None


def jsonl_lines(text):
    """JSONL uses LF/CRLF, not the Unicode separators allowed inside JSON strings."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


def checkpoint_events(text, *, parser=json.loads):
    try:
        events = [parser(line) for line in jsonl_lines(text)]
    except RecursionError:
        raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "checkpoint JSON structure is too deep") from None
    if not all(isinstance(event, dict) and isinstance(event.get("type"), str) for event in events):
        raise ValueError("SDK JSONL records must be typed event objects")
    for _ in structure_nodes(events):
        pass
    return events


def structure_nodes(value):
    pending = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > MAX_STRUCTURE_DEPTH:
            raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "checkpoint structure exceeds its supported depth")
        yield current
        children = current.values() if isinstance(current, dict) else current if isinstance(current, list) else ()
        pending.extend((item, depth + 1) for item in children)


def public_events(text, *, model_provider=None):
    """Match the legacy transformation; do not remove/reparent SDK event records."""
    if model_provider == "openai-responses":
        from .behavior_responses_checkpoint import project_public_events
        return project_public_events(text)
    events = checkpoint_events(text)
    removed = []
    for event in events:
        filter_private_event(event, removed)
    return events, removed


def filter_private_event(event, removed):
    data = event.get("data")
    if not isinstance(data, dict):
        return
    for field in PRIVATE_FIELDS:
        if field in data:
            removed.append(data.pop(field))
    if event.get("type") in PRIVATE_EVENTS:
        for field in CONTENT_FIELDS:
            if field in data:
                removed.append(data[field])
                data[field] = ""


def _reject_private_structures(value, *, model_provider=None):
    fields = RESPONSES_PRIVATE_FIELDS if model_provider == "openai-responses" else PRIVATE_FIELDS
    for node in structure_nodes(value):
        if isinstance(node, dict) and any(
                key in fields and item not in (None, "", [], {}) for key, item in node.items()):
            raise Problem(409, "SDK_STATE_PRIVATE_MATERIAL", "checkpoint contains unapproved private fields")


def validate_checkpoint_files(files, *, projected_event_ids=None, public_sidecars=(), model_provider=None):
    events = checkpoint_events(files["events.jsonl"])
    filtered, _ = public_events(files["events.jsonl"], model_provider=model_provider)
    if events != filtered:
        raise Problem(409, "SDK_STATE_PRIVATE_MATERIAL", "checkpoint private event filtering is required")
    for event in events:
        # These native compaction records contain model request/response copies.
        # Their safe transformation is not an approved recovery contract.
        if event.get("type") in {"model.model_call_success", "model.messages_snapshot"}:
            if (projected_event_ids is None or event.get("id") not in projected_event_ids or event.get("data") != {}):
                raise Problem(409, "SDK_STATE_PRIVATE_MATERIAL", "raw model snapshot persistence is not approved")
        _reject_private_structures(event, model_provider=model_provider)
    for name, text in files.items():
        if name == "events.jsonl" or name in public_sidecars:
            continue
        try:
            value = json.loads(text)
        except RecursionError:
            raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "checkpoint sidecar structure is too deep") from None
        except (ValueError, TypeError):
            continue
        _reject_private_structures(value, model_provider=model_provider)


def reject_private_copies(files, removed, forbidden_values=(), *, paths=(), nested_json=False):
    needles = {value for value in forbidden_values if isinstance(value, str) and value}
    def private_text(value):
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                if item:
                    yield item
            elif isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, dict):
                pending.extend(value for key, value in item.items() if key not in {"type", "id"})
    for value in removed:
        needles.update(private_text(value))
    variants = {form for value in needles for form in (
        value, json.dumps(value, ensure_ascii=False)[1:-1], json.dumps(value, ensure_ascii=True)[1:-1])}
    if not variants:
        return
    pending, seen = list((*files.values(), *paths)), set()
    while pending:
        text = pending.pop()
        if text in seen:
            continue
        seen.add(text)
        if any(needle in text for needle in variants):
            raise Problem(409, "SDK_STATE_PRIVATE_MATERIAL", "private material remains outside supported event fields")
        if nested_json:
            # Decode only quoted scalar escapes, never user-authored JSON structures.
            pending.extend(json_string_values(text))


def checkpoint_files(original, forbidden_values=(), *, model_provider=None):
    files = copy.copy(original)
    events, removed = public_events(files["events.jsonl"], model_provider=model_provider)
    files["events.jsonl"] = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)
    validate_checkpoint_files(files, model_provider=model_provider)
    reject_private_copies(files, removed, forbidden_values)
    return files
