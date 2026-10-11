"""Thin group panel controls; original group IDs and local UNKNOWN intents survive reload."""

import secrets

from pydantic import TypeAdapter

from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_wire import Digest
from shared_context.native_group_wire import (
    NATIVE_GROUP_ERRORS, NATIVE_GROUP_PREFIX, NativeGroupActivity, NativeGroupList, NativeGroupListQuery,
    NativeGroupOpen, NativeGroupOptions, NativeGroupStarted, NativeGroupTranscript, NativeGroupView, groups_enabled,
)
from .adapter import ConsoleError, PullAdapter, canonical, require, sha, strict_json

MESSAGES = {code: "群組操作未通過核對；保留原編號並讀回狀態，不會自動重送。" for code in NATIVE_GROUP_ERRORS}
MESSAGES.update({
    "NATIVE_GROUP_DISABLED": "群組聊天尚未啟用；不會改動目前一對一對話。",
    "NATIVE_GROUP_UNKNOWN": "原群組操作尚未確認；請核對原紀錄，不要重新開群組。",
    "NATIVE_GROUP_ALLOWANCE": "群組已到回合或張貼上限，保留原結果與收尾名額。",
})


def decode(model, raw):
    try:
        strict_json(raw)
        return model.model_validate_json(raw)
    except (ValueError, TypeError):
        raise ConsoleError("NATIVE_GROUP_STATE_INVALID", 502) from None


