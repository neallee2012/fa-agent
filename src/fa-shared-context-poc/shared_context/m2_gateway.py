"""M2 固定模型 relay；SSE 完整緩衝後原樣回傳，不是即時逐段串流。

需同時設定 CONTEXT_M2_RUN_ID、CONTEXT_M2_MODEL、CONTEXT_M2_MAX_REQUESTS。
每個 run 使用一筆持久 CAS row；送出前先保留 attempt，失敗不退回額度。
原始 max_requests 固定在 1..120；可由操作人員成對設定 CONTEXT_M2_ADDITIONAL_REQUESTS
（canonical 整數 1..40）與 CONTEXT_M2_AMENDMENT_ID（Identifier），明確增加一次額度。
首次已授權存取以既有 ETag CAS 寫入 amendment；不建立替代 run，不改舊紀錄的語意值。
已保存的 amendment 設定不可更換或移除；寫入衝突／不確定時不重試、不轉送模型。
budget 的 max_requests 是有效上限，original_max_requests 保留原始上限，amendment 未設定時為 null。
儲存時 attempt／usage 使用短欄位別名，仍接受舊長欄位；公開 records 始終使用原本長欄位。
budget 的 upstream_response 只表示 API 收到上游回覆，不表示用戶端收到。
請求上限 4 MiB；轉送 JSON 清空 image URL 後，以 UTF-8 序列化計算，上限 128 KiB。
最多一張內嵌 PNG，上限 256 KiB、1280x800；超限直接拒絕，不截斷文字或自動刪除圖片。
所有轉送請求固定 parallel_tool_calls=false，即使用戶端指定 true。
成功回覆上限 2 MiB；原生錯誤保留 HTTP status，超過 64 KiB 時明示截斷。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import struct
from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated, Callable, Literal, Mapping
from uuid import uuid4

import anyio
import httpx
from azure.core.credentials import TokenCredential
from azure.core.exceptions import AzureError
from fastapi import Depends, FastAPI, Request, Response
from pydantic import ConfigDict, Field, JsonValue, TypeAdapter, ValidationError, field_validator, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .auth import Actor
from .backup_models import BackupETag, BackupId, BackupSHA256, OwnerOid
from .errors import Problem
from .models import ID_PATTERN, Identifier, StrictModel
from .service import ContextService
from .storage import Record

CHAT_PATH = "/model/v1/chat/completions"
BUDGET_PATH = "/model/v1/budget"
MODEL = "gpt-4.1-mini"
MODEL_VERSION = "2025-04-14"
MODEL_SCOPE = "https://ai.azure.com/.default"
MAX_MODEL_BODY_BYTES = 4 * 1024 * 1024
MAX_MODEL_TEXT_BYTES = 128 * 1024
MAX_MODEL_IMAGE_BYTES = 256 * 1024
MAX_MODEL_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_MODEL_ERROR_BYTES = 64 * 1024
RESPONSE_HEADERS = {
    "content-type", "x-request-id", "x-ms-request-id", "apim-request-id", "retry-after",
    "x-ratelimit-limit-requests", "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens",
}
MILESTONE_PREFIXES = {"m2": "", "m3": "/m3", "m4": "/m4"}


@dataclass(frozen=True)
class M2Settings:
    run_id: str
    model: str
    max_requests: int
    additional_requests: int | None = None
    amendment_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.run_id, str) or not re.fullmatch(ID_PATTERN, self.run_id):
            raise ValueError("CONTEXT_M2_RUN_ID must match ID_PATTERN")
        if self.model != MODEL:
            raise ValueError("CONTEXT_M2_MODEL must be gpt-4.1-mini")
        if type(self.max_requests) is not int or not 1 <= self.max_requests <= 120:
            raise ValueError("CONTEXT_M2_MAX_REQUESTS must be 1..120")
        if (self.additional_requests is None) != (self.amendment_id is None):
            raise ValueError("CONTEXT_M2_ADDITIONAL_REQUESTS and CONTEXT_M2_AMENDMENT_ID must be configured together")
        if self.additional_requests is not None:
            if type(self.additional_requests) is not int or not 1 <= self.additional_requests <= 40:
                raise ValueError("CONTEXT_M2_ADDITIONAL_REQUESTS must be 1..40")
            if not isinstance(self.amendment_id, str) or not re.fullmatch(ID_PATTERN, self.amendment_id):
                raise ValueError("CONTEXT_M2_AMENDMENT_ID must match ID_PATTERN")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> M2Settings | None:
        values = os.environ if environ is None else environ
        names = ("CONTEXT_M2_RUN_ID", "CONTEXT_M2_MODEL", "CONTEXT_M2_MAX_REQUESTS")
        amendment_names = ("CONTEXT_M2_ADDITIONAL_REQUESTS", "CONTEXT_M2_AMENDMENT_ID")
        if not any(name in values for name in names + amendment_names):
            return None
        if not all(name in values for name in names):
            raise ValueError("CONTEXT_M2_RUN_ID, CONTEXT_M2_MODEL and CONTEXT_M2_MAX_REQUESTS must be configured together")
        if not re.fullmatch(r"[1-9][0-9]{0,2}", values[names[2]]):
            raise ValueError("CONTEXT_M2_MAX_REQUESTS must be a canonical integer in 1..120")
        additional, amendment_id = None, None
        if any(name in values for name in amendment_names):
            if not all(name in values for name in amendment_names):
                raise ValueError("CONTEXT_M2_ADDITIONAL_REQUESTS and CONTEXT_M2_AMENDMENT_ID must be configured together")
            if not re.fullmatch(r"[1-9][0-9]?", values[amendment_names[0]]):
                raise ValueError("CONTEXT_M2_ADDITIONAL_REQUESTS must be a canonical integer in 1..40")
            additional, amendment_id = int(values[amendment_names[0]]), values[amendment_names[1]]
        return cls(values[names[0]], values[names[1]], int(values[names[2]]), additional, amendment_id)


@dataclass(frozen=True)
class M4ModelSettings(M2Settings):
    def __post_init__(self):
        from .m4_config import ADDITIONAL_REQUESTS, AMENDMENT_ID, MAX_REQUESTS
        from .m4_fixtures import SUITE

        if (
            self.run_id != SUITE or self.model != MODEL or type(self.max_requests) is not int
            or self.max_requests != MAX_REQUESTS
            or (self.additional_requests is not None and type(self.additional_requests) is not int)
            or (self.additional_requests, self.amendment_id) not in (
                (None, None), (ADDITIONAL_REQUESTS, AMENDMENT_ID),
            )
        ):
            raise ValueError("M4 requires its original run/budget and exact approved allowance amendment")


class _CompactRecord(StrictModel):
    model_config = ConfigDict(populate_by_name=True)


class _Usage(_CompactRecord):
    prompt_tokens: Annotated[int, Field(ge=0, le=10 ** 9, alias="p")]
    completion_tokens: Annotated[int, Field(ge=0, le=10 ** 9, alias="c")]
    total_tokens: Annotated[int, Field(ge=0, le=10 ** 9, alias="t")]


class _Attempt(_CompactRecord):
    attempt_id: BackupId = Field(alias="i")
    owner_oid: OwnerOid = Field(alias="o")
    state: Literal["unknown", "upstream_response"] = Field(default="unknown", alias="s")
    upstream_status: Annotated[int, Field(ge=100, le=599)] | None = Field(default=None, alias="h")
    request_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:/-]{1,128}$")] | None = Field(default=None, alias="r")
    usage: _Usage | None = Field(default=None, alias="u")


class _Amendment(StrictModel):
    id: Identifier
    additional_requests: Annotated[int, Field(ge=1, le=40)]


class _Counter(StrictModel):
    run_id: Identifier
    binding: BackupSHA256
    max_requests: Annotated[int, Field(ge=1, le=120)]
    entries: Annotated[list[_Attempt], Field(max_length=160)] = Field(default_factory=list)
    amendment: _Amendment | None = None

    @property
    def effective_max_requests(self) -> int:
        return self.max_requests + (self.amendment.additional_requests if self.amendment is not None else 0)

    @model_validator(mode="after")
    def valid_entries(self):
        ids = [entry.attempt_id for entry in self.entries]
        if len(ids) != len(set(ids)) or len(ids) > self.effective_max_requests:
            raise ValueError("invalid persisted model attempt counter")
        return self


class _M4Amendment(_Amendment):
    additional_requests: Annotated[int, Field(ge=1, le=120)]


class _M4Counter(_Counter):
    entries: Annotated[list[_Attempt], Field(max_length=180)] = Field(default_factory=list)
    amendment: _M4Amendment | None = None


_counter_etag = TypeAdapter(BackupETag)

_Text = Annotated[str, Field(max_length=MAX_MODEL_TEXT_BYTES)]
_Name = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
_CallId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
_Tokens = Annotated[int, Field(ge=1, le=1024)]


class _TextPart(StrictModel):
    type: Literal["text"]
    text: _Text


class _ImageURL(StrictModel):
    url: Annotated[str, Field(max_length=MAX_MODEL_BODY_BYTES)]
    detail: Literal["auto", "low", "high"] = "auto"

    @field_validator("url")
    @classmethod
    def bounded_png(cls, value: str) -> str:
        prefix = "data:image/png;base64,"
        if not value.startswith(prefix):
            raise ValueError("only inline PNG images are supported")
        try:
            data = base64.b64decode(value[len(prefix):], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid PNG base64") from exc
        if (not 24 <= len(data) <= MAX_MODEL_IMAGE_BYTES or data[:8] != b"\x89PNG\r\n\x1a\n"
                or data[8:16] != b"\x00\x00\x00\rIHDR"):
            raise ValueError("invalid or oversized PNG")
        width, height = struct.unpack(">II", data[16:24])
        if not 1 <= width <= 1280 or not 1 <= height <= 800:
            raise ValueError("PNG dimensions exceed the gateway limit")
        return value


class _ImagePart(StrictModel):
    type: Literal["image_url"]
    image_url: _ImageURL


class _FunctionCall(StrictModel):
    name: _Name
    arguments: _Text


class _ToolCall(StrictModel):
    id: _CallId
    type: Literal["function"]
    function: _FunctionCall


class _CopilotToolSource(StrictModel):
    kind: Literal["tool"]
    name: _Name


class _Message(StrictModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: _Text | Annotated[list[_TextPart | _ImagePart], Field(min_length=1, max_length=32)] | None = None
    name: _Name | None = None
    tool_call_id: _CallId | None = None
    tool_calls: Annotated[list[_ToolCall], Field(min_length=1, max_length=64)] | None = None
    refusal: Annotated[str, Field(max_length=16384)] | None = None
    copilot_tool_source: _CopilotToolSource | None = Field(default=None, alias="__copilotMessageSource", exclude=True)

    @model_validator(mode="after")
    def role_fields(self):
        if self.copilot_tool_source is not None and self.role != "tool":
            raise ValueError("Copilot tool source metadata requires a tool message")
        if self.role != "assistant" and {"tool_calls", "refusal"} & self.model_fields_set:
            raise ValueError("assistant-only fields")
        if self.role != "tool" and "tool_call_id" in self.model_fields_set:
            raise ValueError("tool_call_id requires the tool role")
        if self.role == "tool" and (not self.tool_call_id or not isinstance(self.content, str)):
            raise ValueError("tool messages require text and tool_call_id")
        if self.content is None and not (self.role == "assistant" and (self.tool_calls or self.refusal)):
            raise ValueError("message content is required")
        if self.role != "user" and isinstance(self.content, list):
            if any(isinstance(part, _ImagePart) for part in self.content):
                raise ValueError("images are supported only in user messages")
        return self


class _FunctionDefinition(StrictModel):
    name: _Name
    description: Annotated[str, Field(max_length=16384)] | None = None
    parameters: dict[str, JsonValue]
    strict: bool | None = None

    @field_validator("parameters")
    @classmethod
    def object_parameters(cls, value: dict) -> dict:
        if value.get("type") != "object":
            raise ValueError("tool parameters must be an object schema")
        return value


class _Tool(StrictModel):
    type: Literal["function"]
    function: _FunctionDefinition


class _FunctionName(StrictModel):
    name: _Name


class _ToolChoice(StrictModel):
    type: Literal["function"]
    function: _FunctionName


class _StreamOptions(StrictModel):
    include_usage: bool = False


class _ChatRequest(StrictModel):
    model: Literal["gpt-4.1-mini"] = MODEL
    messages: Annotated[list[_Message], Field(min_length=1, max_length=128)]
    tools: Annotated[list[_Tool], Field(max_length=64)] | None = None
    tool_choice: Literal["none", "auto", "required"] | _ToolChoice | None = None
    parallel_tool_calls: bool | None = None
    stream: bool = False
    stream_options: _StreamOptions | None = None
    max_tokens: _Tokens = 1024
    max_completion_tokens: _Tokens | None = None
    n: Annotated[int, Field(ge=1, le=1)] = 1
    store: bool = False
    temperature: Annotated[float, Field(ge=0, le=2, allow_inf_nan=False)] | None = None
    top_p: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None = None
    frequency_penalty: Annotated[float, Field(ge=-2, le=2, allow_inf_nan=False)] | None = None
    presence_penalty: Annotated[float, Field(ge=-2, le=2, allow_inf_nan=False)] | None = None
    seed: Annotated[int, Field(ge=-(2 ** 31), lt=2 ** 31)] | None = None
    stop: Annotated[str, Field(max_length=256)] | Annotated[
        list[Annotated[str, Field(max_length=256)]], Field(min_length=1, max_length=4)
    ] | None = None

    @model_validator(mode="after")
    def supported_controls(self):
        if self.store or {"max_tokens", "max_completion_tokens"} <= self.model_fields_set:
            raise ValueError("store must be false and only one token limit is allowed")
        if self.stream_options is not None and not self.stream:
            raise ValueError("stream_options requires stream=true")
        names = [tool.function.name for tool in self.tools or []]
        if len(names) != len(set(names)):
            raise ValueError("tool names must be unique")
        if isinstance(self.tool_choice, _ToolChoice) and self.tool_choice.function.name not in names:
            raise ValueError("tool_choice must name a supplied tool")
        if self.tool_choice == "required" and not names:
            raise ValueError("required tool_choice needs tools")
        return self


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(_value: str):
    raise ValueError("nonfinite JSON number")


def _bounded_tree(value: JsonValue, depth: int = 0) -> int:
    if depth > 20:
        raise ValueError("JSON nesting exceeds 20 levels")
    if isinstance(value, dict):
        if any(len(key) > 256 for key in value):
            raise ValueError("JSON key is too long")
        children = value.values()
    elif isinstance(value, list):
        if len(value) > 2048:
            raise ValueError("JSON array is too large")
        children = value
    else:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("nonfinite JSON number")
        return 1
    nodes = 1
    for child in children:
        nodes += _bounded_tree(child, depth + 1)
        if nodes > 20000:
            raise ValueError("JSON structure exceeds 20000 nodes")
    return nodes


def model_payload(body: bytes) -> dict:
    try:
        raw = json.loads(body, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        _bounded_tree(raw)
        chat = _ChatRequest.model_validate(raw)
        payload = chat.model_dump(exclude_unset=True, exclude_none=True)
        payload.pop("max_completion_tokens", None)
        payload.update(
            model=MODEL, max_tokens=chat.max_completion_tokens or chat.max_tokens, n=1, store=False,
            parallel_tool_calls=False,
        )
        text_only = deepcopy(payload)
        images = 0
        for message in text_only["messages"]:
            if isinstance(message.get("content"), list):
                for part in message["content"]:
                    if part["type"] == "image_url":
                        images += 1
                        part["image_url"]["url"] = ""
        if images > 1 or len(json.dumps(text_only, ensure_ascii=False).encode("utf-8")) > MAX_MODEL_TEXT_BYTES:
            raise ValueError("request exceeds one image or 128 KiB of serialized text/tools")
        return payload
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise Problem(422, "INVALID_MODEL_REQUEST", "unsupported or unbounded model request fields") from exc


def response_usage(body: bytes, streaming: bool) -> _Usage | None:
    """驗證原生完成訊號；不重寫 JSON／SSE，也不補造缺少的 usage。"""
    try:
        if streaming:
            events, done, native_error = [], False, False
            frames = re.split(r"\r?\n\r?\n", body.decode("utf-8"))
            if len(frames) > 8192:
                raise ValueError("too many SSE events")
            for frame in frames:
                data = "\n".join(line[5:].removeprefix(" ") for line in frame.splitlines()
                                 if line.startswith("data:"))
                if not data:
                    continue
                if done or len(data) > 256 * 1024:
                    raise ValueError("invalid SSE event sequence or size")
                if data == "[DONE]":
                    done = True
                    continue
                value = json.loads(data, parse_constant=_invalid_constant)
                if not isinstance(value, dict):
                    raise ValueError("invalid native SSE event")
                if "error" in value or any(line.strip() == "event: error" for line in frame.splitlines()):
                    native_error = True
                else:
                    events.append(value)
            if not done and not native_error:
                raise ValueError("native SSE did not finish")
        else:
            events = [json.loads(body, parse_constant=_invalid_constant)]
            native_error = False
        usage, finished = None, False
        for event in events:
            if not isinstance(event, dict) or not isinstance(event.get("choices"), list):
                raise ValueError("missing native choices")
            choices = event["choices"]
            if len(choices) > 1 or (not streaming and len(choices) != 1):
                raise ValueError("unexpected native choice count")
            if choices:
                if not isinstance(event.get("model"), str) or event["model"] not in {MODEL, f"{MODEL}-{MODEL_VERSION}"}:
                    raise ValueError("unexpected native model")
                if event.get("object") != ("chat.completion.chunk" if streaming else "chat.completion"):
                    raise ValueError("unexpected native response object")
                choice = choices[0]
                if (not isinstance(choice, dict) or type(choice.get("index")) is not int or choice["index"] != 0
                        or not isinstance(choice.get("delta" if streaming else "message"), dict)):
                    raise ValueError("invalid native choice")
                reason = choice.get("finish_reason")
                if reason is not None:
                    if reason not in ("stop", "length", "tool_calls", "content_filter"):
                        raise ValueError("unsupported native finish reason")
                    finished = True
            if event.get("usage") is not None:
                raw_usage = event["usage"]
                if not isinstance(raw_usage, dict):
                    raise ValueError("invalid native usage")
                usage = _Usage.model_validate({key: raw_usage.get(key) for key in _Usage.model_fields})
        if not finished and not native_error:
            raise ValueError("no native completion or error")
        return usage
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise Problem(502, "M2_UPSTREAM_PROTOCOL_ERROR", "native model response is malformed or incomplete") from exc


def response_headers(upstream: httpx.Response, secrets: tuple[str, ...]) -> dict[str, str]:
    headers = {}
    for name, value in upstream.headers.items():
        if name not in RESPONSE_HEADERS:
            continue
        if (not value or len(value) > 256 or any(ord(c) < 32 or ord(c) > 126 for c in value)
                or any(secret and secret in value for secret in secrets)):
            raise Problem(502, "M2_UPSTREAM_PROTOCOL_ERROR", "unsafe native response headers")
        headers[name] = value
    return headers


def is_model_upload(scope: dict, *, milestone: str = "m2") -> bool:
    if milestone not in MILESTONE_PREFIXES:
        raise ValueError("model gateway milestone must be m2, m3 or m4")
    return scope["method"] == "POST" and scope["path"] == MILESTONE_PREFIXES[milestone] + CHAT_PATH


async def read_model_body(request: Request) -> bytes:
    if request.query_params:
        raise Problem(422, "INVALID_MODEL_REQUEST", "model routes do not accept query parameters")
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise Problem(415, "MODEL_CONTENT_TYPE", "Content-Type must be application/json")
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise Problem(415, "MODEL_CONTENT_ENCODING", "encoded requests are not supported")
    declared = request.headers.get("content-length")
    if declared is not None:
        if not re.fullmatch(r"[0-9]{1,10}", declared):
            raise Problem(422, "INVALID_MODEL_REQUEST", "invalid Content-Length")
        if int(declared) > MAX_MODEL_BODY_BYTES:
            raise Problem(413, "MODEL_REQUEST_TOO_LARGE", "model request exceeds 4 MiB")
    parts, total = [], 0
    try:
        with anyio.fail_after(30):
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_MODEL_BODY_BYTES:
                    raise Problem(413, "MODEL_REQUEST_TOO_LARGE", "model request exceeds 4 MiB")
                parts.append(chunk)
    except TimeoutError as exc:
        raise Problem(408, "MODEL_REQUEST_TIMEOUT", "model request body timed out") from exc
    except ClientDisconnect as exc:
        raise Problem(400, "MODEL_REQUEST_INCOMPLETE", "model request was disconnected") from exc
    if declared is not None and int(declared) != total:
        raise Problem(400, "MODEL_REQUEST_INCOMPLETE", "body length differs from Content-Length")
    return b"".join(parts)


class M2Gateway:
    def __init__(self, service: ContextService, settings: M2Settings, project_endpoint: str,
                 credential: TokenCredential, http: httpx.AsyncClient | None = None, *, milestone: str = "m2"):
        if milestone not in MILESTONE_PREFIXES:
            raise ValueError("model gateway milestone must be m2, m3 or m4")
        if milestone == "m3" and (not settings.run_id.startswith("m3-") or settings.amendment_id is not None):
            raise ValueError("M3 requires its own m3- run ID and has no allowance amendment")
        if milestone == "m4":
            M4ModelSettings(settings.run_id, settings.model, settings.max_requests,
                            settings.additional_requests, settings.amendment_id)
        self.milestone = milestone
        self.chat_path = MILESTONE_PREFIXES[milestone] + CHAT_PATH
        self.budget_path = MILESTONE_PREFIXES[milestone] + BUDGET_PATH
        endpoint = project_endpoint.removesuffix("/")
        if not re.fullmatch(
            r"https://[a-z0-9][a-z0-9-]{1,62}\.services\.ai\.azure\.com/api/projects/[A-Za-z0-9][A-Za-z0-9_-]{0,63}",
            endpoint,
        ):
            raise ValueError("M2 requires a canonical, fixed Foundry project endpoint")
        self.service, self.settings, self.credential = service, settings, credential
        self.url = endpoint + "/openai/v1/chat/completions"
        allowed = sorted((a.oid, a.actor, a.team) for a in service.auth.actors.values()
                         if a.actor in {"agent-a", "agent-b"})
        if not allowed or len(allowed) > 2 or len({entry[2] for entry in allowed}) != 1:
            raise ValueError("M2 requires the existing A/B allowlist within one team")
        self.binding = hashlib.sha256(json.dumps(
            [self.url, settings.model, MODEL_VERSION, allowed], separators=(",", ":"),
        ).encode()).hexdigest()
        self.partition = milestone + "-" + service.auth.tenant_id
        self.row = "budget:" + settings.run_id
        amendment_type = _M4Amendment if milestone == "m4" else _Amendment
        self.counter_type = _M4Counter if milestone == "m4" else _Counter
        self.amendment = (
            amendment_type(id=settings.amendment_id, additional_requests=settings.additional_requests)
            if settings.amendment_id is not None and settings.additional_requests is not None else None
        )
        self._counter_seen = False
        self.http = http if http is not None else httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=0), trust_env=False,
            timeout=httpx.Timeout(60, connect=10, write=20, pool=5), follow_redirects=False,
        )

    def authorize(self, actor: Actor) -> None:
        self.service.authorize(actor, actor.team)
        if actor.actor not in {"agent-a", "agent-b"}:
            raise Problem(403, "M2_ACTOR_DENIED", "only the allowlisted M2 actors can use this gateway")

    async def aclose(self) -> None:
        await self.http.aclose()

    def _read_counter(self) -> tuple[Record | None, _Counter]:
        seen_before_read = self._counter_seen
        try:
            record = self.service.repository.get(self.partition, self.row)
        except Problem as exc:
            if exc.status == 404 and exc.body["error"]["code"] == "NOT_FOUND":
                if seen_before_read or self.amendment is not None:
                    raise Problem(502, "M2_COUNTER_UNCERTAIN", "existing model counter required; no reset or new amended run") from exc
                return None, self.counter_type(
                    run_id=self.settings.run_id, binding=self.binding, max_requests=self.settings.max_requests,
                )
            raise
        except (ValueError, KeyError, TypeError) as exc:
            raise Problem(502, "M2_COUNTER_INVALID", "model counter could not be decoded; no model call allowed") from exc
        try:
            state = self.counter_type.model_validate(record.payload)
            _counter_etag.validate_python(record.etag, strict=True)
        except ValidationError as exc:
            raise Problem(502, "M2_COUNTER_INVALID", "persisted model counter is invalid; no model call allowed") from exc
        if (state.run_id != self.settings.run_id or state.binding != self.binding
                or state.max_requests != self.settings.max_requests):
            raise Problem(409, "M2_RUN_CONFIG_MISMATCH", "existing run configuration cannot be changed or reset")
        self._counter_seen = True
        if state.amendment != self.amendment:
            if state.amendment is not None:
                raise Problem(409, "M2_AMENDMENT_MISMATCH", "persisted allowance amendment cannot be changed or removed")
            state.amendment = self.amendment
            record = self._save_counter(record, state)
        return record, state

    def _save_counter(self, record: Record | None, state: _Counter) -> Record:
        payload = state.model_dump(exclude_none=True, by_alias=True)
        self._counter_seen = True
        try:
            saved = (
                self.service.repository.create(self.partition, self.row, payload) if record is None
                else self.service.repository.replace(self.partition, self.row, payload, record.etag)
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise Problem(502, "M2_COUNTER_UNCERTAIN", "counter write acknowledgement is uncertain") from exc
        try:
            _counter_etag.validate_python(saved.etag, strict=True)
        except ValidationError as exc:
            raise Problem(502, "M2_COUNTER_UNCERTAIN", "counter write acknowledgement is invalid") from exc
        if saved.payload != payload:
            raise Problem(502, "M2_COUNTER_UNCERTAIN", "counter differs from its acknowledgement")
        return saved

    def reserve(self, actor: Actor) -> str:
        record, state = self._read_counter()
        if len(state.entries) >= state.effective_max_requests:
            raise Problem(429, "M2_ATTEMPT_LIMIT", "model attempt budget is exhausted")
        attempt_id = uuid4().hex
        state.entries.append(_Attempt(attempt_id=attempt_id, owner_oid=actor.oid))
        self._save_counter(record, state)
        return attempt_id

    def finish(self, attempt_id: str, status: int, request_id: str | None, usage: _Usage | None) -> None:
        record, state = self._read_counter()
        entry = next((entry for entry in state.entries if entry.attempt_id == attempt_id), None)
        if record is None or entry is None:
            raise Problem(502, "M2_COUNTER_UNCERTAIN", "reserved attempt disappeared; model outcome is unknown")
        entry.state, entry.upstream_status, entry.request_id, entry.usage = "upstream_response", status, request_id, usage
        self._save_counter(record, state)

    def budget(self, actor: Actor) -> dict:
        self.authorize(actor)
        _, state = self._read_counter()
        return {
            "run_id": state.run_id, "model": self.settings.model, "model_version": MODEL_VERSION,
            "max_requests": state.effective_max_requests, "original_max_requests": state.max_requests,
            "amendment": state.amendment.model_dump() if state.amendment is not None else None,
            "attempts": len(state.entries), "remaining": state.effective_max_requests - len(state.entries),
            "records": [entry.model_dump() for entry in state.entries],
            "delivery_state": "unknown",
        }

    async def forward(self, actor: Actor, body: bytes, caller_authorization: str = "",
                      *, client_request: Request | None = None) -> Response:
        self.authorize(actor)
        payload = model_payload(body)
        if self.milestone == "m4" and payload.get("stream"):
            payload["stream_options"] = {"include_usage": True}
        attempt_id = await run_in_threadpool(self.reserve, actor)
        if client_request is not None and await client_request.is_disconnected():
            raise Problem(499, "M2_CLIENT_DISCONNECTED", "attempt reserved; client delivery and model outcome are unknown")
        try:
            access = await run_in_threadpool(self.credential.get_token, MODEL_SCOPE)
        except AzureError as exc:
            raise Problem(503, "M2_TOKEN_UNAVAILABLE", "model credential is unavailable") from exc
        if not isinstance(access.token, str) or not 1 <= len(access.token) <= 16384 or any(
            ord(c) < 33 or ord(c) > 126 for c in access.token
        ):
            raise Problem(503, "M2_TOKEN_UNAVAILABLE", "model credential is invalid")
        secrets = (access.token, caller_authorization.partition(" ")[2])
        request = httpx.Request("POST", self.url, headers={
            "Authorization": "Bearer " + access.token, "Content-Type": "application/json",
            "Accept": "text/event-stream" if payload.get("stream") else "application/json",
            "Accept-Encoding": "identity",
        }, content=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        try:
            with anyio.fail_after(90):
                upstream = await self.http.send(request, stream=True, auth=None, follow_redirects=False)
                self.http.cookies.clear()
                try:
                    headers = response_headers(upstream, secrets)
                    if upstream.headers.get("content-encoding", "identity").lower() != "identity":
                        raise Problem(502, "M2_UPSTREAM_PROTOCOL_ERROR", "encoded native response is not supported")
                    expected_type = "text/event-stream" if payload.get("stream") else "application/json"
                    if upstream.status_code < 300 and headers.get("content-type", "").split(";")[0].strip() != expected_type:
                        raise Problem(502, "M2_UPSTREAM_PROTOCOL_ERROR", "unexpected native response content type")
                    parts, total = [], 0
                    limit = MAX_MODEL_RESPONSE_BYTES if upstream.status_code < 300 else MAX_MODEL_ERROR_BYTES
                    truncated = False
                    async for chunk in upstream.aiter_bytes():
                        total += len(chunk)
                        if total > limit:
                            if upstream.status_code < 300:
                                raise Problem(502, "M2_RESPONSE_TOO_LARGE", "model response exceeds 2 MiB")
                            parts.append(chunk[:len(chunk) - (total - limit)])
                            truncated = True
                            break
                        parts.append(chunk)
                    raw = b"".join(parts)
                    if any(secret and secret.encode() in raw for secret in secrets):
                        raise Problem(502, "M2_UPSTREAM_PROTOCOL_ERROR", "native response reflected a credential")
                    usage = response_usage(raw, bool(payload.get("stream"))) if upstream.status_code < 300 else None
                    request_id = next((headers[key] for key in ("x-request-id", "x-ms-request-id", "apim-request-id")
                                       if key in headers), None)
                    if request_id is not None and not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", request_id):
                        raise Problem(502, "M2_UPSTREAM_PROTOCOL_ERROR", "invalid native request ID")
                    if client_request is not None and await client_request.is_disconnected():
                        raise Problem(499, "M2_CLIENT_DISCONNECTED", "client disconnected; reserved attempt remains unknown")
                    await run_in_threadpool(self.finish, attempt_id, upstream.status_code, request_id, usage)
                    if self.milestone == "m4" and upstream.status_code < 300:
                        from .m4_fixtures import PROMPT_TOKENS_LIMIT

                        if usage is None or usage.prompt_tokens <= 0 or (
                            usage.total_tokens != usage.prompt_tokens + usage.completion_tokens
                        ):
                            raise Problem(502, "M4_NATIVE_USAGE_REQUIRED", "M4 requires complete native token usage",
                                          attempt_id=attempt_id)
                        if usage.prompt_tokens > PROMPT_TOKENS_LIMIT:
                            raise Problem(413, "M4_INPUT_TOKEN_LIMIT", "native model input exceeded the M4 token limit",
                                          attempt_id=attempt_id, prompt_tokens=usage.prompt_tokens,
                                          max_prompt_tokens=PROMPT_TOKENS_LIMIT)
                    headers["X-" + self.milestone.upper() + "-Attempt-Id"] = attempt_id
                    if truncated:
                        headers["X-" + self.milestone.upper() + "-Response-Truncated"] = "true"
                    return Response(raw, status_code=upstream.status_code, headers=headers)
                finally:
                    await upstream.aclose()
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise Problem(504, "M2_UPSTREAM_TIMEOUT", "model attempt outcome is unknown; do not blindly retry") from exc
        except httpx.RequestError as exc:
            raise Problem(502, "M2_UPSTREAM_UNAVAILABLE", "model attempt outcome is unknown; do not blindly retry") from exc


def add_model_routes(app: FastAPI, verifier: Callable, gateway: M2Gateway) -> None:
    identity = Depends(verifier)

    @app.post(gateway.chat_path)
    async def completions(request: Request, actor: Actor = identity):
        gateway.authorize(actor)
        return await gateway.forward(
            actor, await read_model_body(request), request.headers.get("authorization", ""), client_request=request,
        )

    @app.get(gateway.budget_path)
    def budget(request: Request, actor: Actor = identity):
        gateway.authorize(actor)
        if request.query_params:
            raise Problem(422, "INVALID_MODEL_REQUEST", "budget does not accept query parameters")
        return gateway.budget(actor)
