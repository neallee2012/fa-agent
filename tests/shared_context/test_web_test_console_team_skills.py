"""Console proxy for owner Team skill review, wired to the real /team-skills/v1 routes (offline)."""

import hashlib
from types import SimpleNamespace

import httpx
import pytest

from web_test_console.adapter import ConsoleError, PullAdapter
from web_test_console.team_skills import TeamSkills, install_team_skill_routes, is_team_skill_write
from test_m3_auth import jwk_client, signing_key  # noqa: F401  (fixtures used by team_skills)
from test_team_skills_api import SKILL, SKILL_TEXT, RESOURCE_TEXT, owner_draft, team_skills, upload_skill  # noqa: F401
from test_web_test_console_frozen_interface import real_interface  # noqa: F401


def console_for(api, identity="owner"):
    """A minimal console whose backend is the real review API app; scopes come from the signed token."""
    token = api.headers(identity)["Authorization"].removeprefix("Bearer ")
    scopes = {"owner": {"Behavior.Read", "Behavior.Approve"}, "read_only": {"Behavior.Read"}}[identity]
    sent = []

    def forward(request):
        sent.append((request.method, request.url.path, request.headers.get("if-match")))
        response = api.client.request(request.method, request.url.path, params=request.url.params,
                                      headers=dict(request.headers), content=request.content)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    console = SimpleNamespace(
        config=SimpleNamespace(backend_origin="http://review-api.test"),
        backend=httpx.Client(transport=httpx.MockTransport(forward)),
        vault=SimpleNamespace(get=lambda: token, allows=lambda scope: scope in scopes),
        active=lambda: None)
    return TeamSkills(console), sent


def test_console_reviews_and_decides_once_through_the_real_review_api(team_skills):
    api = team_skills
    upload_skill(api.service)
    skills, sent = console_for(api)
    page = skills.reviews()
    assert [(entry["artifact_id"], entry["status"]) for entry in page["entries"]] == [(SKILL, "awaiting_decision")]
    view = skills.artifact(SKILL)
    version = view["artifact"]["versions"][-1]
    read = skills.version(SKILL, version["version_id"])
    assert read["text"] == SKILL_TEXT and read["version"]["sha256"] == hashlib.sha256(SKILL_TEXT.encode()).hexdigest()
    assert skills.resource(SKILL, version["version_id"], "resource.md")["text"] == RESOURCE_TEXT

    body = {"decision_id": "d" * 32, "version_id": version["version_id"], "sha256": version["sha256"],
            "metadata_sha256": version["metadata_sha256"], "decision": "approve"}
    approved = skills.decide(SKILL, {**body, "etag": view["etag"]})
    assert approved["artifact"]["effective_version_id"] == version["version_id"]
    assert approved["original_request_resent"] is False
    assert [item for item in sent if item[0] == "POST"] == [("POST", f"/team-skills/v1/skills/{SKILL}/decisions", view["etag"])]
    revision = approved["artifact"]["revision"]
    again = skills.decide(SKILL, {**body, "etag": view["etag"]})
    assert again["artifact"]["revision"] == revision, "the same decision ID returns the original record"

    with pytest.raises(ConsoleError) as refused:
        skills.decide(SKILL, {**body, "decision_id": "c" * 32, "etag": approved["etag"]})
    assert refused.value.code == "M3_ALREADY_DECIDED"

    deactivated = skills.deactivate(SKILL, {
        "schema_version": "fa-skill-deactivation-v1", "decision_id": "e" * 32, "version_id": version["version_id"],
        "sha256": version["sha256"], "metadata_sha256": version["metadata_sha256"], "etag": approved["etag"]})
    assert deactivated["artifact"]["effective_version_id"] is None
    assert skills.reviews()["entries"][0]["status"] == "deactivated"


def test_read_only_owner_cannot_decide_and_nothing_is_sent(team_skills):
    upload_skill(team_skills.service)
    skills, sent = console_for(team_skills, "read_only")
    view = skills.artifact(SKILL)
    version = view["artifact"]["versions"][-1]
    with pytest.raises(ConsoleError) as denied:
        skills.decide(SKILL, {"decision_id": "d" * 32, "version_id": version["version_id"], "sha256": version["sha256"],
                              "metadata_sha256": version["metadata_sha256"], "decision": "approve", "etag": view["etag"]})
    assert denied.value.code == "TEAM_SKILLS_OWNER_ONLY"
    assert all(method == "GET" for method, _, _ in sent)
    with pytest.raises(ConsoleError) as invalid:
        skills.decide(SKILL, {"decision_id": "d" * 32, "decision": "approve", "etag": view["etag"]})
    assert invalid.value.code == "TEAM_SKILL_INPUT_INVALID"


