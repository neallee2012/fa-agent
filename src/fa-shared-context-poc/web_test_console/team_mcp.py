"""Owner management of Team MCP servers through the behavior API's /team-mcp/v1 routes.

The API decides every permission and writes the Team's own Foundry toolbox version; this console only proxies the
owner's reads and one change at a time. A change keeps the request ID the page chose, so resending the same command
reuses its toolbox version. A key typed for a new connection passes through once, in that one body to the API; it is
not stored, logged or echoed here.
"""

import json
import re

from shared_context.team_mcp_wire import TeamMcpChange, TeamMcpChangeResult, TeamMcpConnections, TeamMcpView

from .adapter import ConsoleError, PullAdapter, require, strict_json

PREFIX = "/team-mcp/v1"
MAX_READ_BYTES = 128 * 1024
MAX_CHANGE_BYTES = 16 * 1024
_REQUEST_ID = re.compile(r"[a-f0-9]{32}")
TEAM_MCP_ERRORS = frozenset({
    "TEAM_MCP_OWNER_ONLY", "TEAM_MCP_UNAVAILABLE", "TEAM_MCP_CHANGED", "TEAM_MCP_INVALID", "TEAM_MCP_LIMIT",
    "TEAM_MCP_EXISTS", "TEAM_MCP_MISSING", "TEAM_MCP_LAST_SERVER", "TEAM_MCP_CONNECTION_DENIED",
    "TEAM_MCP_CONNECTION_CREATE_UNAVAILABLE", "TEAM_MCP_COMMAND_CONFLICT", "TEAM_MCP_FORBIDDEN",
    "TEAM_MCP_REFUSED", "TEAM_MCP_OUTCOME_UNKNOWN", "TEAM_MCP_PLAINTEXT_SECRET", "TEAM_MCP_FEED_URL_INVALID",
    "TEAM_MCP_FEED_UNAVAILABLE",
})
MESSAGES = {code: "MCP 設定未通過核對；請重新讀取，不會自動重送。" for code in TEAM_MCP_ERRORS}
MESSAGES.update({
    "TEAM_MCP_UNAVAILABLE": "這個環境還沒開放從設定頁管理 MCP；新增 MCP 請找部署線。",
    "TEAM_MCP_OWNER_ONLY": "只有這個 Team 的真人主人，且目前登入具備 Behavior.Approve，才能新增、修改或停用 MCP。",
    "TEAM_MCP_CHANGED": "Team 的 MCP 設定剛被改過（Foundry 的預設版本已不同）；這次沒有套用。請重新讀取後再改。",
    "TEAM_MCP_INVALID": "MCP 設定不完整或格式不符；沒有送出。",
    "TEAM_MCP_INPUT_INVALID": "MCP 設定不完整或格式不符；沒有送出。",
    "TEAM_MCP_LIMIT": "Team 最多 8 個 MCP；沒有新增。",
    "TEAM_MCP_EXISTS": "已有同名的 MCP；要改內容請用「修改」。",
    "TEAM_MCP_MISSING": "找不到這個 MCP；請重新讀取。",
    "TEAM_MCP_LAST_SERVER": "這是 Team 最後一個 MCP，不能停用：Foundry 的 toolbox 版本至少要有一個工具，而 Team 的 toolbox 不刪除。請先新增另一個 MCP，再停用這個。",
    "TEAM_MCP_CONNECTION_DENIED": "這個 connection 不可用、不是 MCP 支援的類別，或屬於別的 Team；沒有送出。",
    "TEAM_MCP_CONNECTION_CREATE_UNAVAILABLE": "這個環境還不能從設定頁建立 connection；請改選既有的 connection。沒有送出。",
    "TEAM_MCP_COMMAND_CONFLICT": "這個變更編號已用在另一個變更；不會覆寫。",
    "TEAM_MCP_FORBIDDEN": "Foundry 拒絕了這次寫入（權限不足）；沒有自動重試，請通知部署線。",
    "TEAM_MCP_REFUSED": "Foundry 拒絕了這個 MCP 設定，沒有建立新版本；請檢查網址、工具名稱和金鑰欄位後重送新的變更。",
    "TEAM_MCP_OUTCOME_UNKNOWN": "MCP 變更結果未確認；請按「用原編號重送」，不會建立第二個版本。",
    "TEAM_MCP_PLAINTEXT_SECRET": "網址看起來含有金鑰；金鑰請填在下方的金鑰欄位，或放在 Foundry 的 connection 裡。沒有送出。",
    "TEAM_MCP_FEED_URL_INVALID": "標頭是 x-feed-url 時，金鑰欄位要貼一個完整的 https feed 網址（443 埠、不含帳密或 #）。沒有送出。",
    "TEAM_MCP_FEED_UNAVAILABLE": "這個環境還沒設定讀 feed 的簽章金鑰，不能建立 x-feed-url 的 connection。沒有送出，請通知部署線。",
    "TEAM_MCP_RESPONSE_INVALID": "MCP 設定的回應格式不符；不當成完成，請重新讀取。",
})


