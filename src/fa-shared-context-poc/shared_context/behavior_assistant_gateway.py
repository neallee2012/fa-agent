"""One-send model gateway; no counter reset or replay permission.

HTTPX's 60 seconds bound inactivity per socket operation. The model route has
its own absolute 75-second deadline and its caller waits at most 90 seconds.
A timeout or cancelled caller cannot prove provider idle.
"""

import copy
import json
import logging

import httpx
from fastapi import HTTPException

from .behavior_assistant_boundary import AssistantModelBoundary
from .behavior_http_diagnostics import model_failure, safe_key_error
from .behavior_assistant_deadlines import check_server_model, model_sync
from .errors import Problem

MAX_REQUEST_IMAGES = 3
MAX_IMAGE_DATA_URL_CHARS = 1_400_000
CAPABILITY_IMAGE_BODY_BYTES = 6 * 1024 * 1024
_PNG_URL_PREFIX = "data:image/png;base64,"


def _check_images(messages, allow_images):
    count = 0
    for message in messages:
        content = message.get("content", message.get("output")) if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") not in {"image_url", "input_image"}:
                continue
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            count += 1
            if (not allow_images or count > MAX_REQUEST_IMAGES or not isinstance(url, str)
                    or not url.startswith(_PNG_URL_PREFIX) or len(url) > MAX_IMAGE_DATA_URL_CHARS):
                raise Problem(403, "ASSISTANT_IMAGES_DENIED",
                              "images are allowed only for this turn's captured image capability, within its bound")


