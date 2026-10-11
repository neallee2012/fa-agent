"""Typing prewarm through the API (README#typing-prewarm): signed owner route -> original Foundry transport -> fake Hosted.

Only a ``prewarm`` invocation is sent; it never submits, reads a conversation, takes the session lock or answers
with an error after the entry check.
"""

import threading
import time
from time import monotonic

import httpx
import pytest

from shared_context import behavior_capabilities, behavior_native_turn_api
from shared_context.behavior_capabilities import CapabilityGrant
from shared_context.behavior_native_sessions import native_sessions
from shared_context.behavior_native_turn_api import NativeTurnGateway, PREFIX
from shared_context.behavior_native_turn_wire import NATIVE_REQUEST, NativePrewarmToolbox, NativePrewarmView
from shared_context.tool_catalog import ToolboxEntry
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_turn_api import native_api, submit  # noqa: F401


def toolbox_grant(entry_id, toolbox, label, version=None):
    return CapabilityGrant(entry_id=entry_id, entry_sha256="a" * 64, kind="mcp-toolbox", tools=(entry_id + "-x",),
                           vision=False, toolbox=ToolboxEntry(type="toolbox", toolbox=toolbox, server_label=label,
                                                              version=version))


GRANTS = {
    "WebIQ": toolbox_grant("WebIQ", "fa-org-tools", "WebIQ"),
    # One Team toolbox version holds one grant per server: named once.
    "team-feed": toolbox_grant("team-feed", "fa-team-team-one", "feed", "2"),
    "team-learn": toolbox_grant("team-learn", "fa-team-team-one", "learn", "2"),
    "fa-shell-v1": CapabilityGrant(entry_id="fa-shell-v1", entry_sha256="b" * 64, kind="fa-builtin",
                                   tools=("run_command",), vision=False),
}
EXPECTED = (NativePrewarmToolbox(toolbox="fa-org-tools"), NativePrewarmToolbox(toolbox="fa-team-team-one", version="2"))


class Hosted:
    """The fake Hosted of test_native_turn_api, answering ``prewarm`` too (optionally held or refused)."""

    def __init__(self, case):
        self.case, self.prewarms, self.reply, self.hold = case, [], None, None
        case.foundry.http._transport = httpx.MockTransport(self)

    def __call__(self, request):
        command = NATIVE_REQUEST.validate_json(request.content)
        if command.operation != "prewarm":
            return self.case.remote(request)
        self.prewarms.append(command)
        if self.hold is not None:
            self.hold.wait(10)
        if self.reply is not None:
            return self.reply(request)
        target = self.case.remote.target
        view = NativePrewarmView(agent_id=target.agent_id, hosted_session_id=target.session_id,
                                 toolboxes=len(command.toolboxes))
        return httpx.Response(200, content=view.model_dump_json().encode(), headers={
            "content-type": "application/json", "x-agent-invocation-id": "platform-prewarm",
            "x-agent-session-id": target.session_id})


@pytest.fixture
def hosted(native_api, monkeypatch):
    monkeypatch.setattr(behavior_capabilities, "runtime_grants", lambda runtime: GRANTS)
    return Hosted(native_api)


def prewarm(case, body=b"{}"):
    return case.client.post(PREFIX + "/prewarm", content=body,
                            headers={**case.headers, "Content-Type": "application/json"})


def test_an_owner_prewarm_sends_one_prewarm_naming_each_toolbox_version_once(native_api, hosted, caplog):
    case = native_api
    caplog.set_level("WARNING")
    response = prewarm(case)
    assert response.status_code == 200, response.text
    assert response.json() == {"state": "sent", "toolboxes": 2}
    assert [command.toolboxes for command in hosted.prewarms] == [EXPECTED]
    # Nothing else reached Hosted: no session read, conversation read, submit or Responses call.
    assert [command.operation for _, command in case.remote.calls] == []
    assert "FA_NATIVE_PREWARM outcome=sent toolboxes=2" in caplog.text


def test_a_second_prewarm_within_the_interval_reaches_nothing(native_api, hosted, monkeypatch):
    case = native_api
    assert prewarm(case).json()["state"] == "sent"
    assert prewarm(case).json() == {"state": "recent", "toolboxes": 0}
    assert len(hosted.prewarms) == 1
    later = monotonic() + behavior_native_turn_api.PREWARM_INTERVAL_SECONDS + 1
    monkeypatch.setattr(behavior_native_turn_api, "monotonic", lambda: later)
    assert prewarm(case).json()["state"] == "sent" and len(hosted.prewarms) == 2


def lost(request):
    raise httpx.ReadTimeout("lost", request=request)


def test_a_refused_or_lost_prewarm_answers_skipped_and_only_a_moved_binding_forgets_the_reader(native_api, hosted):
    case = native_api
    sessions = native_sessions(case.composition)
    gateway = NativeTurnGateway(case.composition, enabled=True)
    hosted.reply = lost
    value, _ = gateway.prewarm(case.c.human)
    assert value.state == "skipped" and sessions.reader_cached()
    gateway._prewarmed = None
    hosted.reply = lambda request: httpx.Response(409, json={"error": {"code": "NATIVE_SESSION_CHANGED"}})
    value, _ = gateway.prewarm(case.c.human)
    assert value.state == "skipped" and not sessions.reader_cached()
    assert len(hosted.prewarms) == 2 and case.remote.calls == []


def test_a_prewarm_without_a_current_session_reaches_nothing(native_api, hosted, monkeypatch):
    case = native_api
    monkeypatch.setattr("shared_context.behavior_native_sessions._read_platform_session", lambda client: None)
    native_sessions(case.composition).forget()
    response = prewarm(case)
    # The selected version has no session yet (a follow still to come): nothing is created or followed.
    assert response.status_code == 200 and response.json()["state"] == "skipped"
    assert hosted.prewarms == [] and case.remote.calls == []


def test_a_send_never_waits_for_a_prewarm_that_is_still_waking_the_session(native_api, hosted):
    case = native_api
    hosted.hold = threading.Event()
    gateway = NativeTurnGateway(case.composition, enabled=True)
    answers = []
    waking = threading.Thread(target=lambda: answers.append(gateway.prewarm(case.c.human)[0]))
    waking.start()
    deadline = monotonic() + 5
    while not hosted.prewarms and monotonic() < deadline:
        time.sleep(0.01)
    assert hosted.prewarms and waking.is_alive()
    accepted = submit(case)
    assert accepted.status_code == 202, accepted.text
    assert waking.is_alive() and answers == []
    hosted.hold.set()
    waking.join(5)
    assert answers[0].state == "sent"
    assert [command.operation for _, command in case.remote.calls if command.operation != "session_read"] == ["submit"]


def test_a_prewarm_needs_the_signed_owner_and_an_empty_body(native_api, hosted):
    case = native_api
    unsigned = case.client.post(PREFIX + "/prewarm", json={})
    assert unsigned.status_code == 401
    assert prewarm(case, b'{"toolboxes": []}').status_code == 422
    assert hosted.prewarms == []
