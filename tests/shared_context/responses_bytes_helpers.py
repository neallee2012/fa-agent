"""Synthetic Responses SSE bytes, moved verbatim from test_instruction_responses_native.py so native tests do not
import a test module that uses the legacy `assistant` fixture."""
import json


SUMMARY = "The public summary says to check the original source."
OPAQUE = "PRIVATE_ENCRYPTED_REASONING_CANARY"


def response_bytes(ordinal, *, text=None, tool=None, public_summary=SUMMARY, observed_reasoning=False, arguments=None):
    rid, reason_id, message_id = f"resp_offline_{ordinal}", f"rs_offline_{ordinal}", f"msg_offline_{ordinal}"
    reason = {"id": reason_id, "type": "reasoning", "status": "completed", "encrypted_content": OPAQUE,
              "summary": [{"type": "summary_text", "text": public_summary}]}
    if observed_reasoning:
        # Field set seen in the 2026-09-30 Hosted rejection log (values are synthetic).
        reason = {"id": reason_id, "type": "reasoning", "encrypted_content": OPAQUE, "content": [],
                  "response_id": rid, "summary": reason["summary"]}
    part = {"type": "output_text", "text": text or f"Original public answer {ordinal}.", "annotations": []}
    message = {"id": message_id, "type": "message", "role": "assistant", "status": "completed", "content": [part]}
    if tool is not None:
        encoded_arguments = json.dumps({} if arguments is None else arguments)
        message = {"id": f"fc_{ordinal}", "type": "function_call", "call_id": f"call_{ordinal}",
                   "name": tool, "arguments": encoded_arguments, "status": "completed"}
    response = {"id": rid, "object": "response", "created_at": 1, "model": "gpt-5.4-mini", "status": "completed",
                "output": [reason, message], "error": None, "usage": {
                    "input_tokens": 21, "output_tokens": 13, "total_tokens": 34,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 8}}}
    events = []

    def add(kind, **data):
        events.append({"type": kind, "sequence_number": len(events), **data})

    add("response.created", response={**response, "status": "in_progress", "output": [], "usage": None})
    add("response.output_item.added", output_index=0, item={"id": reason_id, "type": "reasoning", "summary": []})
    add("response.reasoning_summary_part.added", item_id=reason_id, output_index=0, summary_index=0,
        part={"type": "summary_text", "text": ""})
    add("response.reasoning_summary_text.delta", item_id=reason_id, output_index=0, summary_index=0, delta=public_summary)
    add("response.reasoning_summary_text.done", item_id=reason_id, output_index=0, summary_index=0, text=public_summary)
    add("response.reasoning_summary_part.done", item_id=reason_id, output_index=0, summary_index=0, part=reason["summary"][0])
    add("response.output_item.done", output_index=0, item=reason)
    if tool is None:
        add("response.output_item.added", output_index=1, item={**message, "content": [], "status": "in_progress"})
        add("response.content_part.added", item_id=message_id, output_index=1, content_index=0,
            part={"type": "output_text", "text": "", "annotations": []})
        add("response.output_text.delta", item_id=message_id, output_index=1, content_index=0, delta=part["text"])
        add("response.output_text.done", item_id=message_id, output_index=1, content_index=0, text=part["text"])
        add("response.content_part.done", item_id=message_id, output_index=1, content_index=0, part=part)
    else:
        add("response.output_item.added", output_index=1, item={**message, "arguments": "", "status": "in_progress"})
        add("response.function_call_arguments.delta", item_id=message["id"], output_index=1, delta=encoded_arguments)
        add("response.function_call_arguments.done", item_id=message["id"], output_index=1, arguments=encoded_arguments)
    add("response.output_item.done", output_index=1, item=message)
    add("response.completed", response=response)
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
