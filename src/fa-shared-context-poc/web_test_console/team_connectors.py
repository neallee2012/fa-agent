"""Owner management of Team connectors (the API's /team-connectors/v1) and each Agent's connector switch (the
API's /work-policy/v1/agent-connectors).

The API decides every permission and writes every version; this console proxies the owner's reads and one change at
a time, keeping the request ID the page chose, so resending the same body after an unknown reply finds the same
toolbox version or saved choice. The page sees only what it shows: no toolbox or connection name, client ID, secret,
token or consent URL. A client secret typed for the first add passes through once, in that one body to the API; it
is not stored, logged or echoed here.
"""

import json
import re

from shared_context.connector_registry import CONNECTORS
from shared_context.team_connector_wire import (
    AgentConnectorsView, SelectAgentConnectors, TeamConnectorChange, TeamConnectorChangeResult, TeamConnectorState,
    TeamConnectorsView,
)

from .adapter import ConsoleError, PullAdapter, require, strict_json
from .team_mcp import TEAM_MCP_ERRORS

PREFIX = "/team-connectors/v1"
AGENT_PATH = "/work-policy/v1/agent-connectors"
MAX_READ_BYTES = 64 * 1024
MAX_CHANGE_BYTES = 8 * 1024
_REQUEST_ID = re.compile(r"[a-f0-9]{32}")
# The connector API reuses the Team MCP owner, version and Foundry failure codes; the page names them per connector.
API_ERRORS = TEAM_MCP_ERRORS | {"TEAM_CONNECTOR_SECRET_REQUIRED", "TEAM_CONNECTOR_TOOLBOX_TAKEN"}
AGENT_API_ERRORS = frozenset({
    "AGENT_CONNECTORS_CHANGED", "AGENT_CONNECTORS_CONFLICT", "AGENT_CONNECTOR_UNKNOWN", "AGENT_CONNECTORS_INVALID",
    "WORK_POLICY_HUMAN_REQUIRED", "BEHAVIOR_ACCESS_DENIED",
})
_AGENT_OWNER_ONLY = {"WORK_POLICY_HUMAN_REQUIRED", "BEHAVIOR_ACCESS_DENIED"}
MESSAGES = {
    "TEAM_CONNECTOR_UNAVAILABLE": "這個環境還沒開放 Connectors；要加入 Microsoft Teams 請找部署線。",
    "TEAM_CONNECTOR_OWNER_ONLY": "Connectors 只有這個 Team 的真人主人能查看；變更還需要目前登入具備 Behavior.Approve。",
    "TEAM_CONNECTOR_CHANGED": "Connector 設定剛被改過，這次沒有套用。已重新讀取，請確認後再改。",
    "TEAM_CONNECTOR_INVALID": "Connector 設定不完整或格式不符；沒有送出。",
    "TEAM_CONNECTOR_INPUT_INVALID": "Connector 設定不完整或格式不符；沒有送出。",
    "TEAM_CONNECTOR_COMMAND_CONFLICT": "這個變更編號已用在另一個變更；不會覆寫。請重新讀取後再改。",
    "TEAM_CONNECTOR_FORBIDDEN": "Foundry 拒絕了這次寫入（權限不足）；沒有自動重試，請通知部署線。",
    "TEAM_CONNECTOR_REFUSED": "Foundry 拒絕了這個設定，沒有建立新版本；請通知部署線。",
    "TEAM_CONNECTOR_OUTCOME_UNKNOWN": "Connector 變更結果未確認；請按「用原編號重送」，不會建立第二個版本。",
    "TEAM_CONNECTOR_CONNECTION_CREATE_UNAVAILABLE": "這個環境沒有設定這個 connector 的 Entra 應用程式，還不能加入；沒有送出，請通知部署線。",
    "TEAM_CONNECTOR_SECRET_REQUIRED": "第一次加入需要 Entra 應用程式的用戶端密碼；沒有送出。",
    "TEAM_CONNECTOR_TOOLBOX_TAKEN": "這個 connector 的 toolbox 裡是別處寫的設定；這裡不覆寫，請通知部署線。",
    "TEAM_CONNECTOR_RESPONSE_INVALID": "Connector 設定的回應格式不符；不當成完成，請重新讀取。",
    "AGENT_CONNECTORS_UNAVAILABLE": "這個環境還沒開放 Connectors。",
    "AGENT_CONNECTORS_OWNER_ONLY": "只有這個 Team 的真人主人，且目前登入具備 Behavior.Approve，才能開關這位助理的 connector。",
    "AGENT_CONNECTORS_CHANGED": "這位助理的 connector 設定剛被改過，這次沒有套用。已重新讀取，請確認後再改。",
    "AGENT_CONNECTORS_CONFLICT": "這個變更編號已用在另一個變更；不會覆寫。",
    "AGENT_CONNECTOR_UNKNOWN": "沒有這個 connector；沒有送出。",
    "AGENT_CONNECTORS_INVALID": "保存的 connector 設定不屬於這位助理；沒有寫入，請通知部署線。",
    "AGENT_CONNECTORS_INPUT_INVALID": "connector 開關的內容不符格式；沒有送出。",
    "AGENT_CONNECTORS_RESPONSE_INVALID": "connector 開關的回應格式不符；不當成完成，請重新讀取。",
}


