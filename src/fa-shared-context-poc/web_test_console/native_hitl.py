"""Owner control/readback on H1, using the console's existing durable once journal."""

from pydantic import TypeAdapter

from shared_context.behavior_wire import Digest
from shared_context.native_hitl_wire import (
    MAX_RULES, NATIVE_HITL_ERRORS, NATIVE_HITL_HTTP_BYTES, NATIVE_HITL_PREFIX,
    NativeHitlControlReceipt, NativeHitlDecision, NativeHitlRuleChange, NativeHitlRules,
    NativeHitlApprovalReference, NativeHitlPendingPage, NativeHitlPendingQuery, NativeHitlReadback, NativeHitlTurn,
    hitl_key, hitl_settings,
)
from .adapter import ConsoleError, PullAdapter, canonical, require, sha, strict_json

PHASE = "native-hitl"
MAX_WRITE_BYTES = 8192
# The decided card is shown again as a one-line record (Neal 2026-10-07); a long command or target is cut for it.
DISPLAY_CHARS = 500
MESSAGES = {code: "原操作或規則未通過核對；保留原紀錄，請重新讀取，不會自動重送。" for code in NATIVE_HITL_ERRORS}
MESSAGES.update({
    "NATIVE_HITL_UNAVAILABLE": "真人批准介面未啟用，或這個 API 版本尚未提供；不會改走其他批准入口。",
    "NATIVE_HITL_OWNER_ONLY": "只有這個 Team 的真人主人，且目前登入具備 Behavior.Approve，才能修改規則或決定。",
    "NATIVE_HITL_RULE_LIMIT": "這個 Team 已有 20 條規則；不能再新增或自動覆蓋，仍可另外明選「允許這次」。",
    "NATIVE_HITL_RULES_CHANGED": "規則已由其他操作變更；請重新讀取，再確認這次修改或決定。",
    "NATIVE_HITL_EXPIRED": "原等待期限已到；不能延長、恢復或批准這個舊 call。",
    "NATIVE_HITL_TURN_CLOSED": "原回合已結束；保存的決定不能恢復等待或重新執行。",
    "NATIVE_HITL_ORIGINAL_REQUIRED": "找不到完整原控制請求；不能另造編號核對或重送。",
    "NATIVE_HITL_ORIGINAL_INVALID": "保存的原控制請求與內容雜湊不符；保留原件，不重送。",
})


def decode(model, raw):
    try:
        strict_json(raw)
        return model.model_validate_json(raw)
    except (ValueError, TypeError):
        raise ConsoleError("NATIVE_HITL_RECORD_INVALID", 502) from None


def action_display(pending):
    """What the owner's card showed, saved beside the decision only to show it again; never part of the command."""
    action = pending.command.action
    command = action.arguments.get("command") if action.category == "shell" else None
    routine = None
    if action.category == "routine":
        routine = {key: value[:DISPLAY_CHARS] if isinstance(value := action.arguments.get(key), str) else None
                   for key in ("local_time", "time_zone", "content")}
    return {"tool": action.tool, "category": action.category, "target": action.target[:DISPLAY_CHARS],
            "command": command[:DISPLAY_CHARS] if isinstance(command, str) else None,
            "toolbox": pending.command.review.source == "toolbox",
            **({} if routine is None else {"routine": routine})}


class NativeHitlAdapter(PullAdapter):
    def read(self, path, model, token, command=None, *, params=None):
        try:
            raw, headers = self.send(
                "GET" if command is None else "POST", NATIVE_HITL_PREFIX + path,
                None if command is None else command.model_dump(mode="json"), token, limit=NATIVE_HITL_HTTP_BYTES,
                params=params)
        except ConsoleError as error:
            if error.upstream_status == 404:
                raise ConsoleError("NATIVE_HITL_UNAVAILABLE", 409, 404) from None
            raise
        require(headers.get("cache-control") == "no-store"
                and headers.get("content-type", "").split(";")[0] == "application/json",
                "NATIVE_HITL_RECORD_INVALID", 502)
        value = decode(model, raw)
        if isinstance(value, NativeHitlRules):
            require(headers.get("etag") == value.etag, "NATIVE_HITL_RECORD_INVALID", 502)
        return value