class GroupControl:
    def __init__(self, console):
        self.console = console
        self.enabled = groups_enabled()
        self.adapter = PullAdapter(console.config, console.backend)
        self.proposal = "group-" + secrets.token_hex(16)

    def token(self):
        require(self.enabled and self.console.native_instructions is not None, "NATIVE_GROUP_DISABLED")
        self.console.active()
        return self.console.vault.get()

    def read(self, path, model, token, body=None):
        raw, headers = self.adapter.send("GET" if body is None else "POST", NATIVE_GROUP_PREFIX + path,
                                        body, token, limit=512 * 1024)
        self.console.native_instructions._current(token)
        require(headers.get("cache-control") == "no-store", "NATIVE_GROUP_STATE_INVALID", 502)
        return decode(model, raw)

    def options(self):
        if not self.enabled:
            return {"options": NativeGroupOptions(enabled=False).model_dump(mode="json"), "request_id": None}
        token = self.token()
        with self.console.journal.connect() as db:
            rows = db.execute("SELECT task_id,intent,outcome FROM intents WHERE phase='group-open' ORDER BY rowid").fetchall()
        pending = next((strict_json(intent)["body"] for rid, intent, outcome in rows
                        if strict_json(outcome)["state"] == "UNKNOWN"
                        and (self.console.journal.get(rid, "group-open-read") or {}).get("state")
                        not in {"VERIFIED", "REFUSED"}), None)
        if pending is not None:
            self.proposal = pending["request_id"]
        return {"options": self.read("/options", NativeGroupOptions, token).model_dump(mode="json"),
                "request_id": self.proposal, "pending_opening": pending}

    def listing(self, after=None):
        token = self.token()
        query = NativeGroupListQuery(after=after)
        return self.read("/list", NativeGroupList, token, query.model_dump(mode="json")).model_dump(mode="json")

    def _group_id(self, value):
        try:
            return TypeAdapter(Digest).validate_python(value, strict=True)
        except ValueError:
            raise ConsoleError("NATIVE_GROUP_INVALID", 400) from None

    def open(self, data):
        token = self.token()
        command = decode(NativeGroupOpen, canonical(data))
        native = self.console.native_instructions
        with self.console.message_lock("group-open"):
            phase = "group-open"
            journal = self.console.journal
            previous = journal.get(command.request_id, phase)
            body = command.model_dump(mode="json")
            if previous is not None:
                require(previous["intent"]["body"] == body, "NATIVE_GROUP_CONFLICT")
                try:
                    group = self.read("/openings/" + command.request_id, NativeGroupView, token)
                except ConsoleError as error:
                    if (previous["state"] != "UNKNOWN" or error.code != "NATIVE_GROUP_NOT_FOUND"
                            or error.upstream_status != 404):
                        raise
                    # The human retries only this create-only opening, never a new ID or a model start.
                    journal.finish(command.request_id, phase, {
                        key: value for key, value in previous.items() if key != "intent"}, keep_confirmed=True)
                    try:
                        group = self.read("/groups", NativeGroupView, token, body)
                    except ConsoleError as refused:
                        if ((refused.upstream_status or refused.status) in {400, 403, 409, 422}
                                and refused.code != "NATIVE_GROUP_UNKNOWN"):
                            observation = journal.get(command.request_id, "group-open-read")
                            if observation is None:
                                journal.begin(command.request_id, "group-open-read", {
                                    "request_id": command.request_id, "created_at": native.clock().isoformat()})
                            journal.finish(command.request_id, "group-open-read", {"state": "REFUSED", "code": refused.code})
                            self.proposal = "group-" + secrets.token_hex(16)
                        raise
            else:
                require(command.request_id == self.proposal, "NATIVE_GROUP_CONFLICT")
                journal.begin(command.request_id, phase, {
                    "body": body, "body_sha256": sha(canonical(body)), "created_at": native.clock().isoformat(),
                })
                try:
                    group = self.read("/groups", NativeGroupView, token, body)
                except ConsoleError as error:
                    journal.finish(command.request_id, phase, {"state": "UNKNOWN", "code": error.code})
                    raise
                journal.finish(command.request_id, phase, {"state": "CONFIRMED", "group_id": group.group_id})
            require((group.opening_request_id, group.question, group.orchestrator_agent_id, group.member_agent_ids)
                    == (command.request_id, command.content, command.orchestrator_agent_id, command.member_agent_ids),
                    "NATIVE_GROUP_STATE_INVALID", 502)
            if previous is not None:
                if journal.get(command.request_id, "group-open-read") is None:
                    journal.begin(command.request_id, "group-open-read", {
                        "group_id": group.group_id, "created_at": native.clock().isoformat()})
                journal.finish(command.request_id, "group-open-read", {"state": "VERIFIED", "group_id": group.group_id})
            self.proposal = "group-" + secrets.token_hex(16)
            return group.model_dump(mode="json")

    def observe(self, group_id):
        token = self.token()
        group_id = self._group_id(group_id)
        transcript = self.read("/groups/" + group_id + "/transcript", NativeGroupTranscript, token)
        activity = self.read("/groups/" + group_id + "/activity", NativeGroupActivity, token)
        require(transcript.group.group_id == activity.group_id == group_id, "NATIVE_GROUP_STATE_INVALID", 502)
        selected = self.console.api_prefix.removeprefix("/agents/")
        for receipt in activity.turns:
            if receipt.agent_id == selected or self.console.journal.get(receipt.request_id, "group-start") is not None:
                self._attach(receipt, group_id)
        return {"transcript": transcript.model_dump(mode="json"),
                "turns": [{"agent_id": turn.agent_id, "conversation_id": turn.conversation_id,
                           "request_id": turn.request_id, "status": turn.status, "failure_code": turn.failure_code}
                          for turn in activity.turns],
                "unconfirmed_request_ids": list(activity.unconfirmed_request_ids)}

    def start(self, group_id):
        token = self.token()
        group_id = self._group_id(group_id)
        native = self.console.native_instructions
        group = self.read("/groups/" + group_id, NativeGroupView, token)
        selected = self.console.api_prefix.removeprefix("/agents/")
        if not selected:
            selected = self.read("/options", NativeGroupOptions, token).default_agent_id
        require(selected == group.orchestrator_agent_id, "NATIVE_GROUP_SCOPE_DENIED", 403)
        with self.console.message_lock(group_id):
            journal = self.console.journal
            original = journal.get(group_id, "group-start")
            if original is not None:
                return self.observe(group_id)
            journal.begin(group_id, "group-start", {
                "group_id": group_id, "state_profile": "foundry-responses-v1",
                "body": InstructionSubmit(request_id=group_id, content=group.question).model_dump(mode="json"),
                "created_at": native.clock().isoformat(),
            })
            try:
                started = self.read("/groups/" + group_id + "/start", NativeGroupStarted, token, {})
                require(started.group_id == group_id, "NATIVE_GROUP_STATE_INVALID", 502)
            except ConsoleError as error:
                journal.finish(group_id, "group-start", {"state": "UNKNOWN", "code": error.code,
                                                       "upstream_status": error.upstream_status or error.status})
                raise
            journal.finish(group_id, "group-start", {"state": "CONFIRMED"})
            self._attach(started.receipt, group_id)
            return self.observe(group_id)

    def _attach(self, receipt, group_id):
        native = self.console.native_instructions
        thread = native.store.observe_native(receipt)
        journal = self.console.journal
        start = journal.get(receipt.request_id, "group-start")
        if start is not None and receipt.source is None:
            require(start["intent"]["body"] == receipt.command.model_dump(mode="json"), "NATIVE_GROUP_STATE_INVALID", 502)
            native.store.prepare(receipt.command.model_dump(mode="json"), state_profile=receipt.state_profile)
        if journal.get(receipt.request_id, "group-observation") is None:
            journal.begin(receipt.request_id, "group-observation", {
                "state_profile": receipt.state_profile, "group_id": group_id,
                "created_at": native.clock().isoformat(),
            })
        journal.finish(receipt.request_id, "group-observation", {
            "state": "VERIFIED", "native_receipt": receipt.model_dump(mode="json")})
        native._remember(thread, receipt)
        native.observed[thread] = native.clock().isoformat()
        return thread

    def open_turn(self, group_id, request_id):
        token = self.token()
        group_id = self._group_id(group_id)
        activity = self.read("/groups/" + group_id + "/activity", NativeGroupActivity, token)
        receipt = next((item for item in activity.turns if item.request_id == request_id), None)
        require(receipt is not None, "NATIVE_GROUP_NOT_FOUND", 404)
        selected = self.console.api_prefix.removeprefix("/agents/")
        native = self.console.native_instructions
        if selected:
            require(selected == receipt.agent_id, "NATIVE_GROUP_SCOPE_DENIED", 403)
        else:
            require(native.adapter.receipt(request_id, token).agent_id == receipt.agent_id,
                    "NATIVE_GROUP_SCOPE_DENIED", 403)
        thread = self._attach(receipt, group_id)
        native.refresh(thread, token)
        if receipt.result is not None:
            native._answer(receipt.conversation_id, receipt.request_id, receipt.result, token)
        return self.console.state(thread)