def _decode(model, raw, code):
    try:
        strict_json(raw)
        return model.model_validate_json(raw)
    except (ValueError, TypeError):
        raise ConsoleError(code, 502) from None


def _parse(model, data, code):
    try:
        return model.model_validate_json(json.dumps(data))
    except (ValueError, TypeError):
        raise ConsoleError(code, 400) from None


def _card(state: TeamConnectorState) -> dict:
    """One connector as the page shows it. ``version`` is the toolbox default the owner read, sent back as the
    change's expected_version. ``foreign``: a default version FA did not write for this Team; nothing is changed."""
    status = ("not-added" if state.default_version is None else "foreign" if state.settings is None
              else "enabled" if state.settings.enabled else "disabled")
    return {"connector_id": state.connector_id, "display_name": state.display_name, "disclosure": state.disclosure,
            "revoke_url": state.revoke_url, "version": state.default_version, "status": status,
            "settings": None if state.settings is None else state.settings.model_dump(mode="json")}


class TeamConnectors:
    def __init__(self, console):
        self.console = console
        # Team routes live at the API root; the Agent switch goes through the selected Agent's own prefix.
        self.team = PullAdapter(console.config, console.backend)

    def _token(self, code, *, write=False):
        self.console.active()
        token = self.console.vault.get()
        if write:
            require(self.console.vault.allows("Behavior.Approve"), code, 403)
        return token

    def _can_manage(self):
        return bool(self.console.vault.allows("Behavior.Approve"))

    def _team_call(self, method, path, token, body=None):
        try:
            raw, headers = self.team.send(method, PREFIX + path, body, token, limit=MAX_READ_BYTES)
        except ConsoleError as error:
            if error.code == "NO_RESULT" and error.upstream_status == 404:
                raise ConsoleError("TEAM_CONNECTOR_UNAVAILABLE", 409, 404) from None
            if error.code.startswith("TEAM_MCP_"):
                raise ConsoleError(error.code.replace("TEAM_MCP_", "TEAM_CONNECTOR_", 1), error.status,
                                   error.upstream_status) from None
            raise
        require(headers.get("cache-control") == "no-store", "TEAM_CONNECTOR_RESPONSE_INVALID", 502)
        return raw

    def _agent_call(self, method, token, body=None):
        try:
            raw, headers = self.console.adapter.send(method, AGENT_PATH, body, token, limit=MAX_READ_BYTES)
        except ConsoleError as error:
            if error.code == "NO_RESULT" and error.upstream_status == 404:
                raise ConsoleError("AGENT_CONNECTORS_UNAVAILABLE", 409, 404) from None
            if error.code in _AGENT_OWNER_ONLY:
                raise ConsoleError("AGENT_CONNECTORS_OWNER_ONLY", 403, error.upstream_status) from None
            raise
        require(headers.get("cache-control") == "no-store", "AGENT_CONNECTORS_RESPONSE_INVALID", 502)
        return _decode(AgentConnectorsView, raw, "AGENT_CONNECTORS_RESPONSE_INVALID")

    def connectors(self):
        view = _decode(TeamConnectorsView, self._team_call("GET", "/connectors", self._token("TEAM_CONNECTOR_OWNER_ONLY")),
                       "TEAM_CONNECTOR_RESPONSE_INVALID")
        return {"schema_version": "fa-console-team-connectors-v1", "can_manage": self._can_manage(),
                "connectors": [_card(state) for state in view.connectors]}

    def change(self, data):
        command = _parse(TeamConnectorChange, data, "TEAM_CONNECTOR_INPUT_INVALID")
        require(_REQUEST_ID.fullmatch(command.request_id), "TEAM_CONNECTOR_INPUT_INVALID", 400)
        token = self._token("TEAM_CONNECTOR_OWNER_ONLY", write=True)
        body = command.model_dump(mode="json")
        if command.client_secret is not None:
            # The dump masks the secret; only this one outgoing body carries it.
            body["client_secret"] = command.client_secret.get_secret_value()
        result = _decode(TeamConnectorChangeResult, self._team_call("POST", "/changes", token, body),
                         "TEAM_CONNECTOR_RESPONSE_INVALID")
        require(result.request_id == command.request_id and result.connector.connector_id == command.connector_id,
                "TEAM_CONNECTOR_RESPONSE_INVALID", 502)
        return {"schema_version": "fa-console-team-connector-change-v1", "request_id": result.request_id,
                "state": result.state, "toolbox_version": result.toolbox_version, "connector": _card(result.connector),
                "original_request_resent": False}

    def _agent_view(self, view: AgentConnectorsView):
        off = set(view.disabled_connector_ids)
        return {"schema_version": "fa-console-agent-connectors-v1", "revision": view.revision,
                "applies_to": view.applies_to, "can_manage": self._can_manage(),
                "connectors": [{"connector_id": ref.id, "display_name": ref.display_name, "on": ref.id not in off}
                               for ref in CONNECTORS.values()]}

    def agent_connectors(self):
        return self._agent_view(self._agent_call("GET", self._token("AGENT_CONNECTORS_OWNER_ONLY")))

    def select_agent_connectors(self, data):
        command = _parse(SelectAgentConnectors, data, "AGENT_CONNECTORS_INPUT_INVALID")
        require(_REQUEST_ID.fullmatch(command.request_id), "AGENT_CONNECTORS_INPUT_INVALID", 400)
        token = self._token("AGENT_CONNECTORS_OWNER_ONLY", write=True)
        view = self._agent_call("POST", token, command.model_dump(mode="json"))
        # The same request ID returns its original result, also after later changes.
        require(view.revision == command.expected_revision + 1
                and view.disabled_connector_ids == command.disabled_connector_ids,
                "AGENT_CONNECTORS_RESPONSE_INVALID", 502)
        return self._agent_view(view)


