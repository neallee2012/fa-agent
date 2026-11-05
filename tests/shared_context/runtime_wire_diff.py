"""What one Copilot runtime binary sends to the model for a fixed set of FA turns, and how two runtimes differ.

Offline only: the real SDK and runtime run each turn against a scripted model; no model call, no Azure.

    python runtime_wire_diff.py record <copilot-runtime.exe> <out.json>
    python runtime_wire_diff.py diff <a.json> <b.json>

Each model request is recorded at three points:

- ``runtime``: the body the runtime built, before ``_ModelGateway._shape_fields`` (m2_copilot.py);
- ``gateway``: the body after FA's shaping, as the gateway hands it on;
- ``sent``: what reaches the model endpoint. Instruction turns go through ``DirectResponses``/``DirectCompletions``
  (behavior_model_direct.py), as native turns do; legacy turns through the API's ``AssistantProvider.send``
  (behavior_assistant_gateway.py), whose allowlist refusal code is kept as ``refused``.

``record`` runs under whatever Python and ``PYTHONPATH`` source tree it is started with and writes both into the
file, so a runtime that needs another SDK package is recorded with that package and the FA source that pinned it.
Nothing here imports the test modules: their conftest would put this tree's source first on the path.
"""
import asyncio
import copy
import hashlib
import importlib.metadata
import json
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
FACTS = ROOT / "src" / "fa-shared-context-poc" / "model-facts.json"
MODELS = ("gpt-4.1-mini", "gpt-5.4-mini", "gpt-6.1-sol")
# Every case calls one tool, so its first request offers the tool and its second carries the tool output.
CASES = {"legacy-completions-gpt-4.1-mini": (None, "gpt-4.1-mini")} | {
    f"instruction-{wire}-{model}": (wire, model) for wire in ("completions", "responses") for model in MODELS}
TEXT_LIMIT = 80
DIRECT_URL = "https://offline-fa.services.ai.azure.com/api/projects/offline/openai/v1/"
FAILURE_EVENTS = {"session.error", "m2.provider_failed", "m2.network_denied"}


