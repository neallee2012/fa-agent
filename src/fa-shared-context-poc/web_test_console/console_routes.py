"""The workbench's own browser-facing routes, grouped by job; the boundary around them stays in ``bridge.py``."""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from shared_context.assistant_image_wire import MAX_IMAGE_BYTES as _MAX_USER_IMAGE_BYTES

from .adapter import ConsoleError, require, sha, strict_json
from .agent_connections import read_messages
from .agent_onboarding import parse_input as onboarding_input
from . import prewarm as typing_prewarm
from . import realtime

# One base64 PNG plus its name; the API refuses anything over the same image bound.
MAX_IMAGE_UPLOAD_BODY_BYTES = (_MAX_USER_IMAGE_BYTES + 2) // 3 * 4 + 4096


async def require_no_input(request):
    require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
    async for chunk in request.stream():
        require(not chunk, "LOCAL_REQUEST_DENIED", 403)


async def read_bounded(request, limit, code, status):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        require(len(raw) <= limit, code, status)
    return bytes(raw)


async def read_json_input(request, limit, valid_shape, invalid_code):
    try:
        body = strict_json(await read_bounded(request, limit, "LOCAL_REQUEST_DENIED", 403))
        require(valid_shape(body), "LOCAL_REQUEST_DENIED", 403)
    except (ValueError, TypeError):
        raise ConsoleError(invalid_code, 400) from None
    return body


def is_submit_shape(body):
    return (type(body) is dict and {"content", "model"} <= set(body)
            and set(body) <= {"content", "model", "attachments", "files", "images"})


def is_image_upload_shape(body):
    return type(body) is dict and set(body) == {"name", "png_base64"} and all(type(body[key]) is str for key in body)


def is_append_shape(body):
    return type(body) is dict and set(body) == {"append_id", "content"}


def check_received_query(query):
    require(set(query) <= {"request_id", "thread_id"} and all(len(query.getlist(key)) == 1 for key in query)
            and "request_id" in query, "LOCAL_REQUEST_DENIED", 403)


def check_platform_history_query(query):
    require(set(query) <= {"after", "previous"} and len(query.multi_items()) == len(query)
            and ("previous" not in query or query["previous"] == "1"), "LOCAL_REQUEST_DENIED", 403)


def live_text_position(query):
    require(set(query) <= {"starting_after"} and len(query.getlist("starting_after")) <= 1,
            "LOCAL_REQUEST_DENIED", 403)
    raw = query.get("starting_after")
    require(raw is None or raw.isascii() and raw.isdigit() and len(raw) <= 12, "LOCAL_REQUEST_DENIED", 403)
    return None if raw is None else int(raw)


def attachment(raw, media_type, filename, **headers):
    return Response(raw, media_type=media_type, headers={
        "Content-Disposition": f'attachment; filename="{filename}"', "x-content-sha256": sha(raw), **headers})


def skill_draft_markdown(value):
    require(value.receipt is not None and value.body is not None, "NATIVE_RESULT_MISSING", 409)
    return attachment(value.body.encode("utf-8"), "text/markdown", "fa-skill-draft.md",
                      **{"X-Content-Type-Options": "nosniff"})


def install_page_routes(app, boundary, scope, realtime_host, prewarm):
    remote = boundary.remote

    @app.get("/")
    def page():
        response = HTMLResponse(typing_prewarm.page(realtime.page(
            Path(__file__).with_name("index.html").read_text(encoding="utf-8"), realtime_host), prewarm))
        response.set_cookie(boundary.cookie_name, boundary.cookie, httponly=True, samesite="strict", path="/",
                            secure=remote is not None)
        return response

    @app.get("/api/state")
    def state(request: Request):
        # Remote only: the signed-in name for display; identity is still checked by the API, not by this label.
        shown = {} if remote is None else {"remote_user": remote.display_name(request.headers)}
        if scope.agents is not None and scope.selected_agent(request) is None:
            return {**scope.agents.bootstrap(), "csrf": boundary.csrf, **shown}
        context = scope.console(request)
        return {**scope.state(context.state(request.query_params.get("thread_id")), request),
                "csrf": boundary.csrf, **shown}

    @app.get("/api/realtime/negotiate")
    async def realtime_negotiate(request: Request, console=Depends(scope.console)):
        await require_no_input(request)
        return await run_in_threadpool(realtime.negotiate, console, realtime_host)


