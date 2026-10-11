"""Thin group panel uses the existing signed owner API, local journal and native attention reads."""

import pytest

from conftest import InMemoryRepository
from test_web_test_console_responses import responses_console
from test_web_test_console_native import (
    native_console, native_api, instruction_models, jwk_client, signing_key, synthetic_standing_v1,
)
from test_native_responses_api import responses_api


@pytest.fixture
def group_console(request, monkeypatch):
    from shared_context import native_group_api
    monkeypatch.setenv("FA_NATIVE_GROUPS_ENABLED", "1")
    repository = InMemoryRepository()
    monkeypatch.setattr(native_group_api, "TableRepository", lambda _client: repository)
    console, env = request.getfixturevalue("responses_console")
    from shared_context.behavior_agent_registry import register_agent_scope
    from test_agent_scope_registry import peer_policy
    c = console.api.c
    register_agent_scope(c.h.runtime.store, c.h.config, peer_policy(c.h.config, c.control.policy),
                         c.human, now=c.h.time)
    native_group_api.install_native_group_routes(
        console.api.client.app, console.api.composition, repository=repository,
        environ={"FA_NATIVE_GROUPS_ENABLED": "1"})
    return console, env


def test_group_open_start_and_read_do_not_replace_the_original_composer_draft(group_console):
    console, _env = group_console
    client, headers = console.ui.client, console.ui.headers
    before = client.get("/api/state").json()["composer"]
    options = client.get("/api/groups/options")
    assert options.status_code == 200, options.text
    value = options.json()
    assert value["options"]["enabled"] and not value["options"]["images"]
    agents = value["options"]["member_agent_ids"]
    assert len(agents) >= 2, value["options"]
    opened = client.post("/api/groups/open", headers=headers, json={
        "request_id": value["request_id"], "orchestrator_agent_id": console.api.remote.target.agent_id,
        "member_agent_ids": agents[:2], "content": "Compare the original public proposals.",
    })
    assert opened.status_code == 200, opened.text
    group_id = opened.json()["group_id"]
    started = client.post("/api/groups/" + group_id + "/start", headers=headers, json={})
    assert started.status_code == 200, started.text
    assert started.json()["turns"][0]["request_id"] == group_id
    assert client.get("/api/state?thread_id=" + before["thread_id"]).json()["composer"]["content"] == before["content"]
    posts = len([call for call in _env.calls if call.url.path.endswith("/responses")])
    again = client.post("/api/groups/" + group_id + "/start", headers=headers, json={})
    assert again.status_code == 200 and len([call for call in _env.calls if call.url.path.endswith("/responses")]) == posts
    read = client.post("/api/groups/" + group_id + "/turns/" + group_id, headers=headers, json={})
    assert read.status_code == 200, read.text
    assert read.json()["composer"]["state_profile"] == "foundry-responses-v1"
    assert read.json()["tasks"][-1]["native_receipt"]["status"] == "accepted"


def test_unknown_group_start_keeps_the_original_intent_and_uses_native_bounded_reconcile(group_console):
    from datetime import timedelta

    console, env = group_console
    client, headers = console.ui.client, console.ui.headers
    options = client.get("/api/groups/options").json()
    opened = client.post("/api/groups/open", headers=headers, json={
        "request_id": options["request_id"], "orchestrator_agent_id": console.api.remote.target.agent_id,
        "member_agent_ids": options["options"]["member_agent_ids"][:2], "content": "Keep my original group start.",
    })
    assert opened.status_code == 200, opened.text
    group_id = opened.json()["group_id"]
    env.state["response_failure"] = 502
    assert client.post("/api/groups/" + group_id + "/start", headers=headers, json={}).status_code >= 400
    original = console.ui.console.journal.get(group_id, "group-start")
    assert original["state"] == "UNKNOWN"
    count = len([call for call in env.calls if call.url.path.endswith("/responses")])
    observed = client.post("/api/groups/" + group_id + "/start", headers=headers, json={})
    assert observed.status_code == 200, observed.text
    assert len([call for call in env.calls if call.url.path.endswith("/responses")]) == count
    assert console.ui.console.journal.get(group_id, "group-start") == original
    env.state["response_failure"] = None
    console.c.h.time += timedelta(seconds=3)
    reconciled = client.post("/api/tasks/" + group_id + "/submit-reconcile", headers=headers, json={})
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["task"]["native_receipt"]["dispatch"]["state"] == "already_queued"
    assert console.ui.console.journal.get(group_id, "group-start") == original


