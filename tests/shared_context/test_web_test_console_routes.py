"""The bridge's assembled surface: route order, the browser boundary, and each route's input refusals."""
import json

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount, WebSocketRoute

from test_native_turn_api import instruction_models, jwk_client, native_api, signing_key, synthetic_standing_v1
from test_web_test_console import console
from test_web_test_console_native import first, native_console, post, send

ROUTES = [
    ("GET", "/novnc/{path:path}"),
    ("WEBSOCKET", "/api/agents/{agent_id}/desktop"),
    ("GET", "/api/native-hitl/team/attention"),
    ("GET", "/api/native-hitl/team/pending/{pending_id}"),
    ("POST", "/api/native-hitl/team/decisions"),
    ("POST", "/api/native-hitl/team/reconcile"),
    ("GET", "/api/native-hitl/{thread_id}/rules"),
    ("POST", "/api/native-hitl/{thread_id}/rules"),
    ("POST", "/api/native-hitl/{thread_id}/reconcile"),
    ("GET", "/api/native-hitl/{thread_id}/pending/{pending_id}"),
    ("POST", "/api/native-hitl/{thread_id}/decisions"),
    ("GET", "/api/groups/options"),
    ("POST", "/api/groups/list"),
    ("POST", "/api/groups/open"),
    ("GET", "/api/groups/{group_id}"),
    ("POST", "/api/groups/{group_id}/start"),
    ("POST", "/api/groups/{group_id}/turns/{request_id}"),
    ("POST", "/api/native-hitl/team/answers"),
    ("POST", "/api/native-hitl/team/answer-reconcile"),
    ("POST", "/api/native-hitl/{thread_id}/answers"),
    ("POST", "/api/native-hitl/{thread_id}/answer-reconcile"),
    ("GET", "/api/native-hitl/team/consents/{consent_id}"),
    ("POST", "/api/native-hitl/team/consents"),
    ("POST", "/api/native-hitl/team/consent-reconcile"),
    ("GET", "/api/native-hitl/{thread_id}/consents/{consent_id}"),
    ("POST", "/api/native-hitl/{thread_id}/consents"),
    ("POST", "/api/native-hitl/{thread_id}/consent-reconcile"),
    ("GET", "/api/team-skills/reviews"),
    ("GET", "/api/team-skills/skills/{artifact}"),
    ("GET", "/api/team-skills/skills/{artifact}/versions/{version_id}"),
    ("GET", "/api/team-skills/skills/{artifact}/versions/{version_id}/resources/{name}"),
    ("POST", "/api/team-skills/skills/{artifact}/decisions"),
    ("POST", "/api/team-skills/skills/{artifact}/deactivations"),
    ("POST", "/api/team-skills/drafts"),
    ("GET", "/api/team-mcp/servers"),
    ("GET", "/api/team-mcp/connections"),
    ("POST", "/api/team-mcp/changes"),
    ("GET", "/api/team-connectors"),
    ("POST", "/api/team-connectors/changes"),
    ("GET", "/api/agent-connectors"),
    ("POST", "/api/agent-connectors"),
    ("GET", "/api/routines"),
    ("POST", "/api/routines/{routine_id}/versions/{version_id}/disable"),
    ("GET", "/"),
    ("GET", "/api/state"),
    ("GET", "/api/realtime/negotiate"),
    ("GET", "/api/agents"),
    ("GET", "/api/agents/onboarding"),
    ("POST", "/api/agents/onboarding"),
    ("GET", "/api/agents/onboarding/options"),
    ("GET", "/api/agents/onboarding/{request_id}"),
    ("POST", "/api/agents/onboarding/{request_id}/continue"),
    ("POST", "/api/agents/onboarding/{request_id}/prepare"),
    ("GET", "/api/agent-template"),
    ("POST", "/api/agent-template/inspect"),
    ("GET", "/api/agent-template/options"),
    ("GET", "/api/agent-template/system-prompt"),
    ("GET", "/api/agent-template/selection"),
    ("POST", "/api/agent-template/select"),
    ("POST", "/api/agent-template/selection-reconcile"),
    ("GET", "/favicon.ico"),
    ("POST", "/api/authenticate"),
    ("POST", "/api/authenticate/refresh"),
    ("POST", "/api/instructions/{thread_id}/next"),
    ("POST", "/api/instructions/conversations/new"),
    ("POST", "/api/instructions/prewarm"),
    ("POST", "/api/instructions/{thread_id}/reset"),
    ("GET", "/api/instructions/progress"),
    ("GET", "/api/instructions/models"),
    ("GET", "/api/instructions/received"),
    ("GET", "/api/instructions/{task_id}/reply"),
    ("GET", "/api/instructions/{task_id}/reply.txt"),
    ("GET", "/api/instructions/{task_id}/context"),
    ("GET", "/api/instructions/{task_id}/system-prompt"),
    ("GET", "/api/instructions/{task_id}/skill-draft"),
    ("GET", "/api/instructions/{task_id}/skill-draft.md"),
    ("GET", "/api/instructions/{task_id}/images"),
    ("GET", "/api/instructions/{thread_id}/platform-history"),
    ("GET", "/api/instructions/{task_id}/images/{version_id}"),
    ("GET", "/api/instructions/{task_id}/delegation"),
    ("GET", "/api/instructions/{task_id}/agent-messages"),
    ("POST", "/api/instructions/{task_id}/confirm-reply"),
    ("POST", "/api/instructions/{task_id}/cancel"),
    ("GET", "/api/instructions/{task_id}/cancel-receipt"),
    ("POST", "/api/instructions/{request_id}/submit"),
    ("POST", "/api/instructions/{request_id}/image-upload"),
    ("POST", "/api/instructions/{request_id}/append"),
    ("GET", "/api/instructions/{request_id}/appends"),
    ("GET", "/api/instructions/{request_id}/status"),
    ("GET", "/api/instructions/{request_id}/stream"),
    ("GET", "/api/instructions/{request_id}/progress"),
    ("GET", "/api/instructions/{request_id}/user-text-file-capability"),
    ("GET", "/api/instructions/{request_id}/files/{filename}"),
    ("GET", "/api/instructions/{request_id}/attachment-sources"),
    ("POST", "/api/tasks/{task_id}/{action}"),
    ("GET", "/api/tasks/{task_id}/payload"),
    ("GET", "/api/tasks/{task_id}/summary"),
]
NO_INPUT_READS = [
    "/api/realtime/negotiate", "/api/agents", "/api/agents/onboarding", "/api/agents/onboarding/options",
    "/api/agents/onboarding/r1", "/api/agent-template", "/api/agent-template/options",
    "/api/agent-template/system-prompt", "/api/agent-template/selection",
    "/api/instructions/r1/reply.txt", "/api/instructions/r1/context", "/api/instructions/r1/system-prompt",
    "/api/instructions/r1/skill-draft", "/api/instructions/r1/skill-draft.md", "/api/instructions/r1/images",
    "/api/instructions/r1/images/v1", "/api/instructions/r1/delegation", "/api/instructions/r1/agent-messages",
    "/api/instructions/r1/cancel-receipt", "/api/instructions/r1/appends", "/api/instructions/r1/status",
    "/api/instructions/r1/progress", "/api/instructions/r1/user-text-file-capability",
    "/api/instructions/r1/files/a.txt", "/api/instructions/r1/attachment-sources",
]
LARGE_BODY = {"padding": "x" * 64}


