"""Offline tests for importing Team toolbox skills (ADR-0014): zip parsing, draft-equivalent checks, the SDK read."""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

from shared_context.team_mcp import TeamMcpFoundryClient, TeamMcpFoundryError
from shared_context.team_skill_draft_wire import OwnerSkillDraftInput
from shared_context.team_toolbox_skills import MAX_TOOLBOX_SKILLS, TeamToolboxSkill, toolbox_skill

NAME = "state-change-authorization"
BODY = f"---\nname: {NAME}\ndescription: Decide whether a change is authorized.\n---\n\n# Authorization\n\n- Check.\n"


def not_found():
    error = ResourceNotFoundError("missing")
    error.status_code = 404
    return error


def archive(files: dict[str, str | bytes]) -> bytes:
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path, content in files.items():
            bundle.writestr(path, content)
    return raw.getvalue()


def corrupt_member() -> bytes:
    data = bytearray(archive({"SKILL.md": BODY * 20}))
    start = 30 + len("SKILL.md")  # the member's deflate data follows its local header
    data[start:start + 4] = b"\xff\xff\xff\xff"
    return bytes(data)


def encrypted_member() -> bytes:
    data = bytearray(archive({"SKILL.md": BODY}))
    data[6] |= 1
    data[data.index(b"PK\x01\x02") + 8] |= 1
    return bytes(data)


@pytest.mark.parametrize("path",["SKILL.md", f"{NAME}/SKILL.md"])
def test_a_single_skill_md_is_importable_and_the_owner_draft_accepts_it_as_is(path):
    skill = toolbox_skill(NAME, "1", "Decide whether a change is authorized.", archive({path: BODY}))
    assert skill.refusal is None and skill.body == BODY
    assert skill.sha256 == hashlib.sha256(BODY.encode("utf-8")).hexdigest()
    OwnerSkillDraftInput.model_validate({"schema_version": "fa-team-owner-skill-draft-v1", "title": skill.name,
                                         "arguments": {"purpose": skill.description, "body": skill.body}})


@pytest.mark.parametrize(("name", "description", "bundle", "refusal"), [
    (NAME, "ok", None, "too_large"),
    (NAME, "ok", archive({"SKILL.md": BODY, "scripts/run.py": "print(1)"}), "multiple_files"),
    (NAME, "ok", archive({"README.md": BODY}), "no_skill_md"),
    (NAME, "ok", archive({"other/SKILL.md": BODY}), "no_skill_md"),
    (NAME, "ok", archive({"SKILL.md": "x" * 8193}), "too_large"),
    (NAME, "ok", b"not a zip", "unavailable"),
    (NAME, "ok", corrupt_member(), "unavailable"),
    (NAME, "ok", encrypted_member(), "unavailable"),
    (NAME, "ok", archive({"SKILL.md": b"\xff\xfe"}), "invalid_body"),
    (NAME, "ok", archive({"SKILL.md": "﻿" + BODY}), "invalid_body"),
    (NAME, "ok", archive({"SKILL.md": BODY.replace("Check", "Ch​eck")}), "invalid_body"),
    (NAME, "x" * 513, archive({"SKILL.md": BODY}), "invalid_purpose"),
    (NAME, None, archive({"SKILL.md": BODY}), "invalid_purpose"),
    ("n" * 65, "ok", archive({"SKILL.md": BODY}), "invalid_name"),
])
def test_anything_the_owner_draft_would_refuse_is_marked_not_importable(name, description, bundle, refusal):
    skill = toolbox_skill(name, "1", description, bundle)
    assert (skill.refusal, skill.body, skill.sha256) == (refusal, None, None)


class FakeSkills:
    def __init__(self, bundles, defaults):
        self.bundles, self.defaults, self.calls = bundles, defaults, []

    def get(self, name):
        self.calls.append(("get", name))
        if name not in self.defaults:
            raise not_found()
        return {"name": name, "default_version": self.defaults[name]}

    def get_version(self, name, version):
        self.calls.append(("get_version", name, version))
        if (name, version) not in self.bundles:
            raise not_found()
        return {"name": name, "version": version, "description": "About " + name}

    def download_version(self, name, version):
        self.calls.append(("download_version", name, version))
        data = self.bundles[(name, version)]
        return iter(data[index:index + 1024] for index in range(0, len(data), 1024))


