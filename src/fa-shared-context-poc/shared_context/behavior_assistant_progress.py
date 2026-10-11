"""Pure public SDK projection, safe for the minimal Hosted image."""
import logging
import re
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit
from . import behavior_assistant_wire as aw
from .errors import safe_text


_TOOL_DETAILS = {
    "WebIQ-MCP-web": ("query", "Search query: "),
    "WebIQ-MCP-browse": ("url", "Open URL: "),
    "playwright-browser_navigate": ("url", "Open URL: "),
    "read_skill": ("name", "Skill name: "),
    "read_skill_resource": ("name", "Skill name: "),
}
_CREDENTIAL = re.compile(
    r"""(?ix)(?<!\w)[`*_"']*(?:authorization|(?:access|refresh)[-_ ]?token|api[-_ ]?key|client[-_ ]?secret|
    (?:aws[-_ ]?)?(?:secret[-_ ]?access[-_ ]?key|access[-_ ]?key[-_ ]?id)|
    password|passwd|pwd|accountkey|sharedaccesssignature|cookie|set-cookie|token|secret|sig)
    (?![A-Za-z0-9])[\s`*_"']*(?:[:=]|\bis\b)\s*[^\r\n]+"""
)
_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+|AKIA[A-Z0-9]{16})\b")
_URL = re.compile(r"(?<!\S)\S*://\S+")
_HTTP_SCHEME = re.compile(r"https?://", re.IGNORECASE)
MODEL_FAILURE_CODES = frozenset({
    "MODEL_CALL_LIMIT_REACHED", "ASSISTANT_MODEL_BUDGET", "AGENT_MESSAGE_ROOT_EXPIRED",
})
PEER_FAILURE_CODES = frozenset({
    "AGENT_MESSAGE_RESULT_UNCONFIRMED", "AGENT_MESSAGE_ALLOWANCE",
    "AGENT_MESSAGE_ROOT_EXPIRED", "AGENT_MESSAGE_HANDOFF_EXISTS",
})


def _public_url(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("unsupported public URL")
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        if parsed.port is not None:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        logging.getLogger(__name__).warning("INSTRUCTION_PUBLIC_URL_UNAVAILABLE")
        return "[URL OMITTED]"


def _inline_url(match):
    value = match.group()
    # An apostrophe may be inside userinfo; remove prose delimiters only after matching the full URL.
    url = value.rstrip(".,;!?)}>'\"`")
    if url.count("://") != 1:
        logging.getLogger(__name__).warning("INSTRUCTION_PUBLIC_URL_UNAVAILABLE")
        return "[URL OMITTED]" + value[len(url):]
    scheme = _HTTP_SCHEME.search(url)
    start = 0 if scheme is None else scheme.start()
    return url[:start] + _public_url(url[start:]) + value[len(url):]


def _public_summary(value, prefix):
    text = safe_text(value, limit=len(value))
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)",
                  "[REDACTED]", text, flags=re.DOTALL)
    text = re.sub(r"```.*?(?:```|$)", "[CONTENT OMITTED]", text, flags=re.DOTALL)
    text = _URL.sub(_inline_url, text)
    text = _CREDENTIAL.sub("[REDACTED]", text)
    text = _TOKEN.sub("[REDACTED]", text)
    text = " ".join(text.split())
    summary = prefix + text
    return summary if len(summary) <= 256 else summary[:253] + "..."


def _tool_summary(name, arguments):
    detail = _TOOL_DETAILS.get(name)
    if detail is None:
        return None
    field, prefix = detail
    if not isinstance(arguments, dict) or not isinstance(arguments.get(field), str):
        logging.getLogger(__name__).warning("INSTRUCTION_PUBLIC_TOOL_DETAIL_UNAVAILABLE")
        return None
    value = arguments[field]
    if field == "url":
        value = _public_url(value)
    return _public_summary(value, prefix)


def _assistant_summary(data, allowed_tools):
    requests = data.get("tool_requests")
    if data.get("parent_tool_call_id") is not None or data.get("phase") not in (None, "commentary"):
        return None
    if data.get("phase") != "commentary" and not (
        isinstance(requests, list) and any(
            isinstance(request, dict) and request.get("name") in allowed_tools for request in requests
        )
    ):
        return None
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return _public_summary(content, "Assistant note: ")


