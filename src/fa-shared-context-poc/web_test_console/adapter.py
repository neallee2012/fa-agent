"""Fixed HTTP client contract. The backend remains the authentication authority."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import re
import subprocess
import time
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator

from shared_context import behavior_wire as wire
from shared_context.behavior_assistant_wire import AssistantSubmit
from shared_context.behavior_delivery import _Payload
from shared_context.behavior_inbox import INBOX_CHANNEL_ID, INBOX_VERSION
# This bridge decodes the ADR-0009 phase 2c notice kinds; the API adds them (and clears the slot) only when told.
from shared_context.behavior_native_turn_wire import (
    NOTICE_KINDS_HEADER, NOTICE_KINDS_VERSION, ROLLBACK_NOTICE_HEADER, ROLLBACK_NOTICE_VERSION, UPGRADE_VETOED_HEADER,
    UPGRADE_VETOED_VERSION, UPGRADE_WAITING_HEADER, UPGRADE_WAITING_VERSION)
from shared_context.behavior_operations import control_key


def now_utc():
    return datetime.now(timezone.utc)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def strict_json(raw):
    def pairs(items):
        value = dict(items)
        if len(value) != len(items):
            raise ValueError("duplicate keys")
        return value
    def invalid(_):
        raise ValueError("non-finite JSON")
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except RecursionError as error:  # nested deeper than the parser's recursion limit: just another unreadable body
        raise ValueError("JSON nested too deep") from error


def error_code(raw):
    """The API problem body's ``error.code`` when it is a string; anything else is None."""
    try:
        problem = strict_json(bytes(raw))
    except (ValueError, TypeError):
        return None
    error = problem.get("error") if type(problem) is dict else None
    code = error.get("code") if type(error) is dict else None
    return code if type(code) is str else None


# Backend statuses without a code of their own; every read maps them the same way.
STATUS_ERRORS = {401: "LOGIN_REQUIRED", 403: "BACKEND_DENIED", 404: "NO_RESULT",
                 409: "RESULT_UNKNOWN", 412: "RESULT_UNKNOWN", 429: "BACKEND_LIMIT"}


class ConsoleError(Exception):
    def __init__(self, code, status=409, upstream_status=None, *, not_dispatched=False, server_following=False,
                 automatic_retry=False):
        super().__init__(code)
        self.code, self.status, self.upstream_status = code, status, upstream_status
        self.not_dispatched = not_dispatched
        self.server_following = server_following
        # A read the API says may be retried as is (a transient failure); never set for a write.
        self.automatic_retry = automatic_retry


def require(value, code, status=409):
    if not value:
        raise ConsoleError(code, status)


class FixedTask(wire.WireModel):
    task_id: wire.Identifier
    label: Annotated[str, Field(min_length=1, max_length=80)]
    description: Annotated[str, Field(max_length=400)] = ""
    mode: Literal["normal_pull"] = "normal_pull"
    submission: Literal["assistant", "lifecycle"] = "assistant"
    binding_id: wire.Identifier
    job_id: wire.Identifier
    work_id: wire.Identifier
    event_id: wire.Identifier
    root_request_id: wire.Identifier
    obligation_id: wire.Identifier
    owner_record_id: wire.Identifier
    route: wire.DeliveryRoute
    channel_version: Literal[INBOX_VERSION]
    submit_request_id: wire.Identifier
    fetch_request_id: wire.Identifier
    ack_request_id: wire.Identifier
    handoff_id: wire.Identifier | None = None  # always None; kept so the config SHA of existing journals does not change
    allowed_actions: tuple[Literal["status", "submit", "fetch", "acknowledge"], ...] = ("status",)
    max_payload_bytes: Annotated[int, Field(ge=1, le=131072)] = 32768

    @model_validator(mode="after")
    def bounded(self):
        if (self.route.channel_id != INBOX_CHANNEL_ID or len(set(self.allowed_actions)) != len(self.allowed_actions)
                or len({self.submit_request_id, self.fetch_request_id, self.ack_request_id}) != 3):
            raise ValueError("one authenticated-pull destination and unique fixed command IDs required")
        if self.handoff_id is not None:
            raise ValueError("normal pull cannot supply a retained handoff")
        return self


