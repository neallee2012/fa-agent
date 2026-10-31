"""Fixed categories and allowlisted correlation IDs; never raw HTTP content or credentials."""

import asyncio
import httpx
import re
from pydantic import ValidationError

from .errors import Problem


_EXCEPTION_TYPES = {
    ModuleNotFoundError: "ModuleNotFoundError", ImportError: "ImportError",
    RuntimeError: "RuntimeError", ValueError: "ValueError", TypeError: "TypeError",
    KeyError: "KeyError", OSError: "OSError", PermissionError: "PermissionError",
    FileNotFoundError: "FileNotFoundError", ValidationError: "ValidationError",
    httpx.ConnectTimeout: "ConnectTimeout", httpx.ReadTimeout: "ReadTimeout",
    httpx.WriteTimeout: "WriteTimeout", httpx.PoolTimeout: "PoolTimeout",
    httpx.ConnectError: "ConnectError", httpx.ReadError: "ReadError",
    httpx.WriteError: "WriteError", httpx.CloseError: "CloseError",
    httpx.LocalProtocolError: "LocalProtocolError", httpx.RemoteProtocolError: "RemoteProtocolError",
    httpx.DecodingError: "DecodingError",
    httpx.StreamClosed: "StreamClosed", httpx.StreamConsumed: "StreamConsumed",
    httpx.ResponseNotRead: "ResponseNotRead",
    TimeoutError: "TimeoutError", asyncio.CancelledError: "CancelledError",
}

from .behavior_sdk_state import SDK_STATE_ERRORS

from .m3_skills import SKILL_ERRORS
from .behavior_user_turn import USER_TURN_ERRORS
from .behavior_model_capacity import MODEL_CAPACITY_ERRORS
from .behavior_work_memory import WORK_MEMORY_ERRORS
from .behavior_skill_copy import SOP_COPY_ERRORS
from .behavior_skill_draft import SKILL_DRAFT_ERRORS
from .behavior_skill_catalog import CATALOG_BUDGET_ERRORS
from .behavior_attachments import ATTACHMENT_ERRORS
from .behavior_text_files import USER_FILE_READ_ERRORS
from .behavior_assistant_reply import ASSISTANT_REPLY_READ_ERRORS
from .behavior_work_memory import REVIEWED_NOTE_ERRORS
from .behavior_capabilities import CAPABILITY_ERRORS
from .memory_candidate_wire import MEMORY_CANDIDATE_ERRORS
from .assistant_image_wire import IMAGE_DELIVERY_ERRORS
from .behavior_agent_message_wire import AGENT_MESSAGE_ERRORS
from .behavior_wire import SDK_MEMORY_REFUSAL_CODES

# The one acquire allowlist: the API publishes exactly these codes and Hosted diagnostics accept exactly them.
_ACQUIRE_CODES = SDK_STATE_ERRORS | SKILL_ERRORS | USER_TURN_ERRORS | MODEL_CAPACITY_ERRORS | WORK_MEMORY_ERRORS | SOP_COPY_ERRORS | SKILL_DRAFT_ERRORS | CATALOG_BUDGET_ERRORS | ATTACHMENT_ERRORS | USER_FILE_READ_ERRORS | ASSISTANT_REPLY_READ_ERRORS | REVIEWED_NOTE_ERRORS | CAPABILITY_ERRORS | MEMORY_CANDIDATE_ERRORS | IMAGE_DELIVERY_ERRORS | AGENT_MESSAGE_ERRORS | frozenset({
    "INSTRUCTION_REPLY_STORAGE_LIMIT",
    "BEHAVIOR_CONFLICT", "BEHAVIOR_REQUEST_CONFLICT", "BEHAVIOR_COMMIT_UNKNOWN", "BEHAVIOR_READ_FAILED",
    "BEHAVIOR_PROTOCOL_ERROR", "BEHAVIOR_CONFIGURATION", "BEHAVIOR_NATIVE_EVIDENCE_REQUIRED",
    "BEHAVIOR_CURRENT_POLICY_DENIED", "BEHAVIOR_BINDING_DENIED",
    "MIGRATION_HISTORY_INVALID", "ASSISTANT_START_UNKNOWN",
    "ASSISTANT_EXECUTION_FENCED", "ASSISTANT_TICKET_DENIED", "ASSISTANT_BINDING_DENIED",
    "ASSISTANT_ACQUIRE_CHANGED", "ASSISTANT_COMMAND_ID",
    "ASSISTANT_LEDGER_ALREADY_COMMITTED", "ASSISTANT_NATIVE_UNKNOWN", "NATIVE_CLAIM_DENIED",
    "NATIVE_EXECUTOR_IDENTITY_DENIED", "ASSISTANT_NATIVE_DENIED", "ASSISTANT_MODEL_UNKNOWN",
    "ASSISTANT_MODEL_BUDGET", "AGENT_MESSAGE_ROOT_EXPIRED",
    "RESIDENT_WORKER_CHANGED", "RESIDENT_WORKER_DISABLED",
})
_ACQUIRE_PHASES = frozenset({"native_evidence", "acquire_authority", "native_claim", "hosted_claim", "assistant_authority"})

