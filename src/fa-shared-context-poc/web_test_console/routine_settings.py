"""Owner view of one Agent's daily routines in settings: list them and disable one. The API decides everything.

A routine is created only by the owner's allow on its approval card in a chat. Disabling saves a new, disabled version
of the same routine against the exact version the owner saw (``expected_version_id``). Its request ID comes from that
version, so a resend after an unknown reply finds the routine already disabled and writes nothing more. Nothing here
edits, re-enables or deletes a routine.
"""

import json
import re

from shared_context.behavior_native_turn_wire import material_digest
from shared_context.native_routine_wire import NativeRoutineChange, NativeRoutineList, NativeRoutineView

from .adapter import ConsoleError, PullAdapter, require, strict_json

PREFIX = "/native-routines/v1/agents/"
MAX_PAGES = 50
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
MESSAGES = {
    "ROUTINE_SETTINGS_UNAVAILABLE": "這個環境沒有開啟排程。",
    "ROUTINE_SETTINGS_NO_AGENT": "還不知道這位助理是哪一個；請先在對話裡送一則訊息，再回來查看排程。",
    "ROUTINE_SETTINGS_OWNER_ONLY": "只有這個 Team 的真人主人，且目前登入具備 Behavior.Approve，才能停用排程。",
    "ROUTINE_SETTINGS_CHANGED": "這個排程剛有變動，這次沒有停用；請重新讀取後再確認。",
    "ROUTINE_SETTINGS_INVALID_REQUEST": "請從清單選一個排程；不接受其他內容。",
    "ROUTINE_SETTINGS_RESPONSE_INVALID": "排程的回應格式不符；不當成完成，請重新讀取。",
    "NATIVE_ROUTINES_DISABLED": "這個環境沒有開啟排程。",
    "NATIVE_ROUTINE_PROPOSALS_DISABLED": "這個環境還沒開放從對話建立排程；沒有建立排程，也沒有記錄決定。",
    "NATIVE_ROUTINE_CHANGED": "這個排程剛有變動，這次沒有寫入；請重新讀取後再確認。",
    "NATIVE_ROUTINE_CONFLICT": "這個排程編號已有不同內容；沒有覆寫。",
    "NATIVE_ROUTINE_INVALID": "排程內容不符格式；沒有寫入。",
    "NATIVE_ROUTINE_MISSING": "找不到這個排程；請重新讀取。",
    "NATIVE_ROUTINE_SCOPE": "這個排程不屬於目前的主人或助理；沒有寫入。",
    "NATIVE_ROUTINE_INACTIVE": "排程的主人目前不是有效的 Team 主人；沒有寫入。",
}


def _decode(model, raw):
    try:
        strict_json(raw)
        return model.model_validate_json(raw)
    except (ValueError, TypeError):
        raise ConsoleError("ROUTINE_SETTINGS_RESPONSE_INVALID", 502) from None