def _reasoning_summary(data):
    container = data.get("reasoning_blocks")
    if container is None or data.get("parent_tool_call_id") is not None:
        return None
    if (not isinstance(container, dict) or set(container) != {"provider", "blocks"}
            or container.get("provider") != "openai-responses" or not isinstance(container.get("blocks"), list)):
        logging.getLogger(__name__).warning("INSTRUCTION_PUBLIC_REASONING_UNAVAILABLE")
        return None
    parts = []
    for block in container["blocks"]:
        if (not isinstance(block, dict) or block.get("type") != "reasoning"
                or not isinstance(block.get("summary"), list)):
            logging.getLogger(__name__).warning("INSTRUCTION_PUBLIC_REASONING_UNAVAILABLE")
            return None
        for item in block["summary"]:
            if (not isinstance(item, dict) or set(item) != {"type", "text"}
                    or item.get("type") != "summary_text" or not isinstance(item.get("text"), str)):
                logging.getLogger(__name__).warning("INSTRUCTION_PUBLIC_REASONING_UNAVAILABLE")
                return None
            parts.append(item["text"])
    text = "\n".join(parts).strip()
    return _public_summary(text, "") if text else None


def provider_refusal(failure):
    """A provider's definite refusal of a model request: its status, standard code and parameter path only.

    ``failure`` is the boundary's ``model_diagnostics`` result, which admits only identifier-shaped provider values.
    """
    if not isinstance(failure, dict) or failure.get("upstream_error_code") != "ASSISTANT_PROVIDER_HTTP_FAILURE":
        return None
    from .behavior_model_direct import DEFINITE_REFUSALS

    status = failure.get("provider_status")
    # The same statuses the direct sender treats as refused before processing; 408 and 5xx stay unresolved.
    if type(status) is not int or status not in DEFINITE_REFUSALS:
        return None
    parts = [f"Model provider refused: status={status}"]
    for key, label in (("provider_error_code", "code"), ("provider_error_param", "param")):
        if isinstance(failure.get(key), str):
            parts.append(f"{label}={failure[key]}")
    return " ".join(parts)


