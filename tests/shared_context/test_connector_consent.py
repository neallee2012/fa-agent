"""C3: a toolbox source this Agent has not connected becomes the owner's Connect card, resumed at most once.

Offline: real JWT/HITL API/CAS and NativeEffects, the toolbox as an httpx MockTransport, the model as a double that
must never be asked. The consent links are synthetic.
"""

import asyncio
import json
import logging
from datetime import timedelta

import httpx
import pytest

from shared_context.m2_copilot import ToolFailure
from shared_context.native_hitl import validate_native_hitl_history
from shared_context.native_hitl_wire import (
    ConnectorConsent, ConnectorConsentDecision, ConnectorConsentReadback, NativeHitlCall, NativeHitlEnd,
    NativeHitlPendingQuery,
)
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_hitl import error, hitl, inventory, post  # noqa: F401
from test_native_hitl_attention import attention  # noqa: F401
from test_native_toolbox import REVOKED, Toolbox
from test_native_toolbox_approval import ARGUMENTS, packet, permit, platform, setup  # noqa: F401
from test_native_toolbox_parallel import boundary

PREFIX = "/native-hitl/v1"
FIRST, SECOND = ("https://consent.example.test/login?state=" + letter * 2290 + "&src=1" for letter in "AB")
# Shape of the dev toolbox's real -32006 (#746): a sentence, then one error per unconsented source. JSON escapes the
# links' "&", so only a decoded message gives them back whole.
MESSAGE = ("tools/list failed for 2 tool source(s), succeeded for 1 tool source(s) " + json.dumps({"errors": [
    {"name": "ghprobe_a", "type": "mcp", "error": {"code": "CONSENT_REQUIRED", "message": FIRST}},
    {"name": "ghprobe_b", "type": "mcp", "error": {"code": "CONSENT_REQUIRED", "message": SECOND}}]}).replace(
        "&", "\\u0026"))


def not_connected(body):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32006, "message": MESSAGE}})


def revoked(body):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32602, "message": REVOKED}})


class Provider(Toolbox):
    """The toolbox while this Agent's connection is missing (``answer`` for ``method``) until ``connected``."""

    def __init__(self, method, answer=not_connected):
        super().__init__()
        self.method, self.answer, self.connected = method, answer, False
        original = self.handle

        def handle(request):
            if request.method != "GET" and not self.connected:
                body = json.loads(request.content)
                if body["method"] == self.method:
                    self.requests.append((request, body))
                    if body["method"] == "tools/call":
                        self.calls.append(body["params"])
                    return self.answer(body)
            return original(request)
        self.transport = httpx.MockTransport(handle)

    def count(self, method):
        return sum(body["method"] == method for _, body in self.requests)


def page(c, turn):
    response = post(c, "/turn-consents", NativeHitlPendingQuery(turn=turn))
    assert response.status_code == 200, response.text
    return response


async def card(case, count=1):
    for _ in range(400):
        response = page(case.c, case.gate.turn)
        if len(response.json()["references"]) >= count:
            return response
        await asyncio.sleep(0.005)
    pytest.fail("no Connect card was saved")


def readback(c, cid):
    response = c.client.get(PREFIX + "/consents/" + cid, headers=c.headers())
    assert response.status_code == 200, response.text
    return ConnectorConsentReadback.model_validate_json(response.content)


def click(c, item, choice="resume", request_id="owner-click", digest=None):
    return post(c, "/consent-decisions", ConnectorConsentDecision(
        request_id=request_id, consent_id=item["item_id"], consent_digest=digest or item["consent"]["consent_digest"],
        choice=choice), read_only=True)