class CliSettings(wire.WireModel):
    executable: Annotated[str, Field(min_length=1, max_length=2048)]
    azure_config_dir: Annotated[str, Field(min_length=1, max_length=2048)]
    subscription_id: Annotated[str, Field(pattern=r"^[0-9a-fA-F-]{36}$")]


class ConsoleConfig(wire.WireModel):
    schema_version: Literal["fa-web-test-console-v1"] = "fa-web-test-console-v1"
    scope_id: wire.Identifier
    backend_origin: str
    tenant_id: wire.Identifier
    human_oid: wire.Identifier
    client_id: wire.Identifier
    audience: Annotated[str, Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9:/._-]+$")]
    not_before: datetime
    expires_at: datetime
    approval_reference: wire.Identifier
    approval_sha256: wire.Digest
    backend_configuration_sha256: wire.Digest
    backend_core_sha256: wire.Digest
    cli: CliSettings | None = None
    tasks: Annotated[tuple[FixedTask, ...], Field(min_length=1, max_length=16)]

    @field_validator("backend_origin")
    @classmethod
    def origin(cls, value):
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.netloc != parsed.hostname
                or parsed.path or parsed.query or parsed.fragment
                or re.fullmatch(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?", parsed.hostname) is None):
            raise ValueError("one exact canonical HTTPS origin required")
        return value

    @model_validator(mode="after")
    def bounded(self):
        if (any(v.tzinfo is None or v.utcoffset() != timedelta(0) for v in (self.not_before, self.expires_at))
                or not timedelta(0) < self.expires_at-self.not_before <= timedelta(hours=4)
                or len({t.task_id for t in self.tasks}) != len(self.tasks)
                or len({t.binding_id for t in self.tasks}) != len(self.tasks)
                or any(t.route.tenant_id != self.tenant_id or t.route.recipient_id != self.human_oid for t in self.tasks)):
            raise ValueError("finite UTC scope, unique original bindings and designated recipient required")
        return self

    @property
    def sha256(self):
        return sha(canonical(self.model_dump(mode="json")))


class SteadyConsoleConfig(wire.WireModel):
    """Client connection, not a grant or an extension of a legacy scope."""
    schema_version: Literal["fa-web-test-console-steady-v2"] = "fa-web-test-console-steady-v2"
    authorization_mode: Literal["backend-per-work"]
    console_id: wire.Identifier
    backend_origin: str
    tenant_id: wire.Identifier
    human_oid: wire.Identifier
    client_id: wire.Identifier
    audience: Annotated[str, Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9:/._-]+$")]
    backend_configuration_sha256: wire.Digest
    backend_core_sha256: wire.Digest
    cli: CliSettings | None = None
    tasks: Annotated[tuple[FixedTask, ...], Field(min_length=1, max_length=16)]

    @field_validator("backend_origin")
    @classmethod
    def origin(cls, value):
        return ConsoleConfig.origin(value)

    @model_validator(mode="after")
    def bound_tasks(self):
        if (len({t.task_id for t in self.tasks}) != len(self.tasks)
                or len({t.binding_id for t in self.tasks}) != len(self.tasks)
                or any(t.route.tenant_id != self.tenant_id or t.route.recipient_id != self.human_oid for t in self.tasks)):
            raise ValueError("unique original bindings and designated recipient required")
        return self

    @property
    def sha256(self):
        return sha(canonical(self.model_dump(mode="json")))


def parse_config(raw):
    data = strict_json(raw)
    if type(data) is not dict:
        raise ValueError("console configuration must be an object")
    version = data.get("schema_version", "fa-web-test-console-v1")
    models = {"fa-web-test-console-v1": ConsoleConfig, "fa-web-test-console-steady-v2": SteadyConsoleConfig}
    if version == "fa-web-test-console-standing-v3":
        from .standing import StandingConsoleConfig
        return StandingConsoleConfig.model_validate_json(raw)
    if type(version) is not str or version not in models:
        raise ValueError("unknown console configuration version")
    return models[version].model_validate_json(raw)


class TokenVault:
    """In-memory preflight only; JWT signatures/permissions are verified upstream."""
    def __init__(self, config, *, clock=now_utc):
        self.config, self.clock, self._token = config, clock, None

    def set(self, token):
        self.clear()
        require(isinstance(token, str) and 0 < len(token) <= 16384 and not any(c.isspace() for c in token),
                "LOGIN_REQUIRED", 401)
        try:
            parts = token.split(".")
            require(len(parts) == 3, "LOGIN_REQUIRED", 401)
            claims = strict_json(base64.urlsafe_b64decode(parts[1]+"="*(-len(parts[1]) % 4)))
            c, now = self.config, self.clock().timestamp()
            require(claims.get("tid") == c.tenant_id and claims.get("oid") == c.human_oid
                    and claims.get("aud") == c.audience
                    and claims.get("iss") in {f"https://sts.windows.net/{c.tenant_id}/",
                        f"https://login.microsoftonline.com/{c.tenant_id}/v2.0"}
                    and claims.get("idtyp", "user") == "user"
                    and isinstance(claims.get("scp"), str) and "Behavior.Read" in claims["scp"].split()
                    and any(claims.get(key) == c.client_id for key in ("appid", "azp"))
                    and all(key not in claims or claims[key] == c.client_id for key in ("appid", "azp"))
                    and not claims.get("roles"),
                    "IDENTITY_MISMATCH", 403)
            require(type(claims.get("exp")) in {int, float} and claims["exp"] > now
                    and type(claims.get("nbf", 0)) in {int, float} and claims.get("nbf", 0) <= now,
                    "LOGIN_EXPIRED", 401)
            self._expires = claims["exp"]
            self._scopes = frozenset(claims["scp"].split())
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ConsoleError("LOGIN_REQUIRED", 401) from None
        self._token = SecretStr(token)

    def get(self):
        require(self._token is not None, "LOGIN_REQUIRED", 401)
        require(self.clock().timestamp() < self._expires, "LOGIN_EXPIRED", 401)
        return self._token.get_secret_value()

    def clear(self):
        self._token = None
        self._scopes = frozenset()

    def allows(self, scope):
        self.get()
        return scope in self._scopes

    @property
    def expires_at(self):
        return None if self._token is None else self._expires

    def state(self):
        if self._token is None:
            return "LOGIN_REQUIRED"
        return "AVAILABLE" if self.clock().timestamp() < self._expires else "LOGIN_EXPIRED"


class CliTokenProvider:
    def __init__(self, config):
        self.config = config

    def __call__(self):
        c, setting = self.config, self.config.cli
        require(setting is not None, "CLI_NOT_CONFIGURED", 503)
        executable, directory = Path(setting.executable), Path(setting.azure_config_dir)
        require(executable.is_absolute() and executable.is_file() and directory.is_absolute() and directory.is_dir(),
                "CLI_NOT_CONFIGURED", 503)
        env = {**os.environ, "AZURE_CONFIG_DIR": str(directory), "PYTHONUTF8": "1",
               "AZURE_CORE_COLLECT_TELEMETRY": "no", "AZURE_LOGGING_ENABLE_LOG_FILE": "false",
               "AZURE_CORE_NO_COLOR": "true"}
        try:
            completed = subprocess.run([
                str(executable), "account", "get-access-token",
                "--subscription", setting.subscription_id, "--resource", c.audience,
                "--only-show-errors", "--output", "json",
            ], env=env, capture_output=True, timeout=30, check=False, shell=False, encoding="utf-8")
            require(completed.returncode == 0 and len(completed.stdout) <= 32768, "LOGIN_REQUIRED", 401)
            result = strict_json(completed.stdout)
            require(result.get("tenant") == c.tenant_id and result.get("subscription") == setting.subscription_id
                    and result.get("tokenType") == "Bearer", "IDENTITY_MISMATCH", 403)
            return result["accessToken"]
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError):
            raise ConsoleError("LOGIN_REQUIRED", 401) from None


