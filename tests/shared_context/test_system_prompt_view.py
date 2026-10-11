"""Read-only system prompt view: the real native executor's bound text versus the server rebuild; no cloud or model."""

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace

import httpx
import pytest

from shared_context.behavior_agent_role_prompt import (
    AGENT_ROLE_PROFILE, AGENT_ROLE_V4_PROFILE, ROLE_SECTION, RolePromptCapture, assemble_role_v4_prompt,
)
from shared_context.behavior_agent_template import TemplateRole
from shared_context.behavior_native_turn_api import PREFIX
from shared_context.behavior_native_turn_wire import NativeReceipt, NativeSubmit
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.behavior_system_prompt_view import (
    CurrentSystemPrompt, TurnSystemPrompt, check_current_view, check_turn_view, current_view, turn_view,
)
from shared_context.errors import Problem
from shared_context.m3_skills import PurposeSkillCatalog
from shared_context.native_assembly import AssemblyPart, NativeAssembly
from shared_context.native_agent_tools import NOT_SELECTED_REASON
from shared_context.native_capability_tools import approved_skills_provider
from shared_context.native_sdk_profiles import ORIGIN_ADDENDUM
from test_native_assembly import install_executor, offline_sdk, run_turn, skill_request
from test_native_responses import response_submit, responses  # noqa: F401
from test_native_turn_api import native_api, submit as api_submit  # noqa: F401
from test_native_turn_wire import receipt_for, submit
from test_native_turns import local_env  # noqa: F401
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401

ROLE_TEXT = "你是 Team 的會計助理，回覆用繁體中文。"


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def with_profile(request, profile, *, role=True, origin=False):
    data = request.model_dump(mode="json")
    record = assemble_prompt(profile).record
    data["capture"]["template"]["role"] = TemplateRole(
        profile=profile, prefix_sha256=record.prefix_sha256,
        capability_catalog_version=record.capability_catalog_version).model_dump(mode="json")
    data["capture"]["role_prompt"] = RolePromptCapture(
        version=3, sha256=sha(ROLE_TEXT), text=ROLE_TEXT).model_dump(mode="json") if role else None
    if origin:
        data["capture"]["template"]["settings"]["turn_origin_profile"] = "fa-instruction-context-origin-v1"
    return type(request).model_validate_json(json.dumps(data))


@pytest.mark.parametrize("profile,role,origin", [
    (AGENT_ROLE_PROFILE, True, False),
    (AGENT_ROLE_PROFILE, True, True),
    (AGENT_ROLE_V4_PROFILE, True, True),
    (AGENT_ROLE_V4_PROFILE, False, False),
    ("fa-instruction-v4", False, True),
])
def test_rebuilt_turn_is_exactly_the_system_message_the_native_executor_bound(
        responses, monkeypatch, tmp_path, profile, role, origin):
    bound = []

    async def sdk(**options):
        bound.append(options["system_message"])
        return await offline_sdk(**options)

    install_executor(responses, monkeypatch, tmp_path, sdk)
    request = with_profile(response_submit(), profile, role=role, origin=origin)
    completed = run_turn(responses, request)
    view = check_turn_view(turn_view(completed))
    assert view.state == "rebuilt" and view.reasons == ()
    assert view.text == bound[0] and view.sha256 == sha(bound[0]) == view.saved_sha256
    assert all(segment.state == "verified" for segment in view.segments)
    kinds = [segment.kind for segment in view.segments]
    assert ("role" in kinds) is role and ("origin" in kinds) is origin
    if role:
        assert [s.text for s in view.segments if s.kind == "role"] == [ROLE_TEXT]
        assert bound[0].endswith(ROLE_TEXT) is not (origin and profile == AGENT_ROLE_PROFILE)
    # The new-conversation preview uses the same composition: no per-turn section, so the same bytes.
    capture = completed.capture
    current = check_current_view(current_view(capture.template, None, capture.capabilities, capture.role_prompt))
    role_last = role and profile == AGENT_ROLE_V4_PROFILE
    assert current.text == bound[0]
    assert current.runtime_before_role == (("teammates", "groups", "reminders", "providers") if role_last else ())
    assert current.runtime_at_end == (("continuation",) if role_last else (
        "teammates", "groups", "reminders", "providers", "continuation"))