def test_a_listing_without_consent_waits_before_the_model_and_lists_once_more_after_one_resume(
        attention, tmp_path, caplog):
    hitl, toolbox = attention, Provider("tools/list")
    case = setup(hitl, tmp_path, "never", toolbox=toolbox)

    async def check():
        with caplog.at_level(logging.DEBUG):
            binding = asyncio.create_task(case.bind())
            listed = await card(case)
            assert not binding.done(), "the model must not start while the card waits"
            item = listed.json()["attention"][0]
            assert (item["type"], item["need"], item["step"], item["connector_id"], item["server_label"], item["wait"]) == (
                "connect_required", "consent", "tools/list", None, "WebIQ", "interactive")
            assert item["consent"]["call"]["tool_call_id"].startswith("connect-")
            # The links are only in the private readback, whole and in their original order.
            view = readback(hitl, item["item_id"])
            assert view.consent.command.consent.consent_urls == (FIRST, SECOND)
            assert view.status == "pending" and view.consent.command.consent.attempt == 1
            team = hitl.client.get(PREFIX + "/attention", headers=hitl.headers())
            assert [entry["item"]["type"] for entry in team.json()["items"]] == ["connect_required"]
            toolbox.connected = True
            resumed = click(hitl, item)
            assert resumed.status_code == 200 and resumed.json()["result"]["status"] == "resume"
            # A double click: the same click returns the saved original; another one is refused.
            assert click(hitl, item).json() == resumed.json()
            error(click(hitl, item, request_id="owner-click-two"), 409, "NATIVE_HITL_STATE_CONFLICT")
            tools = await binding
        assert tools["WebIQ-web"].description == "Search the web"
        assert toolbox.count("tools/list") == 2 and toolbox.calls == []
        assert await permit(case, "call-after") == (True, None)
        output = await tools["WebIQ-web"].handler(packet("call-after"))
        assert json.loads(output.text)["text"] == "found\ncited"
        assert readback(hitl, item["item_id"]).attention is None and page(hitl, case.gate.turn).json()["attention"] == []
        await case.gate.close()
        assert readback(hitl, item["item_id"]).turn_closed
        for response in (listed, team, resumed):
            assert "consent.example.test" not in response.text
        assert "consent.example.test" not in caplog.text and case.sender.observed == []
    asyncio.run(check())


def test_a_listing_still_not_connected_after_its_one_resume_binds_tools_that_only_say_so(hitl, tmp_path):
    toolbox = Provider("tools/list")
    case = setup(hitl, tmp_path, "never", toolbox=toolbox)

    async def check():
        binding = asyncio.create_task(case.bind())
        item = (await card(case)).json()["attention"][0]
        assert click(hitl, item).status_code == 200
        tools = await binding
        assert toolbox.count("tools/list") == 2 and len(page(hitl, case.gate.turn).json()["references"]) == 1
        assert await permit(case, "call-stub") == (True, None)
        with pytest.raises(ToolFailure) as failed:
            await tools["WebIQ-web"].handler(packet("call-stub"))
        assert failed.value.public_code == "NATIVE_CONNECTOR_NOT_CONNECTED" and toolbox.calls == []
        await case.gate.close()
    asyncio.run(check())


def test_a_call_without_consent_resends_the_same_call_once_after_one_resume(hitl, tmp_path, caplog):
    toolbox = Provider("tools/call")
    case = setup(hitl, tmp_path, "never", toolbox=toolbox)

    async def check():
        web = (await case.bind())["WebIQ-web"]
        assert await permit(case, "call-consent") == (True, None)
        with caplog.at_level(logging.DEBUG):
            running = asyncio.create_task(web.handler(packet("call-consent")))
            item = (await card(case)).json()["attention"][0]
            assert (item["step"], item["consent"]["call"]["tool_call_id"]) == ("tools/call", "call-consent")
            original = readback(hitl, item["item_id"]).consent.command.consent
            assert original.original_call_digest is not None and len(toolbox.calls) == 1
            toolbox.connected = True
            assert click(hitl, item).status_code == 200
            assert click(hitl, item, request_id="owner-click-two").status_code == 409
            output = await running
        assert json.loads(output.text)["text"] == "found\ncited"
        assert toolbox.calls == [{"name": "WebIQ___web", "arguments": ARGUMENTS}] * 2
        assert "consent.example.test" not in caplog.text
        await case.gate.close()
    asyncio.run(check())