def code(response):
    return response.status_code, response.json()["error"]["code"]


def surface(app):
    rows = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            methods = sorted(route.methods)
            assert len(methods) == 1, route
            rows.append((methods[0], route.path))
        elif isinstance(route, WebSocketRoute):
            rows.append(("WEBSOCKET", route.path))
        else:
            assert isinstance(route, Mount), route
            rows.append(("MOUNT", route.path))
    return rows


def test_the_assembled_routes_keep_their_exact_order(native_console):
    assert surface(native_console.ui.app) == ROUTES


def test_the_browser_boundary_is_the_only_middleware(native_console):
    assert [entry.cls.__name__ for entry in native_console.ui.app.user_middleware] == ["BaseHTTPMiddleware"]


def test_every_response_carries_the_security_headers(native_console):
    client = native_console.ui.client
    for response in (client.get("/api/state"), client.get("/api/state", headers={"Origin": "http://evil.example"})):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["content-security-policy"] == (
            "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self' ws://127.0.0.1:8877; img-src 'self' blob: data:; frame-ancestors 'none'; "
            "form-action 'none'; base-uri 'none'")


@pytest.mark.parametrize("path", ["/", "/api/state"])
@pytest.mark.parametrize("headers", [
    {"Host": "127.0.0.1:9999"}, {"Sec-Fetch-Site": "cross-site"}, {"Origin": "http://evil.example"},
], ids=["host", "fetch-site", "origin"])
def test_a_foreign_browser_request_is_refused_before_any_route(native_console, path, headers):
    response = native_console.ui.client.get(path, headers=headers)
    assert code(response) == (403, "LOCAL_REQUEST_DENIED")
    assert response.json()["error"]["automatic_retry"] is False