def _chat_sse(reply):
    base = {"id": "chatcmpl-offline", "object": "chat.completion.chunk", "created": 1, "model": "offline"}
    reason = "tool_calls" if "tool_calls" in reply else "stop"
    chunks = [{**base, "choices": [{"index": 0, "delta": {"role": "assistant", **reply}, "finish_reason": None}]},
              {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
              {**base, "choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}]
    return ("".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()


def _tool_call():
    return {"tool_calls": [{"index": 0, "id": "call-offline", "type": "function",
                            "function": {"name": "read_context", "arguments": "{}"}}]}


def _responses_sse(ordinal, tool):
    """A minimal Responses stream: one function call, or one message."""
    if tool:
        item = {"id": f"fc_{ordinal}", "type": "function_call", "call_id": f"call_{ordinal}", "name": "read_context",
                "arguments": "{}", "status": "completed"}
    else:
        item = {"id": f"msg_{ordinal}", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "OFFLINE_DONE", "annotations": []}]}
    response = {"id": f"resp_{ordinal}", "object": "response", "created_at": 1, "model": "offline",
                "status": "completed", "output": [item], "error": None,
                "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}}
    events = [("response.created", {"response": {**response, "status": "in_progress", "output": [], "usage": None}}),
              ("response.output_item.added", {"output_index": 0, "item": item}),
              ("response.output_item.done", {"output_index": 0, "item": item}),
              ("response.completed", {"response": response})]
    return "".join(f"event: {kind}\ndata: {json.dumps({'type': kind, 'sequence_number': n, **data})}\n\n"
                   for n, (kind, data) in enumerate(events)).encode()


async def _record_case(name, wire, model, runtime, work):
    import httpx
    from shared_context import behavior_assistant_wire as aw
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    from shared_context.behavior_assistant_gateway import AssistantProvider
    from shared_context.behavior_model_capacity import DeclaredModelLimits, ModelCapacitySnapshot
    from shared_context.behavior_model_direct import DirectCompletions, DirectResponses
    from shared_context.behavior_sdk_files import SdkSessionFiles
    from shared_context.errors import Problem
    from shared_context.m2_copilot import ToolOutput, ToolSpec, _ModelGateway, run_copilot

    requests, events, instruction = [], [], wire is not None
    session_id = name.replace(".", "-")
    provider_name = "openai-responses" if wire == "responses" else None
    original = _ModelGateway._shape_fields

    def shape(gateway, body):
        requests.append({"runtime": copy.deepcopy(body)})
        original(gateway, body)

    def reply(request):
        requests[-1]["sent"] = json.loads(request.content)
        return scripted(request)

    def scripted(request):
        first = len(requests) == 1
        if wire == "responses":
            content = _responses_sse(len(requests), first)
        else:
            content = _chat_sse(_tool_call() if first else {"content": "OFFLINE_DONE"})
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"}, content=content)

    def captured(request):
        reply(request)
        raise _Captured()

    # Legacy turns reach the model through the API's AssistantProvider; it is stopped right after building the body.
    provider = AssistantProvider("https://offline.invalid/openai/v1/chat/completions",
                                 SimpleNamespace(get_token=lambda _: SimpleNamespace(token="offline")),
                                 transport=httpx.MockTransport(captured))

    async def api(request):
        requests[-1]["gateway"] = json.loads(request.content)
        try:
            await provider.send(requests[-1]["gateway"])
        except Problem as problem:
            if "sent" not in requests[-1]:
                requests[-1]["refused"] = problem.body["error"]["code"]
        return scripted(request)

    async def read_context(_):
        if not instruction:
            return ToolOutput("{}")
        return ToolOutput(aw.InstructionContext(source={
            "source_id": "source", "version": 1, "sha256": "a" * 64, "byte_length": 4}, content="Data").model_dump_json())

    async def reserve(_):
        pass

    extra = {"http_transport": httpx.MockTransport(api)}
    if instruction:
        facts = json.loads(FACTS.read_text(encoding="utf-8"))[model]
        limits = DeclaredModelLimits(context_window_tokens=facts.get("context_window_tokens"),
                                     max_prompt_tokens=facts.get("max_prompt_tokens"),
                                     max_output_tokens=facts.get("max_output_tokens"))
        # Native instruction turns (FA_NATIVE_TURNS) send straight to the project endpoint, wired as in native_sdk.py.
        direct = (DirectResponses if provider_name else DirectCompletions)(
            DIRECT_URL + ("responses" if provider_name else "chat/completions"), lambda: "offline", model,
            transport=httpx.MockTransport(reply))

        async def sender(request):
            requests[-1]["gateway"] = json.loads(request.content)
            return await direct.send(request)

        def unrouted(request):
            raise RuntimeError("instruction model request bypassed the native sender")

        extra = dict(instruction=True, model=model, model_provider=provider_name,
                     http_transport=httpx.MockTransport(unrouted),
                     supports_reasoning_effort=facts.get("supports_reasoning_effort"),
                     model_capacity=ModelCapacitySnapshot.from_declared(model, limits),
                     reasoning_summary="detailed" if provider_name and facts.get("supports_reasoning_summary") else None,
                     session_files=SdkSessionFiles(session_id, model_provider=provider_name),
                     behavior_boundary=AssistantModelBoundary(reserve, sender, text_reply=True, streaming=True))
    schema = aw.Empty.model_json_schema() if instruction else {"type": "object", "properties": {}}

    def on_event(event):
        if event.get("type") in FAILURE_EVENTS:
            events.append(event)

    _ModelGateway._shape_fields = shape
    error = None
    try:
        # Native instruction turns pass no timeout (send until idle); legacy turns keep one (send_and_wait).
        await asyncio.wait_for(run_copilot(
            state_dir=work / name / "state", working_directory=work / "work", sdk_session_id=session_id, resume=False,
            prompt="Read the source with read_context, then answer in one sentence.",
            tools=[ToolSpec("read_context", "Read the source.", schema, read_context)],
            api_url="https://offline.invalid", token_provider=lambda: "offline", on_event=on_event,
            timeout_seconds=None if instruction else 90, runtime_path=runtime, **extra), 120)
    except Exception as failure:
        # run_copilot raises its own message "from None"; the cause stays on __context__.
        error = f"{type(failure).__name__}: {failure} <- {failure.__context__!r}"
    finally:
        _ModelGateway._shape_fields = original
    return {"error": error, "failure_events": events, "requests": requests}


class _Captured(Exception):
    pass


def record(runtime, out):
    runtime = Path(runtime).resolve(strict=True)
    work = Path(tempfile.mkdtemp(prefix="fa-wire-"))
    (work / "work").mkdir()
    # Stopping the legacy send after capture is logged as an unresolved provider call; expected here.
    logging.getLogger("shared_context.behavior_assistant_gateway").setLevel(logging.ERROR)
    import shared_context
    source = Path(shared_context.__file__).resolve().parent

    def git(*args):
        return subprocess.run(["git", "-C", str(source), *args], capture_output=True, text=True).stdout.strip()

    # A tree unpacked inside another checkout would otherwise report that checkout's HEAD.
    top = git("rev-parse", "--show-toplevel")
    head = git("rev-parse", "HEAD") if top and Path(top).resolve() == source.parents[2] else None
    if head and git("status", "--porcelain", "--", "."):
        head += "+dirty"
    result = {"meta": {
        "runtime": str(runtime), "runtime_sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
        "sdk": importlib.metadata.version("github-copilot-sdk"), "python": sys.executable, "fa_source": str(source),
        "fa_head": head}, "cases": {}}
    for name, (wire, model) in CASES.items():
        result["cases"][name] = asyncio.run(_record_case(name, wire, model, runtime, work))
        print(name, result["cases"][name]["error"] or f"{len(result['cases'][name]['requests'])} requests")
    shutil.rmtree(work, ignore_errors=True)
    Path(out).write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")


def flatten(value, path=""):
    """``{path: leaf}``; long text becomes its length, since prompts carry timestamps and runtime-owned wording."""
    if isinstance(value, dict):
        if not value:
            return {path: {}}
        return {key: leaf for name, item in value.items() for key, leaf in flatten(item, f"{path}.{name}" if path else name).items()}
    if isinstance(value, list):
        if not value:
            return {path: []}
        return {key: leaf for index, item in enumerate(value) for key, leaf in flatten(item, f"{path}[{index}]").items()}
    if isinstance(value, str) and len(value) > TEXT_LIMIT:
        return {path: f"<text len={len(value)}>"}
    # The runtime draws a new prompt_cache_key per session; only its presence is comparable.
    return {path: "<per session>" if path == "prompt_cache_key" else value}


_ABSENT = "<absent>"
STAGES = ("runtime", "gateway", "sent", "refused")


def diff(a, b):
    """One line per changed leaf: ``case request#N stages path: a -> b``, stages sharing the same change joined."""
    lines = []
    for case in sorted(set(a["cases"]) | set(b["cases"])):
        left, right = a["cases"].get(case, {}), b["cases"].get(case, {})
        if left.get("error") != right.get("error"):
            lines.append(f"{case} error: {left.get('error')} -> {right.get('error')}")
        kinds = [[event.get("type") for event in side.get("failure_events", [])] for side in (left, right)]
        if kinds[0] != kinds[1]:
            lines.append(f"{case} failure_events: {kinds[0]} -> {kinds[1]}")
        sent_a, sent_b = left.get("requests", []), right.get("requests", [])
        if len(sent_a) != len(sent_b):
            lines.append(f"{case} requests: {len(sent_a)} -> {len(sent_b)}")
        for index, (one, two) in enumerate(zip(sent_a, sent_b), 1):
            changes = {}
            for stage in STAGES:
                flat_a, flat_b = flatten(one.get(stage, _ABSENT)), flatten(two.get(stage, _ABSENT))
                for path in sorted(set(flat_a) | set(flat_b)):
                    old, new = flat_a.get(path, _ABSENT), flat_b.get(path, _ABSENT)
                    if old != new:
                        change = (path or ".", json.dumps(old, ensure_ascii=False), json.dumps(new, ensure_ascii=False))
                        changes.setdefault(change, []).append(stage)
            lines += [f"{case} request#{index} {','.join(stages)} {path}: {old} -> {new}"
                      for (path, old, new), stages in changes.items()]
    return lines


def main(argv):
    if len(argv) == 3 and argv[0] == "record":
        record(argv[1], argv[2])
    elif len(argv) == 3 and argv[0] == "diff":
        a, b = (json.loads(Path(path).read_text(encoding="utf-8")) for path in argv[1:])
        for side, data in (("a", a), ("b", b)):
            print(f"{side}: {json.dumps(data['meta'], ensure_ascii=False)}")
        lines = diff(a, b)
        print("\n".join(lines) if lines else "no field differs")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