class AssistantProvider:
    def __init__(self, endpoint, credential, *, transport=None):
        url = httpx.URL(endpoint)
        if (url.scheme != "https" or not url.host or url.username or url.password or url.query or url.fragment
                or not url.path.endswith("/openai/v1/chat/completions")):
            raise Problem(403, "ASSISTANT_PROVIDER_CONFIGURATION", "exact server-owned OpenAI endpoint is required")
        self.endpoint, self.credential, self.transport = endpoint, credential, transport
        self.responses_endpoint = str(endpoint).removesuffix("/chat/completions") + "/responses"
        self.shared_http = None

    async def aclose(self):
        if self.shared_http is not None:
            await self.shared_http.aclose()
            self.shared_http = None

    def validate(self, body, *, text_reply=False, model=None, skill_access=False, user_turn_access=False,
                 work_memory_access=False, sop_copy_access=False, skill_draft_access=False,
                 skill_resource_access=False, user_file_access=False, assistant_reply_access=False,
                 reviewed_note_access=False, capability_tools=frozenset(), allow_images=False, model_provider=None):
        if (capability_tools or allow_images) and not text_reply:
            raise Problem(403, "ASSISTANT_TOOLS_DENIED", "capabilities belong only to free instructions")
        allowed = {
            "model", "messages", "tools", "tool_choice", "stream", "stream_options",
            "max_tokens", "temperature", "top_p", "n", "stop", "parallel_tool_calls",
            "presence_penalty", "frequency_penalty", "store",
            "max_completion_tokens", "reasoning_effort",
        }
        responses = model_provider == "openai-responses"
        if model_provider is not None and (not responses or not text_reply):
            raise Problem(403, "ASSISTANT_PROVIDER_CONFIGURATION", "provider must belong to this original instruction")
        if responses:
            allowed = {
                "model", "input", "instructions", "tools", "tool_choice", "stream", "store", "reasoning",
                "max_output_tokens", "temperature", "top_p", "parallel_tool_calls", "prompt_cache_key", "include",
            }
        messages = body.get("input" if responses else "messages")
        if (set(body) - allowed or body.get("model") != (model if text_reply else "gpt-4.1-mini")
                or not text_reply and body.get("max_tokens") != 1024
                or body.get("stream") is not True or not isinstance(messages, list)
                or not messages or not text_reply and len(messages)>128 or body.get("n", 1) != 1
                or len(json.dumps(body,ensure_ascii=not text_reply).encode()) > (
                    CAPABILITY_IMAGE_BODY_BYTES if allow_images else 128*1024 if text_reply else 24000)):
            raise Problem(422, "ASSISTANT_MODEL_REQUEST_INVALID", "bounded pinned-model request is required")
        if responses:
            if (body.get("store") is not False or body.get("include", []) not in ([], ["reasoning.encrypted_content"])
                    or any(not isinstance(item, dict) or item.get("type", "message") not in (
                        "message", "function_call", "function_call_output", "reasoning") for item in messages)):
                raise Problem(422, "ASSISTANT_MODEL_REQUEST_INVALID", "Responses must carry complete local input without provider storage")
            reasoning = body.get("reasoning")
            if reasoning is not None and (not isinstance(reasoning, dict) or set(reasoning)-{"effort", "summary"}
                    or reasoning.get("summary") not in (None, "auto", "concise", "detailed")
                    or reasoning.get("effort") not in (None, "none", "minimal", "low", "medium", "high", "xhigh")):
                raise Problem(422, "ASSISTANT_MODEL_REQUEST_INVALID", "unsupported Responses reasoning options")
        _check_images(messages, allow_images)
        tools = body.get("tools", [])
        names = {"read_context", "save_result", "finish_task", "execute_operation"}
        if text_reply and skill_access:
            names.add("read_skill")
        if text_reply and skill_access and skill_resource_access:
            names.add("read_skill_resource")
        if text_reply and user_turn_access:
            names.add("read_user_turn")
        if user_file_access:
            if not text_reply or not user_turn_access:
                raise Problem(403, "ASSISTANT_TOOLS_DENIED", "original user-turn capability is required for file reads")
            names.add("read_user_file")
        if assistant_reply_access:
            if not text_reply or not user_turn_access:
                raise Problem(403, "ASSISTANT_TOOLS_DENIED", "original user-turn capability is required for reply reads")
            names.add("read_assistant_reply")
        if text_reply and work_memory_access:
            names.add("read_work_memory")
        if text_reply and reviewed_note_access:
            names.add("read_reviewed_note")
        if text_reply and sop_copy_access:
            names.add("copy_sop_to_skill_draft")
        if text_reply and skill_draft_access:
            names.add("save_skill_draft")
        if text_reply and capability_tools:
            if names & set(capability_tools):
                raise Problem(403, "ASSISTANT_TOOLS_DENIED", "capability tools cannot shadow a base tool")
            names.update(capability_tools)
        if not isinstance(tools, list) or not text_reply and len(tools) > 4 or any(
            not isinstance(tool, dict) or tool.get("type") != "function"
            or responses and not isinstance(tool.get("name"), str)
            or (tool.get("name") if responses else tool.get("function", {}).get("name")) not in names for tool in tools
        ):
            raise Problem(403, "ASSISTANT_TOOLS_DENIED", "only the bounded assistant tool catalog is allowed")

    async def send(self, body, *, before_send=None, text_reply=False, model=None, stream_response=False,
                   skill_access=False, user_turn_access=False, work_memory_access=False, sop_copy_access=False,
                   skill_draft_access=False, skill_resource_access=False, user_file_access=False,
                   assistant_reply_access=False, reviewed_note_access=False, capability_tools=frozenset(),
                   allow_images=False, model_provider=None, diagnostic_keys=()):
        self.validate(body,text_reply=text_reply,model=model,skill_access=skill_access,user_turn_access=user_turn_access,
                      work_memory_access=work_memory_access, sop_copy_access=sop_copy_access,
                      skill_draft_access=skill_draft_access,
                      skill_resource_access=skill_resource_access, user_file_access=user_file_access,
                      assistant_reply_access=assistant_reply_access, reviewed_note_access=reviewed_note_access,
                      capability_tools=capability_tools, allow_images=allow_images, model_provider=model_provider)
        outgoing = copy.deepcopy(body)
        responses = model_provider == "openai-responses"
        endpoint = self.responses_endpoint if responses else self.endpoint
        outgoing["store"] = False
        if not responses:outgoing["n"] = 1
        if not text_reply:outgoing["parallel_tool_calls"]=False
        if text_reply and not responses:
            outgoing["stream_options"]={**outgoing.get("stream_options",{}),"include_usage":True}
        phase, status = "provider_token", None
        client = None
        response = None
        owns_client = self.shared_http is None
        handed_off = False
        try:
            check_server_model()
            token = (await model_sync(self.credential.get_token, "https://ai.azure.com/.default")).token
            if before_send is not None:
                phase = "before_send"
                await before_send()
            check_server_model()
            phase = "provider_request"
            client = self.shared_http or httpx.AsyncClient(
                transport=self.transport or httpx.AsyncHTTPTransport(retries=0),
                trust_env=False, follow_redirects=False, timeout=httpx.Timeout(60, connect=5),
            )
            request = client.build_request("POST", endpoint, json=outgoing, headers={
                "Authorization": "Bearer " + token, "Accept-Encoding": "identity",
            })
            check_server_model()
            response = await client.send(request, stream=True)
            phase, status = "provider_response", response.status_code
            if response.status_code != 200:
                logging.getLogger(__name__).warning(
                    "ASSISTANT_PROVIDER_HTTP_FAILURE status=%s", response.status_code,
                )
                if text_reply:
                    raw=bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw)>8192:break
                    code=None
                    if len(raw)<=8192:
                        try:
                            value=json.loads(raw)
                            code=value.get("error",{}).get("code") if isinstance(value,dict) else None
                        except (ValueError,TypeError,AttributeError):
                            code=None
                    await response.aclose()
                    if code in {"context_length_exceeded","context_window_exceeded"}:
                        raise Problem(413,"ASSISTANT_CONTEXT_LIMIT","selected provider rejected this context; no truncation or retry",
                            upstream_error_code="ASSISTANT_CONTEXT_LIMIT",provider_status=status,
                            upstream_error_phase="provider_response")
                    raise Problem(503,"ASSISTANT_MODEL_OUTCOME_UNKNOWN","original model HTTP request is unresolved",
                        upstream_error_code="ASSISTANT_PROVIDER_HTTP_FAILURE",provider_status=status,
                        upstream_error_phase="provider_response")
            # Credential-containing bodies/headers cannot leave this adapter.
            if text_reply and stream_response:
                from .behavior_instruction_stream import BoundedStream
                if response.headers.get("content-type", "").split(";")[0] != "text/event-stream":
                    await response.aclose()
                    raise Problem(502, "ASSISTANT_MODEL_STREAM_INVALID", "original model stream content type differs")
                result = httpx.Response(200, request=httpx.Request("POST",endpoint),
                    headers={"content-type": "text/event-stream"},
                    stream=BoundedStream(response, close=client.aclose if owns_client else None, check=check_server_model))
                handed_off = True
                return result
            result = await AssistantModelBoundary(None, text_reply=text_reply).response(response)
            check_server_model()
            return result
        except (HTTPException, Problem):
            raise
        except Exception as error:
            detail = model_failure(error, phase=phase)
            if detail["upstream_error_phase"] == "output_boundary":
                detail["upstream_error_phase"] = phase
            detail.setdefault("upstream_error_code", (
                "ASSISTANT_PROVIDER_HTTP_FAILURE" if status is not None and status != 200 else
                "ASSISTANT_PROVIDER_TRANSPORT_FAILURE" if isinstance(error, httpx.TransportError) else
                "ASSISTANT_PROVIDER_RESPONSE_UNRESOLVED"))
            if status is not None:
                detail["provider_status"] = status
            logging.getLogger(__name__).warning(
                "ASSISTANT_PROVIDER_UNRESOLVED phase=%s status=%s exception_type=%s boundary_phase=%s key=%s",
                phase, status, detail["exception_type"], detail.get("boundary_phase"),
                safe_key_error(error, diagnostic_keys),
            )
            raise Problem(503, "ASSISTANT_MODEL_OUTCOME_UNKNOWN",
                          "original model outcome is unavailable; no retry", **detail) from None
        finally:
            if response is not None and not handed_off:
                await response.aclose()
            if client is not None and owns_client and not handed_off:
                await client.aclose()