def public_event(value,seq,tools,*,allowed_tools=(),model_provider=None,reasoning_summary=False,sdk_session_id=None,
                 model_failure_code=None,model_failure=None):
    kind=value.get("type")
    at=value.get("timestamp")
    if not isinstance(at,str):return None
    timestamp=datetime.fromisoformat(at.replace("Z","+00:00"))
    data=value.get("data",{})
    if (kind == "m2.relay_queued" and isinstance(data, dict) and data.get("tool_name") == "send_agent_message"
            and "send_agent_message" in allowed_tools and isinstance(data.get("recipient"), str)):
        return aw.PublicProgressEvent(seq=seq, at=timestamp, kind="stage", status="started",
            summary=_public_summary(data["recipient"], "已排隊，")[:180] + " 換版完成後處理；尚未確認送達，不會重送。")
    if (kind in {"m2.provider_failed", "session.error"} and isinstance(model_failure_code, str)
            and model_failure_code in MODEL_FAILURE_CODES):
        return aw.PublicProgressEvent(seq=seq, at=timestamp, kind="error", status="failed",
                                      summary=model_failure_code)
    refused = provider_refusal(model_failure) if kind in {"m2.provider_failed", "session.error"} else None
    if refused is not None:
        return aw.PublicProgressEvent(seq=seq, at=timestamp, kind="error", status="failed", summary=refused)
    if (kind == "m2.tool_failure" and isinstance(data, dict) and data.get("tool_name") == "send_agent_message"
            and "send_agent_message" in allowed_tools and isinstance(data.get("code"), str)
            and data["code"] in PEER_FAILURE_CODES):
        return aw.PublicProgressEvent(seq=seq, at=timestamp, kind="error",
            status="unknown" if data["code"] == "AGENT_MESSAGE_RESULT_UNCONFIRMED" else "failed",
            summary=data["code"])
    if kind in {"m2.permission", "m2.tool_failure"} and isinstance(data, dict) and data.get("tool_name") in allowed_tools:
        from .native_hitl_wire import NATIVE_HITL_FAILURE_GUIDANCE, native_hitl_failure_feedback
        code = data.get("hitl") if kind == "m2.permission" else data.get("code")
        if isinstance(code, str) and code in NATIVE_HITL_FAILURE_GUIDANCE:
            return aw.PublicProgressEvent(seq=seq, at=timestamp, kind="error",
                status="unknown" if code in {"NATIVE_HITL_EFFECT_UNKNOWN", "NATIVE_HITL_AUTHORITY_UNAVAILABLE"} else "failed",
                summary=native_hitl_failure_feedback(code))
    if kind in {"session.start","assistant.turn_start","session.idle"}:
        return aw.PublicProgressEvent(seq=seq,at=timestamp,kind="stage",
            status="succeeded" if kind=="session.idle" else "started",
            summary={"session.start":"SDK session started","assistant.turn_start":"SDK turn started",
                     "session.idle":"SDK turn ended"}[kind])
    if kind=="session.error":
        return aw.PublicProgressEvent(seq=seq,at=timestamp,kind="error",status="unknown",
                                      summary="SDK reported an unresolved error")
    if kind not in {"assistant.message","tool.execution_start","tool.execution_complete"}:
        return None
    if not isinstance(data,dict):
        logging.getLogger(__name__).warning("INSTRUCTION_PUBLIC_EVENT_DATA_INVALID")
        return None
    if kind=="assistant.message":
        if value.get("agent_id") not in (None,sdk_session_id):
            return None
        summary=_assistant_summary(data,allowed_tools)
        reasoning=(_reasoning_summary(data) if model_provider=="openai-responses" and reasoning_summary else None)
        return None if summary is None and reasoning is None else aw.PublicProgressEvent(
            seq=seq,at=timestamp,kind="stage",status="started",summary=summary or "OpenAI reasoning summary",
            reasoning_summary=reasoning)
    call=data.get("tool_call_id")
    if not isinstance(call,str) or not call:
        logging.getLogger(__name__).warning("INSTRUCTION_PUBLIC_TOOL_ID_UNAVAILABLE")
        return None
    if kind=="tool.execution_start":
        if call in tools:
            tools[call]=None
            logging.getLogger(__name__).warning("INSTRUCTION_PUBLIC_TOOL_ID_REUSED")
            return None
        name=data.get("tool_name")
        if not isinstance(name,str) or name not in allowed_tools:
            tools[call]=None
            return None
        # Capability tools keep "<entry>-<tool>" names; the stored public name stays in the original
        # letters/digits/underscore format so existing readers keep accepting progress records.
        public=name.replace("-","_")
        tools[call]=(public,seq,name)
        return aw.PublicProgressEvent(seq=seq,at=timestamp,kind="tool_started",
            tool_name=public,tool_start_seq=seq,status="started",
            summary=_tool_summary(name,data.get("arguments")) or "Configured tool call started")
    if kind=="tool.execution_complete" and tools.get(call) is not None:
        if type(data.get("success")) is not bool:
            logging.getLogger(__name__).warning("INSTRUCTION_PUBLIC_TOOL_RESULT_UNAVAILABLE")
            return None
        name,start_seq,raw=tools[call]
        # Retain a tombstone: a reused raw ID must never attach a late result to a new call.
        tools[call]=None
        source=None
        if raw=="read_context" and data.get("success") is True:
            result=data.get("result") or {}
            content=result.get("content") if isinstance(result,dict) else None
            if isinstance(content,str):
                try:source=aw.parse_instruction_context(content).source
                except ValueError:
                    from .behavior_native_turn_wire import NativeInstructionContext
                    try:
                        NativeInstructionContext.model_validate_json(content)
                    except ValueError:
                        logging.getLogger(__name__).warning("INSTRUCTION_PUBLIC_SOURCE_UNAVAILABLE")
        return aw.PublicProgressEvent(seq=seq,at=timestamp,kind="tool_completed",tool_name=name,
            tool_start_seq=start_seq,
            status="succeeded" if data.get("success") is True else "failed",source=source,
            summary="Tool returned a result" if data.get("success") is True else "Tool did not return a confirmed result")
    return None