def test_captured_capability_text_is_rebuilt_or_named_as_a_turn_section(responses, monkeypatch, tmp_path):
    request, entries, _ = skill_request(response_submit())
    bound = []

    def service(incoming):
        result = PurposeSkillCatalog(schema_version="fa-approved-skill-catalog-v2", entries=entries)
        return httpx.Response(200, content=result.model_dump_json().encode())

    async def sdk(**options):
        bound.append(options["system_message"])
        return await offline_sdk(**options)

    install_executor(responses, monkeypatch, tmp_path, sdk, api_token_provider=lambda: "OFFLINE-api-token",
                     capability_providers=(approved_skills_provider(transport=httpx.MockTransport(service)),))
    completed = run_turn(responses, request)
    view = check_turn_view(turn_view(completed))
    capabilities = next(segment for segment in view.segments if segment.kind == "capabilities")
    assert capabilities.state == "verified" and "fa-skills-v1" in capabilities.text
    assert view.capability_entries == ("fa-skills-v1",)
    providers = [item for item in view.runtime_sections if item.name == "providers"]
    if view.state == "rebuilt":
        assert view.text == bound[0] and not providers
    else:
        # The provider text is produced in the turn and saved only as bytes and SHA-256; it is never guessed.
        assert view.reasons == ("turn_sections_not_saved",) and view.text is None
        assert providers and providers[0].state == "included" and providers[0].sha256 is not None
        assert all(segment.state == "verified" for segment in view.segments)
        assert all(segment.text in bound[0] for segment in view.segments)


def _part(name, text, **values):
    return AssemblyPart(name=name, state="included", utf8_bytes=len(text.encode()), sha256=sha(text), **values)


def test_a_real_turn_without_collaboration_names_its_sentence_instead_of_a_mismatch(responses, monkeypatch, tmp_path):
    """PA-03: Hosted always wires the peer tools; without collaboration they add one sentence, named as possible."""
    from shared_context.native_agent_tools import NativeAgentTools

    class Directory:
        async def directory(self, _conversation_id=None):
            raise AssertionError("the directory is not read without collaboration")

    bound = []

    async def sdk(**options):
        bound.append(options["system_message"])
        return await offline_sdk(**options)

    install_executor(responses, monkeypatch, tmp_path, sdk,
                     agent_tools=lambda receipt, control: NativeAgentTools(Directory(), receipt, control))
    completed = run_turn(responses, with_profile(response_submit(), AGENT_ROLE_V4_PROFILE, origin=True))
    assert ("\n\n" + NOT_SELECTED_REASON + ROLE_SECTION) in bound[0]
    view = check_turn_view(turn_view(completed))
    # The sentence is a fixed constant, so the turn is rebuilt exactly and nothing is left as a possible section.
    assert view.state == "rebuilt" and view.reasons == () and view.runtime_sections == ()
    assert view.text == bound[0] and view.sha256 == sha(bound[0]) == view.saved_sha256
    assert [segment.kind for segment in view.segments] == [
        "fixed", "origin", "teammates_note", "role_heading", "role"]
    assert [segment.text for segment in view.segments if segment.kind == "teammates_note"] == [
        "\n\n" + NOT_SELECTED_REASON]