def install_group_routes(app, requested_console):
    from fastapi import Depends, Request
    from starlette.concurrency import run_in_threadpool

    def control(console):
        require(console.groups is not None, "NATIVE_GROUP_DISABLED")
        return console.groups

    async def body(request, *, empty=False):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            require(len(raw) <= (16 if empty else 48 * 1024), "NATIVE_GROUP_INVALID", 413)
        try:
            value = strict_json(bytes(raw) or b"{}")
        except (ValueError, TypeError):
            raise ConsoleError("NATIVE_GROUP_INVALID", 400) from None
        require(type(value) is dict and (not empty or not value), "NATIVE_GROUP_INVALID", 400)
        return value

    @app.get("/api/groups/options")
    async def options(console=Depends(requested_console)):
        return await run_in_threadpool(control(console).options)

    @app.post("/api/groups/list")
    async def listing(request: Request, console=Depends(requested_console)):
        value = decode(NativeGroupListQuery, canonical(await body(request)))
        return await run_in_threadpool(control(console).listing, value.after)

    @app.post("/api/groups/open")
    async def open_group(request: Request, console=Depends(requested_console)):
        return await run_in_threadpool(control(console).open, await body(request))

    @app.get("/api/groups/{group_id}")
    async def read_group(group_id: str, console=Depends(requested_console)):
        return await run_in_threadpool(control(console).observe, group_id)

    @app.post("/api/groups/{group_id}/start")
    async def start(group_id: str, request: Request, console=Depends(requested_console)):
        await body(request, empty=True)
        return await run_in_threadpool(control(console).start, group_id)

    @app.post("/api/groups/{group_id}/turns/{request_id}")
    async def open_turn(group_id: str, request_id: str, request: Request, console=Depends(requested_console)):
        await body(request, empty=True)
        return await run_in_threadpool(control(console).open_turn, group_id, request_id)
