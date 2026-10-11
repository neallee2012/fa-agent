"""Dispatch watermarks at the existing SDK tool seam, not another execution loop."""

import asyncio
import json
import logging

from .behavior_capabilities import CAPABILITY_TOOL_GUIDANCE
from .behavior_native_turn_wire import material_digest
from .errors import Problem
from .m2_copilot import ToolFailure, ToolOutput
from .native_continuation_wire import NativeOperation
from .native_effects import NativeEffectClaim, NativeEffects


_GUIDANCE = (
    "原操作的效果或對應關係尚未確認；保留原結果與待決狀態，不要換工具、參數或 call ID 重做。"
    "請說明已完成的部分及仍需真人核對的操作。")
CAPABILITY_TOOL_GUIDANCE["NATIVE_CONTINUATION_EFFECT_UNKNOWN"] = _GUIDANCE

READ_ONLY_TOOLS = frozenset({
    "read_context", "read_user_turn", "read_user_file", "read_assistant_reply",
    "read_skill", "read_skill_resource", "read_agent_message", "read_group", "read_continuation_context",
    "read_tool_output",
    # Fixed guide text; offered in every native turn and refused outside a person's own turn, nothing to journal.
    "read_writing_style",
})


class NativeOperationGuard:
    def __init__(self, effects: NativeEffects, bindings: dict[str, str], *, prior=None):
        self.effects, self.bindings, self.prior = effects, dict(bindings), prior
        self.claims = {}
        self.locks = {}
        self.index_lock = asyncio.Lock()
        self.direct = set()
        self.observations = []
        self.starts = {}
        self.sdk_session_id = None
        self.completion_checks = {}
        receipt = effects.receipt
        self.root_request_id = (receipt.request_id if receipt.continuation is None
                                else receipt.continuation.root_request_id)
        if prior:
            roots = {record.operation.root_request_id for record in prior if record.operation is not None}
            if (len(roots) != 1 or any(record.operation is None or (
                    record.agent_id, record.conversation_id) != (receipt.agent_id, receipt.conversation_id)
                    for record in prior)):
                raise Problem(409, "NATIVE_EFFECT_CONFLICT", "recovery operations do not share the original scope")
            root = roots.pop()
            if receipt.continuation is not None and root != self.root_request_id:
                raise Problem(409, "NATIVE_EFFECT_CONFLICT", "recovery operation roots differ")
            self.root_request_id = root

    @staticmethod
    def _failed():
        return ToolFailure(_GUIDANCE, public_code="NATIVE_CONTINUATION_EFFECT_UNKNOWN")

    async def start(self):
        await self.effects.start_operations()

    def _operation(self, packet):
        name, call_id, arguments = packet.get("tool_name"), packet.get("tool_call_id"), packet.get("arguments")
        if name not in self.bindings or not isinstance(call_id, str) or not call_id or not isinstance(arguments, dict):
            raise self._failed()
        return NativeOperation.model_validate_json(json.dumps({
            "root_request_id": self.root_request_id,
            "operation_id": material_digest({"root": self.root_request_id,
                                            "rid": self.effects.receipt.request_id, "call": call_id}),
            "tool_name": name, "arguments": arguments, "binding_digest": self.bindings[name],
        }, allow_nan=False))

    @staticmethod
    def _same_action(left, right):
        return (left.tool_name, left.arguments, left.binding_digest) == (
            right.tool_name, right.arguments, right.binding_digest)

    async def prepare(self, packet):
        if packet.get("tool_name") in READ_ONLY_TOOLS:
            return "read"
        operation = self._operation(packet)
        call_id = packet["tool_call_id"]
        async with self.locks.setdefault(call_id, asyncio.Lock()):
            retained = self.claims.get(call_id)
            if retained is not None:
                if retained.record.refusal_code is not None or not self._same_action(retained.record.operation, operation):
                    raise self._failed()
                return "cached" if retained.record.status == "done" else "execute"
            if self.prior:
                candidates = {}
                for record in self.prior:
                    if self._same_action(record.operation, operation):
                        candidates[record.operation.operation_id] = record
                if len(candidates) != 1:
                    raise self._failed()
                original = next(iter(candidates.values()))
                if original.status == "done":
                    self.claims[call_id] = NativeEffectClaim(original, "", False)
                    return "cached"
                if original.dispatch_started is not False or original.refusal_code is not None:
                    raise self._failed()
                operation = original.operation
            try:
                async with self.index_lock:
                    claim = await self.effects.prepare_operation(call_id, operation)
            except Problem:
                raise self._failed() from None
            if not claim.fresh and claim.record.status != "done":
                raise self._failed()
            self.claims[call_id] = claim
            return "cached" if claim.record.status == "done" else "execute"

    async def _dispatch(self, packet):
        call_id = packet["tool_call_id"]
        claim = self.claims.get(call_id)
        if claim is None or not self._same_action(claim.record.operation, self._operation(packet)):
            raise self._failed()
        try:
            dispatched = await self.effects.mark_dispatched(claim)
        except Problem:
            raise self._failed() from None
        self.claims[call_id] = dispatched
        return dispatched

    async def dispatched(self, packet):
        if packet.get("tool_name") in READ_ONLY_TOOLS:
            return None
        async with self.locks.setdefault(packet["tool_call_id"], asyncio.Lock()):
            return await self._dispatch(packet)

    async def refused(self, packet, code):
        if packet.get("tool_name") in READ_ONLY_TOOLS:
            return
        async with self.locks.setdefault(packet["tool_call_id"], asyncio.Lock()):
            claim = self.claims.get(packet["tool_call_id"])
            if claim is None:
                raise self._failed()
            try:
                self.claims[packet["tool_call_id"]] = await self.effects.refuse_operation(
                    claim, code or "NATIVE_EFFECT_REFUSED")
            except Problem:
                raise self._failed() from None

    async def cached_feedback(self, packet):
        claim = self.claims.get(packet["tool_call_id"])
        if claim is None or claim.record.status != "done":
            raise self._failed()
        return ("原操作已完成，不重新執行。請讀取 read_continuation_context 的原結果；"
                "此處只重述已保存內容：" + (await self._output(claim.record)).text[:768])

    async def pre_tool(self, data, context, original):
        if data.get("toolName") != "task" or data.get("sessionId") != self.sdk_session_id:
            return None if original is None else await original(data, context)
        matches = [call_id for call_id, value in self.starts.items()
                   if value == ("task", data.get("toolArgs"))]
        if original is None:
            return {"permissionDecision": "deny", "permissionDecisionReason": _GUIDANCE}
        if len(matches) != 1:
            from .behavior_delegation_runtime import not_started
            # Nothing ran for this hook call: two calls with identical arguments cannot be told apart.
            return {"permissionDecision": "deny", "permissionDecisionReason": not_started(
                "tool_call_ambiguous", " Give each subtask its own distinct prompt.")}
        packet = {"tool_name": "task", "tool_call_id": matches[0], "arguments": data["toolArgs"]}
        try:
            if await self.prepare(packet) == "cached":
                return {"permissionDecision": "deny", "permissionDecisionReason": await self.cached_feedback(packet)}
            decision = await original(data, context)
            if not isinstance(decision, dict) or decision.get("permissionDecision") != "allow":
                await self.refused(packet, "NATIVE_OPERATION_REFUSED")
                # The SDK never runs a denied call; a retry with the same arguments must match only itself.
                self.starts.pop(packet["tool_call_id"], None)
                return decision or {"permissionDecision": "deny", "permissionDecisionReason": _GUIDANCE}
            await self.dispatched(packet)
            self.direct.add(packet["tool_call_id"])
            return decision
        except ToolFailure as error:
            return {"permissionDecision": "deny", "permissionDecisionReason": error.public_message}

    async def _output(self, record):
        result = record.result
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise self._failed()
        image = None
        if result.get("image") is not None:
            from .native_turns import EFFECTS_STORE
            from .native_effects import NativeEffectImage
            async with self.effects.runtime._store(EFFECTS_STORE) as store:
                image = await self.effects._load_image(store, record, NativeEffectImage.model_validate(result["image"]))
        return ToolOutput(result["text"], png_base64=image)

    async def invoke(self, packet, handler):
        if packet.get("tool_name") in READ_ONLY_TOOLS:
            return await handler(packet)
        call_id = packet["tool_call_id"]
        async with self.locks.setdefault(call_id, asyncio.Lock()):
            claim = self.claims.get(call_id)
            if claim is None or not self._same_action(claim.record.operation, self._operation(packet)):
                raise self._failed()
            if claim.record.status == "done":
                return await self._output(claim.record)
            claim = await self._dispatch(packet)
            output = await handler(packet)
            if not isinstance(output, ToolOutput):
                raise self._failed()
            result = {"text": output.text}
            if output.png_base64 is not None:
                result["image"] = (await self.effects.save_image(claim, output.png_base64)).model_dump(mode="json")
            done = await self.effects.finish(claim, result)
            self.claims[call_id] = NativeEffectClaim(done, claim.etag, False)
            return output

    def observe(self, event):
        if event.get("agent_id") not in {None, self.sdk_session_id}:
            return
        if event.get("type") == "tool.execution_start":
            data = event.get("data") or {}
            call_id = data.get("tool_call_id", data.get("toolCallId"))
            if isinstance(call_id, str):
                self.starts[call_id] = (data.get("tool_name", data.get("toolName")), data.get("arguments"))
            return
        if event.get("type") != "tool.execution_complete":
            return
        data = event.get("data") or {}
        call_id = data.get("tool_call_id", data.get("toolCallId"))
        if call_id not in self.direct or data.get("success") is not True:
            return
        result = data.get("result")
        if isinstance(result, dict) and (
                result.get("binary_results_for_llm") or result.get("binaryResultsForLlm")):
            logging.getLogger(__name__).warning("NATIVE_OPERATION_UNCONFIRMED reason=binary_result_not_retained")
            return
        text = result.get("content") if isinstance(result, dict) else None
        if not isinstance(text, str):
            logging.getLogger(__name__).warning("NATIVE_OPERATION_UNCONFIRMED reason=result_shape")
            return

        async def finish():
            async with self.locks[call_id]:
                claim = self.claims[call_id]
                if claim.record.status == "done":
                    return
                output = text
                checker = self.completion_checks.get(claim.record.operation.tool_name)
                if checker is not None:
                    observed = await checker(call_id)
                    if observed.status != "completed" or not isinstance(observed.result, str):
                        logging.getLogger(__name__).warning("NATIVE_OPERATION_UNCONFIRMED reason=child_result")
                        return
                    output = observed.result
                done = await self.effects.finish(claim, {"text": output})
                self.claims[call_id] = NativeEffectClaim(done, claim.etag, False)

        self.observations.append(asyncio.create_task(finish()))

    async def close(self):
        for task in self.observations:
            try:
                await task
            except (Problem, OSError) as error:
                logging.getLogger(__name__).warning(
                    "NATIVE_OPERATION_UNCONFIRMED exception_type=%s", type(error).__name__)
