"""Read only the fixed core 2.2 task contract; never reclaim, cancel or rewrite a lease."""

from datetime import datetime, timezone
import json
from urllib.parse import quote

import httpx
from azure.ai.agentserver.core import get_request_context
from azure.core.exceptions import AzureError

from .errors import Problem

_AUXILIARY_TASKS = (
    "fa-native-terminal-notification-v1",
    "fa-native-agent-enqueue-v1",
    "fa-native-version-message-v1",
)
_RESPONSES_TASKS = ("responses_resilient_one_shot", "responses_resilient_multi_turn")


class NativeTaskObserver:
    def __init__(self, endpoint, identity, token_provider, *, transport=None):
        self.endpoint, self.identity, self.token_provider = endpoint.rstrip("/"), identity, token_provider
        self.transport = transport

    async def _get(self, suffix="", *, params=None, missing=False):
        try:
            token = await self.token_provider()
            if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
                raise Problem(503, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the original task credential is unavailable")
            headers = {"Authorization": "Bearer " + token, "Accept-Encoding": "identity"}
            call_id = get_request_context().call_id
            if call_id is not None:
                headers["x-agent-foundry-call-id"] = call_id
            async with httpx.AsyncClient(
                transport=self.transport or httpx.AsyncHTTPTransport(retries=0),
                trust_env=False, follow_redirects=False, timeout=httpx.Timeout(10, connect=5),
            ) as client:
                async with client.stream("GET", self.endpoint + "/tasks" + suffix,
                                         params={"api-version": "v1", **(params or {})}, headers=headers) as response:
                    if response.status_code == 404 and missing:
                        return None
                    if response.status_code != 200:
                        raise Problem(response.status_code if response.status_code in {401, 403} else 503,
                                      "NATIVE_ENVIRONMENT_UNAVAILABLE", "the original task record could not be read")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > 1_000_000:
                            raise ValueError("task metadata exceeds the fixed bound")
                        raw.extend(chunk)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("task metadata must be an object")
            return value
        except (AzureError, OSError, httpx.HTTPError, ValueError):
            raise Problem(503, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the original task observation is unconfirmed") from None

    def _scope(self, value, *, task_id=None, source_sid=None, task_name=None):
        if (not isinstance(value, dict) or value.get("agent_name") != self.identity.agent_name
                or not isinstance(value.get("id"), str)
                or task_id is not None and value["id"] != task_id
                or source_sid is not None and value.get("session_id") != source_sid
                or not isinstance(value.get("session_id"), str)):
            raise Problem(503, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the task observation belongs to another scope")
        if task_name is not None and (
                not isinstance(value.get("tags"), dict) or value["tags"].get("task_name") != task_name):
            raise Problem(503, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the task function filter was not retained")
        return value

    async def original(self, *, task_id=None):
        task_id = self.identity.agent_id if task_id is None else task_id
        value = await self._get("/" + quote(task_id, safe=""), missing=True)
        return None if value is None else self._scope(value, task_id=task_id)

    async def quiet(self, source_sid, *, task_id=None, include_responses=False):
        original = await self.original(task_id=task_id)
        if original is not None:
            if original.get("status") != "suspended" or original.get("lease") is not None:
                return False
            payload = original.get("payload")
            if not isinstance(payload, dict):
                raise Problem(503, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the original task payload is unavailable")
            steering = payload.get("steering")
            if steering is not None:
                if not isinstance(steering, dict):
                    raise Problem(503, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the original steering record is invalid")
                pending = steering.get("pending_inputs", [])
                if not isinstance(pending, list):
                    raise Problem(503, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the original pending inputs are invalid")
                if pending or steering.get("active_input") is not None:
                    return False
        for task_name in _AUXILIARY_TASKS + (_RESPONSES_TASKS if include_responses else ()):
            page = await self._get(params={
                "agent_name": self.identity.agent_name, "session_id": source_sid, "status": "in_progress",
                "source_type": "agentserver.task", "tag.task_name": task_name,
                "omit_attachment_values": "true", "limit": 1,
            })
            rows = page.get("data", page.get("items"))
            if not isinstance(rows, list) or type(page.get("has_more")) is not bool:
                raise Problem(503, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the task list page is incomplete")
            if rows:
                for row in rows:
                    self._scope(row, source_sid=source_sid, task_name=task_name)
                return False
            if page["has_more"]:
                raise Problem(503, "NATIVE_ENVIRONMENT_UNAVAILABLE", "an empty task page still has unobserved work")
        return True

    async def suspended_input(self, *, task_id=None):
        original = await self.original(task_id=task_id)
        if original is None or original.get("status") != "suspended" or original.get("lease") is not None:
            return None
        source = original.get("source")
        if not isinstance(source, dict) or (source.get("type"), source.get("name")) != (
                "agentserver.task", "fa-native-agent-turn-v1"):
            raise Problem(503, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the suspended task is not the original native chain")
        payload = original.get("payload")
        if not isinstance(payload, dict):
            raise Problem(503, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the suspended task input is unavailable")
        steering = payload.get("steering")
        if steering is not None:
            if not isinstance(steering, dict) or not isinstance(steering.get("pending_inputs", []), list):
                raise Problem(503, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the suspended steering record is invalid")
            if steering.get("pending_inputs") or steering.get("active_input") is not None:
                return None
        value = payload.get("input")
        if value is None:
            return None
        if (not isinstance(value, dict) or "__attachment_ref__" in value
                or payload.get("last_input_id") != value.get("request_id")):
            raise Problem(503, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the original suspended input cannot be confirmed")
        return value

    async def owned_input(self, *, task_id=None):
        original = await self.original(task_id=task_id)
        if original is None or original.get("status") != "in_progress":
            return None
        lease = original.get("lease")
        expected = self.identity.agent_name + "|session:" + self.identity.session_id
        if not isinstance(lease, dict) or lease.get("owner") != expected:
            raise Problem(503, "AGENT_VERSION_FOLLOWING", "the original task is held by another session")
        if original["session_id"] == self.identity.session_id:
            return None
        try:
            expiry = datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                raise ValueError("lease timestamp needs an offset")
        except (ValueError, TypeError, KeyError, AttributeError):
            raise Problem(503, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the original lease expiry is unconfirmed") from None
        if expiry > datetime.now(timezone.utc):
            raise Problem(503, "AGENT_VERSION_FOLLOWING", "the original owned lease is still active")
        payload = original.get("payload")
        if not isinstance(payload, dict):
            raise Problem(503, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the persisted original input is unavailable")
        steering = payload.get("steering")
        value = (steering["active_input"] if isinstance(steering, dict)
                 and steering.get("drain_in_progress") is True and "active_input" in steering else payload.get("input"))
        if not isinstance(value, dict) or "__attachment_ref__" in value:
            raise Problem(503, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the original N1 input is not inline")
        return value