def _decode(model, raw):
    try:
        strict_json(raw)
        return model.model_validate_json(raw)
    except (ValueError, TypeError):
        raise ConsoleError("TEAM_MCP_RESPONSE_INVALID", 502) from None


class TeamMcpAdapter(PullAdapter):
    def call(self, method, path, token, *, body=None, limit=MAX_READ_BYTES):
        try:
            raw, headers = self.send(method, PREFIX + path, body, token, limit=limit)
        except ConsoleError as error:
            if error.code == "NO_RESULT" and error.upstream_status == 404:
                raise ConsoleError("TEAM_MCP_UNAVAILABLE", 409, 404) from None
            raise
        require(headers.get("cache-control") == "no-store", "TEAM_MCP_RESPONSE_INVALID", 502)
        return raw


class TeamMcp:
    def __init__(self, console):
        self.console = console
        self.adapter = TeamMcpAdapter(console.config, console.backend)

    def _token(self, *, write=False):
        self.console.active()
        token = self.console.vault.get()
        if write:
            require(self.console.vault.allows("Behavior.Approve"), "TEAM_MCP_OWNER_ONLY", 403)
        return token

    def servers(self):
        view = _decode(TeamMcpView, self.adapter.call("GET", "/servers", self._token())).model_dump(mode="json")
        # The page shows the form only to a login that may change Team MCP; the API still decides every write.
        return {**view, "can_manage": bool(self.console.vault.allows("Behavior.Approve"))}

    def connections(self):
        return _decode(TeamMcpConnections, self.adapter.call("GET", "/connections", self._token())).model_dump(mode="json")

    def change(self, data):
        try:
            command = TeamMcpChange.model_validate_json(json.dumps(data))
        except (ValueError, TypeError) as error:
            code = "TEAM_MCP_FEED_URL_INVALID" if "TEAM_MCP_FEED_URL_INVALID" in str(error) else "TEAM_MCP_INPUT_INVALID"
            raise ConsoleError(code, 400) from None
        require(_REQUEST_ID.fullmatch(command.request_id), "TEAM_MCP_INPUT_INVALID", 400)
        body = command.model_dump(mode="json")
        if command.new_connection is not None:
            # The dump masks the key; only this one outgoing body carries it.
            body["new_connection"]["secret"] = command.new_connection.secret.get_secret_value()
        raw = self.adapter.call("POST", "/changes", self._token(write=True), body=body)
        result = _decode(TeamMcpChangeResult, raw)
        require(result.request_id == command.request_id and result.state == "applied", "TEAM_MCP_RESPONSE_INVALID", 502)
        return result.model_dump(mode="json")


def install_team_mcp_routes(app, requested_console):
    from fastapi import Depends, Request
    from starlette.concurrency import run_in_threadpool

    def control(console):
        from .standing import StandingConsoleConfig
        require(type(console.config) is StandingConsoleConfig, "TEAM_MCP_UNAVAILABLE")
        return TeamMcp(console)

    async def read(request):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)

    async def body(request, limit):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            require(len(raw) <= limit, "TEAM_MCP_INPUT_INVALID", 413)
        try:
            value = strict_json(bytes(raw))
        except (ValueError, TypeError):
            raise ConsoleError("TEAM_MCP_INPUT_INVALID", 400) from None
        require(type(value) is dict, "TEAM_MCP_INPUT_INVALID", 400)
        return value

    @app.get("/api/team-mcp/servers")
    async def servers(request: Request, console=Depends(requested_console)):
        await read(request)
        return await run_in_threadpool(control(console).servers)

    @app.get("/api/team-mcp/connections")
    async def connections(request: Request, console=Depends(requested_console)):
        await read(request)
        return await run_in_threadpool(control(console).connections)

    @app.post("/api/team-mcp/changes")
    async def change(request: Request, console=Depends(requested_console)):
        return await run_in_threadpool(control(console).change, await body(request, MAX_CHANGE_BYTES))


def is_team_mcp_write(path):
    return path == "/api/team-mcp/changes"