@dataclass(frozen=True)
class VerifiedResult:
    payload: bytes = field(repr=False)
    binding: wire.DeliveryBinding
    exchange_id: str
    token: SecretStr = field(repr=False)
    parsed: _Payload = field(repr=False)

    def public(self):
        content = self.parsed.content
        try:
            text = None if content is None else content.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        return {"sha256": self.binding.payload_digest, "byte_length": len(self.payload),
                "binding": self.binding.model_dump(mode="json"), "work_state": self.parsed.work_state,
                "message_kind": self.parsed.message_kind, "notification_code": self.parsed.notification_code,
                "content": text, "content_unavailable": text is None, "verified_bytes": True,
                "unresolved_operations": self.parsed.unresolved_operations, "known_effects": self.parsed.known_effects}


def checked_result(task, body, headers, delivery_id):
    names = ("x-behavior-exchange", "x-behavior-ack-token", "x-behavior-binding",
             "x-behavior-payload-sha256", "x-behavior-confirmation-level")
    require(0 < len(body) <= task.max_payload_bytes and all(len(headers.get_list(n)) == 1 for n in names)
            and headers.get("content-encoding", "identity") == "identity"
            and headers.get("content-type", "").split(";")[0] == "application/json",
            "RESULT_INVALID", 502)
    try:
        encoded = headers["x-behavior-binding"]
        require(len(encoded) <= 16384 and re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", encoded) is not None,
                "RESULT_INVALID", 502)
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        strict_json(raw)
        binding = wire.DeliveryBinding.model_validate_json(raw)
        strict_json(body)
        payload = _Payload.model_validate_json(body)
        exchange, token = headers[names[0]], headers[names[1]]
        wire.Command(request_id=exchange)
        wire.Command(request_id=token)
    except (ValueError, TypeError, ValidationError):
        raise ConsoleError("RESULT_INVALID", 502) from None
    require(headers[names[4]] == "client_received"
            and token.encode() not in body and (payload.content is None or token.encode() not in payload.content)
            and sha(body) == binding.payload_digest == headers[names[3]]
            and binding.obligation_id == task.obligation_id and binding.delivery_id == delivery_id
            and binding.route == task.route and binding.channel_version == task.channel_version
            and payload.event_id == task.event_id and payload.root_request_id == task.root_request_id
            and payload.work_id == task.work_id and payload.responsibility_owner == task.owner_record_id
            and payload.message_kind in {"final", "failure"}
            and payload.work_state in {"succeeded", "failed", "cancelled"}
            and tuple(v.model_dump(mode="json") for v in payload.artifacts) ==
                tuple(v.model_dump(mode="json") for v in binding.artifacts),
            "RESULT_BINDING_MISMATCH", 502)
    return VerifiedResult(body, binding, exchange, SecretStr(token), payload)