def test_only_the_page_itself_is_readable_without_the_page_cookie(console):
    client = TestClient(console.bridge_app, base_url="http://127.0.0.1:8765")
    assert code(client.get("/api/state")) == (403, "LOCAL_REQUEST_DENIED")
    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "HttpOnly" in page.headers["set-cookie"] and "SameSite=strict" in page.headers["set-cookie"]
    assert "Secure" not in page.headers["set-cookie"] and page.headers["set-cookie"].startswith("fa_console=")
    assert client.get("/api/state").status_code == 200


@pytest.mark.parametrize("damage", ["no-origin", "no-csrf", "wrong-csrf", "not-json", "query", "put", "delete"])
def test_a_write_needs_origin_csrf_json_and_no_query(native_console, damage):
    ui = native_console.ui
    headers = {**ui.headers, "Content-Type": "application/json"}
    method, path = "POST", "/api/authenticate"
    if damage == "no-origin":
        headers.pop("Origin")
    elif damage == "no-csrf":
        headers.pop("X-Console-CSRF")
    elif damage == "wrong-csrf":
        headers["X-Console-CSRF"] = "x" * 43
    elif damage == "not-json":
        headers["Content-Type"] = "text/plain"
    elif damage == "query":
        path += "?x=1"
    else:
        method = damage.upper()
    assert code(ui.client.request(method, path, content=b"{}", headers=headers)) == (403, "LOCAL_REQUEST_DENIED")


@pytest.mark.parametrize("content,status", [
    (b"", 200), (b"{}", 200), (b" {} ", 200), (b"[]", 403), (b'{"a":1}', 403), (b" " * 14 + b"{}", 200), (b" " * 15 + b"{}", 403),
])
def test_a_route_without_input_takes_only_an_empty_body(native_console, content, status):
    ui = native_console.ui
    response = ui.client.post("/api/authenticate", content=content,
                              headers={**ui.headers, "Content-Type": "application/json"})
    assert response.status_code == status


# A path listed here reaches the app with a large body; the expected status is what the route, not the gate, answers.
INPUT_PATHS = [
    "/api/instructions/r1/submit", "/api/instructions/deep/er/submit", "/api/instructions/r1/append",
    "/api/instructions/r1/image-upload", "/api/native-hitl/r1/rules", "/api/native-hitl/r1/decisions",
    "/api/native-hitl/r1/reconcile", "/api/native-hitl/r1/answers", "/api/native-hitl/r1/answer-reconcile",
    "/api/native-hitl/r1/consents", "/api/native-hitl/r1/consent-reconcile", "/api/groups/open", "/api/groups/list",
    "/api/agent-template/inspect", "/api/agent-template/select", "/api/agent-template/selection-reconcile",
    "/api/team-skills/drafts", "/api/team-skills/skills/s1/decisions", "/api/team-skills/skills/s1/deactivations",
    "/api/team-mcp/changes", "/api/team-connectors/changes", "/api/agent-connectors",
]
GATED_PATHS = [
    "/api/instructions/deep/er/append", "/api/instructions/deep/er/image-upload", "/api/instructions/r1/next",
    "/api/native-hitl/r1/other", "/api/native-hitl/deep/er/rules", "/api/groups/other",
    "/api/agent-template/other", "/api/team-skills/skills/bad name/decisions", "/api/team-mcp/other",
    "/api/team-connectors/other", "/api/agents/onboarding",
]


def gate_refusal(response):
    """True only for the generic body gate's refusal, which a route's own refusals cannot be told from by status."""
    return response.status_code == 403 and response.json()["error"]["code"] == "LOCAL_REQUEST_DENIED"


@pytest.mark.parametrize("path", INPUT_PATHS)
def test_input_paths_pass_the_small_body_gate(native_console, path):
    ui = native_console.ui
    response = ui.client.post(path, json=LARGE_BODY, headers=ui.headers)
    # submit, append and image-upload refuse the wrong keys themselves with the same code; a well-formed shape shows
    # the gate let them through.
    if path.endswith(("/submit", "/append", "/image-upload")):
        shape = {"content": "x" * 64, "model": "m"} if path.endswith("/submit") else (
            {"append_id": "a", "content": "x" * 64} if path.endswith("/append") else
            {"name": "x" * 64, "png_base64": "AAAA"})
        response = ui.client.post(path, json=shape, headers=ui.headers)
    assert not gate_refusal(response), (path, response.text)