def test_group_panel_default_off_makes_no_backend_request(native_console):
    console = native_console
    before = len(console.calls)
    response = console.ui.client.get("/api/groups/options")
    assert response.status_code == 200 and response.json()["options"]["enabled"] is False
    assert len(console.calls) == before


def test_single_agent_console_never_starts_a_different_group_leader(group_console):
    console, _env = group_console
    client, headers = console.ui.client, console.ui.headers
    options = client.get("/api/groups/options").json()
    assert options["options"]["default_agent_id"] == console.api.remote.target.agent_id
    other = next(agent for agent in options["options"]["member_agent_ids"]
                 if agent != options["options"]["default_agent_id"])
    group = client.post("/api/groups/open", headers=headers, json={
        "request_id": options["request_id"], "orchestrator_agent_id": other,
        "member_agent_ids": options["options"]["member_agent_ids"][:2], "content": "Different original leader.",
    }).json()
    before = len(console.calls)
    rejected = client.post("/api/groups/" + group["group_id"] + "/start", headers=headers, json={})
    assert rejected.status_code == 403 and rejected.json()["error"]["code"] == "NATIVE_GROUP_SCOPE_DENIED"
    assert all(method == "GET" for method, _path, _body in console.calls[before:])
    assert console.ui.console.journal.get(group["group_id"], "group-start") is None


@pytest.mark.parametrize("refused", [False, True])
def test_lost_open_before_api_arrival_can_reconcile_then_create_only_the_same_group(group_console, monkeypatch, refused):
    import httpx

    console, _env = group_console
    client, headers = console.ui.client, console.ui.headers
    options = client.get("/api/groups/options").json()
    body = {"request_id": options["request_id"], "orchestrator_agent_id": options["options"]["default_agent_id"],
            "member_agent_ids": options["options"]["member_agent_ids"][:2], "content": "Keep this exact opening."}
    old = console.ui.console.backend._transport
    attempts = []

    def missing(request):
        if request.method == "POST" and request.url.path == "/native-groups/v1/groups":
            attempts.append(bytes(request.content))
            if len(attempts) == 1:
                raise httpx.ReadTimeout("OFFLINE opening never reached the API", request=request)
            if len(attempts) == 2 and refused:
                return httpx.Response(403, json={"error": {"code": "NATIVE_GROUP_MEMBER_REQUIRED"}})
        return old.handle_request(request)

    monkeypatch.setattr(console.ui.console.backend, "_transport", httpx.MockTransport(missing))
    first = client.post("/api/groups/open", headers=headers, json=body)
    assert first.status_code >= 400
    original = console.ui.console.journal.get(body["request_id"], "group-open")
    assert original["state"] == "UNKNOWN"
    assert client.get("/api/groups/options").json()["pending_opening"] == body
    restored = client.post("/api/groups/open", headers=headers, json=body)
    assert restored.status_code == (403 if refused else 200), restored.text
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert console.ui.console.journal.get(body["request_id"], "group-open") == original
    renewed = client.get("/api/groups/options").json()
    assert renewed["pending_opening"] is None
    if refused:
        assert console.ui.console.journal.get(body["request_id"], "group-open-read")["state"] == "REFUSED"
        assert renewed["request_id"] != body["request_id"]
        restored = client.post("/api/groups/open", headers=headers, json={**body, "request_id": renewed["request_id"]})
        assert restored.status_code == 200, restored.text
    assert client.post("/api/groups/list", headers=headers, json={}).json()["groups"] == [restored.json()]