def test_if_match_and_review_cursor_are_limited_to_team_skill_routes():
    adapter = PullAdapter(SimpleNamespace(backend_origin="http://review-api.test"),
                          httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))))
    for call in (
        lambda: adapter.send("POST", "/work-policy/v1/agent-template/select", {}, "token", if_match='"etag"'),
        lambda: adapter.send("GET", "/team-skills/v1/skills/x", None, "token", if_match='"etag"'),
        lambda: adapter.send("GET", "/team-skills/v1/skills/x", None, "token", params={"after": "a"}),
        lambda: adapter.send("GET", "/team-skills/v1/reviews", None, "token", params={"after": "../x"}),
    ):
        with pytest.raises(ConsoleError) as denied:
            call()
        assert denied.value.code == "ORIGIN_DENIED"
    assert is_team_skill_write("/api/team-skills/skills/receipt-guide/decisions")
    assert not is_team_skill_write("/api/team-skills/skills/../decisions")
    assert not is_team_skill_write("/api/team-skills/reviews")
    assert is_team_skill_write("/api/team-skills/drafts")
    assert not is_team_skill_write("/api/team-skills/drafts/x")


def test_bridge_admits_only_bounded_csrf_checked_team_skill_writes(real_interface):
    c = real_interface
    ui, headers = c.browser.ui, c.browser.headers
    path = "/api/team-skills/skills/receipt-guide/decisions"
    body = {"decision_id": "d" * 32, "version_id": "1" * 32, "sha256": "e" * 64, "decision": "approve", "etag": '"x"'}
    assert ui.post(path, json=body, headers={"Origin": headers["Origin"]}).json()["error"]["code"] == "LOCAL_REQUEST_DENIED"
    assert ui.post(path + "?x=1", json=body, headers=headers).json()["error"]["code"] == "LOCAL_REQUEST_DENIED"
    oversized = ui.post(path, json={**body, "padding": "x" * 4096}, headers=headers)
    assert (oversized.status_code, oversized.json()["error"]["code"]) == (413, "TEAM_SKILL_INPUT_INVALID")
    admitted = ui.post(path, json=body, headers=headers).json()["error"]["code"]
    assert admitted not in {"LOCAL_REQUEST_DENIED", "TEAM_SKILL_INPUT_INVALID"}


def test_console_routes_are_installed_once():
    from fastapi import FastAPI
    app = FastAPI()
    install_team_skill_routes(app, lambda: None)
    paths = sorted(route.path for route in app.routes if route.path.startswith("/api/team-skills"))
    assert paths == sorted({
        "/api/team-skills/reviews", "/api/team-skills/skills/{artifact}",
        "/api/team-skills/skills/{artifact}/versions/{version_id}",
        "/api/team-skills/skills/{artifact}/versions/{version_id}/resources/{name}",
        "/api/team-skills/skills/{artifact}/decisions", "/api/team-skills/skills/{artifact}/deactivations",
        "/api/team-skills/drafts"})
    assert not any(getattr(route, "methods", set()) & {"DELETE", "PUT", "PATCH"}
                   for route in app.routes if route.path.startswith("/api/team-skills"))

def test_console_saves_one_owner_draft_through_the_real_api_and_the_same_content_finds_it(team_skills):
    skills, sent = console_for(team_skills)
    first = skills.draft(owner_draft())
    entry = first["entry"]
    assert entry["status"] == "awaiting_decision" and entry["owner"] == "team-owner" and entry["title"] == "Owner checklist"
    assert first["original_request_resent"] is False
    again = skills.draft(owner_draft())
    assert again["entry"] == entry
    assert [item for item in sent if item[0] == "POST"] == [("POST", "/team-skills/v1/drafts", None)] * 2
    assert [item["artifact_id"] for item in skills.reviews()["entries"]] == [entry["artifact_id"]]
    version = skills.version(entry["artifact_id"], entry["latest_version"]["version_id"])
    assert version["text"] == "Follow the owner's checklist.\n"


def test_owner_draft_input_is_checked_before_sending_and_read_only_cannot_write(team_skills):
    skills, sent = console_for(team_skills)
    for body in (owner_draft(title="a\nb"), {**owner_draft(), "extra": 1}, owner_draft(body="x" * 8193), []):
        with pytest.raises(ConsoleError) as invalid:
            skills.draft(body)
        assert invalid.value.code == "TEAM_SKILL_DRAFT_INVALID"
    assert sent == []
    reader, sent = console_for(team_skills, "read_only")
    with pytest.raises(ConsoleError) as denied:
        reader.draft(owner_draft())
    assert denied.value.code == "TEAM_SKILLS_OWNER_ONLY" and sent == []


def test_bridge_admits_the_bounded_owner_draft_and_nothing_larger(real_interface):
    c = real_interface
    ui, headers = c.browser.ui, c.browser.headers
    path = "/api/team-skills/drafts"
    assert ui.post(path, json=owner_draft(), headers={"Origin": headers["Origin"]}).json()["error"]["code"] == "LOCAL_REQUEST_DENIED"
    oversized = ui.post(path, json={**owner_draft(), "padding": "x" * (128 * 1024)}, headers=headers)
    assert (oversized.status_code, oversized.json()["error"]["code"]) == (413, "TEAM_SKILL_DRAFT_INVALID")
    admitted = ui.post(path, json=owner_draft(), headers=headers).json()["error"]["code"]
    assert admitted not in {"LOCAL_REQUEST_DENIED", "TEAM_SKILL_DRAFT_INVALID"}