def test_turn_sections_hide_unpinned_text_and_old_turns_never_borrow_current_versions():
    request = with_profile(submit(), AGENT_ROLE_V4_PROFILE, origin=True)
    receipt = receipt_for(request, "completed")
    base = assemble_role_v4_prompt().system_message
    full = base + ORIGIN_ADDENDUM + "\n\n## Teammates\nPeer directory" + ROLE_SECTION + "\n\n" + ROLE_TEXT
    assembly = NativeAssembly(state="recorded", capture_digest=receipt.capture_digest, parts=(
        _part("base", base), _part("role", ROLE_TEXT), _part("origin", ORIGIN_ADDENDUM),
        _part("teammates", "## Teammates\nPeer directory"), _part("system_message", full)))
    view = check_turn_view(turn_view(receipt.model_copy(update={"assembly": assembly})))
    assert view.state == "unreconstructable" and view.reasons == ("turn_sections_not_saved",)
    assert view.text is None and view.saved_sha256 == sha(full)
    states = {segment.kind: segment for segment in view.segments}
    assert states["fixed"].text == base and states["origin"].text == ORIGIN_ADDENDUM and states["role"].text == ROLE_TEXT
    # The shared role heading is not pinned by any saved part of this turn, so it is hidden, not reprinted.
    assert states["role_heading"].state == "unverified" and states["role_heading"].text is None
    assert [(item.name, item.state, item.sha256) for item in view.runtime_sections] == [
        ("teammates", "included", sha("## Teammates\nPeer directory"))]
    assert "Peer directory" not in view.model_dump_json()

    old = check_turn_view(turn_view(receipt))
    assert old.state == "unreconstructable" and old.reasons == ("assembly_not_recorded",)
    assert {s.kind: s.state for s in old.segments} == {
        "fixed": "verified", "origin": "unverified", "role_heading": "unverified", "role": "verified"}

    # No teammate to list still adds one sentence; it is named, not reported as an unexplained mismatch.
    lonely = NativeAssembly(state="recorded", capture_digest=receipt.capture_digest, parts=(
        _part("base", base), _part("role", ROLE_TEXT), _part("origin", ORIGIN_ADDENDUM),
        AssemblyPart(name="teammates", state="omitted", reason="no_results"),
        _part("system_message", full.replace("## Teammates\nPeer directory", "No teammate sentence"))))
    named = check_turn_view(turn_view(receipt.model_copy(update={"assembly": lonely})))
    assert named.reasons == ("turn_sections_not_saved",)
    assert [(item.name, item.state) for item in named.runtime_sections] == [("teammates", "possible")]

    # PA-03: collaboration not selected adds its fixed sentence; turns saved before that sentence rebuild without it.
    plain = base + ORIGIN_ADDENDUM + ROLE_SECTION + "\n\n" + ROLE_TEXT
    with_note = plain.replace(ROLE_SECTION, "\n\n" + NOT_SELECTED_REASON + ROLE_SECTION, 1)
    for text, note in ((with_note, True), (plain, False)):
        unselected = NativeAssembly(state="recorded", capture_digest=receipt.capture_digest, parts=(
            _part("base", base), _part("role", ROLE_TEXT), _part("origin", ORIGIN_ADDENDUM),
            AssemblyPart(name="teammates", state="omitted", reason="not_selected"), _part("system_message", text)))
        seen = check_turn_view(turn_view(receipt.model_copy(update={"assembly": unselected})))
        assert seen.state == "rebuilt" and seen.text == text and seen.reasons == () and seen.runtime_sections == ()
        assert ("teammates_note" in [segment.kind for segment in seen.segments]) is note
    # Only not_selected may carry it: the same text with the roster's no_results reason is not rebuilt by guessing.
    other = NativeAssembly(state="recorded", capture_digest=receipt.capture_digest, parts=(
        _part("base", base), _part("role", ROLE_TEXT), _part("origin", ORIGIN_ADDENDUM),
        AssemblyPart(name="teammates", state="omitted", reason="no_results"), _part("system_message", with_note)))
    guessed = check_turn_view(turn_view(receipt.model_copy(update={"assembly": other})))
    assert guessed.state == "unreconstructable" and guessed.reasons == ("turn_sections_not_saved",)

    changed = receipt.model_copy(update={"capture": receipt.capture.model_copy(update={
        "template": receipt.capture.template.model_copy(update={"role": receipt.capture.template.role.model_copy(
            update={"prefix_sha256": "0" * 64})})})})
    mismatch = check_turn_view(turn_view(changed))
    assert "segment_mismatch" in mismatch.reasons
    assert {s.kind: s.state for s in mismatch.segments}["fixed"] == "unverified"
    assert all(base not in (segment.text or "") for segment in mismatch.segments)