def install_agent_routes(app, agents, onboarding):
    @app.get("/api/agents")
    async def agent_directory(request: Request):
        await require_no_input(request)
        require(agents is not None, "AGENT_DIRECTORY_UNAVAILABLE")
        return await run_in_threadpool(agents.refresh_directory)

    # Onboarding always uses the logged-in default Team entry; a not-yet-ready Agent is never a request target.
    def available():
        require(onboarding is not None, "AGENT_ONBOARDING_UNAVAILABLE")

    @app.get("/api/agents/onboarding")
    async def agent_onboardings(request: Request):
        await require_no_input(request)
        available()
        return await run_in_threadpool(onboarding.list)

    @app.post("/api/agents/onboarding")
    async def create_agent(request: Request):
        available()
        # Name, a 2048-byte role and up to 12 tool and 4 skill references stay well under this bound.
        raw = await read_bounded(request, 16384, "AGENT_ONBOARDING_INPUT_INVALID", 413)
        return await run_in_threadpool(onboarding.create, onboarding_input(raw))

    @app.get("/api/agents/onboarding/options")
    async def agent_onboarding_options(request: Request):
        await require_no_input(request)
        available()
        return await run_in_threadpool(onboarding.options)

    @app.get("/api/agents/onboarding/{request_id}")
    async def agent_onboarding_status(request_id: str, request: Request):
        await require_no_input(request)
        available()
        return await run_in_threadpool(onboarding.refresh, request_id)

    @app.post("/api/agents/onboarding/{request_id}/continue")
    async def continue_agent_onboarding(request_id: str):
        available()
        return await run_in_threadpool(onboarding.resume, request_id)

    @app.post("/api/agents/onboarding/{request_id}/prepare")
    async def prepare_agent_onboarding(request_id: str):
        available()
        return await run_in_threadpool(onboarding.prepare, request_id)


