"""Pre-SDK boundary for the finite metrics profile, NOT arbitrary secret detection.

Only declared tool calls with typed numeric/empty arguments can enter the SDK.
Legacy provider streams are reconstructed. For authenticated free instructions,
successful SSE bytes pass unchanged to the official SDK parser; error bodies and
credentials never become conversation content.
Transport credentials stay exclusively in Python's fixed HTTP credential channel.
"""

import asyncio
import json
import logging

import httpx
from pydantic import TypeAdapter, ValidationError
from .behavior_assistant_wire import Answer, Empty
from .behavior_assistant_wire import ContextResult, EffectResult, ToolReceipt, parse_instruction_context
from .behavior_http_diagnostics import AssistantModelFailure, model_failure
from .m3_skills import SkillReadResult, PurposeSkillReadResult, ResourceSkillReadResult
from .m3_models import Filename, SkillResourceRead
from .behavior_user_turn import UserTurnRead
from .behavior_text_files import UserFileRead
from .behavior_assistant_reply import AssistantReplyRead
from .behavior_delegation_wire import DelegationReply


_USER_TURN_FAILURE_GUIDANCE = {
    "USER_TURN_SCOPE_DENIED": "Only completed past turn numbers in this conversation are readable.",
    "USER_TURN_NOT_AVAILABLE": "The requested original user turn is unavailable; report missing information rather than inventing it.",
    "USER_TURN_SOURCE_CHANGED": "Original user-text provenance changed; do not reuse cached text or substitute another source.",
    "USER_TURN_SOURCE_LIMIT": "The complete original text or its evidence exceeds the supported read bound; do not truncate it or bypass the bound.",
    "USER_FILE_NOT_FOUND": "The specified completed turn has no literal file with that logical name; do not substitute another file.",
    "USER_FILE_READ_DISABLED": "This original profile does not enable historical user-file reads.",
    "ASSISTANT_REPLY_NOT_AVAILABLE": "The specified completed turn saved no assistant reply; do not reconstruct one from a summary.",
    "ASSISTANT_REPLY_READ_DISABLED": "This original profile does not enable past assistant-reply reads.",
}
from .behavior_work_memory import WorkMemoryRead
from .behavior_skill_copy import SopCopyReceipt
from .behavior_skill_draft import SkillDraftReceipt