_MODEL_CODES = _ACQUIRE_CODES | frozenset({
    "INSTRUCTION_REPLY_STORAGE_LIMIT",
    "MODEL_CALL_LIMIT_REACHED",
    "ASSISTANT_MODEL_OUTCOME_UNKNOWN", "ASSISTANT_MODEL_REQUEST_INVALID", "ASSISTANT_MODEL_BUDGET",
    "ASSISTANT_MODEL_ID", "ASSISTANT_MODEL_REPLAY", "ASSISTANT_TOOLS_DENIED",
    "ASSISTANT_MODEL_MISMATCH",
    "ASSISTANT_CONTEXT_LIMIT",
    "ASSISTANT_MODEL_DEADLINE",
    "ASSISTANT_PROVIDER_HTTP_FAILURE", "ASSISTANT_PROVIDER_TRANSPORT_FAILURE",
    "ASSISTANT_PROVIDER_RESPONSE_UNRESOLVED", "ASSISTANT_SOURCE_DENIED", "ASSISTANT_SOURCE_CHANGED",
    "ASSISTANT_CONTEXT_CHANGED", "ASSISTANT_CLAIM_EXPIRED", "SOURCE_EXPIRED",
})
_MODEL_PHASES = _ACQUIRE_PHASES | frozenset({
    "model_authority", "model_consume", "model_request", "provider_evidence", "provider_token", "before_send",
    "provider_request", "provider_response", "authority", "transport", "output_boundary",
    "request_budget", "model", "body_budget", "replayed_request", "http_status", "stream",
    "destination", "websocket", "sdk_turn", "sdk_shutdown", "sdk_event", "unknown",
    "server_model_deadline", "client_model_timeout",
})
_HTTP_PHASES = frozenset({
    "request_decode", "token", "request", "response_status", "response_headers", "response_body",
    "response_decode", "response_close",
})
_BOUNDARY_PHASES = frozenset({
    "response_status", "response_headers", "response_body", "response_decode", "sse_frame",
    "sse_terminal", "tool_schema", "response_close",
})
_MODEL_FIELDS = {
    "upstream_phase": _HTTP_PHASES, "upstream_error_code": _MODEL_CODES,
    "upstream_error_phase": _MODEL_PHASES, "boundary_phase": _BOUNDARY_PHASES,
    "exception_type": frozenset((*_EXCEPTION_TYPES.values(), "Problem", "UNCLASSIFIED")),
}
_PROVIDER_IDENTIFIER = re.compile(r"[A-Za-z0-9_.\-\[\]]{1,96}")


