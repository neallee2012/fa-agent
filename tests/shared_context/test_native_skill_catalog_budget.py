"""SK-03/SK-04: the #75 skill catalog budget in the native lane (C2 provider and native capability API).

The real gateway behind an offline transport and the real o200k counter; no SDK process or model runs.
Degradation stages need a declared input limit far below any real model, so they are proven here; the
end-to-end SDK path is covered with a normal capacity in test_native_skill_resources.
"""

import hashlib
import json

import pytest

from shared_context.behavior_agent_template import TemplateSkill
from shared_context.behavior_capabilities import CapabilityCapture
from shared_context.behavior_native_capability_wire import NativeTurnScope
from shared_context.behavior_native_turn_wire import NativeReceipt, NativeSubmit, NativeTurnCapture
from shared_context.behavior_skill_catalog import DESCRIPTION_PREFIX, SkillCatalogPresentation
from shared_context.errors import Problem
from shared_context.m3_skills import PurposeSkillCatalog, PurposeSkillEntry, PurposeSkillReadResult
from shared_context.native_assembly import NativeAssemblyCollector
from test_agent_runtime import peer_runtime  # noqa: F401
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_agent_api import relay_case  # noqa: F401
from test_native_capability_api import SKILLS, code, gateway, skilled, skilled_http  # noqa: F401
from test_native_capability_tools import Upstream, bind, context_for, packet
from test_skill_catalog_budget_api import tokenizer  # noqa: F401

NAMES = ("alpha-receipts-0123456789abcdef0123456789abcdef",
         "beta-receipts-fedcba9876543210fedcba9876543210",
         "gamma-receipts-9876543210abcdef9876543210abcdef")
PURPOSE = "Use current approved receipts to explain the actual result. " * 4


def body(name):
    return f"APPROVED_BUDGET_SKILL_CANARY {name}: verify the receipt.\n"


class BudgetSkills:
    """Three approved skills with long purposes, so the catalog has something to shorten and omit."""

    def _entry(self, name, team):
        raw = body(name).encode("utf-8")
        return PurposeSkillEntry(
            name=name, title="Receipt guide " + name.split("-")[0] + " " + "x" * 120, version_id="1" * 32,
            sha256=hashlib.sha256(raw).hexdigest(), byte_length=len(raw), team=team, source_work="legacy-source",
            source_work_revision=1, description=PURPOSE,
            metadata_sha256=hashlib.sha256((name + PURPOSE).encode("utf-8")).hexdigest())

    def catalog(self, actor, team, selected, *, schema_version):
        assert schema_version == "fa-approved-skill-catalog-v2" and actor is not None
        entries = tuple(self._entry(name, team) for name in selected if name in NAMES)
        return PurposeSkillCatalog(schema_version=schema_version, entries=entries,
                                   unavailable_count=len(selected) - len(entries))

    def read(self, actor, team, selected, expected, *, include_resources=False, include_tool_requirements=False):
        if expected not in self.catalog(actor, team, selected, schema_version="fa-approved-skill-catalog-v2").entries:
            raise Problem(403, "SKILL_SOURCE_CHANGED", "original configured approved skill is no longer current")
        return PurposeSkillReadResult(schema_version="fa-approved-skill-read-v2", skill=expected, text=body(expected.name))


@pytest.fixture
def budgeted(skilled_http, tokenizer, monkeypatch):
    # Event loops first (skilled_http): the startup fixture forbids the socket pair a new loop opens.
    (case, _), loops = skilled_http
    a = case.base.a
    authority, binding = a.assistant.authority, a.runtime.work_policy.policy.template
    skills = BudgetSkills()
    monkeypatch.setattr(authority, "skills", skills)
    monkeypatch.setattr(a.runtime.work_policy.runtime.dep.memory, "_count", tokenizer.count)
    catalog = skills.catalog(authority.actors.get(binding.agent_oid), binding.team_id, NAMES,
                             schema_version="fa-approved-skill-catalog-v2")
    refs = tuple(TemplateSkill(name=item.name, version_id=item.version_id, sha256=item.sha256,
                               metadata_sha256=item.metadata_sha256) for item in catalog.entries)
    case.loop = loops[0]
    return case, refs, catalog, tokenizer