def test_current_view_refuses_a_version_this_build_cannot_rebuild_and_checks_are_strict():
    capture = with_profile(submit(), AGENT_ROLE_V4_PROFILE).capture
    view = current_view(capture.template, "selection-1", None, capture.role_prompt)
    assert check_current_view(view).selection_id == "selection-1" and view.role_version == 3
    changed = capture.template.model_copy(update={"role": capture.template.role.model_copy(
        update={"prefix_sha256": "0" * 64})})
    with pytest.raises(Problem) as refused:
        current_view(changed, None, None, capture.role_prompt)
    assert refused.value.status == 409 and refused.value.body["error"]["code"] == "SYSTEM_PROMPT_UNRECONSTRUCTABLE"
    tampered = view.model_copy(update={"text": view.text + "x"})
    with pytest.raises(ValueError):
        check_current_view(tampered)
    rebuilt = turn_view(receipt_for(submit(), "completed").model_copy(update={"assembly": NativeAssembly(
        state="recorded", parts=(_part("system_message", "not this"),))}))
    assert rebuilt.state == "unreconstructable" and rebuilt.reasons == ("mismatch",)
    forged = rebuilt.model_copy(update={"state": "rebuilt", "reasons": (), "text": "forged",
                                        "sha256": sha("forged"), "utf8_bytes": 6})
    with pytest.raises(ValueError):
        check_turn_view(forged)


def test_signed_api_reads_are_owner_only_pure_reads(native_api, monkeypatch):
    case = native_api
    assert api_submit(case).status_code == 202
    capture = case.remote.records["native-rid"].capture
    calls, guard = len(case.remote.calls), case.c.control.store._guard()
    current = case.client.get("/work-policy/v1/agent-template/system-prompt", headers=case.headers)
    assert current.status_code == 200, current.text
    assert current.headers["cache-control"] == "no-store"
    view = check_current_view(CurrentSystemPrompt.model_validate_json(current.content))
    expected = current_view(capture.template, capture.profile_selection_id, capture.capabilities, capture.role_prompt)
    assert view == expected
    assert len(case.remote.calls) == calls and case.c.control.store._guard() == guard
    assert case.client.get("/work-policy/v1/agent-template/system-prompt").status_code == 401
    from shared_context.behavior_native_capture import system_prompt_inputs
    with pytest.raises(Problem) as denied:
        system_prompt_inputs(case.c.control, case.composition.assistant.authority,
                             replace(case.c.human, team="other-team"))
    assert denied.value.status == 403

    case.remote.completed("native-rid")
    turn = case.client.get(PREFIX + "/requests/native-rid/system-prompt", headers=case.headers)
    assert turn.status_code == 200, turn.text
    old = check_turn_view(TurnSystemPrompt.model_validate_json(turn.content))
    assert old.state == "unreconstructable" and old.reasons == ("assembly_not_recorded",) and old.text is None
    original = case.remote.records["native-rid"]
    case.remote.records["native-rid"] = original.model_copy(update={"assembly": NativeAssembly(
        state="recorded", capture_digest=original.capture_digest, parts=(_part("system_message", view.text),))})
    guard = case.c.control.store._guard()
    rebuilt = check_turn_view(TurnSystemPrompt.model_validate_json(
        case.client.get(PREFIX + "/requests/native-rid/system-prompt", headers=case.headers).content))
    assert rebuilt.state == "rebuilt" and rebuilt.text == view.text
    # Only the original receipt was read: no usage, ledger or peer write.
    assert case.c.control.store._guard() == guard
    assert [command.operation for _, command in case.remote.calls[calls:] if command.operation != "session_read"] == [
        "read", "read"]
    monkeypatch.delenv("FA_NATIVE_TURNS")
    disabled = case.client.get("/work-policy/v1/agent-template/system-prompt", headers=case.headers)
    assert disabled.status_code == 503 and disabled.json()["error"]["code"] == "NATIVE_TURNS_DISABLED"


def test_system_prompt_view_import_has_no_hosted_sdk_dependency():
    script = """
import importlib.abc
import sys
class RejectHosted(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'copilot' or fullname.startswith(('copilot.', 'azure.ai.agentserver')):
            raise AssertionError('Hosted SDK import: ' + fullname)
sys.meta_path.insert(0, RejectHosted())
from shared_context.behavior_system_prompt_view import current_view, turn_view
from shared_context.behavior_native_capture import system_prompt_inputs
print('sdk-independent')
"""
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", script],
                            env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "sdk-independent"