_NATIVE_TURNS = "/work-policy/v1/native-turns/"
_RESPONSES_TURNS = "/work-policy/v1/responses-turns/"
_TEMPLATE = "/work-policy/v1/agent-template"
_ID = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
_ROUTINE_DEFINITIONS = re.compile(r"/native-routines/v1/agents/" + _ID + "/definitions(/" + _ID + ")?")
_PARSED_PREFIXES = ("/team-skills/v1/", "/team-mcp/v1/", "/team-connectors/v1/", "/native-groups/v1/",
                    "/native-hitl/v1/", "/work-policy/v1/")
_DELEGATION_READ = re.compile(r"/work-policy/v1/conversations/[^/]+/turns/[1-9][0-9]*/delegation")
_NATIVE_NOT_DISPATCHED_PATHS = frozenset({
    "/work-policy/v1/native-turns/submit", "/work-policy/v1/responses-turns/submit",
    "/work-policy/v1/native-turns/cancel", "/work-policy/v1/responses-turns/cancel",
    "/work-policy/v1/native-turns/delivered", "/work-policy/v1/native-turns/append"})
_SERVER_FOLLOWING = frozenset({(409, "NATIVE_SESSION_CHANGED"), (503, "AGENT_VERSION_FOLLOWING"),
                               (503, "NATIVE_EXECUTION_UNAVAILABLE")})
_NATIVE_EXTRA_CODES = frozenset({
    "FOUNDRY_INVOCATION_UNKNOWN", "FOUNDRY_RESPONSE_INVALID", "FOUNDRY_NATIVE_BINDING_MISMATCH",
    "BEHAVIOR_COMMIT_UNKNOWN", "AGENT_USAGE_CONFLICT", "INSTRUCTION_ROUTE_CHANGED"})
_DELEGATION_CODES = frozenset({
    "DELEGATION_UNKNOWN", "DELEGATION_UNAVAILABLE", "DELEGATION_DISABLED", "DELEGATION_SESSION_READ_ONLY",
    "DELEGATION_PARENT_REQUIRED", "DELEGATION_SCOPE_CHANGED", "DELEGATION_RECORD_INVALID", "DELEGATION_RESULT_INVALID"})