def budget_started(case, refs, *, prompt_tokens, budget=True, rid="budget-rid", text="Explain the receipts."):
    base = case.sender.capture
    model = base.model.model_copy(update={
        "context_window_tokens": prompt_tokens + 1024, "max_prompt_tokens": prompt_tokens, "max_output_tokens": 1024})
    settings = base.template.settings.model_copy(update={
        "capabilities": (SKILLS.ref,), "model_capacity_profile": "fa-model-capacity-v1",
        "skill_catalog_budget_profile": "fa-skill-catalog-budget-v1" if budget else None})
    template = base.template.model_copy(update={"skills": refs, "settings": settings})
    capture = NativeTurnCapture.model_validate_json(json.dumps({
        **base.model_dump(mode="json"), "template": template.model_dump(mode="json"),
        "model": model.model_dump(mode="json"), "model_capacity": model.capacity_snapshot().model_dump(mode="json"),
        "capabilities": CapabilityCapture(head_selection_id=None, grants=(SKILLS,)).model_dump(mode="json")}))
    command = case.sender.command.model_copy(update={"request_id": rid, "content": text})
    record = NativeReceipt(
        agent_id=case.sender.agent_id, hosted_session_id=case.sender.hosted_session_id, request_id=rid,
        conversation_id=case.sender.conversation_id, command=command, capture=capture,
        submit_digest=NativeSubmit(command=command, capture=capture).sha256, capture_digest=capture.sha256,
        status="started", turn_number=1, cancel_requested=False)
    case.remotes[0].records[rid] = record
    return NativeTurnScope(conversation_id=record.conversation_id, request_id=rid)


def upstream_for(case):
    upstream = Upstream(case)
    upstream.routes["/skill-catalog-presentation"] = (NativeTurnScope, gateway(case).skill_catalog_presentation)
    return upstream


def stage_limits(case, refs):
    """Declared input limits that make the real counter land on each #75 stage for this catalog."""
    native, limits, prompt = gateway(case), {}, 200_000
    for stage in ("full", "shortened", "names", "omitted"):
        scope = budget_started(case, refs, prompt_tokens=prompt, rid=f"probe-{stage}")
        view = native.skill_catalog_presentation(case.principal, scope)
        assert view.stage == stage, (stage, view.stage, prompt)
        limits[stage] = prompt
        prompt = (view.description_tokens - 1) * 50
    return limits


def bound(loop, case, scope):
    context = context_for(case, scope)
    context.assembly = NativeAssemblyCollector(context.receipt)
    upstream = upstream_for(case)
    tools = bind(loop, context, upstream)
    return tools, context.assembly.snapshot(), upstream


