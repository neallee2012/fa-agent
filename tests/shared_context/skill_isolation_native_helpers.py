"""從 test_skill_isolation_native.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

import hashlib
import json
import os
from pathlib import Path
import httpx
from copilot import CopilotRequestHandler
from sdk_checkpoint_local import sse


CANARY_NAME = "sk12-task-local-canary"


CANARY_MARKER = "SK12_NATIVE_PERSONAL_CANARY_83D4"


WITNESS_URL = "https://sk12.invalid/openai/v1/chat/completions"


class ProbeBlocked(RuntimeError):
    pass


def require(value, code):
    if not value:
        raise AssertionError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def owned_path(value, root):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(root):
        raise ProbeBlocked("DISCOVERY_ROOT_OUTSIDE_TASK")
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ProbeBlocked("DISCOVERY_ROOT_REPARSE_POINT")
    if not path.resolve().is_relative_to(root):
        raise ProbeBlocked("DISCOVERY_ROOT_ESCAPE")
    return path


def child_environment(root):
    value = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT")
             if name in os.environ}
    home, temp = root / "home", root / "temp"
    value.update({
        "HOME": str(home), "USERPROFILE": str(home), "HOMEDRIVE": home.drive,
        "HOMEPATH": str(home)[len(home.drive):],
        "APPDATA": str(home / "roaming"), "LOCALAPPDATA": str(home / "local"),
        "XDG_CONFIG_HOME": str(root / "config"), "TMPDIR": str(temp), "TMP": str(temp), "TEMP": str(temp),
        "COPILOT_SKIP_CLI_DOWNLOAD": "1",
    })
    for path in (home, temp, home / "roaming", home / "local", root / "config",
                 root / "work", root / "data"):
        path.mkdir(parents=True, exist_ok=True)
    return value


class WitnessProvider(CopilotRequestHandler):
    def __init__(self, observation):
        self.observation, self.count = observation, 0
        self.request_digests = set()

    async def open_websocket(self, context):
        self.observation.violations.append("UNEXPECTED_WEBSOCKET_TRANSPORT")
        raise ProbeBlocked("UNEXPECTED_WEBSOCKET_TRANSPORT")

    async def send_request(self, request, context):
        observation = self.observation
        if request.method != "POST" or str(request.url) != WITNESS_URL:
            observation.violations.append("UNEXPECTED_HTTP_DESTINATION")
            raise ProbeBlocked("UNEXPECTED_HTTP_DESTINATION")
        self.count += 1
        packet = json.loads(request.content)
        streaming = packet.get("stream")
        body_sha = hashlib.sha256(request.content).hexdigest()
        catalog = json.dumps(packet["tools"]) + json.dumps([
            message for message in packet["messages"] if message.get("role") == "system"
        ])
        recorded = {"phase": "witness", "ordinal": self.count,
                    "body_sha256": body_sha,
                    "requested_streaming": streaming if type(streaming) is bool else None,
                    "stream_field_present": "stream" in packet,
                    "canary_name_visible": CANARY_NAME in json.dumps(packet),
                    "catalog_name_visible": CANARY_NAME in catalog,
                    "canary_body_visible": CANARY_MARKER in json.dumps(packet)}
        observation.requests.append(recorded)
        if streaming is not None and type(streaming) is not bool:
            observation.diagnostic_hint = "WITNESS_STREAM_SHAPE_UNSUPPORTED"
            raise ProbeBlocked(observation.diagnostic_hint)
        if body_sha in self.request_digests:
            recorded["classification"] = "retry_same_digest"
            observation.diagnostic_hint = "REPEATED_WITNESS_REQUEST"
            raise ProbeBlocked(observation.diagnostic_hint)
        self.request_digests.add(body_sha)
        recorded["classification"] = "new_request_digest"
        require(len(self.request_digests) <= 2, "WITNESS_REQUEST_BUDGET_EXCEEDED")

        def response(delta):
            try:
                if streaming is True:
                    result = sse(request, delta, len(self.request_digests))
                else:
                    message = {"role": "assistant", **delta}
                    if "tool_calls" in delta:
                        message["content"] = None
                        message["tool_calls"] = [
                            {name: call[name] for name in ("id", "type", "function")}
                            for call in delta["tool_calls"]
                        ]
                    result = httpx.Response(200, request=request, json={
                        "id": f"offline-{len(self.request_digests)}",
                        "object": "chat.completion", "created": 1, "model": "gpt-4.1-mini",
                        "choices": [{"index": 0, "message": message,
                                     "finish_reason": "tool_calls" if "tool_calls" in delta else "stop"}],
                        "usage": {"prompt_tokens": 1500, "completion_tokens": 100, "total_tokens": 1600},
                    })
            except Exception as error:
                recorded["response_error_type"] = type(error).__name__
                raise
            recorded.update(response_status=result.status_code,
                            response_content_type=result.headers.get("content-type"),
                            response_sha256=hashlib.sha256(result.content).hexdigest())
            return result

        tool = next((item["function"] for item in packet["tools"] if item["function"]["name"] == "skill"), None)
        if len(self.request_digests) == 1:
            schema = None if tool is None else tool.get("parameters")
            recorded["skill_schema_sha256"] = None if schema is None else digest(schema)
            if not isinstance(schema, dict) or "skill" not in schema.get("properties", {}):
                observation.diagnostic_hint = "NATIVE_SKILL_SCHEMA_NOT_ESTABLISHED"
                return response({"content": "The probe cannot invoke an unsupported skill schema."})
            require(CANARY_MARKER not in json.dumps(packet), "WITNESS_PROGRESSIVE_BODY_DIFFERS")
            delta = {"tool_calls": [{"index": 0, "id": "sk12-witness-read", "type": "function",
                                     "function": {"name": "skill", "arguments": json.dumps({"skill": CANARY_NAME})}}]}
        else:
            if CANARY_MARKER not in json.dumps(packet):
                observation.diagnostic_hint = "NATIVE_CANARY_LOAD_NOT_ESTABLISHED"
                return response({"content": "The probe did not establish the native skill body."})
            delta = {"content": "The inert isolation witness was observed."}
        return response(delta)
