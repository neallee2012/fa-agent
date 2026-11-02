"""Pins what the owner proxies do with a bad API reply and with a login that may not write (offline, no runtime)."""

from types import SimpleNamespace

import httpx
import pytest

from shared_context.native_group_wire import NativeGroupOptions
from shared_context.native_hitl_wire import NativeHitlRules
from web_test_console.adapter import ConsoleError
from web_test_console.groups import GroupControl
from web_test_console.native_hitl import NativeHitlAdapter
from web_test_console.routine_settings import RoutineSettings
from web_test_console.team_mcp import TeamMcp
from web_test_console.team_skills import TeamSkills

ORIGIN = "http://proxy-api.test"
NOT_JSON = b"not json"
DUPLICATE_KEY = b'{"a": 1, "a": 2}'
NOT_FINITE = b'{"a": NaN}'
WRONG_SHAPE = b"{}"
BAD_REPLIES = [NOT_JSON, DUPLICATE_KEY, NOT_FINITE, WRONG_SHAPE]


class Vault:
    """Records the order of the token steps; ``get`` raises when logged out, like the real vault."""

    def __init__(self, calls, *, logged_in=True, scopes=()):
        self.calls, self.logged_in, self.scopes = calls, logged_in, scopes

    def get(self):
        self.calls.append("get")
        if not self.logged_in:
            raise ConsoleError("LOGIN_REQUIRED", 401)
        return "token"

    def allows(self, scope):
        self.calls.append(("allows", scope))
        return scope in self.scopes


def console_for(content=b"{}", *, calls=None, logged_in=True, scopes=("Behavior.Approve",), active=None):
    calls = [] if calls is None else calls

    def reply(request):
        headers = {"cache-control": "no-store", "content-type": "application/json"}
        return httpx.Response(200, headers=headers, content=content)

    def check_active():
        calls.append("active")
        if active is not None:
            raise active

    return SimpleNamespace(
        config=SimpleNamespace(backend_origin=ORIGIN), backend=httpx.Client(transport=httpx.MockTransport(reply)),
        vault=Vault(calls, logged_in=logged_in, scopes=scopes), active=check_active, api_prefix="/agents/agent-1",
        native_instructions=SimpleNamespace(_current=lambda token: None))


def refused(call):
    with pytest.raises(ConsoleError) as error:
        call()
    return error.value.code, error.value.status, error.value.upstream_status


READS = {
    "routine list": (lambda c: RoutineSettings(c).list(), "ROUTINE_SETTINGS_RESPONSE_INVALID"),
    "routine disable": (lambda c: RoutineSettings(c).disable("routine-1", "version-1"),
                        "ROUTINE_SETTINGS_RESPONSE_INVALID"),
    "mcp servers": (lambda c: TeamMcp(c).servers(), "TEAM_MCP_RESPONSE_INVALID"),
    "mcp connections": (lambda c: TeamMcp(c).connections(), "TEAM_MCP_RESPONSE_INVALID"),
    "skill reviews": (lambda c: TeamSkills(c).reviews(), "TEAM_SKILL_RESPONSE_INVALID"),
    "skill version": (lambda c: TeamSkills(c).version("skill-1", "a" * 32), "TEAM_SKILL_RESPONSE_INVALID"),
    "group read": (lambda c: GroupControl(c).read("/options", NativeGroupOptions, "token"),
                   "NATIVE_GROUP_STATE_INVALID"),
    "hitl read": (lambda c: NativeHitlAdapter(c.config, c.backend).read("/rules", NativeHitlRules, "token"),
                  "NATIVE_HITL_RECORD_INVALID"),
}


@pytest.mark.parametrize("raw", BAD_REPLIES, ids=["not-json", "duplicate-key", "not-finite", "wrong-shape"])
@pytest.mark.parametrize("name", READS)
def test_a_bad_api_reply_is_refused_with_each_proxys_own_code(name, raw):
    call, code = READS[name]
    assert refused(lambda: call(console_for(raw))) == (code, 502, None)


def test_a_change_with_a_bad_reply_is_not_reported_as_applied():
    command = {"schema_version": "fa-team-mcp-change-v2", "request_id": "a" * 32, "expected_version": "1",
               "operation": "add", "server_label": "Docs",
               "server": {"server_label": "Docs", "server_url": "https://mcp.example.test/docs",
                          "project_connection_id": "safe-aad", "allowed_tools": ["search"],
                          "require_approval": "always"}}
    assert refused(lambda: TeamMcp(console_for(NOT_JSON)).change(command)) == ("TEAM_MCP_RESPONSE_INVALID", 502, None)


WRITERS = [(RoutineSettings, "ROUTINE_SETTINGS_OWNER_ONLY"), (TeamMcp, "TEAM_MCP_OWNER_ONLY"),
           (TeamSkills, "TEAM_SKILLS_OWNER_ONLY")]


@pytest.mark.parametrize("control,code", WRITERS)
def test_a_read_does_not_ask_for_the_write_permission(control, code):
    calls = []
    assert control(console_for(calls=calls, scopes=()))._token() == "token"
    assert calls == ["active", "get"]


@pytest.mark.parametrize("control,code", WRITERS)
def test_a_write_needs_the_approve_permission(control, code):
    calls = []
    assert control(console_for(calls=calls))._token(write=True) == "token"
    assert calls == ["active", "get", ("allows", "Behavior.Approve")]
    denied = console_for(scopes=("Behavior.Read",))
    assert refused(lambda: control(denied)._token(write=True)) == (code, 403, None)


@pytest.mark.parametrize("control,code", WRITERS)
def test_a_logged_out_writer_is_told_to_log_in_not_that_it_is_not_the_owner(control, code):
    calls = []
    console = console_for(calls=calls, logged_in=False, scopes=())
    assert refused(lambda: control(console)._token(write=True)) == ("LOGIN_REQUIRED", 401, None)
    assert calls == ["active", "get"]


@pytest.mark.parametrize("control,code", WRITERS)
def test_an_inactive_console_refuses_before_the_token_is_read(control, code):
    calls = []
    console = console_for(calls=calls, active=ConsoleError("CONSOLE_INACTIVE", 409))
    assert refused(lambda: control(console)._token()) == ("CONSOLE_INACTIVE", 409, None)
    assert calls == ["active"]