@pytest.mark.parametrize("stage", ["full", "shortened", "names", "omitted"])
def test_the_native_reader_uses_the_75_presentation_at_every_stage_and_records_it(budgeted, stage):
    case, refs, catalog, tokenizer = budgeted
    loop = case.loop
    limits = stage_limits(case, refs)
    # The user names the last skill, so the omitted stage keeps exactly that one (#75: named first).
    scope = budget_started(case, refs, prompt_tokens=limits[stage], text=f"Use {NAMES[2]} for this.")
    view = gateway(case).skill_catalog_presentation(case.principal, scope)
    tools, assembly, upstream = bound(loop, case, scope)
    reader = tools["read_skill"][0]
    assert view.stage == stage and reader.description == view.description
    assert view.description_tokens == tokenizer.count(view.description.encode()) <= view.budget_tokens
    # Same declared capacity and catalog: the same presentation is recomputed (SK-03).
    assert gateway(case).skill_catalog_presentation(case.principal, scope) == view
    # No enum: omitted names must stay out of the schema as well, as in the old lane.
    assert "enum" not in reader.parameters["properties"]["name"]
    shown = {entry["name"] for entry in json.loads(view.description[len(DESCRIPTION_PREFIX):])["entries"]}
    assert assembly.skill_catalog_stage == stage
    assert {skill.selected.name: (skill.catalogued, skill.catalog_reason) for skill in assembly.skills} == {
        name: (True, None) if name in shown else (False, "omitted_by_budget") for name in NAMES}
    if stage == "omitted":
        assert NAMES[2] in shown and len(shown) < len(NAMES) and view.omitted_count == len(NAMES) - len(shown)
    # An omitted name is still an authorized skill: read_skill(name) reads its approved body.
    for name in NAMES:
        read = loop.run_until_complete(reader.handler(packet("read_skill", {"name": name}, "call-" + name[:5])))
        assert PurposeSkillReadResult.model_validate_json(read.text).text == body(name)
    assert [suffix for suffix, _, _ in upstream.calls][:2] == ["/skill-catalog", "/skill-catalog-presentation"]


def test_without_the_budget_the_native_reader_and_its_bytes_are_unchanged(budgeted):
    case, refs, catalog, _ = budgeted
    loop = case.loop
    scope = budget_started(case, refs, prompt_tokens=272_000, budget=False)
    tools, assembly, upstream = bound(loop, case, scope)
    reader = tools["read_skill"][0]
    assert reader.description.endswith(catalog.model_dump_json())
    assert reader.parameters["properties"]["name"]["enum"] == sorted(NAMES)
    assert "/skill-catalog-presentation" not in [suffix for suffix, _, _ in upstream.calls]
    assert assembly.skill_catalog_stage is None and "skill_catalog_stage" not in assembly.model_dump_json()
    assert all((skill.catalogued, skill.catalog_reason) == (True, None) for skill in assembly.skills)
    with pytest.raises(Problem) as disabled:
        gateway(case).skill_catalog_presentation(case.principal, scope)
    assert code(disabled) == "SKILL_CATALOG_BUDGET_DISABLED"


def test_an_unknown_token_count_is_refused_not_counted_as_zero(budgeted, monkeypatch):
    case, refs, _, _ = budgeted
    scope = budget_started(case, refs, prompt_tokens=272_000)
    monkeypatch.setattr(case.base.a.runtime.work_policy.runtime.dep.memory, "_count", lambda _: None)
    with pytest.raises(Problem) as unknown:
        gateway(case).skill_catalog_presentation(case.principal, scope)
    assert code(unknown) == "SKILL_CATALOG_TOKEN_COUNT_UNKNOWN"


@pytest.mark.parametrize("change", ["capacity", "catalog", "omitted"])
def test_hosted_refuses_a_presentation_that_differs_from_its_capture(budgeted, change):
    case, refs, _, _ = budgeted
    loop = case.loop
    scope = budget_started(case, refs, prompt_tokens=272_000)
    upstream = upstream_for(case)
    served = upstream.routes["/skill-catalog-presentation"][1]

    def tampered(principal, command):
        view = served(principal, command)
        update = {"capacity": {"capacity_sha256": "0" * 64}, "catalog": {"catalog_sha256": "0" * 64},
                  "omitted": {"description": DESCRIPTION_PREFIX + json.dumps({
                      "entries": [], "omitted_count": 0, "unavailable_count": 0, "lookup": "x"})}}[change]
        return SkillCatalogPresentation.model_validate({**view.model_dump(), **update})

    upstream.routes["/skill-catalog-presentation"] = (NativeTurnScope, tampered)
    context = context_for(case, scope)
    with pytest.raises(Problem) as changed:
        bind(loop, context, upstream)
    assert code(changed) == "SKILL_CATALOG_VIEW_CHANGED"