@pytest.mark.parametrize("ending", ["decline", "expired", "turn_closed", "action_changed"])
def test_a_card_that_cannot_resume_never_resends(hitl, tmp_path, ending):
    toolbox = Provider("tools/call")
    case = setup(hitl, tmp_path, "never", toolbox=toolbox)

    async def check():
        web = (await case.bind())["WebIQ-web"]
        assert await permit(case, "call-stop") == (True, None)
        running = asyncio.create_task(web.handler(packet("call-stop")))
        item = (await card(case)).json()["attention"][0]
        toolbox.connected = True
        if ending == "decline":
            assert click(hitl, item, "decline").json()["result"]["status"] == "decline"
        elif ending == "expired":
            hitl.now += timedelta(seconds=86401)
            error(click(hitl, item), 410, "NATIVE_HITL_EXPIRED")
        elif ending == "turn_closed":
            assert post(hitl, "/end", NativeHitlEnd(request_id="end-one", turn=case.gate.turn), hitl.agent).status_code == 200
            error(click(hitl, item), 409, "NATIVE_HITL_TURN_CLOSED")
        else:
            error(click(hitl, item, digest="f" * 64), 409, "NATIVE_HITL_ACTION_CHANGED")
            await asyncio.sleep(0.05)
            assert not running.done(), "a click on another card never resumes this one"
            assert click(hitl, item, "decline", request_id="owner-decline").status_code == 200
        with pytest.raises(ToolFailure) as failed:
            await running
        assert failed.value.public_code == "NATIVE_CONNECTOR_NOT_CONNECTED"
        assert len(toolbox.calls) == 1
        if ending == "expired":
            hitl.now -= timedelta(seconds=86401)
        await case.gate.close()
        # The model sees only the fixed text: no link, and no generic "authority refused".
        shown = boundary().public_tool_failure("WebIQ-web", failed.value.public_code, failed.value.public_message)
        assert shown.startswith("Tool call failed [NATIVE_CONNECTOR_NOT_CONNECTED]. This service is not connected")
        assert "consent.example.test" not in shown
    asyncio.run(check())


@pytest.mark.parametrize("origin", ["agent", "relay"])
def test_a_turn_nobody_can_answer_makes_no_card_and_binds_the_stub_with_its_other_tools(hitl, tmp_path, origin):
    toolbox = Provider("tools/list")
    case = setup(hitl, tmp_path, "never", toolbox=toolbox, from_agent=origin == "agent",
                 rid="tr-" + "a" * 40 if origin == "relay" else "rid-one")

    async def check():
        tools = await asyncio.wait_for(case.bind(), 5)
        assert toolbox.count("tools/list") == 1
        assert page(hitl, case.gate.turn).json()["references"] == []
        assert "Not connected for this assistant" in tools["WebIQ-web"].description
        assert "from the Connect card in the workbench, in a conversation with this Agent" in tools[
            "WebIQ-web"].description
        assert await permit(case, "call-stub") == (True, None)
        with pytest.raises(ToolFailure) as failed:
            await tools["WebIQ-web"].handler(packet("call-stub"))
        assert failed.value.public_code == "NATIVE_CONNECTOR_NOT_CONNECTED" and toolbox.calls == []
        await case.gate.close()
    asyncio.run(check())


def test_a_call_in_a_turn_nobody_can_answer_makes_no_card(hitl, tmp_path):
    toolbox = Provider("tools/call")
    case = setup(hitl, tmp_path, "never", toolbox=toolbox, rid="tr-" + "b" * 40)

    async def check():
        web = (await case.bind())["WebIQ-web"]
        assert await permit(case, "call-relay") == (True, None)
        with pytest.raises(ToolFailure) as failed:
            await asyncio.wait_for(web.handler(packet("call-relay")), 5)
        assert failed.value.public_code == "NATIVE_CONNECTOR_NOT_CONNECTED" and len(toolbox.calls) == 1
        assert page(hitl, case.gate.turn).json()["references"] == []
        await case.gate.close()
    asyncio.run(check())


def test_after_a_decline_the_same_conversation_binds_the_stub_and_another_conversation_asks_again(hitl, tmp_path):
    async def turn(rid, conversation):
        toolbox = Provider("tools/list")
        case = setup(hitl, tmp_path, "never", toolbox=toolbox, rid=rid)
        case.context.receipt = case.context.receipt.model_copy(update={"conversation_id": conversation})
        binding = asyncio.create_task(case.bind())
        for _ in range(100):
            if binding.done() or page(hitl, case.gate.turn).json()["references"]:
                break
            await asyncio.sleep(0.005)
        return case, binding

    async def check():
        case, binding = await turn("rid-one", "cid-one")
        item = (await card(case)).json()["attention"][0]
        assert click(hitl, item, "decline").status_code == 200
        assert "Not connected" in (await binding)["WebIQ-web"].description
        await case.gate.close()
        # A later turn, same conversation: the stub at once, no second card.
        case, binding = await turn("rid-two", "cid-one")
        assert "Not connected" in (await asyncio.wait_for(binding, 5))["WebIQ-web"].description
        assert page(hitl, case.gate.turn).json()["references"] == []
        await case.gate.close()
        # Another conversation of the same Agent: the card again.
        case, binding = await turn("rid-three", "cid-two")
        assert not binding.done() and len(page(hitl, case.gate.turn).json()["references"]) == 1
        item = (await card(case)).json()["attention"][0]
        assert click(hitl, item, "decline", request_id="owner-click-three").status_code == 200
        await binding
        await case.gate.close()
    asyncio.run(check())


