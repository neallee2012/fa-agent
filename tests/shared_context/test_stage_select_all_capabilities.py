"""deploy/stage/select_all_capabilities.py builds a template the server accepts and keeps the chosen state profile."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared_context.behavior_agent_template import AgentTemplate, export_template
from shared_context.behavior_capabilities import BUILTIN_CAPABILITIES, available_grants, check_selectable
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.errors import Problem

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("offline_stage_select", ROOT / "deploy" / "stage" / "select_all_capabilities.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def options():
    """What the stage options endpoint offers: every builtin plus the API secret broker it must leave out."""
    grants = available_grants(None, tuple(BUILTIN_CAPABILITIES))
    offered = [{"entry_id": grant.entry_id, "entry_sha256": grant.entry_sha256} for grant in grants.values()]
    offered.append({"entry_id": "WebIQ-MCP", "entry_sha256": "0" * 64})
    return {"capabilities": offered, "skills": [], "more_skills": False}, grants


def test_template_leaves_out_what_the_profile_cannot_compose_and_keeps_the_state_profile(script):
    offered, grants = options()
    wanted = script.template(offered)
    profile = script.DEFAULTS["prompt_profile"]
    chosen = [item["entry_id"] for item in wanted["settings"]["capabilities"]]
    # fa-delegate-reader-v1 needs read_user_file, which fa-instruction-v3/v4 do not declare (422 before this fix).
    assert "fa-delegate-reader-v1" not in chosen and "WebIQ-MCP" not in chosen
    assert chosen == sorted(set(BUILTIN_CAPABILITIES) - {"fa-delegate-reader-v1"})
    assert wanted["settings"]["state_profile"] == "foundry-responses-v1"
    template = AgentTemplate.model_validate_json(json.dumps(wanted))
    assert template.settings.state_profile == "foundry-responses-v1"
    assert template.role.prefix_sha256 == assemble_prompt(profile).record.prefix_sha256
    # The server's own selection rule accepts the whole set.
    assert check_selectable(template.settings.capabilities, profile, grants) == template.settings.capabilities


def test_a_profile_that_declares_the_reader_keeps_it(script, monkeypatch):
    offered, grants = options()
    monkeypatch.setitem(script.DEFAULTS, "prompt_profile", "fa-instruction-agent-role-v4")
    template = AgentTemplate.model_validate_json(json.dumps(script.template(offered)))
    assert "fa-delegate-reader-v1" in [ref.entry_id for ref in template.settings.capabilities]
    assert check_selectable(template.settings.capabilities, "fa-instruction-agent-role-v4", grants)


def test_keep_capabilities_changes_only_the_stage_settings(script, monkeypatch):
    offered, _ = options()
    owner = script.template(offered)
    # The owner's own narrower choice on the old manual profile, as stage A/B had before 2026-10-06.
    owner["settings"]["capabilities"] = owner["settings"]["capabilities"][:3]
    owner["settings"]["checkpoint_profile"] = {"name": "manual-no-custom-v1", "platform": "linux-x64",
                                               "provider": "openai-responses"}
    current = {"selection_id": "s" * 64, "template": owner}
    wanted = script.keep_current(current)
    assert wanted["settings"]["capabilities"] == owner["settings"]["capabilities"]
    assert wanted["skills"] == owner["skills"] and wanted["role"] == owner["role"]
    assert wanted["settings"]["checkpoint_profile"] == script.DEFAULTS["settings"]["checkpoint_profile"]
    assert wanted["settings"]["checkpoint_profile"]["name"] == "auto-no-custom-v1"
    changed = [key for key, value in wanted["settings"].items() if owner["settings"].get(key) != value]
    assert changed == ["checkpoint_profile"]
    assert current["template"]["settings"]["checkpoint_profile"]["name"] == "manual-no-custom-v1"
    AgentTemplate.model_validate_json(json.dumps(wanted))


def test_keep_capabilities_needs_an_existing_selection(script):
    with pytest.raises(SystemExit):
        script.keep_current({"selection_id": None})


def test_profile_defaults_and_assistants_come_from_the_selected_profile(script):
    assert script.conversation_defaults(script.STAGE) == (script.STAGE["conversation_defaults"], "profile")
    bare = {key: value for key, value in script.STAGE.items() if key != "conversation_defaults"}
    assert script.conversation_defaults(bare) == (script.DEFAULTS, "stage.json")
    assert script.assistant_names(script.STAGE) == ["fa-stage-a", "fa-stage-b"]
    own = {"prompt_profile": "fa-instruction-v4", "excluded_capabilities": [],
           "settings": {"state_profile": "foundry-responses-v1"}}
    assert script.conversation_defaults({**bare, "conversation_defaults": own}) == (own, "profile")


def _owner_template(script):
    """A valid reference template explicitly supplied by the owner for an unselected Agent."""
    offered, _ = options()
    seeded = script.template({**offered, "capabilities": offered["capabilities"][:2]},
                             {"prompt_profile": "fa-instruction-v1", "excluded_capabilities": [], "settings": {}})
    AgentTemplate.model_validate_json(json.dumps(seeded))
    return seeded


def test_keep_capabilities_without_a_selection_requires_an_explicit_owner_template(script):
    with pytest.raises(SystemExit, match="--initial-template"):
        script.keep_current({"selection_id": None})


def test_initial_template_loader_preserves_exact_source_references(script, tmp_path):
    initial = _owner_template(script)
    path = tmp_path / "owner-template.json"
    path.write_text(json.dumps(initial), encoding="utf-8")
    loaded = script.load_initial_template(path)
    assert loaded == AgentTemplate.model_validate_json(json.dumps(initial)).model_dump(mode="json")
    assert loaded["role"] == initial["role"]
    assert loaded["settings"]["capabilities"] == initial["settings"]["capabilities"]
    assert loaded["skills"] == initial["skills"]


def test_keep_capabilities_applies_settings_to_an_explicit_owner_template(script):
    initial = _owner_template(script)
    wanted = script.keep_current({"selection_id": None}, initial)
    assert wanted["role"] == initial["role"]
    assert wanted["settings"]["capabilities"] == initial["settings"]["capabilities"]
    assert wanted["skills"] == initial["skills"]
    assert wanted["settings"]["state_profile"] == "foundry-responses-v1"
    for key, value in script.DEFAULTS["settings"].items():
        assert wanted["settings"][key] == value
    assert "state_profile" not in initial["settings"]
    AgentTemplate.model_validate_json(json.dumps(wanted))


class _Unselected:
    """A scoped fresh Agent; template export would return the policy's 409 profile-unsupported response."""

    def __init__(self, export_error=None):
        self.selection, self.posts, self.gets = {"selection_id": None}, [], []
        self.export_error = export_error or {"status": 409, "code": "AGENT_TEMPLATE_PROFILE_UNSUPPORTED"}

    def get(self, path, headers=None):
        self.gets.append(path.rsplit("/", 1)[-1])
        if path.endswith("/selection"):
            return _Reply(self.selection)
        if path.endswith("/options"):
            return _Reply({"capabilities": [], "skills": []})
        raise AssertionError(f"scoped template export returns {self.export_error}")

    def post(self, path, json=None, headers=None):
        self.posts.append(path.rsplit("/", 1)[-1])
        if path.endswith("/select"):
            self.selection = {"selection_id": "u" * 64, "applies_to": "new-conversations-only",
                              "template": _Stage._server(json["template"])}
        return _Reply({}, 200)