_CONFLICT_CODES = frozenset({
    "CONVERSATION_ARCHIVED", "CONVERSATION_SESSION_RESET_REQUIRED", "CONVERSATION_SESSION_UNAVAILABLE",
    "CONVERSATION_SESSION_VERSION_CHANGED", "CONVERSATION_TURN_UNRESOLVED", "WORK_SUBMIT_UNKNOWN",
    "CANCELLATION_RECEIPT_UNKNOWN", "CANCELLATION_RECEIPT_CHANGED", "CANCELLATION_RECEIPT_INVALID",
    "CONVERSATION_TRANSFER_NOT_READY", "CONVERSATION_TRANSFER_SCOPE", "CONVERSATION_TRANSFER_ARCHIVE_INVALID",
    "CONVERSATION_TRANSFER_UNAVAILABLE", "CONVERSATION_TRANSFER_UNKNOWN", "CONVERSATION_TRANSFER_CHANGED",
    "CONVERSATION_TRANSFER_CONFLICT", "CONVERSATION_TRANSFER_INVALID", "CONVERSATION_TRANSFER_BUDGET"})
_SYSTEM_PROMPT_CODES = frozenset({
    "SYSTEM_PROMPT_UNRECONSTRUCTABLE", "AGENT_TEMPLATE_PROFILE_UNSUPPORTED", "AGENT_ROLE_PROMPT_INVALID",
    "AGENT_ROLE_PROMPT_MISSING", "CAPABILITY_SOURCES_CHANGED", "NATIVE_TURNS_DISABLED"})
_SELECTION_PATHS = frozenset({_TEMPLATE + "/select", _TEMPLATE + "/selection", _TEMPLATE + "/selection-reconcile"})
_SELECTION_CODES = frozenset({
    "PROFILE_SELECTION_UNSUPPORTED", "PROFILE_SELECTION_DISABLED", "PROFILE_SELECTION_DENIED",
    "PROFILE_SELECTION_CONFLICT", "PROFILE_SELECTION_SOURCES_CHANGED", "PROFILE_SELECTION_MISSING",
    "PROFILE_SELECTION_INVALID", "PROFILE_SELECTION_UNKNOWN"})


def _allow(ok):
    require(ok, "ORIGIN_DENIED", 403)


def _int_within(params, key, high):
    return key not in params or type(params[key]) is int and 1 <= params[key] <= high


def _only_after_id(params):
    return set(params) == {"after"} and type(params["after"]) is str and re.fullmatch(_ID, params["after"]) is not None


def _check_origin(url, origin, path):
    _allow(urlsplit(url).netloc == urlsplit(origin).netloc
           and path.startswith("/") and not path.startswith("//") and "?" not in path and "#" not in path)


def _request_headers(token, if_match):
    return {"Authorization": "Bearer "+token, "Content-Type": "application/json",
            "Accept-Encoding": "identity", NOTICE_KINDS_HEADER: NOTICE_KINDS_VERSION,
            UPGRADE_WAITING_HEADER: UPGRADE_WAITING_VERSION,
            ROLLBACK_NOTICE_HEADER: ROLLBACK_NOTICE_VERSION, UPGRADE_VETOED_HEADER: UPGRADE_VETOED_VERSION,
            **({} if if_match is None else {"If-Match": if_match})}


def _read_limited(response, token, limit, start):
    data = bytearray()
    for chunk in response.iter_bytes():
        data.extend(chunk)
        require(len(data) <= limit and time.monotonic()-start < 120, "RESPONSE_LIMIT", 502)
    require(token.encode() not in data, "RESULT_INVALID", 502)
    return data


def _require_no_store(data, headers):
    require(not data and headers.get("cache-control") == "no-store", "DELEGATION_RESPONSE_INVALID", 502)


def _check_if_match(method, path, if_match):
    # Only a Team skill decision carries the owner's observed ETag; nothing else sends If-Match.
    _allow(if_match is None or path.startswith("/team-skills/v1/") and method == "POST" and type(if_match) is str
           and 0 < len(if_match) <= 256 and if_match.isprintable())


def _check_attention_params(get_only, params):
    from pydantic import TypeAdapter
    from shared_context.native_hitl_wire import AttentionCursor
    _allow(get_only and set(params) <= {"cursor", "limit"} and _int_within(params, "limit", 20))
    if "cursor" in params:
        try:
            TypeAdapter(AttentionCursor).validate_python(params["cursor"], strict=True)
        except ValueError:
            raise ConsoleError("ORIGIN_DENIED", 403) from None