@pytest.mark.parametrize("path", GATED_PATHS)
def test_other_paths_keep_the_small_body_gate(native_console, path):
    ui = native_console.ui
    assert gate_refusal(ui.client.post(path, json=LARGE_BODY, headers=ui.headers))


@pytest.mark.parametrize("path", INPUT_PATHS)
def test_without_instructions_or_standing_config_every_path_keeps_the_gate(console, path):
    response = console.browser.post(path, json=LARGE_BODY, headers=console.bridge_headers)
    assert gate_refusal(response)


@pytest.mark.parametrize("path", NO_INPUT_READS)
def test_a_read_refuses_any_query_or_body(native_console, path):
    client = native_console.ui.client
    assert code(client.get(path + "?x=1")) == (403, "LOCAL_REQUEST_DENIED")
    assert code(client.request("GET", path, content=b"x")) == (403, "LOCAL_REQUEST_DENIED")


@pytest.mark.parametrize("path,expected", [
    ("/api/agents", "AGENT_DIRECTORY_UNAVAILABLE"), ("/api/agents/onboarding", "AGENT_ONBOARDING_UNAVAILABLE"),
    ("/api/agents/onboarding/options", "AGENT_ONBOARDING_UNAVAILABLE"),
    ("/api/agents/onboarding/r1", "AGENT_ONBOARDING_UNAVAILABLE"),
])
def test_agent_reads_without_a_directory_say_so_after_the_input_check(native_console, path, expected):
    assert native_console.ui.client.get(path).json()["error"]["code"] == expected


@pytest.mark.parametrize("path", [
    "/api/agents/onboarding", "/api/agents/onboarding/r1/continue", "/api/agents/onboarding/r1/prepare",
])
def test_agent_onboarding_writes_without_a_directory_are_unavailable(native_console, path):
    ui = native_console.ui
    assert ui.client.post(path, json={}, headers=ui.headers).json()["error"]["code"] == "AGENT_ONBOARDING_UNAVAILABLE"


def test_an_agent_header_is_refused_without_a_directory_and_when_repeated(native_console):
    client = native_console.ui.client
    assert client.get("/api/state", headers={"X-Console-Agent": "a"}).json()["error"]["code"] == "AGENT_NOT_AVAILABLE"
    repeated = client.get("/api/state", headers=[("X-Console-Agent", "a"), ("X-Console-Agent", "b")])
    assert code(repeated) == (403, "LOCAL_REQUEST_DENIED")


OVERSIZED = b" " * 32769


@pytest.mark.parametrize("suffix,body,expected", [
    ("submit", None, (400, "INSTRUCTION_INVALID")),
    ("submit", b'{"content":"x"}', (403, "LOCAL_REQUEST_DENIED")),
    ("submit", b'{"content":"x","model":"m","extra":1}', (403, "LOCAL_REQUEST_DENIED")),
    ("submit", b"[]", (403, "LOCAL_REQUEST_DENIED")),
    ("submit", OVERSIZED, (403, "LOCAL_REQUEST_DENIED")),
    ("append", None, (400, "INSTRUCTION_INVALID")),
    ("append", b'{"content":"x"}', (403, "LOCAL_REQUEST_DENIED")),
    ("append", b'{"append_id":"a","content":"x","extra":1}', (403, "LOCAL_REQUEST_DENIED")),
    ("append", OVERSIZED, (403, "LOCAL_REQUEST_DENIED")),
    ("image-upload", None, (400, "USER_IMAGE_INPUT_INVALID")),
    ("image-upload", b'{"name":"a.png"}', (403, "LOCAL_REQUEST_DENIED")),
    ("image-upload", b'{"name":1,"png_base64":"AAAA"}', (403, "LOCAL_REQUEST_DENIED")),
], ids=lambda value: "oversized" if value is OVERSIZED else None)
def test_instruction_inputs_refuse_bad_bodies(native_console, suffix, body, expected):
    ui = native_console.ui
    response = ui.client.post("/api/instructions/r1/" + suffix, content=b"{bad" if body is None else body,
                              headers={**ui.headers, "Content-Type": "application/json"})
    assert code(response) == expected