def test_fresh_scoped_agent_without_initial_template_makes_no_export_or_post(script, tmp_path):
    policy = SimpleNamespace(
        active=True, read_enabled=True, sha256="p" * 64, profile=SimpleNamespace(prompt_profile=None),
        agent_scope="registered-peer", authority_domain="scope", agent_id="peer",
    )

    class _EmptyStore:
        def consistent_read(self, read):
            return read()

        def read_snapshot(self, keys):
            return {key: None for key in keys}

    runtime = object()
    control = SimpleNamespace(runtime=runtime, store=_EmptyStore(), policy=policy, _human=lambda *_: None,
                              installed=lambda: policy)
    authority = SimpleNamespace(runtime=runtime)
    with pytest.raises(Problem) as refused:
        export_template(control, authority, object())
    assert refused.value.status == 409
    assert refused.value.body["error"]["code"] == "AGENT_TEMPLATE_PROFILE_UNSUPPORTED"

    stage = _Unselected()
    agent = {"name": "fa-v18-b", "agent_id": "b"}
    with pytest.raises(SystemExit, match="--initial-template"):
        script.select(stage, {}, tmp_path, agent, "r1", False, True)
    assert stage.gets == ["selection"]
    assert stage.posts == []


def test_unknown_fresh_selection_reconciles_its_saved_template_without_resending(script, tmp_path):
    initial = _owner_template(script)
    stage = _Unselected()
    agent = {"name": "fa-v18-b", "agent_id": "b"}
    intent = tmp_path / "select-fa-v18-b-r1-intent.json"
    intent.write_text(json.dumps({"body": {
        "request_id": "r1-fa-v18-b", "expected_selection_id": None, "template": initial,
    }}), encoding="utf-8")
    result, wanted = script.select(stage, {}, tmp_path, agent, "r1", False, True)
    assert result["outcome"] == "intent-exists-reconciled"
    assert script._same(wanted, initial)
    assert stage.posts == ["selection-reconcile"]
    assert stage.gets == []
    assert stage.selection["selection_id"] is None