class NativeHitlControl:
    def __init__(self, native):
        self.native, self.console, self.journal = native, native.console, native.journal
        self.team_id = self.console.config.standing_policy.template.team_id
        self.enabled = hitl_settings()[0]
        # Team control routes live at the API root, not under an Agent relay prefix.
        self.adapter = NativeHitlAdapter(self.console.config, self.console.backend)
        self.observations = {}
        self.pages, self.errors = {}, {}

    def token(self, thread, *, write=False):
        require(self.enabled, "NATIVE_HITL_UNAVAILABLE", 409)
        self.console.active()
        if thread is not None:
            require(self.native.handles(thread), "ACTION_NOT_ALLOWED", 403)
        token = self.console.vault.get()
        if write:
            require(self.console.vault.allows("Behavior.Approve"), "NATIVE_HITL_OWNER_ONLY", 403)
        return token

    def call_scope(self, thread, call, token, *, read_original=False):
        if thread is None:
            identity = (self.console.api_prefix.removeprefix("/agents/") if self.console.api_prefix
                        else str(self.console.config.standing_policy.agent_id))
            require(identity == call.agent_id, "NATIVE_HITL_SCOPE_DENIED", 403)
        else:
            self.native._saved(thread)
            require(self.native.thread_for(call.request_id) == thread, "NATIVE_HITL_SCOPE_DENIED", 403)
        if read_original:
            original = self.native._receipt(call.request_id, token)
            require(self._turn(original) == self._turn(call), "NATIVE_HITL_SCOPE_DENIED", 403)

    def _read(self, path, model, token, command=None):
        value = self.adapter.read(path, model, token, command)
        self.native._current(token)
        if isinstance(value, NativeHitlRules):
            require(value.team_id == self.team_id, "NATIVE_HITL_SCOPE_DENIED", 403)
        return value

    def rules(self, thread):
        token = self.token(thread)
        rules = self._read("/rules", NativeHitlRules, token)
        return {"rules": rules.model_dump(mode="json"), "read_at": self.native.clock().isoformat(),
                "can_approve": self.console.vault.allows("Behavior.Approve"), "commands": self.history()}

    def team_attention(self, cursor=None):
        from shared_context.native_hitl_wire import AttentionCursor, NativeHitlTeamAttentionPage
        token = self.token(None)
        params = {"limit": 20}
        if cursor is not None:
            try:
                params["cursor"] = TypeAdapter(AttentionCursor).validate_python(cursor, strict=True)
            except ValueError:
                raise ConsoleError("NATIVE_HITL_INVALID_REQUEST", 400) from None
        value = self.adapter.read("/attention", NativeHitlTeamAttentionPage, token, params=params)
        self.native._current(token)
        require(value.team_id == self.team_id, "NATIVE_HITL_SCOPE_DENIED", 403)
        return value.model_dump(mode="json")

    @staticmethod
    def _turn(call):
        return NativeHitlTurn(agent_id=call.agent_id, conversation_id=call.conversation_id, request_id=call.request_id)

    def refresh(self, thread, token, *, only=None):
        """Read each turn's pending approvals; `only` limits it to one RID (the running turn's light status read).

        A finished turn whose pages were already read closed cannot gain a new approval, so it is not read again.
        """
        if not self.enabled:
            return
        try:
            for rid, original in tuple(self.native.receipts.items()):
                if self.native.request_threads.get(rid) != thread or only is not None and rid != only:
                    continue
                if self.pages.get(rid, {}).get("closed") and original.status not in {"accepted", "started"}:
                    continue
                self._read_turn(thread, rid, original, token)
            self.errors.pop(thread, None)
        except ConsoleError as error:
            self.errors[thread] = error.code
            if error.status in {401, 403} or error.upstream_status in {401, 403}:
                raise

    def _read_turn(self, thread, rid, original, token):
        turn = self._turn(original)
        references, attention, after, closed = [], [], 0, False
        while True:
            page = self._read("/turn-pending", NativeHitlPendingPage, token, NativeHitlPendingQuery(
                turn=turn, after=after))
            require(page.turn == turn and all(self._turn(ref.call) == turn for ref in page.references)
                    and all(item.approval in page.references for item in page.attention),
                    "NATIVE_HITL_RECORD_INVALID", 502)
            references.extend(page.references)
            attention.extend(page.attention)
            closed = closed or page.closed
            if page.next_after is None:
                break
            require(page.next_after > after, "NATIVE_HITL_RECORD_INVALID", 502)
            after = page.next_after
        require(len({ref.pending_id for ref in references}) == len(references),
                "NATIVE_HITL_RECORD_INVALID", 502)
        self.pages[rid] = {"thread_id": thread, "references": references,
                           "attention": [] if closed else attention, "closed": closed}

    def attention_label(self, thread, call):
        original = self.native.receipts.get(call.request_id)
        return (original.command.content[:60] if original is not None
                and self.native.request_threads.get(call.request_id) == thread else f"原回合 {call.request_id[:8]}")

    def attention(self):
        if not self.enabled or self.console.vault.state() != "AVAILABLE":
            return []
        result = []
        for page in self.pages.values():
            for item in page["attention"]:
                label = self.attention_label(page["thread_id"], item.approval.call)
                result.append({"thread_id": page["thread_id"], "label": label, "item": item.model_dump(mode="json")})
        return result

    def view(self, thread):
        if not self.enabled:
            return {}
        authenticated = self.console.vault.state() == "AVAILABLE"
        try:
            commands = self.history() if authenticated else []
            error = self.errors.get(thread)
        except ConsoleError as problem:
            commands, error = [], problem.code
        return {"enabled": True, "team_attention_available": True,
                "can_approve": authenticated and self.console.vault.allows("Behavior.Approve"),
                "commands": commands, "error": None if error is None else {"code": error},
                "references": [ref.model_dump(mode="json") for page in self.pages.values()
                               if authenticated and page["thread_id"] == thread for ref in page["references"]]}

    def _pending(self, thread, identifier, token):
        try:
            TypeAdapter(Digest).validate_python(identifier, strict=True)
        except ValueError:
            raise ConsoleError("NATIVE_HITL_INVALID_REQUEST", 400) from None
        value = self._read("/pending/" + identifier, NativeHitlReadback, token)
        pending, reference = value.pending, value.approval
        call = pending.command.action.call
        require(pending.pending_id == reference.pending_id == identifier
                and pending.sha256 == reference.pending_digest and reference.call == call,
                "NATIVE_HITL_RECORD_INVALID", 502)
        self.call_scope(thread, call, token, read_original=True)
        require(pending.tenant_id == self.console.config.tenant_id and pending.team_id == self.team_id,
                "NATIVE_HITL_SCOPE_DENIED", 403)
        return value

    def pending(self, thread, identifier):
        token = self.token(thread)
        value = self._pending(thread, identifier, token)
        rules = self._read("/rules", NativeHitlRules, token)
        require(value.pending.team_id == rules.team_id, "NATIVE_HITL_SCOPE_DENIED", 403)
        return {"readback": value.model_dump(mode="json"), "rules": rules.model_dump(mode="json"),
                "can_approve": self.console.vault.allows("Behavior.Approve"),
                "read_at": self.native.clock().isoformat(), "commands": self.history()}

    def _request_id(self, command, selection):
        return sha(canonical([
            self.console.config.tenant_id, self.team_id, self.console.config.human_oid,
            command.model_dump(mode="json", exclude={"request_id"}), selection,
        ]))

    def command(self, model, data, selection=None):
        require(type(data) is dict and "request_id" not in data, "NATIVE_HITL_INVALID_REQUEST", 400)
        try:
            value = model.model_validate_json(canonical({**data, "request_id": "unassigned"}))
        except (ValueError, TypeError):
            raise ConsoleError("NATIVE_HITL_INVALID_REQUEST", 400) from None
        return model.model_validate_json(canonical({
            **value.model_dump(mode="json"), "request_id": self._request_id(value, selection)}))

    def original(self, identifier):
        entry = self.journal.get(identifier, PHASE)
        require(entry is not None, "NATIVE_HITL_ORIGINAL_REQUIRED")
        try:
            intent = entry["intent"]
            model = {"/rules": NativeHitlRuleChange, "/decisions": NativeHitlDecision}[intent["path"]]
            raw = canonical(intent["body"])
            command = model.model_validate_json(raw)
            require(intent["method"] == "POST" and intent["config_sha256"] == self.console.config.sha256
                    and intent["team_id"] == self.team_id
                    and intent["body_sha256"] == sha(raw) and raw == canonical(command.model_dump(mode="json"))
                    and identifier == command.request_id == self._request_id(command, intent["selection"]),
                    "NATIVE_HITL_ORIGINAL_INVALID", 502)
        except (KeyError, ValueError, TypeError):
            raise ConsoleError("NATIVE_HITL_ORIGINAL_INVALID", 502) from None
        return entry, command

    def history(self):
        values = []
        for identifier, _ in self.journal.entries(PHASE):
            entry, command = self.original(identifier)
            values.append({
                "request_id": identifier, "kind": command.kind, "state": entry["state"],
                "thread_id": entry["intent"]["thread_id"],
                "command": command.model_dump(mode="json"), "selection": entry["intent"]["selection"],
                "code": entry.get("code"), "receipt": entry.get("receipt"),
                "observation": self.observations.get(identifier),
                "display": entry["intent"].get("display"), "created_at": entry["intent"].get("created_at"),
            })
        return values

    def _result(self, command):
        return {"request_id": command.request_id, "commands": self.history(),
                "original_request_resent": False, "purpose": "readback-only-not-execution-permission"}

    def _receipt(self, path, command, team, token, *, reconcile=False):
        receipt = self._read(path + ("/reconcile" if reconcile else ""), NativeHitlControlReceipt, token, command)
        config = self.console.config
        key = hitl_key("intent", config.tenant_id, team, config.human_oid, command.request_id)
        result = receipt.result
        revision = command.expected_rules_revision + int(
            isinstance(command, NativeHitlRuleChange) or command.choice == "allow_rule")
        require(receipt.control_request_id == hitl_key("apply", config.tenant_id, team, key)
                and receipt.control_request_digest == sha(canonical([
                    config.tenant_id, team, config.human_oid, command.model_dump(mode="json")]))
                and result.get("request_id") == command.request_id and result.get("kind") == command.kind
                and result.get("purpose") == "readback-only-not-execution-permission"
                and type(result.get("rules_revision")) is int and result["rules_revision"] == revision,
                "NATIVE_HITL_RECORD_INVALID", 502)
        if isinstance(command, NativeHitlDecision):
            require(result.get("pending_id") == command.pending_id
                    and result.get("status") == ("deny" if command.choice == "deny" else "allow"),
                    "NATIVE_HITL_RECORD_INVALID", 502)
        else:
            require(isinstance(result.get("rule_id"), str)
                    and (command.operation == "add" or result["rule_id"] == command.rule_id),
                    "NATIVE_HITL_RECORD_INVALID", 502)
        return receipt

    def _once(self, thread, path, command, rules, token, selection=None, display=None):
        body = command.model_dump(mode="json")
        self.native._current(token)
        self.token(thread, write=True)
        self.journal.begin(command.request_id, PHASE, {
            "method": "POST", "path": path, "body": body, "body_sha256": sha(canonical(body)),
            "selection": selection, "team_id": rules.team_id, "thread_id": thread,
            "config_sha256": self.console.config.sha256, "created_at": self.native.clock().isoformat(),
            **({} if display is None else {"display": display}),
        })
        # A failed remote after_commit above leaves UNKNOWN and never reaches the writer.
        try:
            self.native._current(token)
            self.token(thread, write=True)
            receipt = self._receipt(path, command, rules.team_id, token)
        except ConsoleError as error:
            self.journal.finish(command.request_id, PHASE, {
                "state": "UNKNOWN", "code": error.code, "observed_at": self.native.clock().isoformat(),
                "automatic_retry": False,
            })
            raise
        self.journal.finish(command.request_id, PHASE, {
            "state": "CONFIRMED", "receipt": receipt.model_dump(mode="json"),
            "observed_at": self.native.clock().isoformat(), "automatic_retry": False,
        })
        return self._result(command)

    @staticmethod
    def version(command, rules):
        require((command.expected_rules_revision, command.expected_rules_etag) == (rules.revision, rules.etag),
                "NATIVE_HITL_RULES_CHANGED")

    def change(self, thread, data):
        with self.console.message_lock(PHASE):
            token = self.token(thread, write=True)
            command = self.command(NativeHitlRuleChange, data)
            if self.journal.get(command.request_id, PHASE) is not None:
                self.original(command.request_id)
                return self._result(command)
            rules = self._read("/rules", NativeHitlRules, token)
            self.version(command, rules)
            require(command.operation != "add" or len(rules.rules) < MAX_RULES, "NATIVE_HITL_RULE_LIMIT")
            return self._once(thread, "/rules", command, rules, token)

    def reconcile(self, thread, identifier):
        with self.console.message_lock(PHASE):
            token = self.token(thread)
            entry, command = self.original(identifier)
            if isinstance(command, NativeHitlDecision):
                reference = decode(NativeHitlApprovalReference, canonical(entry["intent"]["selection"]))
                self.call_scope(thread, reference.call, token)
            self.observations.pop(identifier, None)
            receipt = self._receipt(entry["intent"]["path"], command, entry["intent"]["team_id"], token, reconcile=True)
            self.observations[identifier] = receipt.model_dump(mode="json")
            return self._result(command)

    def decide(self, thread, data):
        with self.console.message_lock(PHASE):
            token = self.token(thread, write=True)
            require(type(data) is dict and not {"request_id", "pending_id", "pending_digest"} & data.keys(),
                    "NATIVE_HITL_INVALID_REQUEST", 400)
            data = dict(data)
            reference = decode(NativeHitlApprovalReference, canonical(data.pop("approval", None)))
            selection = reference.model_dump(mode="json")
            command = self.command(NativeHitlDecision, {
                **data, "pending_id": reference.pending_id, "pending_digest": reference.pending_digest}, selection)
            self.call_scope(thread, reference.call, token)
            if self.journal.get(command.request_id, PHASE) is not None:
                self.original(command.request_id)
                return self._result(command)
            value = self._pending(thread, reference.pending_id, token)
            require(value.approval == reference, "NATIVE_HITL_ACTION_CHANGED")
            require(not value.expired and self.native.clock() < value.pending.expires_at, "NATIVE_HITL_EXPIRED")
            require(not value.turn_closed, "NATIVE_HITL_TURN_CLOSED")
            require(value.status == "pending" and value.decision is None and value.authorization is None,
                    "NATIVE_HITL_STATE_CONFLICT")
            rules = self._read("/rules", NativeHitlRules, token)
            require(value.pending.team_id == rules.team_id, "NATIVE_HITL_SCOPE_DENIED", 403)
            self.version(command, rules)
            require(command.choice != "allow_rule" or len(rules.rules) < MAX_RULES, "NATIVE_HITL_RULE_LIMIT")
            return self._once(thread, "/decisions", command, rules, token, selection, action_display(value.pending))