def is_connector_write(path):
    return path in {"/api/team-connectors/changes", "/api/agent-connectors"}


def install_team_connector_routes(app, requested_console):
    from fastapi import Depends, Request
    from starlette.concurrency import run_in_threadpool

    def control(console, code="TEAM_CONNECTOR_UNAVAILABLE"):
        from .standing import StandingConsoleConfig
        require(type(console.config) is StandingConsoleConfig, code)
        return TeamConnectors(console)

    async def read(request):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)

    async def body(request, code):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            require(len(raw) <= MAX_CHANGE_BYTES, code, 413)
        try:
            value = strict_json(bytes(raw))
        except (ValueError, TypeError):
            raise ConsoleError(code, 400) from None
        require(type(value) is dict, code, 400)
        return value

    @app.get("/api/team-connectors")
    async def connectors(request: Request, console=Depends(requested_console)):
        await read(request)
        return await run_in_threadpool(control(console).connectors)

    @app.post("/api/team-connectors/changes")
    async def change(request: Request, console=Depends(requested_console)):
        return await run_in_threadpool(control(console).change, await body(request, "TEAM_CONNECTOR_INPUT_INVALID"))

    @app.get("/api/agent-connectors")
    async def agent_connectors(request: Request, console=Depends(requested_console)):
        await read(request)
        return await run_in_threadpool(control(console, "AGENT_CONNECTORS_UNAVAILABLE").agent_connectors)

    @app.post("/api/agent-connectors")
    async def select_agent_connectors(request: Request, console=Depends(requested_console)):
        return await run_in_threadpool(control(console, "AGENT_CONNECTORS_UNAVAILABLE").select_agent_connectors,
                                       await body(request, "AGENT_CONNECTORS_INPUT_INVALID"))