def _check_responses_params(get_only, path, params):
    _allow(get_only and re.fullmatch(
        r"/work-policy/v1/responses-turns/(conversations|responses)/[A-Za-z0-9_-]{1,128}/history", path)
        and set(params) <= {"after", "limit"}
        and ("after" not in params or type(params["after"]) is str
             and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", params["after"]))
        and _int_within(params, "limit", 100))


def _check_received_params(get_only, params):
    # The official State store cursor of the previous page (StateStoreCursor), passed back unchanged.
    _allow(get_only and set(params) == {"after"} and type(params["after"]) is str
           and 0 < len(params["after"]) <= 2048 and params["after"].isprintable())


def _check_native_params(get_only, path, params):
    names = {"after_turn", "limit"} if path.endswith("/history") else {"part"} if path.endswith("/result") else set()
    _allow(path.startswith(_NATIVE_TURNS) and get_only and set(params) <= names
           and all(type(value) is int and value >= 0 for value in params.values()))


def _check_params(method, path, body, params):
    get_only = method == "GET" and body is None
    if path == "/native-hitl/v1/attention":
        _check_attention_params(get_only, params)
    elif path.startswith("/team-skills/v1/"):
        _allow(get_only and path == "/team-skills/v1/reviews" and _only_after_id(params))
    elif _ROUTINE_DEFINITIONS.fullmatch(path):
        _allow(get_only and path.endswith("/definitions") and _only_after_id(params))
    elif path.startswith(_RESPONSES_TURNS):
        _check_responses_params(get_only, path, params)
    elif path == "/work-policy/v1/native-turns/received":
        _check_received_params(get_only, params)
    else:
        _check_native_params(get_only, path, params)


def _is_delegation_read(method, path, body):
    return method == "GET" and body is None and _DELEGATION_READ.fullmatch(path) is not None


def _accepted(method, status, path):
    if method != "POST":
        return False
    if status == 201:
        return path == "/native-groups/v1/groups"
    return status == 202 and (
        path.startswith(_NATIVE_TURNS) and (path.endswith("/submit") or path == _NATIVE_TURNS + "append")
        or path in {_RESPONSES_TURNS + "submit", _RESPONSES_TURNS + "reconcile"})


def _problem(data):
    """The problem body's ``error`` value as sent (None when the body is not a JSON object with one), and that
    object's ``code`` as sent (None when ``error`` is not an object or has no ``code``)."""
    try:
        problem = strict_json(bytes(data))
    except (ValueError, TypeError):
        problem = {}
    error = problem.get("error") if type(problem) is dict else None
    return error, error.get("code") if type(error) is dict else None


def _problem_for(method, path, data):
    """``_problem`` only where a body can change the outcome, as before: a read or a route with its own codes."""
    if method == "GET" or path.startswith(_PARSED_PREFIXES) or _ROUTINE_DEFINITIONS.fullmatch(path):
        return _problem(data)
    return None, None


def _retry_hint(method, detail):
    return method == "GET" and type(detail) is dict and detail.get("automatic_retry") is True


def _raise_listed(code, codes, shown, status):
    if type(code) is str and code in codes:
        raise ConsoleError(code, shown, status)


def _raise_service_error(path, status, code):
    # The error sets are imported lazily, as before: they live in modules that import this one.
    client_status = status if 400 <= status < 500 else 502
    if path.startswith("/team-skills/v1/"):
        from .team_skills import TEAM_SKILL_ERRORS
        _raise_listed(code, TEAM_SKILL_ERRORS, client_status, status)
    if path.startswith(("/team-mcp/v1/", "/team-connectors/v1/")):
        from .team_connectors import API_ERRORS
        _raise_listed(code, API_ERRORS, client_status, status)
    if path.startswith("/native-groups/v1/"):
        from shared_context.native_group_wire import NATIVE_GROUP_ERRORS
        _raise_listed(code, NATIVE_GROUP_ERRORS, status, status)
    routine_definitions = _ROUTINE_DEFINITIONS.fullmatch(path) is not None
    if path.startswith("/native-hitl/v1/") or routine_definitions:
        from shared_context.native_hitl_wire import NATIVE_HITL_ERRORS
        from shared_context.native_routine_wire import NATIVE_ROUTINE_ERRORS
        # An owner's allow on a routine card first saves its schedule, so it can return a routine code.
        _raise_listed(code, NATIVE_ROUTINE_ERRORS | (frozenset() if routine_definitions else NATIVE_HITL_ERRORS),
                      status, status)


def _not_dispatched(method, path, body, status, error, code):
    # The server says this send certainly did not go out (rid:<RID> was not created). API #680
    # sets it only then, on a 4xx refusal or a 5xx whose uncertainty is about a version follow,
    # not this message. Without it a 5xx stays UNKNOWN. Its own code is kept, whatever it is.
    # A cancel or delivery acknowledgement carries it when no session was invoked (#813).
    return (method == "POST" and path in _NATIVE_NOT_DISPATCHED_PATHS
            and 400 <= status < 600 and type(code) is str
            and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code) is not None
            and error.get("not_dispatched") is True and isinstance(body, dict)
            and error.get("request_id") == body.get("request_id"))