def test_owner_bootstrap_selects_explicit_template_once_and_preserves_capabilities(script, tmp_path):
    initial = _owner_template(script)
    stage = _Unselected()
    agent = {"name": "fa-v18-a", "agent_id": "a"}
    dry, _ = script.select(stage, {}, tmp_path, agent, "r1", True, True, initial_template=initial)
    assert dry["outcome"] == "dry-run" and dry["would_replace"] is None
    assert dry["capabilities"] == [item["entry_id"] for item in initial["settings"]["capabilities"]]
    assert "state_profile" in dry["changed_settings"] and stage.posts == []
    result, wanted = script.select(stage, {}, tmp_path, agent, "r1", False, True, initial_template=initial)
    assert result["outcome"] == "http_200" and stage.posts == ["select"]
    assert script._same(stage.selection["template"], wanted)
    assert stage.selection["template"]["settings"]["state_profile"] == "foundry-responses-v1"
    assert stage.selection["template"]["role"]["profile"] == "fa-instruction-v1"
    assert stage.selection["template"]["role"] == initial["role"]
    assert stage.selection["template"]["settings"]["capabilities"] == initial["settings"]["capabilities"]
    assert stage.selection["template"]["skills"] == initial["skills"]
    again, _ = script.select(stage, {}, tmp_path, agent, "r1", False, True)
    assert again["outcome"] == "intent-exists-reconciled" and stage.posts == ["select", "selection-reconcile"]


class _Reply:
    def __init__(self, value, status=200):
        self.value, self.status_code = value, status

    def raise_for_status(self):
        return self

    def json(self):
        return self.value


class _Stage:
    """The two agent-template routes as the server answers them: a selection is the server's own JSON form."""

    def __init__(self, template):
        self.selection = {"selection_id": "s" * 64, "applies_to": "new-conversations-only",
                          "template": self._server(template)}
        self.posts = []

    @staticmethod
    def _server(template):
        return AgentTemplate.model_validate_json(json.dumps(template)).model_dump(mode="json")

    def get(self, path, headers=None):
        return _Reply({"capabilities": [], "skills": []} if path.endswith("/options") else self.selection)

    def post(self, path, json=None, headers=None):
        self.posts.append(path.rsplit("/", 1)[-1])
        if path.endswith("/select"):
            self.selection = {"selection_id": "t" * 64, "applies_to": "new-conversations-only",
                              "template": self._server(json["template"])}
        return _Reply({}, 200)


def _manual_owner(script, capabilities):
    offered, _ = options()
    owner = script.template(offered)
    owner["settings"]["capabilities"] = capabilities(owner["settings"]["capabilities"])
    owner["settings"]["checkpoint_profile"] = {"name": "manual-no-custom-v1", "platform": "linux-x64",
                                               "provider": "openai-responses"}
    return owner


@pytest.mark.parametrize("capabilities", [lambda caps: caps[:3], lambda caps: []], ids=["some", "none"])
def test_keep_capabilities_dry_run_reads_the_server_form(script, tmp_path, capabilities):
    stage = _Stage(_manual_owner(script, capabilities))
    result, wanted = script.select(stage, {}, tmp_path, {"name": "fa-stage-a", "agent_id": "a"}, "r1", True, True)
    assert result["changed_settings"] == ["checkpoint_profile"] and wanted is None
    assert result["capabilities"] == [item["entry_id"] for item in
                                      stage.selection["template"]["settings"].get("capabilities", [])]
    assert stage.posts == [] and list(tmp_path.iterdir()) == []


def test_keep_capabilities_writes_once_and_a_rerun_only_reconciles(script, tmp_path):
    stage = _Stage(_manual_owner(script, lambda caps: caps[:3]))
    agent = {"name": "fa-stage-a", "agent_id": "a"}
    result, wanted = script.select(stage, {}, tmp_path, agent, "r1", False, True)
    assert result["outcome"] == "http_200" and stage.posts == ["select"]
    assert script._same(stage.selection["template"], wanted)
    assert stage.selection["template"]["settings"]["checkpoint_profile"]["name"] == "auto-no-custom-v1"
    again, _ = script.select(stage, {}, tmp_path, agent, "r1", False, True)
    assert again["outcome"] == "intent-exists-reconciled" and stage.posts == ["select", "selection-reconcile"]
