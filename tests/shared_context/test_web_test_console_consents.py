"""C3 in the console: a Connect card joins the attention list without its links; one saved click per card."""

import json

import pytest

from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_team_registry import resolve_agent_scope
from shared_context.native_hitl_wire import (
    ConnectorConsent, ConnectorConsentCreate, NativeHitlCall, NativeHitlEnd, NativeHitlTurn,
)
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_turn_api import native_api  # noqa: F401
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_web_test_console_native import native_console, first, send, progress  # noqa: F401
from test_web_test_console_responses import responses_console, responses_api  # noqa: F401
from test_web_test_console_hitl import hitl_console, route, ui_post  # noqa: F401

PHASE = "native-consent"
LINK = "https://consent.example.test/login?state=" + "C" * 2300


def consent_call(c, *, need="consent"):
    draft = first(c)
    if draft["request_id"] not in c.api.remote.records:
        assert send(c, draft).status_code == 200
    original = c.api.remote.records[draft["request_id"]]
    c.api.remote.records[draft["request_id"]] = original.model_copy(update={
        "status": "started", "turn_number": 1, "dispatch": None,
        "response_id": "resp-platform" if original.state_profile == "foundry-responses-v1" else None})
    scope = resolve_agent_scope(c.api.composition.runtime.store, c.api.composition.config, original.agent_id)
    agent = BehaviorPrincipal(str(scope.tenant_id), str(scope.identity.oid), scope.team_id, "agent",
                              scope.identity.actor, frozenset({"read", "execute", "consume", "submit"}))
    c.hitl.agent = agent
    consent = ConnectorConsent(
        call=NativeHitlCall(agent_id=original.agent_id, conversation_id=original.conversation_id,
                            request_id=original.request_id, sdk_session_id="offline-sdk", tool_call_id="connect-local-1"),
        need=need, connector_id="teams", toolbox_name="fa-connector-team-one-teams", server_label="teams_read",
        step="tools/list", consent_urls=(LINK,) if need == "consent" else ())
    receipt = c.hitl.service.submit(agent, ConnectorConsentCreate(
        request_id="consent-one", consent=consent, consent_digest=consent.sha256))
    return draft, receipt.payload["result"]


def attention_of(c, draft):
    state = progress(c, draft["thread_id"])
    assert state.status_code == 200, state.text
    assert "consent.example.test" not in state.text
    return state.json()["composer"]["attention"]


def decisions(c):
    return [call for call in c.calls if call[:2] == ("POST", "/native-hitl/v1/consent-decisions")]


@pytest.mark.parametrize("hitl_console", ["foundry-native-v1", "foundry-responses-v1"], indirect=True)
def test_a_connect_card_shows_its_link_only_in_its_own_read_and_one_click_is_sent_once(hitl_console):
    c = hitl_console
    draft, created = consent_call(c)
    attention = attention_of(c, draft)
    assert [entry["item"]["type"] for entry in attention] == ["connect_required"]
    item = attention[0]["item"]
    assert (item["item_id"], item["need"], item["connector_id"]) == (created["consent_id"], "consent", "teams")
    card = c.ui.client.get(route(c, "consents/" + item["item_id"]), headers=c.ui.headers)
    assert card.status_code == 200, card.text
    assert card.json()["readback"]["consent"]["command"]["consent"]["consent_urls"] == [LINK]
    assert card.json()["connector"]["display_name"] == "Microsoft Teams"
    assert card.json()["connector"]["disclosure"].startswith("Microsoft Teams 經 Work IQ 連線")
    saved = ui_post(c, route(c, "consents"), {"consent": item["consent"], "choice": "resume"})
    assert saved.status_code == 200, saved.text
    assert saved.json()["commands"][0]["command"]["choice"] == "resume"
    assert c.ui.console.journal.get(saved.json()["request_id"], PHASE)["state"] == "CONFIRMED"
    # A double click, even the other button, or a reload only shows the saved original.
    again = ui_post(c, route(c, "consents"), {"consent": item["consent"], "choice": "decline"})
    assert again.status_code == 200 and again.json()["commands"][0]["command"]["choice"] == "resume"
    c.ui.client.__exit__(None, None, None)
    c.ui = c.open()
    assert ui_post(c, route(c, "consents"), {"consent": item["consent"], "choice": "resume"}).status_code == 200
    assert len(decisions(c)) == 1
    assert attention_of(c, draft) == []
    composer = progress(c, draft["thread_id"]).json()["composer"]
    assert composer["native_consents"]["commands"][0]["display"] == {
        "connector_id": "teams", "need": "consent", "name": "Microsoft Teams"}
    assert all(b"consent.example.test" not in call[2] for call in decisions(c))


@pytest.mark.parametrize("boundary", ["closed", "digest", "choice", "no_login"])
def test_a_click_that_cannot_count_never_posts(hitl_console, boundary):
    c = hitl_console
    draft, _ = consent_call(c, need="reconnect")
    item = attention_of(c, draft)[0]["item"]
    value = {"consent": item["consent"], "choice": "resume"}
    if boundary == "closed":
        call = item["consent"]["call"]
        c.hitl.service.submit(c.hitl.agent, NativeHitlEnd(request_id="close-original", turn=NativeHitlTurn(**{
            key: call[key] for key in ("agent_id", "conversation_id", "request_id")})))
    elif boundary == "digest":
        value["consent"] = {**value["consent"], "consent_digest": "f" * 64}
    elif boundary == "choice":
        value["choice"] = "allow_once"
    else:
        c.ui.console.vault.clear()
    before = len(c.calls)
    refused = ui_post(c, route(c, "consents"), value)
    assert refused.status_code in {400, 401, 403, 409, 410, 502}, refused.text
    assert not any(method == "POST" for method, _, _ in c.calls[before:])
    assert c.ui.console.journal.entries(PHASE) == []
    assert json.loads(refused.text)["error"]["automatic_retry"] is False