def _raise_native_turn_error(method, path, body, status, error, code):
    from shared_context.behavior_native_turn_wire import NATIVE_TURN_ERRORS
    not_dispatched = _not_dispatched(method, path, body, status, error, code)
    server_following = (not_dispatched and error.get("server_following") is True
                        and (status, code) in _SERVER_FOLLOWING)
    if not_dispatched or type(code) is str and code in NATIVE_TURN_ERRORS | _NATIVE_EXTRA_CODES:
        raise ConsoleError(code, status, status, not_dispatched=not_dispatched, server_following=server_following)


def _raise_realtime_error(method, path, status, code):
    if method == "GET" and path == "/work-policy/v1/realtime/negotiate":
        _raise_listed(code, {"REALTIME_DISABLED", "REALTIME_UNAVAILABLE"}, 503, status)


def _raise_work_policy_error(method, path, body, status, error, code):
    _raise_realtime_error(method, path, status, code)
    if path == "/work-policy/v1/agent-connectors" and 400 <= status < 500:
        from .team_connectors import AGENT_API_ERRORS
        _raise_listed(code, AGENT_API_ERRORS, status, status)
    if path.startswith((_NATIVE_TURNS, _RESPONSES_TURNS)):
        _raise_native_turn_error(method, path, body, status, error, code)
    for applies, codes in (
            (_is_delegation_read(method, path, body), _DELEGATION_CODES), (True, _CONFLICT_CODES),
            (method == "GET" and path == _TEMPLATE and status == 409, {"AGENT_TEMPLATE_PROFILE_UNSUPPORTED"}),
            (method == "GET" and path == _TEMPLATE + "/system-prompt", _SYSTEM_PROMPT_CODES),
            (path in _SELECTION_PATHS, _SELECTION_CODES)):
        if applies:
            _raise_listed(code, codes, 409, status)


def _raise_known_error(method, path, body, status, error, code):
    """Raise the API's own error code for a non-success response when this route lists it; else return."""
    _raise_service_error(path, status, code)
    if path.startswith("/work-policy/v1/"):
        _raise_work_policy_error(method, path, body, status, error, code)