def _unique(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate field")
    return result


def _manifest_aliases(detail):
    """`; available: a, b` for up to four exact manifest aliases (TOOL-06); any other text is never shown."""
    names = detail.split(", ") if isinstance(detail, str) else []
    try:
        names = _ALIASES.validate_python(names)
    except ValidationError:
        return ""
    return f"; available: {', '.join(names)}" if 0 < len(names) <= 4 else ""


_ALIASES = TypeAdapter(tuple[Filename, ...])


class AssistantModelBoundary:
    @staticmethod
    def delegation_result(value):
        """Accept only the API's typed complete public child outcome, never SDK telemetry."""
        return DelegationReply.model_validate_json(value.model_dump_json())

    def __init__(self, reserve, sender=None, *, text_reply=False, streaming=False, skill_access=False,
                 skill_purpose=False, user_turn_access=False, work_memory_access=False, sop_copy_access=False,
                 skill_draft_access=False, skill_resource_access=False, user_file_access=False,
                 assistant_reply_access=False, reviewed_note_access=False, capability_results=None,
                 native_context=False):
        if skill_purpose and not (text_reply and skill_access):
            raise ValueError("Purpose results require the explicit instruction skill capability")
        if skill_resource_access and not (text_reply and skill_access and skill_purpose):
            raise ValueError("Resource results require the explicit purpose and resource capabilities")
        if user_file_access and not (text_reply and user_turn_access):
            raise ValueError("Historical file reads require the explicit original-user-turn capability")
        if assistant_reply_access and not (text_reply and user_turn_access):
            raise ValueError("Past reply reads require the explicit original-user-turn capability")
        self.reserve = reserve
        self.sender = sender
        self.counter = 0
        self.failure = None
        self.text_reply = text_reply
        self.streaming = streaming
        self.skill_access = skill_access
        self.skill_purpose = skill_purpose
        self.skill_resource_access = skill_resource_access
        self.user_turn_access = user_turn_access
        self.user_file_access = user_file_access
        self.assistant_reply_access = assistant_reply_access
        self.work_memory_access = work_memory_access
        self.reviewed_note_access = reviewed_note_access
        self.sop_copy_access = sop_copy_access
        self.skill_draft_access = skill_draft_access
        if capability_results and not text_reply:
            raise ValueError("Capability tools belong only to free instructions")
        self.capability_results = dict(capability_results or {})
        if native_context and not text_reply:
            raise ValueError("Native origin belongs only to its captured instruction profile")
        self.native_context = native_context

    def record_failure(self, error, *, phase):
        if self.failure is None:
            self.failure = model_failure(error, phase=phase)
        return dict(self.failure)

    def public_tool_failure(self, name: str, code: str | None, detail: str | None = None) -> str | None:
        if self.text_reply and name in self.capability_results and isinstance(code, str):
            from .behavior_capabilities import (
                CAPABILITY_TOOL_GUIDANCE, TOOLBOX_FAILURE_GUIDANCE, McpToolResult,
            )
            from .native_group_wire import NATIVE_GROUP_FAILURE_GUIDANCE
            from .native_hitl_wire import NATIVE_HITL_FAILURE_GUIDANCE, native_hitl_failure_feedback
            if (name, code) in {
                    ("next_group_round", "NATIVE_GROUP_ROUND_CHANGED"),
                    ("send_agent_message", "NATIVE_GROUP_REPLY_REQUIRED")}:
                return f"[{code}] {NATIVE_GROUP_FAILURE_GUIDANCE[code]}"
            if code in NATIVE_HITL_FAILURE_GUIDANCE:
                return native_hitl_failure_feedback(code)
            if code in TOOLBOX_FAILURE_GUIDANCE and self.capability_results[name] is McpToolResult:
                # A failed toolbox call that ran nothing, a read-only one, or a missing connection: the fixed text
                # says what is safe; the tool's own detail (a consent link included) never reaches the model.
                return f"Tool call failed [{code}]. {TOOLBOX_FAILURE_GUIDANCE[code]}"
            guidance = CAPABILITY_TOOL_GUIDANCE.get(code)
            if (guidance is not None and name == "send_agent_message"
                    and code in {"NATIVE_RELAY_RECIPIENT_UNAVAILABLE", "NATIVE_RELAY_ALREADY_HANDED_OFF"}):
                return f"[{code}] {guidance}"
            if guidance is not None and name == "send_agent_message" and code in {
                    "AGENT_MESSAGE_UNKNOWN", "AGENT_MESSAGE_RESULT_UNCONFIRMED",
                    "AGENT_MESSAGE_RECEIPT_INVALID", "AGENT_MESSAGE_WAIT_UNAVAILABLE",
                    "NATIVE_RELAY_OUTCOME_UNKNOWN"}:
                return f"Peer outcome is unconfirmed [{code}]. {guidance}"
            if guidance is not None and (name, code) == ("read_skill_resource", "SKILL_RESOURCE_ARGUMENTS"):
                return f"Invalid arguments [{code}]. {guidance}"
            if guidance is not None and (name, code) == ("read_skill_resource", "SKILL_RESOURCE_NOT_FOUND"):
                return f"File not found [{code}]{_manifest_aliases(detail)}. {guidance}"
            if guidance is not None and (name, code) == ("read_user_file", "USER_FILE_NOT_FOUND"):
                return f"File not found [{code}]. {guidance}"
            return None if guidance is None else (
                f"Authority refused the action [{code}]. {guidance} Do not claim completion or resend unknown effects.")
        if not (self.text_reply and self.user_turn_access
                and (name == "read_user_turn" or name == "read_user_file" and self.user_file_access
                     or name == "read_assistant_reply" and self.assistant_reply_access)
                and isinstance(code, str)):
            return None
        guidance = _USER_TURN_FAILURE_GUIDANCE.get(code)
        if guidance is None:
            return None
        return (f"Authority refused the action [{code}]. {guidance} "
                "Do not claim completion or resend unknown effects.")

    async def before_request(self, digest):
        await self.reserve(digest)

    def tool_result(self, name, text):
        models = {"read_context": ContextResult, "save_result": ToolReceipt,
                  "finish_task": ToolReceipt, "execute_operation": EffectResult}
        if self.text_reply and self.skill_access:
            models["read_skill"] = (ResourceSkillReadResult if self.skill_resource_access
                                    else PurposeSkillReadResult if self.skill_purpose else SkillReadResult)
        if self.text_reply and self.skill_resource_access:
            models["read_skill_resource"] = SkillResourceRead
        if self.text_reply and self.user_turn_access:
            models["read_user_turn"] = UserTurnRead
        if self.text_reply and self.user_file_access:
            models["read_user_file"] = UserFileRead
        if self.text_reply and self.assistant_reply_access:
            models["read_assistant_reply"] = AssistantReplyRead
        if self.text_reply and self.work_memory_access:
            models["read_work_memory"] = WorkMemoryRead
        if self.text_reply and self.reviewed_note_access:
            models["read_reviewed_note"] = WorkMemoryRead
        if self.text_reply and self.sop_copy_access:
            models["copy_sop_to_skill_draft"] = SopCopyReceipt
        if self.text_reply and self.skill_draft_access:
            models["save_skill_draft"] = SkillDraftReceipt
        if self.text_reply:
            for tool_name, model in self.capability_results.items():
                if tool_name in models:
                    raise ValueError("Capability tools cannot replace a base tool result model")
                models[tool_name] = model
        json.loads(text, object_pairs_hook=_unique)
        if self.text_reply and name == "read_context":
            if self.native_context:
                from .behavior_native_turn_wire import NativeInstructionContext
                return NativeInstructionContext.model_validate_json(text).model_dump_json()
            return parse_instruction_context(text).model_dump_json()
        return models[name].model_validate_json(text).model_dump_json()

    async def response(self, response):
        phase, failed = "response_status", False
        handed_off = False
        try:
            if response.status_code != 200:
                raise ValueError()
            phase = "response_headers"
            content_type = response.headers.get("content-type", "").split(";")[0]
            if content_type != "text/event-stream":
                raise ValueError()
            if self.text_reply and self.streaming:
                from .behavior_instruction_stream import BoundedStream
                boundary = self
                class CheckedStream(BoundedStream):
                    async def __aiter__(self):
                        try:
                            async for chunk in super().__aiter__():
                                yield chunk
                        except BaseException as error:
                            boundary.record_failure(error,phase="stream")
                            raise
                streamed = httpx.Response(200,request=response.request,
                    headers={"content-type":"text/event-stream"},stream=CheckedStream(response))
                handed_off = True
                return streamed
            raw = bytearray()
            phase = "response_body"
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > (4*1024*1024 if self.text_reply else 65536):
                    raise ValueError()
            if self.text_reply:
                # The official SDK parses free text/tool SSE. Never rebuild its content or IDs.
                return httpx.Response(200, content=bytes(raw), headers={"content-type": "text/event-stream"},
                                      request=response.request)
            phase = "response_decode"
            lines = raw.decode("utf-8").splitlines()
            messages, done, tool, finish = [], False, None, None
            phase = "sse_frame"
            for line in lines:
                if not line:
                    continue
                if line == "data: [DONE]":
                    if done or finish is None:
                        raise ValueError()
                    done = True
                    continue
                if not line.startswith("data: ") or done:
                    raise ValueError()
                value = json.loads(line[6:], object_pairs_hook=_unique)
                choices = value.get("choices")
                if choices == []:
                    continue
                if not isinstance(choices, list) or len(choices) != 1:
                    raise ValueError()
                if finish is not None:
                    raise ValueError()
                choice = choices[0]
                finish = choice.get("finish_reason")
                if finish is not None and finish not in ("stop", "tool_calls"):
                    raise ValueError()
                delta = choice.get("delta", {})
                if not isinstance(delta, dict) or set(delta) - {"role", "content", "tool_calls", "refusal", "function_call"}:
                    raise ValueError()
                if (delta.get("content") not in (None, "") or delta.get("role") not in (None, "assistant")
                        or delta.get("refusal") is not None or delta.get("function_call") is not None):
                    raise ValueError()
                calls = delta.get("tool_calls")
                if calls is not None and not isinstance(calls, list):
                    raise ValueError()
                if calls:
                    if len(calls) != 1 or not isinstance(calls[0], dict):
                        raise ValueError()
                    part = calls[0]
                    if (set(part) - {"index", "id", "type", "function"}
                            or type(part.get("index", 0)) is not int or part.get("index", 0) != 0
                            or part.get("type") not in (None, "function")):
                        raise ValueError()
                    function = part.get("function")
                    if function is None:
                        function = {}
                    if not isinstance(function, dict) or set(function) - {"name", "arguments"}:
                        raise ValueError()
                    name, arguments = function.get("name"), function.get("arguments")
                    if any(value is not None and not isinstance(value, str) for value in (name, arguments)):
                        raise ValueError()
                    if tool is None:
                        tool = {"name": "", "arguments": ""}
                    tool["name"] += "" if name is None else name
                    tool["arguments"] += "" if arguments is None else arguments
                    if len(tool["arguments"]) > 1024 or len(tool["name"]) > 64:
                        raise ValueError()
            phase = "sse_terminal"
            if not done or finish != ("stop" if tool is None else "tool_calls"):
                raise ValueError()
            self.counter += 1
            if tool is None:
                # A silent turn end is allowed, but never completes the work.
                delta = {"role": "assistant", "content": ""}
            else:
                phase = "tool_schema"
                schema = {
                    "read_context": Empty, "save_result": Answer, "finish_task": Empty,
                    "execute_operation": Empty,
                }.get(tool["name"])
                if schema is None:
                    raise ValueError()
                json.loads(tool["arguments"], object_pairs_hook=_unique)
                parsed = schema.model_validate_json(tool["arguments"])
                delta = {"role": "assistant", "tool_calls": [{
                    "index": 0, "id": f"fa_call_{self.counter}", "type": "function",
                    "function": {"name": tool["name"], "arguments": parsed.model_dump_json()},
                }]}
            common = {"id": f"fa_response_{self.counter}", "object": "chat.completion.chunk",
                      "created": 1, "model": "gpt-4.1-mini"}
            for value, reason in ((delta, None), ({}, finish)):
                messages.append("data: " + json.dumps({**common, "choices": [{
                    "index": 0, "delta": value, "finish_reason": reason,
                }]}) + "\n\n")
            return httpx.Response(200, content=("".join(messages) + "data: [DONE]\n\n").encode(),
                                  headers={"content-type": "text/event-stream"}, request=response.request)
        except (Exception, asyncio.CancelledError) as error:
            failed = True
            detail = model_failure(error, phase="output_boundary")
            detail["boundary_phase"] = phase
            self.record_failure(AssistantModelFailure("Assistant output unresolved", detail), phase="output_boundary")
            if isinstance(error, asyncio.CancelledError):
                raise
            raise AssistantModelFailure("Assistant provider output refused before SDK ingress", detail) from None
        finally:
            try:
                if not handed_off:
                    await response.aclose()
            except Exception as error:
                if failed:
                    logging.getLogger(__name__).warning("ASSISTANT_MODEL_RESPONSE_CLOSE_UNRESOLVED")
                else:
                    detail = model_failure(error, phase="output_boundary")
                    detail["boundary_phase"] = "response_close"
                    self.record_failure(AssistantModelFailure("Assistant output unresolved", detail), phase="output_boundary")
                    raise AssistantModelFailure("Assistant provider output refused before SDK ingress", detail) from None
