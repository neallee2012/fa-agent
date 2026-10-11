"""Read-only system prompt text for the owner: rebuilt on the server, never by the workbench; no SDK, storage or clock.

The text comes from the same functions the native Hosted turn uses (assemble_prompt, assemble_capabilities,
with_role_text, ORIGIN_ADDENDUM) in the same order as ``native_sdk``. Sections a turn adds at run time (teammates,
groups, reminders, provider tools, continuation) are saved only as bytes and SHA-256, so they are named here but never
rebuilt or guessed. A past turn is shown only where its own saved hashes match; otherwise it is "not rebuildable".
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from pydantic import Field

from .behavior_agent_message_wire import NOT_SELECTED_REASON
from .behavior_agent_role_prompt import (
    AGENT_ROLE_PROFILE, ROLE_PLATFORM_PROMPT, ROLE_SECTION, SHARED_ROLE_PROFILES, RolePromptCapture, with_role_text,
)
from .behavior_capability_prompt import assemble_capabilities
from .behavior_skill_prompt import InstructionPromptProfile, assemble_prompt
from .behavior_wire import Digest, Identifier, WireModel
from .errors import Problem
from .native_sdk_profiles import ORIGIN_ADDENDUM

MAX_SYSTEM_PROMPT_VIEW_BYTES = 256 * 1024
Count = Annotated[int, Field(ge=0)]
SegmentKind = Literal["fixed", "capabilities", "role_heading", "role", "origin", "teammates_note"]
# The fixed sentence native_agent_tools adds where the roster would be when collaboration is not selected (PA-03).
TEAMMATES_NOTE = "\n\n" + NOT_SELECTED_REASON
RuntimeName = Literal["teammates", "groups", "reminders", "providers", "continuation"]
RUNTIME_SECTIONS: tuple[RuntimeName, ...] = ("teammates", "groups", "reminders", "providers", "continuation")
TurnReason = Literal[
    "assembly_not_recorded", "system_message_not_recorded", "turn_sections_not_saved", "segment_mismatch",
    "profile_not_in_this_build", "mismatch",
]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _segment(kind, value, state):
    return PromptSegment(kind=kind, state=state, text=value, utf8_bytes=len(value.encode("utf-8")), sha256=_sha(value))


def _segment_consistent(segment) -> bool:
    return segment.text is None or (segment.sha256 == _sha(segment.text)
                                    and segment.utf8_bytes == len(segment.text.encode("utf-8")))


class PromptSegment(WireModel):
    kind: SegmentKind
    # current: today's versions for new conversations; verified: matches that turn's own saved hash; unverified: hidden.
    state: Literal["current", "verified", "unverified"]
    text: str | None = None
    utf8_bytes: Count | None = None
    sha256: Digest | None = None


class CurrentSystemPrompt(WireModel):
    schema_version: Literal["fa-system-prompt-current-v1"] = "fa-system-prompt-current-v1"
    applies_to: Literal["new-conversations-only"] = "new-conversations-only"
    profile: InstructionPromptProfile
    profile_prefix_sha256: Digest
    selection_id: Identifier | None
    role_version: Annotated[int, Field(ge=1)] | None
    role_sha256: Digest | None
    capability_entries: tuple[str, ...]
    segments: tuple[PromptSegment, ...]
    text: str
    utf8_bytes: Count
    sha256: Digest
    # Produced in each turn and never shown here: native_sdk adds teammates, groups, reminders and provider text
    # before a shared role text, and the continuation section after everything, including that role text.
    runtime_before_role: tuple[RuntimeName, ...]
    runtime_at_end: tuple[RuntimeName, ...]
    authorization: Literal[False] = False
    permissions_changed: Literal[False] = False


class TurnRuntimeSection(WireModel):
    name: RuntimeName
    # included: saved bytes and hash only; possible: a short unavailable/continuation note may have been added.
    state: Literal["included", "possible"]
    utf8_bytes: Count | None = None
    sha256: Digest | None = None


class TurnSystemPrompt(WireModel):
    schema_version: Literal["fa-system-prompt-turn-v1"] = "fa-system-prompt-turn-v1"
    agent_id: Identifier
    request_id: Identifier
    conversation_id: Identifier
    turn_number: Annotated[int, Field(ge=1)] | None
    profile: InstructionPromptProfile
    profile_prefix_sha256: Digest
    role_version: Annotated[int, Field(ge=1)] | None
    role_sha256: Digest | None
    capability_entries: tuple[str, ...]
    state: Literal["rebuilt", "unreconstructable"]
    reasons: tuple[TurnReason, ...]
    saved_utf8_bytes: Count | None
    saved_sha256: Digest | None
    segments: tuple[PromptSegment, ...]
    runtime_sections: tuple[TurnRuntimeSection, ...]
    text: str | None
    utf8_bytes: Count | None
    sha256: Digest | None
    authorization: Literal[False] = False


def _compose(profile, grants, role: RolePromptCapture | None, *, origin: bool, teammates_note: bool = False):
    """(kind, text) pieces in native order without per-turn sections, and whether a role text closes them."""
    base = assemble_prompt(profile).system_message
    capability = assemble_capabilities(profile, grants)
    fixed = base if capability is None else capability.system_message
    shared = profile in SHARED_ROLE_PROFILES
    pieces = []
    if profile == AGENT_ROLE_PROFILE:
        # v1 keeps capabilities inside its fixed text, before its own role heading (with_role_capabilities).
        if not fixed.startswith(ROLE_PLATFORM_PROMPT) or not fixed.endswith(ROLE_SECTION):
            raise ValueError("agent-role v1 text differs from its fixed sections")
        pieces.append(("fixed", ROLE_PLATFORM_PROMPT))
        if capability is not None:
            pieces.append(("capabilities", fixed[len(ROLE_PLATFORM_PROMPT):-len(ROLE_SECTION)]))
        pieces.append(("role_heading", ROLE_SECTION + ("" if role is None else "\n\n")))
        if role is not None:
            pieces.append(("role", role.text))
    else:
        if not fixed.startswith(base):
            raise ValueError("capability text must follow the fixed prompt")
        pieces.append(("fixed", base))
        if capability is not None:
            pieces.append(("capabilities", fixed[len(base):]))
    if origin:
        pieces.append(("origin", ORIGIN_ADDENDUM))
    if teammates_note:
        pieces.append(("teammates_note", TEAMMATES_NOTE))
    if shared and role is not None:
        pieces.extend((("role_heading", ROLE_SECTION + "\n\n"), ("role", role.text)))
    # The production composition must give exactly the same bytes, or nothing is shown.
    expected = fixed
    if role is not None and not shared:
        expected = with_role_text(expected, role)
    if origin:
        expected += ORIGIN_ADDENDUM
    if teammates_note:
        expected += TEAMMATES_NOTE
    if role is not None and shared:
        expected = with_role_text(expected, role)
    if "".join(text for _, text in pieces) != expected or any(not text for _, text in pieces):
        raise ValueError("prompt segments differ from the production composition")
    return pieces, base, capability, shared and role is not None


def _entries(grants):
    return () if grants is None else tuple(grant.entry_id for grant in grants.grants)


def current_view(template, selection_id, grants, role) -> CurrentSystemPrompt:
    """What a new native conversation of this Agent would send as system_message now, without per-turn sections."""
    profile = template.role.profile
    try:
        record = assemble_prompt(profile).record
        if (record.prefix_sha256, record.capability_catalog_version) != (
                template.role.prefix_sha256, template.role.capability_catalog_version):
            raise ValueError("the selected prompt version differs from this build")
        pieces, _, _, role_last = _compose(
            profile, grants, role, origin=template.settings.turn_origin_profile is not None)
    except (ValueError, KeyError):
        raise Problem(409, "SYSTEM_PROMPT_UNRECONSTRUCTABLE",
                      "this server cannot rebuild the selected prompt from its versioned text") from None
    text = "".join(value for _, value in pieces)
    return CurrentSystemPrompt(
        profile=profile, profile_prefix_sha256=template.role.prefix_sha256, selection_id=selection_id,
        role_version=None if role is None else role.version, role_sha256=None if role is None else role.sha256,
        capability_entries=_entries(grants),
        segments=tuple(_segment(kind, value, "current") for kind, value in pieces),
        text=text, utf8_bytes=len(text.encode("utf-8")), sha256=_sha(text),
        runtime_before_role=RUNTIME_SECTIONS[:-1] if role_last else (),
        runtime_at_end=RUNTIME_SECTIONS[-1:] if role_last else RUNTIME_SECTIONS,
    )


def _runtime_sections(receipt, parts):
    found = []
    for name in RUNTIME_SECTIONS[:-1]:
        part = parts.get(name)
        if part is None:
            continue
        if part.sha256 is not None and part.hash_scope == "utf8-text":
            found.append(TurnRuntimeSection(name=name, state="included", utf8_bytes=part.utf8_bytes, sha256=part.sha256))
        # These omissions still add one short sentence to system_message (native_agent_tools / native_sdk).
        elif part.reason == "unavailable" or name == "teammates" and part.reason in {"no_results", "not_selected"}:
            found.append(TurnRuntimeSection(name=name, state="possible"))
    if receipt.continuation is not None:
        found.append(TurnRuntimeSection(name="continuation", state="possible"))
    return tuple(found)


def turn_view(receipt) -> TurnSystemPrompt:
    """One saved native turn: rebuilt only from its own capture and checked against its own saved hashes."""
    capture = receipt.capture
    template, role, grants = capture.template, capture.role_prompt, capture.tools
    profile = template.role.profile
    assembly = receipt.assembly
    parts = {} if assembly is None else {part.name: part for part in assembly.parts}

    def saved(name):
        part = parts.get(name)
        return None if part is None or part.hash_scope != "utf8-text" else part.sha256

    message = parts.get("system_message")
    saved_sha = saved("system_message")
    common = dict(
        agent_id=receipt.agent_id, request_id=receipt.request_id, conversation_id=receipt.conversation_id,
        turn_number=receipt.turn_number, profile=profile, profile_prefix_sha256=template.role.prefix_sha256,
        role_version=None if role is None else role.version, role_sha256=None if role is None else role.sha256,
        capability_entries=_entries(grants), saved_sha256=saved_sha,
        saved_utf8_bytes=None if saved_sha is None else message.utf8_bytes,
        runtime_sections=_runtime_sections(receipt, parts),
    )
    try:
        pieces, base, capability, _ = _compose(
            profile, grants, role, origin=template.settings.turn_origin_profile is not None)
    except (ValueError, KeyError):
        return TurnSystemPrompt(**common, state="unreconstructable", reasons=("profile_not_in_this_build",),
                                segments=(), text=None, utf8_bytes=None, sha256=None)
    teammates = parts.get("teammates")
    # A turn without collaboration carries the fixed not-selected sentence; one saved before that sentence did not.
    candidates = [pieces]
    if teammates is not None and teammates.state == "omitted" and teammates.reason == "not_selected":
        candidates.append(_compose(profile, grants, role, origin=template.settings.turn_origin_profile is not None,
                                   teammates_note=True)[0])
    for candidate in candidates:
        text = "".join(value for _, value in candidate)
        if saved_sha is not None and _sha(text) == saved_sha:
            # The whole saved hash pins every byte, so no per-turn section remains unexplained.
            return TurnSystemPrompt(
                **{**common, "runtime_sections": ()}, state="rebuilt", reasons=(),
                segments=tuple(_segment(kind, value, "verified") for kind, value in candidate),
                text=text, utf8_bytes=len(text.encode("utf-8")), sha256=saved_sha)
    # A saved hash only pins its own text; anything it does not cover stays hidden.
    base_ok = _sha(base) == template.role.prefix_sha256 and saved("base") in (None, _sha(base))
    checks = {
        "fixed": base_ok,
        "capabilities": capability is not None and saved("capabilities") == _sha(capability.system_message),
        "role_heading": profile == AGENT_ROLE_PROFILE and base_ok,
        "role": role is not None and saved("role") in (None, role.sha256),
        "origin": saved("origin") == _sha(ORIGIN_ADDENDUM),
    }
    reasons = []
    if assembly is None:
        reasons.append("assembly_not_recorded")
    elif saved_sha is None:
        reasons.append("system_message_not_recorded")
    if common["runtime_sections"]:
        reasons.append("turn_sections_not_saved")
    recorded = {"fixed": "base", "capabilities": "capabilities", "role": "role", "origin": "origin"}
    if any(not checks[kind] and (kind == "fixed" or saved(recorded[kind]) is not None)
           for kind, _ in pieces if kind in recorded):
        reasons.append("segment_mismatch")
    if not reasons:
        reasons.append("mismatch")
    return TurnSystemPrompt(
        **common, state="unreconstructable", reasons=tuple(reasons),
        segments=tuple(_segment(kind, value, "verified") if checks[kind] else PromptSegment(kind=kind, state="unverified")
                       for kind, value in pieces),
        text=None, utf8_bytes=None, sha256=None)


def check_current_view(value: CurrentSystemPrompt) -> CurrentSystemPrompt:
    """Workbench-side consistency check of the server's view; it never composes or substitutes text."""
    if (value.utf8_bytes != len(value.text.encode("utf-8")) or value.sha256 != _sha(value.text)
            or "".join(segment.text or "" for segment in value.segments) != value.text
            or any(segment.state != "current" or segment.text is None or not _segment_consistent(segment)
                   for segment in value.segments)):
        raise ValueError("the system prompt view is inconsistent")
    return value


def check_turn_view(value: TurnSystemPrompt) -> TurnSystemPrompt:
    if any(segment.state == "current" or (segment.state == "verified") != (segment.text is not None)
           or not _segment_consistent(segment) for segment in value.segments):
        raise ValueError("the turn system prompt segments are inconsistent")
    if value.state == "rebuilt":
        if (value.text is None or value.reasons or value.sha256 != value.saved_sha256 or value.sha256 != _sha(value.text)
                or value.utf8_bytes != len(value.text.encode("utf-8"))
                or "".join(segment.text or "" for segment in value.segments) != value.text
                or any(segment.state != "verified" for segment in value.segments)):
            raise ValueError("a rebuilt turn prompt must match its saved hash")
    elif value.text is not None or value.sha256 is not None or value.utf8_bytes is not None or not value.reasons:
        raise ValueError("an unreconstructable turn prompt carries no full text")
    return value