@pytest.mark.parametrize("stop", ["turn_ended", "turn_stopped"])
def test_a_stop_right_after_the_resume_click_wins_and_the_call_is_not_resent(hitl, tmp_path, stop):
    toolbox = Provider("tools/call")
    case = setup(hitl, tmp_path, "never", toolbox=toolbox)

    async def check():
        web = (await case.bind())["WebIQ-web"]
        assert await permit(case, "call-race") == (True, None)
        running = asyncio.create_task(web.handler(packet("call-race")))
        item = (await card(case)).json()["attention"][0]
        toolbox.connected = True
        # The click and the stop land before the Agent reads the card again.
        assert click(hitl, item).json()["result"]["status"] == "resume"
        if stop == "turn_ended":
            assert post(hitl, "/end", NativeHitlEnd(request_id="end-race", turn=case.gate.turn), hitl.agent).status_code == 200
        else:
            case.context.control.cancelled = True
        with pytest.raises(ToolFailure) as failed:
            await running
        assert failed.value.public_code == "NATIVE_CONNECTOR_NOT_CONNECTED"
        assert toolbox.calls == [{"name": "WebIQ___web", "arguments": ARGUMENTS}]
        await case.gate.close()
    asyncio.run(check())


def test_a_revoked_connection_shows_a_reconnect_card_without_a_link_and_is_not_retried_on_its_own(hitl, tmp_path):
    toolbox = Provider("tools/call", revoked)
    case = setup(hitl, tmp_path, "never", toolbox=toolbox)

    async def check():
        web = (await case.bind())["WebIQ-web"]
        assert await permit(case, "call-revoked") == (True, None)
        running = asyncio.create_task(web.handler(packet("call-revoked")))
        item = (await card(case)).json()["attention"][0]
        assert item["need"] == "reconnect"
        assert readback(hitl, item["item_id"]).consent.command.consent.consent_urls == ()
        await asyncio.sleep(0.05)
        assert len(toolbox.calls) == 1 and not running.done()
        assert click(hitl, item, "decline").status_code == 200
        with pytest.raises(ToolFailure) as failed:
            await running
        assert failed.value.public_code == "NATIVE_CONNECTOR_RECONNECT_REQUIRED" and len(toolbox.calls) == 1
        await case.gate.close()
    asyncio.run(check())


def test_connect_cards_are_their_own_records_and_pass_the_history_reader(hitl, tmp_path):
    case = setup(hitl, tmp_path, "never", toolbox=Provider("tools/list"))

    async def check():
        binding = asyncio.create_task(case.bind())
        item = (await card(case)).json()["attention"][0]
        # An approval reader never takes a Connect card, nor the reverse.
        error(hitl.client.get(PREFIX + "/pending/" + item["item_id"], headers=hitl.headers()), 409,
              "NATIVE_HITL_RECORD_INVALID")
        # The Agent itself cannot click its own card.
        error(post(hitl, "/consent-decisions", ConnectorConsentDecision(
            request_id="agent-click", consent_id=item["item_id"], consent_digest=item["consent"]["consent_digest"],
            choice="resume"), hitl.agent), 403)
        assert click(hitl, item, "decline").status_code == 200
        await binding
        await case.gate.close()
    asyncio.run(check())
    kinds = {type(value).__name__ for value in validate_native_hitl_history(inventory(hitl), hitl.config).values()}
    assert {"ConnectorConsentPending", "_ConsentDecision", "_TurnIndex"} <= kinds


def test_the_wire_keeps_one_step_and_no_link_for_a_revoked_connection():
    call = NativeHitlCall(agent_id="a", conversation_id="c", request_id="r", sdk_session_id="s", tool_call_id="t")
    base = dict(call=call, need="consent", connector_id="teams", toolbox_name="fa-connector-team-one-teams",
                server_label="teams_read", step="tools/list")
    assert ConnectorConsent(**base, consent_urls=(FIRST,)).attempt == 1
    for bad in ({"original_call_digest": "0" * 64}, {"step": "tools/call"}, {"need": "reconnect", "consent_urls": (FIRST,)},
                {"consent_urls": ("http://insecure.test/x",)}, {"consent_urls": (FIRST,) * 5}, {"attempt": 2}):
        with pytest.raises(ValueError):
            ConnectorConsent(**{**base, **bad})