class PullAdapter:
    def __init__(self, config, client, *, api_prefix=""):
        if api_prefix:
            require(api_prefix.startswith("/agents/"), "ORIGIN_DENIED", 403)
            try:
                wire.Command(request_id=api_prefix.removeprefix("/agents/"))
            except ValueError:
                raise ConsoleError("ORIGIN_DENIED", 403) from None
        self.config, self.client = config, client
        self.api_prefix = api_prefix

    def request(self, task, action):
        root = "/behavior/v1/bindings/"+task.binding_id
        if action == "status":
            return "GET", root+"/jobs/"+task.job_id, None
        if action == "submit":
            model = AssistantSubmit if task.submission == "assistant" else wire.SubmitWork
            return "POST", root+("/assistant/submit" if task.submission == "assistant" else "/work/submit"), model(
                request_id=task.submit_request_id).model_dump(mode="json")
        if action == "fetch":
            return "POST", root+"/delivery/pull", wire.Command(request_id=task.fetch_request_id).model_dump(mode="json")
        raise ConsoleError("ACTION_NOT_ALLOWED", 403)

    def send(self, method, path, body, token, *, limit=131072, params=None, if_match=None):
        url = self.config.backend_origin+self.api_prefix+path
        _check_origin(url, self.config.backend_origin, path)
        _check_if_match(method, path, if_match)
        if params:
            _check_params(method, path, body, params)
        delegation_read = _is_delegation_read(method, path, body)
        retry_hint = False
        try:
            start = time.monotonic()
            with self.client.stream(method, url, params=params, content=None if body is None else canonical(body),
                                    headers=_request_headers(token, if_match), follow_redirects=False) as response:
                data = _read_limited(response, token, limit, start)
                status = response.status_code
                if status == 204 and delegation_read:
                    _require_no_store(data, response.headers)
                    return None, response.headers
                if status != 200 and not _accepted(method, status, path):
                    detail, code = _problem_for(method, path, data)
                    retry_hint = _retry_hint(method, detail)
                    _raise_known_error(method, path, body, status, detail, code)
                    raise ConsoleError(STATUS_ERRORS.get(status, "BACKEND_UNAVAILABLE"), 409, status)
                return bytes(data), response.headers
        except ConsoleError as error:
            if retry_hint:
                error.automatic_retry = True
            raise
        except (httpx.HTTPError, OSError):
            raise ConsoleError("BACKEND_OFFLINE", 503) from None

    def status(self, task, token):
        data, _ = self.send(*self.request(task, "status"), token)
        try:
            strict_json(data)
            status = wire.JobStatus.model_validate_json(data)
        except (ValueError, TypeError):
            raise ConsoleError("STATUS_INVALID", 502) from None
        require(status.job_id == task.job_id and status.work_id == task.work_id, "STATUS_BINDING_MISMATCH", 502)
        return status

    def submit(self, task, token):
        data, _ = self.send(*self.request(task, "submit"), token)
        try:
            strict_json(data)
            status = wire.JobStatus.model_validate_json(data)
        except (ValueError, TypeError):
            raise ConsoleError("STATUS_INVALID", 502) from None
        require(status.job_id == task.job_id and status.work_id == task.work_id, "STATUS_BINDING_MISMATCH", 502)
        return status

    def fetch(self, task, token, delivery_id):
        data, headers = self.send(*self.request(task, "fetch"), token, limit=task.max_payload_bytes)
        result = checked_result(task, data, headers, delivery_id)
        require(result.parsed.content is None or token.encode() not in result.parsed.content, "RESULT_INVALID", 502)
        return result

    def ack_body(self, task, result):
        return wire.PullAcknowledgment(request_id=task.ack_request_id, exchange_id=result.exchange_id,
            acknowledgment_token=result.token.get_secret_value(), binding=result.binding).model_dump(mode="json")

    def acknowledge(self, task, result, token):
        data, _ = self.send("POST", "/behavior/v1/bindings/"+task.binding_id+"/delivery/acknowledge",
                            self.ack_body(task, result), token)
        try:
            receipt = strict_json(data)
            value = receipt["result"]
            evidence = control_key("inbox-ack-evidence", result.binding.delivery_id)
            require(set(receipt) == {"result", "control_request_id", "control_request_digest"}
                    and receipt["control_request_id"] == control_key(
                        "delivery-request", "receipt", task.obligation_id, result.binding.delivery_id, evidence)
                    and re.fullmatch(r"[0-9a-f]{64}", receipt["control_request_digest"]) is not None
                    and value["obligation_id"] == task.obligation_id
                    and value["fulfillment"] == "fulfilled"
                    and value["delivery_state"] in {"fulfilled", "delivered"}
                    and value["required_confirmation_level"] == "client_received"
                    and value["confirmed_levels"] == ["client_received"]
                    and value["channel_version"] == task.channel_version and value["fulfillment_receipt_id"] == evidence
                    and re.fullmatch(r"[0-9a-f]{64}", value["evidence_digest"]) is not None,
                    "ACK_UNCONFIRMED", 502)
            allowed = {"actual_environment", "execution_enabled", "obligation_id", "fulfillment", "delivery_state",
                       "required_confirmation_level", "confirmed_levels", "channel_version",
                       "fulfillment_receipt_id", "evidence_digest"}
            require(set(value) <= allowed, "ACK_UNCONFIRMED", 502)
        except (ValueError, TypeError, KeyError):
            raise ConsoleError("ACK_UNCONFIRMED", 502) from None
        return receipt