class RoutineSettings:
    def __init__(self, console):
        self.console = console
        # Routine routes live at the API root and name the Agent in their path, not under its relay prefix.
        self.adapter = PullAdapter(console.config, console.backend)
        self.agent_id = self._agent(console)

    @staticmethod
    def _agent(console):
        if console.api_prefix:
            return console.api_prefix.removeprefix("/agents/")
        if console.config.standing_policy.agent_id is not None:
            return str(console.config.standing_policy.agent_id)
        # A single-Agent console without an Agent scope knows its Agent only from its own native turns.
        agents = {value.agent_id for value in tuple(console.native_instructions.receipts.values())}
        require(len(agents) == 1, "ROUTINE_SETTINGS_NO_AGENT")
        return agents.pop()

    def _token(self, *, write=False):
        self.console.active()
        token = self.console.vault.get()
        if write:
            require(self.console.vault.allows("Behavior.Approve"), "ROUTINE_SETTINGS_OWNER_ONLY", 403)
        return token

    def _send(self, method, path, token, body=None, params=None):
        raw, headers = self.adapter.send(method, PREFIX + self.agent_id + path, body, token, limit=512 * 1024,
                                         params=params)
        require(headers.get("cache-control") == "no-store", "ROUTINE_SETTINGS_RESPONSE_INVALID", 502)
        return raw

    def _mine(self, definition):
        return (definition.kind == "routine" and definition.agent_id == self.agent_id
                and definition.tenant_id == self.console.config.tenant_id
                and definition.owner_oid == self.console.config.human_oid)

    def list(self):
        token = self._token()
        views, after = [], None
        for _ in range(MAX_PAGES):
            page = _decode(NativeRoutineList, self._send(
                "GET", "/definitions", token, params=None if after is None else {"after": after}))
            views.extend(page.definitions)
            if page.next_after is None:
                break
            require(after is None or page.next_after > after, "ROUTINE_SETTINGS_RESPONSE_INVALID", 502)
            after = page.next_after
        else:
            raise ConsoleError("ROUTINE_SETTINGS_RESPONSE_INVALID", 502)
        # Only this login's own daily routines; one-time work reminders are not schedules the owner set.
        return {"schema_version": "fa-console-routines-v1", "agent_id": self.agent_id,
                "routines": [view.model_dump(mode="json") for view in views if self._mine(view.definition)],
                "can_manage": self.console.vault.allows("Behavior.Approve")}

    def disable(self, routine_id, version_id):
        require(_ID.fullmatch(routine_id) is not None and _ID.fullmatch(version_id) is not None,
                "ROUTINE_SETTINGS_INVALID_REQUEST", 400)
        token = self._token(write=True)
        current = _decode(NativeRoutineView, self._send("GET", "/definitions/" + routine_id, token))
        require(current.definition.routine_id == routine_id and self._mine(current.definition),
                "ROUTINE_SETTINGS_RESPONSE_INVALID", 502)
        if not current.definition.enabled:
            # Already off (this disable after an unknown reply, or an earlier one): nothing more to write.
            return {"routine": current.model_dump(mode="json")}
        require(current.definition.request_id == version_id, "ROUTINE_SETTINGS_CHANGED", 409)
        data = current.definition.model_dump(mode="json", include=set(NativeRoutineChange.model_fields))
        change = NativeRoutineChange.model_validate_json(json.dumps({
            **data, "request_id": material_digest({
                "kind": "fa-console-routine-disable-v1", "routine_id": routine_id, "version_id": version_id}),
            "expected_version_id": version_id, "enabled": False}))
        saved = _decode(NativeRoutineView, self._send("POST", "/definitions", token, change.model_dump(mode="json")))
        require(saved.definition.request_id == change.request_id and not saved.definition.enabled
                and self._mine(saved.definition), "ROUTINE_SETTINGS_RESPONSE_INVALID", 502)
        return {"routine": saved.model_dump(mode="json")}


def routines_available(console):
    """The 排程 tab and its routes exist only for a standing console with native routines on; the API still decides."""
    from .standing import StandingConsoleConfig
    return (type(console.config) is StandingConsoleConfig and console.native_instructions is not None
            and console.native_instructions.routines.enabled)


def install_routine_settings_routes(app, requested_console):
    from fastapi import Depends, Request
    from starlette.concurrency import run_in_threadpool

    def control(console):
        require(routines_available(console), "ROUTINE_SETTINGS_UNAVAILABLE")
        return RoutineSettings(console)

    async def no_input(request):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            require(len(raw) <= 16, "LOCAL_REQUEST_DENIED", 403)
        require(bytes(raw).strip() in {b"", b"{}"}, "LOCAL_REQUEST_DENIED", 403)

    @app.get("/api/routines")
    async def routines(request: Request, console=Depends(requested_console)):
        await no_input(request)
        return await run_in_threadpool(control(console).list)

    @app.post("/api/routines/{routine_id}/versions/{version_id}/disable")
    async def disable(routine_id: str, version_id: str, request: Request, console=Depends(requested_console)):
        await no_input(request)
        return await run_in_threadpool(control(console).disable, routine_id, version_id)