def install_hitl_routes(app, requested_console):
    from fastapi import Depends, Request
    from starlette.concurrency import run_in_threadpool
    from shared_context.behavior_wire import Command

    def control(console):
        require(console.native_instructions is not None, "NATIVE_HITL_UNAVAILABLE")
        return console.native_instructions.hitl

    async def body(request, *, read=False):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            require(len(raw) <= (0 if read else MAX_WRITE_BYTES), "NATIVE_HITL_INVALID_REQUEST", 413)
        if read:
            return None
        try:
            value = strict_json(bytes(raw))
            require(type(value) is dict, "NATIVE_HITL_INVALID_REQUEST", 400)
            return value
        except (ValueError, TypeError):
            raise ConsoleError("NATIVE_HITL_INVALID_REQUEST", 400) from None

    @app.get("/api/native-hitl/team/attention")
    async def team_attention(request: Request, console=Depends(requested_console)):
        require(set(request.query_params) <= {"cursor"}
                and len(request.query_params.getlist("cursor")) <= 1, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)
        return await run_in_threadpool(control(console).team_attention, request.query_params.get("cursor"))

    @app.get("/api/native-hitl/team/pending/{pending_id}")
    async def team_pending(pending_id: str, request: Request, console=Depends(requested_console)):
        await body(request, read=True)
        return await run_in_threadpool(control(console).pending, None, pending_id)

    @app.post("/api/native-hitl/team/decisions")
    async def team_decide(request: Request, console=Depends(requested_console)):
        return await run_in_threadpool(control(console).decide, None, await body(request))

    @app.post("/api/native-hitl/team/reconcile")
    async def team_reconcile(request: Request, console=Depends(requested_console)):
        command = decode(Command, canonical(await body(request)))
        return await run_in_threadpool(control(console).reconcile, None, command.request_id)

    @app.get("/api/native-hitl/{thread_id}/rules")
    async def rules(thread_id: str, request: Request, console=Depends(requested_console)):
        await body(request, read=True)
        return await run_in_threadpool(control(console).rules, thread_id)

    @app.post("/api/native-hitl/{thread_id}/rules")
    async def change(thread_id: str, request: Request, console=Depends(requested_console)):
        return await run_in_threadpool(control(console).change, thread_id, await body(request))

    @app.post("/api/native-hitl/{thread_id}/reconcile")
    async def reconcile(thread_id: str, request: Request, console=Depends(requested_console)):
        command = decode(Command, canonical(await body(request)))
        return await run_in_threadpool(control(console).reconcile, thread_id, command.request_id)

    @app.get("/api/native-hitl/{thread_id}/pending/{pending_id}")
    async def pending(thread_id: str, pending_id: str, request: Request, console=Depends(requested_console)):
        await body(request, read=True)
        return await run_in_threadpool(control(console).pending, thread_id, pending_id)

    @app.post("/api/native-hitl/{thread_id}/decisions")
    async def decide(thread_id: str, request: Request, console=Depends(requested_console)):
        return await run_in_threadpool(control(console).decide, thread_id, await body(request))