class FakeToolboxes:
    def __init__(self, skills, default="6"):
        self.skills, self.default = skills, default

    def get(self, name):
        return {"name": name, "default_version": self.default}

    def get_version(self, name, version):
        assert version == self.default
        return {"version": version, "tools": [], "skills": self.skills}


def client_for(monkeypatch, toolboxes, skills):
    import azure.ai.projects as projects

    class FakeProject:
        def __init__(self, **kwargs):
            self.toolboxes, self.beta = toolboxes, type("Beta", (), {"skills": skills})()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(projects, "AIProjectClient", FakeProject)
    return TeamMcpFoundryClient("https://project.test", object())


def test_the_default_version_skills_are_read_pinned_or_at_the_skill_default(monkeypatch):
    skills = FakeSkills({("pinned", "3"): archive({"SKILL.md": "# Pinned\n"}),
                         ("follows", "2"): archive({"SKILL.md": "# Follows\n"}),
                         ("huge", "1"): b"x" * (64 * 1024 + 1)},
                        {"follows": "2", "huge": "1", "empty": None})
    references = [{"type": "skill_reference", "name": "pinned", "version": "3"},
                  {"type": "skill_reference", "name": "follows"},
                  {"type": "inline", "content": "x"},
                  {"type": "skill_reference", "name": "gone"},
                  {"type": "skill_reference", "name": "empty"},
                  {"type": "skill_reference", "name": "huge"}]
    default, found, omitted = client_for(monkeypatch, FakeToolboxes(references), skills).toolbox_skills("fa-team-t")
    assert (default, omitted) == ("6", 0)
    assert [(item.name, item.skill_version, item.refusal) for item in found] == [
        ("pinned", "3", None), ("follows", "2", None), ("inline", None, "unsupported_type"),
        ("gone", None, "unavailable"), ("empty", None, "unavailable"), ("huge", "1", "too_large")]
    assert found[1].description == "About follows" and found[1].body == "# Follows\n"
    assert ("get", "pinned") not in skills.calls


def test_more_references_than_the_page_shows_are_counted_and_not_read(monkeypatch):
    references = [{"type": "skill_reference", "name": f"s{index}"} for index in range(MAX_TOOLBOX_SKILLS + 3)]
    skills = FakeSkills({}, {})
    _, found, omitted = client_for(monkeypatch, FakeToolboxes(references), skills).toolbox_skills("fa-team-t")
    assert len(found) == MAX_TOOLBOX_SKILLS and omitted == 3
    assert {call[1] for call in skills.calls} == {f"s{index}" for index in range(MAX_TOOLBOX_SKILLS)}


def test_no_toolbox_or_no_default_lists_nothing_and_a_server_error_is_not_a_refusal(monkeypatch):
    class Missing(FakeToolboxes):
        def get(self, name):
            raise not_found()

    assert client_for(monkeypatch, Missing([]), FakeSkills({}, {})).toolbox_skills("t") == (None, [], 0)
    assert client_for(monkeypatch, FakeToolboxes([], default=None), FakeSkills({}, {})).toolbox_skills("t") == (
        None, [], 0)

    class Failing(FakeSkills):
        def get(self, name):
            raise HttpResponseError("down")

    with pytest.raises(TeamMcpFoundryError):
        client_for(monkeypatch, FakeToolboxes([{"type": "skill_reference", "name": "a"}]),
                   Failing({}, {})).toolbox_skills("t")


def test_an_importable_skill_always_carries_its_body_and_hash():
    with pytest.raises(ValueError):
        TeamToolboxSkill(name="a", body="# A\n")
    with pytest.raises(ValueError):
        TeamToolboxSkill(name="a", refusal="too_large", body="# A\n", sha256="a" * 64)