def install_template_routes(app, requested_console):
    @app.get("/api/agent-template")
    async def agent_template(request: Request, console=Depends(requested_console)):
        await require_no_input(request)
        raw = await run_in_threadpool(console.download_template)
        return attachment(raw, "application/json", "fa-agent-template.json")

    @app.post("/api/agent-template/inspect")
    async def inspect_template(request: Request, console=Depends(requested_console)):
        from shared_context.behavior_agent_template import MAX_TEMPLATE_BYTES
        console.template_token()
        raw = await read_bounded(request, MAX_TEMPLATE_BYTES, "TEMPLATE_INVALID", 413)
        result = await run_in_threadpool(console.inspect_template, raw)
        return JSONResponse(result.model_dump(mode="json"))

    @app.get("/api/agent-template/options")
    async def template_options(request: Request, console=Depends(requested_console)):
        await require_no_input(request)
        result = await run_in_threadpool(console.template_options)
        return JSONResponse(result.model_dump(mode="json"))

    @app.get("/api/agent-template/system-prompt")
    async def template_system_prompt(request: Request, console=Depends(requested_console)):
        await require_no_input(request)
        return JSONResponse(await run_in_threadpool(console.template_system_prompt),
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/agent-template/selection")
    async def template_selection(request: Request, console=Depends(requested_console)):
        await require_no_input(request)
        return await run_in_threadpool(console.read_template_selection)

    @app.post("/api/agent-template/select")
    async def select_template(request: Request, console=Depends(requested_console)):
        from .agent_template import MAX_SELECTION_BYTES, parse_selection
        console.template_token()
        raw = await read_bounded(request, MAX_SELECTION_BYTES, "TEMPLATE_SELECTION_INVALID", 413)
        return await run_in_threadpool(console.select_template, parse_selection(raw))

    @app.post("/api/agent-template/selection-reconcile")
    async def reconcile_template_selection(request: Request, console=Depends(requested_console)):
        from shared_context.behavior_wire import Command
        console.template_token()
        raw = await read_bounded(request, 256, "TEMPLATE_SELECTION_INVALID", 413)
        try:
            strict_json(raw)
            command = Command.model_validate_json(raw)
        except (ValueError, TypeError):
            raise ConsoleError("TEMPLATE_SELECTION_INVALID", 400) from None
        return await run_in_threadpool(console.reconcile_template_selection, command.request_id)


def install_session_routes(app, console, agents):
    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    @app.post("/api/authenticate")
    def authenticate():
        return console.authenticate() if agents is None else agents.authenticate()

    @app.post("/api/authenticate/refresh")
    def refresh_authentication():
        return console.authenticate(automatic=True) if agents is None else agents.authenticate(automatic=True)


def install_conversation_routes(app, scope, boundary, prewarm):
    requested_console, csrf = scope.console, boundary.csrf

    @app.post("/api/instructions/{thread_id}/next")
    def new_instruction(thread_id: str, request: Request, console=Depends(requested_console)):
        return scope.state(console.new_instruction(thread_id), request)

    @app.post("/api/instructions/conversations/new")
    def new_conversation(request: Request, console=Depends(requested_console)):
        return scope.state(console.new_conversation(), request)

    @app.post("/api/instructions/prewarm")
    def prewarm_agent(console=Depends(requested_console)):
        # README#typing-prewarm: a hint only; no lock, journal entry or state, and the page ignores the answer.
        return typing_prewarm.forward(console, prewarm)

    @app.post("/api/instructions/{thread_id}/reset")
    def reset_conversation(thread_id: str, request: Request, console=Depends(requested_console)):
        return scope.state(console.reset_conversation(thread_id), request)

    # P06: the workbench no longer prepares, advances, reconciles, or confirms environment transfer.
    # The server follows versions during submit and reports server_following when the same RID should be retried.

    @app.get("/api/instructions/progress")
    def instruction_progress(request: Request, console=Depends(requested_console)):
        return {**scope.state(console.instruction_progress(request.query_params.get("thread_id")), request), "csrf": csrf}

    @app.get("/api/instructions/models")
    def instruction_models(request: Request, console=Depends(requested_console)):
        return {**scope.state(console.instruction_models(request.query_params.get("thread_id")), request), "csrf": csrf}

    @app.get("/api/instructions/received")
    def instruction_received(request: Request, console=Depends(requested_console)):
        query = request.query_params
        check_received_query(query)
        return {**scope.state(console.instruction_received(query["request_id"], query.get("thread_id")), request),
                "csrf": csrf}

    @app.get("/api/instructions/{task_id}/reply")
    def instruction_reply(task_id: str, request: Request, console=Depends(requested_console)):
        return {**scope.state(console.read_instruction_reply(task_id), request), "csrf": csrf}


def install_reply_read_routes(app, requested_console):
    @app.get("/api/instructions/{task_id}/reply.txt")
    async def instruction_reply_text(task_id: str, request: Request, console=Depends(requested_console)):
        await require_no_input(request)
        raw, turn_number = await run_in_threadpool(console.download_instruction_reply, task_id)
        return attachment(raw, "text/plain", f"fa-reply-{turn_number}.txt")

    @app.get("/api/instructions/{task_id}/context")
    async def instruction_context(task_id: str, request: Request, console=Depends(requested_console)):
        await require_no_input(request)
        return await run_in_threadpool(console.read_instruction_context, task_id)

    @app.get("/api/instructions/{task_id}/system-prompt")
    async def instruction_system_prompt(task_id: str, request: Request, console=Depends(requested_console)):
        await require_no_input(request)
        return JSONResponse(await run_in_threadpool(console.instruction_system_prompt, task_id),
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/instructions/{task_id}/skill-draft")
    async def instruction_skill_draft(task_id: str, request: Request, console=Depends(requested_console)):
        await require_no_input(request)
        value = await run_in_threadpool(console.instruction_skill_draft, task_id)
        return value.model_dump(mode="json")

    @app.get("/api/instructions/{task_id}/skill-draft.md")
    async def instruction_skill_draft_text(task_id: str, request: Request, console=Depends(requested_console)):
        await require_no_input(request)
        return skill_draft_markdown(await run_in_threadpool(console.instruction_skill_draft, task_id))

    @app.get("/api/instructions/{task_id}/images")
    async def instruction_images(task_id: str, request: Request, console=Depends(requested_console)):
        await require_no_input(request)
        value = await run_in_threadpool(console.instruction_images, task_id)
        return {"entries": [entry.model_dump(mode="json") for entry in value.entries]}

    @app.get("/api/instructions/{thread_id}/platform-history")
    async def platform_history(thread_id: str, request: Request, console=Depends(requested_console)):
        check_platform_history_query(request.query_params)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        return await run_in_threadpool(
            console.instruction_platform_history, thread_id, after=request.query_params.get("after"),
            previous=request.query_params.get("previous") == "1")

    @app.get("/api/instructions/{task_id}/images/{version_id}")
    async def instruction_image(task_id: str, version_id: str, request: Request, console=Depends(requested_console)):
        await require_no_input(request)
        raw, number = await run_in_threadpool(console.instruction_image, task_id, version_id)
        return attachment(raw, "image/png", f"fa-screenshot-{number}.png", **{"X-Content-Type-Options": "nosniff"})

    @app.get("/api/instructions/{task_id}/delegation")
    async def instruction_delegation(task_id: str, request: Request, console=Depends(requested_console)):
        await require_no_input(request)
        return await run_in_threadpool(console.read_instruction_delegation, task_id)

    @app.get("/api/instructions/{task_id}/agent-messages")
    async def agent_messages(task_id: str, request: Request, console=Depends(requested_console)):
        await require_no_input(request)
        return await run_in_threadpool(read_messages, console, task_id)


def install_turn_routes(app, scope, boundary):
    requested_console, csrf = scope.console, boundary.csrf

    @app.post("/api/instructions/{task_id}/confirm-reply")
    def confirm_reply(task_id: str, console=Depends(requested_console)):
        return console.confirm_instruction_reply(task_id)

    @app.post("/api/instructions/{task_id}/cancel")
    def cancel_instruction(task_id: str, console=Depends(requested_console)):
        return console.cancel_instruction(task_id)

    @app.get("/api/instructions/{task_id}/cancel-receipt")
    async def cancellation_receipt(task_id: str, request: Request, console=Depends(requested_console)):
        await require_no_input(request)
        return await run_in_threadpool(console.read_cancellation_receipt, task_id)

    @app.post("/api/instructions/{request_id}/submit")
    async def submit_instruction(request_id: str, request: Request, console=Depends(requested_console)):
        body = await read_json_input(request, 32768, is_submit_shape, "INSTRUCTION_INVALID")
        return await run_in_threadpool(console.submit_instruction, request_id, body["content"], body["model"],
                                       body.get("attachments", ()), body.get("files", ()), body.get("images", ()))

    @app.post("/api/instructions/{request_id}/image-upload")
    async def upload_instruction_image(request_id: str, request: Request, console=Depends(requested_console)):
        """B1 PA-08: one browser-converted PNG (at most 512 KiB, sides at most 2000 px), stored before the send."""
        body = await read_json_input(request, MAX_IMAGE_UPLOAD_BODY_BYTES, is_image_upload_shape,
                                     "USER_IMAGE_INPUT_INVALID")
        return await run_in_threadpool(console.upload_instruction_image, request_id, body["name"], body["png_base64"])

    @app.post("/api/instructions/{request_id}/append")
    async def append_instruction(request_id: str, request: Request, console=Depends(requested_console)):
        body = await read_json_input(request, 32768, is_append_shape, "INSTRUCTION_INVALID")
        return await run_in_threadpool(console.append_instruction, request_id, body["append_id"], body["content"])

    @app.get("/api/instructions/{request_id}/appends")
    async def instruction_appends(request_id: str, request: Request, console=Depends(requested_console)):
        require(not request.query_params and not await request.body(), "LOCAL_REQUEST_DENIED", 403)
        return await run_in_threadpool(console.read_instruction_appends, request_id)

    @app.get("/api/instructions/{request_id}/status")
    async def instruction_status(request_id: str, request: Request, console=Depends(requested_console)):
        require(not request.query_params and not await request.body(), "LOCAL_REQUEST_DENIED", 403)
        value = await run_in_threadpool(console.instruction_status, request_id)
        return {**scope.state(value, request), "csrf": csrf}

    @app.get("/api/instructions/{request_id}/stream")
    async def instruction_live_text(request_id: str, request: Request, console=Depends(requested_console)):
        # RTT B: display-only live text, relayed as it arrives; the page still replaces it with /status.
        position = live_text_position(request.query_params)
        require(not await request.body(), "LOCAL_REQUEST_DENIED", 403)
        chunks = await run_in_threadpool(console.instruction_live_text, request_id, position)
        return StreamingResponse(chunks, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/api/instructions/{request_id}/progress")
    async def instruction_public_progress(request_id: str, request: Request, console=Depends(requested_console)):
        require(not request.query_params and not await request.body(), "LOCAL_REQUEST_DENIED", 403)
        return await run_in_threadpool(console.read_instruction_progress, request_id)


def install_file_routes(app, requested_console):
    @app.get("/api/instructions/{request_id}/user-text-file-capability")
    async def instruction_file_capability(request_id: str, request: Request, console=Depends(requested_console)):
        await require_no_input(request)
        return await run_in_threadpool(console.instruction_file_capability, request_id)

    @app.get("/api/instructions/{request_id}/files/{filename}")
    async def instruction_file(request_id: str, filename: str, request: Request, console=Depends(requested_console)):
        await require_no_input(request)
        raw = await run_in_threadpool(console.download_instruction_file, request_id, filename)
        return attachment(raw, "text/plain", filename)

    @app.get("/api/instructions/{request_id}/attachment-sources")
    async def instruction_attachment_sources(request_id: str, request: Request, console=Depends(requested_console)):
        await require_no_input(request)
        return await run_in_threadpool(console.instruction_attachment_sources, request_id)


def install_task_routes(app, requested_console):
    @app.post("/api/tasks/{task_id}/{action}")
    def operate(task_id: str, action: str, console=Depends(requested_console)):
        return console.run(task_id, action)

    @app.get("/api/tasks/{task_id}/payload")
    def download_payload(task_id: str, console=Depends(requested_console)):
        return Response(console.payload(task_id), media_type="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="verified-payload.json"'})

    @app.get("/api/tasks/{task_id}/summary")
    def download_summary(task_id: str, console=Depends(requested_console)):
        return JSONResponse(console.summary(task_id), headers={
            "Content-Disposition": 'attachment; filename="receipt-summary.json"'})