def safe_key_error(error, allowed_ids=()):
    if type(error) is not KeyError:
        return None
    value = error.args[0] if len(error.args) == 1 else None
    fields = {"binding_id", "work_id", "job_id", "session_id", "policy_id", "request_id",
              "request_digest", "entries", "data", "material", "writes", "result", "usage",
              "text", "content", "type"}
    # Only fixed field names or exact server-owned IDs; arbitrary lookup keys can be secrets.
    return value if type(value) is str and (value in fields or value in allowed_ids) else "REDACTED"


def model_diagnostics(values):
    """Only fixed categories; no request IDs, text, URLs or arbitrary type names."""
    result = {key: value for key, allowed in _MODEL_FIELDS.items()
              if type(value := values.get(key)) is str and value in allowed}
    for key in ("upstream_status", "provider_status"):
        value = values.get(key)
        if type(value) is int and 100 <= value <= 599:
            result[key] = value
    # The provider's standard error code and parameter path (e.g. reasoning.effort), never its message.
    for key in ("provider_error_code", "provider_error_param"):
        value = values.get(key)
        if type(value) is str and _PROVIDER_IDENTIFIER.fullmatch(value):
            result[key] = value
    return result


class AssistantModelFailure(RuntimeError):
    def __init__(self, message, diagnostics):
        super().__init__(message)
        self.diagnostics = model_diagnostics(diagnostics)


def model_failure(error, *, phase):
    if isinstance(error, Problem):
        values = error.body["error"]
    elif isinstance(error, AssistantModelFailure):
        values = error.diagnostics
    else:
        values = {}
    result = model_diagnostics(values)
    result.setdefault("upstream_error_phase", phase if phase in _MODEL_PHASES else "unknown")
    result.setdefault("exception_type", _exception_type(error))
    return result


def model_response_diagnostics(headers):
    values = {}
    for field in (*_MODEL_FIELDS, "provider_status"):
        incoming = headers.get_list("x-behavior-model-" + field.replace("_", "-"))
        if len(incoming) == 1:
            value = incoming[0]
            if field == "provider_status":
                value = int(value) if len(value) == 3 and value.isascii() and value.isdigit() else None
            values[field] = value
    # Existing current-evidence dependency emits only the original fixed headers.
    return {**_acquire_diagnostics(headers), **model_diagnostics(values)}


def _acquire_diagnostics(headers, *, sdk_start_binding=None):
    result = {}
    codes = _ACQUIRE_CODES
    if sdk_start_binding is not None and sdk_start_binding.profile == "fa-sdk-start-gate-v2":
        codes |= SDK_MEMORY_REFUSAL_CODES
    for field, header, allowed in (
        ("upstream_error_code", "x-behavior-assistant-error-code", codes),
        ("upstream_error_phase", "x-behavior-assistant-error-phase", _ACQUIRE_PHASES),
    ):
        values = headers.get_list(header)
        if len(values) == 1 and values[0] in allowed:
            result[field] = values[0]
    return result


def _exception_type(error):
    if isinstance(error, Problem):
        value = error.body["error"].get("exception_type")
        return value if type(value) is str and value in _EXCEPTION_TYPES.values() else "Problem"
    # Names of unknown subclasses, messages and request URLs can contain secrets.
    return _EXCEPTION_TYPES.get(type(error), "UNCLASSIFIED")


def native_response_diagnostics(headers):
    """Only opaque GUID/hex correlation IDs and known platform source categories."""
    result = {}
    for field, header in (
        ("request_id", "x-request-id"),
        ("ms_request_id", "x-ms-request-id"),
        ("apim_request_id", "apim-request-id"),
    ):
        values = headers.get_list(header)
        if len(values) == 1 and re.fullmatch(
            r"(?:[a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})", values[0]
        ):
            result[field] = values[0]
    sources = headers.get_list("x-platform-error-source")
    if len(sources) == 1 and sources[0] in {"System", "Container", "Platform", "Agent"}:
        result["platform_error_source"] = sources[0]
    return result


def native_correlation_id(value):
    """Bound the platform invocation selector; never use a call ID, credential or raw header block."""
    return value if type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value) else None