def test_an_image_upload_is_bounded_by_the_one_image_limit(native_console):
    ui = native_console.ui
    limit = (2 * 1024 * 1024 + 2) // 3 * 4 + 4096  # 2 MiB image in base64 plus its name
    response = ui.client.post("/api/instructions/r1/image-upload", content=b" " * (limit + 1),
                              headers={**ui.headers, "Content-Type": "application/json"})
    assert code(response) == (403, "LOCAL_REQUEST_DENIED")


@pytest.mark.parametrize("path,size,expected", [
    ("/api/agent-template/select", 1024 * 1024, (413, "TEMPLATE_SELECTION_INVALID")),
    ("/api/agent-template/selection-reconcile", 257, (413, "TEMPLATE_SELECTION_INVALID")),
    ("/api/agent-template/inspect", 4 * 1024 * 1024, (413, "TEMPLATE_INVALID")),
])
def test_template_writes_are_bounded(native_console, path, size, expected):
    ui = native_console.ui
    response = ui.client.post(path, content=b" " * size, headers={**ui.headers, "Content-Type": "application/json"})
    assert code(response) == expected


@pytest.mark.parametrize("content", [b"{bad", b'{"request_id":"r1","extra":1}'])
def test_a_template_reconcile_needs_one_valid_command(native_console, content):
    ui = native_console.ui
    response = ui.client.post("/api/agent-template/selection-reconcile", content=content,
                              headers={**ui.headers, "Content-Type": "application/json"})
    assert code(response) == (400, "TEMPLATE_SELECTION_INVALID")


@pytest.mark.parametrize("query", [
    "", "?thread_id=t", "?request_id=a&request_id=b", "?request_id=a&x=1", "?request_id=a&thread_id=t&thread_id=u",
])
def test_received_takes_one_request_id_and_at_most_one_thread(native_console, query):
    assert code(native_console.ui.client.get("/api/instructions/received" + query)) == (403, "LOCAL_REQUEST_DENIED")


@pytest.mark.parametrize("query", ["?x=1", "?after=a&after=b", "?previous=0", "?previous=1&previous=1"])
def test_platform_history_takes_only_its_two_single_filters(native_console, query):
    client = native_console.ui.client
    assert code(client.get("/api/instructions/t1/platform-history" + query)) == (403, "LOCAL_REQUEST_DENIED")


@pytest.mark.parametrize("query", ["?x=1", "?starting_after=a", "?starting_after=1&starting_after=2",
                                   "?starting_after=-1", "?starting_after=1234567890123", "?starting_after=%C2%B2"])
def test_live_text_takes_only_one_short_ascii_position(native_console, query):
    client = native_console.ui.client
    assert code(client.get("/api/instructions/r1/stream" + query)) == (403, "LOCAL_REQUEST_DENIED")


def test_an_unknown_console_failure_answers_without_detail(console, monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("secret detail")
    monkeypatch.setattr(console.bridge_app.state.console, "state", explode)
    client = TestClient(console.bridge_app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False,
                        cookies=dict(console.browser.cookies))
    response = client.get("/api/state")
    assert response.status_code == 500
    error = response.json()["error"]
    assert (error["code"], error["automatic_retry"], set(error)) == (
        "LOCAL_FAILURE", False, {"code", "message", "automatic_retry"})
    assert "secret detail" not in response.text


def test_a_refused_read_may_retry_only_when_the_error_says_so(native_console):
    ui = native_console.ui
    refused = ui.client.post("/api/tasks/unknown/status", json={}, headers=ui.headers)
    assert refused.status_code >= 400
    body = refused.json()["error"]
    assert set(body) == {"code", "message", "upstream_status", "automatic_retry"} and body["automatic_retry"] is False


def test_the_favicon_answers_empty_and_a_task_action_keeps_its_catch_all(native_console):
    ui = native_console.ui
    assert ui.client.get("/favicon.ico").status_code == 204
    # GET /api/tasks/x/payload is not swallowed by the POST /api/tasks/{task_id}/{action} route before it.
    assert ui.client.get("/api/tasks/x/payload").status_code != 405
    assert ui.client.get("/api/tasks/x/summary").status_code != 405


def test_a_submitted_turn_still_runs_through_the_assembled_routes(native_console):
    c = native_console
    draft = first(c)
    accepted = send(c, draft)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["task"]["mode"] == "native_turn"
    assert json.loads(post(c, "/api/instructions/conversations/new").text)["composer"]["thread_id"] != draft["thread_id"]
