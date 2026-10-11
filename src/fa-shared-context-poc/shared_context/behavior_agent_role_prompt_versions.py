"""Immutable Team-approved Agent role-text versions and their head; no routes, SDK or prompt assembly."""

import hashlib
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from .behavior_agent_role_prompt import (
    AGENT_ROLE_PROFILE, ROLE_PROFILES, ROLE_PROMPT_MAX_BYTES, RolePromptCapture, role_text_problem,
)
from .behavior_operations import _Model, _parse, control_key
from .behavior_store import ControlChanges, ControlRequest, ControlWrite
from .behavior_wire import Command, Digest, Identifier
from .errors import Problem
from .source_materials import digest

# Generous parse bound only; save_role_prompt refuses anything above ROLE_PROMPT_MAX_BYTES with the limit.
_PARSE_BOUND = 64 * 1024


class SaveAgentRolePrompt(Command):
    text: Annotated[str, Field(min_length=1, max_length=_PARSE_BOUND)]


class AgentRolePromptVersion(_Model):
    kind: Literal["agent_role_prompt_version"] = "agent_role_prompt_version"
    version: Annotated[int, Field(ge=1)]
    sha256: Digest
    text: str
    request_id: Identifier
    approved_by: Identifier
    approved_at: AwareDatetime


class AgentRolePromptHead(_Model):
    kind: Literal["agent_role_prompt_head"] = "agent_role_prompt_head"
    version: Annotated[int, Field(ge=1)]
    sha256: Digest


def version_key(scope_ids, version):
    return control_key("agent-role-prompt", *scope_ids, str(version))


def head_key(scope_ids):
    return control_key("agent-role-prompt-head", *scope_ids)


def _result(value):
    return {"version": value.version, "sha256": value.sha256, "approved_by": value.approved_by,
            "applies_to": "next-turns"}


def _text_refusal(problem):
    return Problem(422, problem, f"role text must be nonblank UTF-8 within {ROLE_PROMPT_MAX_BYTES} bytes",
                   max_bytes=ROLE_PROMPT_MAX_BYTES)


def role_prompt_command(request_id, text):
    """Check the text before building the command, so any oversized input is a 422 with the limit."""
    problem = role_text_problem(text)
    if problem is not None:
        raise _text_refusal(problem)
    try:
        return SaveAgentRolePrompt(request_id=request_id, text=text)
    except ValueError:
        raise Problem(422, "AGENT_ROLE_PROMPT_INVALID", "one valid request ID and role text are required") from None


def _saved_by_request(store, scope, request_id):
    """The retained version this request ID saved under the current head, if any (newest first, bounded reads)."""
    key = head_key(scope)
    row = store.read_snapshot((key,))[key]
    if row is None:
        return None
    version = _parse(row.payload["data"], AgentRolePromptHead).version
    while version >= 1:
        keys = tuple(version_key(scope, number) for number in range(version, max(0, version - 98), -1))
        rows = store.read_snapshot(keys)
        for vkey in keys:
            if rows[vkey] is not None:
                value = _parse(rows[vkey].payload["data"], AgentRolePromptVersion)
                if value.request_id == request_id:
                    return value
        version -= len(keys)
    return None


def save_role_prompt(control, principal, command, *, attempts=3):
    """Saving is the approval: the server assigns head+1 in one CAS; old versions are never rewritten.

    The same request ID with the same content returns its original receipt; different content is a conflict.
    Only an explicit 412 (nothing committed) is tried again with the same request, at most ``attempts`` times.
    """
    control._human(principal, "read")
    control._human(principal, "approve")
    if type(command) is not SaveAgentRolePrompt:
        raise Problem(422, "AGENT_ROLE_PROMPT_INVALID", "a typed role prompt command is required")
    problem = role_text_problem(command.text)
    if problem is not None:
        raise _text_refusal(problem)
    policy = control.installed()
    if not policy.active or not policy.read_enabled:
        raise Problem(403, "WORK_POLICY_REVOKED", "current standing read and work scope is required")
    scope = policy.scope_ids
    current_head = head_key(scope)
    request = ControlRequest(
        request_id=control_key("agent-role-prompt-request", *scope, command.request_id),
        request_digest=digest([policy.sha256, principal.oid, command.model_dump(mode="json")]),
        read_set=(control.policy_key, control.revocation_key, current_head),
    )

    def decide(rows):
        active = control.installed(rows)
        if active != policy or not active.active or not active.read_enabled:
            raise Problem(403, "WORK_POLICY_REVOKED", "the standing scope changed while saving")
        prior = None if rows[current_head] is None else _parse(rows[current_head].payload["data"], AgentRolePromptHead)
        value = AgentRolePromptVersion(
            version=1 if prior is None else prior.version + 1,
            sha256=hashlib.sha256(command.text.encode("utf-8")).hexdigest(), text=command.text,
            request_id=command.request_id, approved_by=principal.oid, approved_at=control.runtime._now(),
        )
        return ControlChanges(writes=(
            ControlWrite(row_id=version_key(scope, value.version), mode="create", data=value.model_dump(mode="json")),
            ControlWrite(row_id=current_head, mode="create" if prior is None else "replace",
                         data=AgentRolePromptHead(version=value.version, sha256=value.sha256).model_dump(mode="json")),
        ), result=_result(value))

    for attempt in range(attempts):
        try:
            return control.store.transact(request, decide)
        except Problem as error:
            code = error.body["error"]["code"]
            if code == "BEHAVIOR_REQUEST_CONFLICT":
                saved = _saved_by_request(control.store, scope, command.request_id)
                text_sha = hashlib.sha256(command.text.encode("utf-8")).hexdigest()
                if saved is not None and saved.sha256 == text_sha and saved.approved_by == principal.oid:
                    # The request digest also pins the installed policy; the same text is already saved.
                    raise Problem(409, "AGENT_ROLE_PROMPT_POLICY_CHANGED",
                                  f"this request ID already saved the same text as version {saved.version}; "
                                  "the standing policy was reinstalled since, so its original receipt is not "
                                  "returned and nothing was saved again", version=saved.version) from None
                if saved is not None and saved.sha256 != text_sha:
                    raise Problem(409, "AGENT_ROLE_PROMPT_CONFLICT",
                                  "this request ID already saved different role text") from None
                raise Problem(409, "AGENT_ROLE_PROMPT_CONFLICT",
                              "this request ID is already bound to different input (other text, approver or "
                              "standing policy); use a new request ID") from None
            if error.status != 412 or code != "BEHAVIOR_CONFLICT" or attempt == attempts - 1:
                raise


def current_role_prompt(store, policy):
    """The head version as a task capture, or None before any save."""
    scope = policy.scope_ids
    key = head_key(scope)
    row = store.read_snapshot((key,))[key]
    if row is None:
        return None
    head = _parse(row.payload["data"], AgentRolePromptHead)
    vkey = version_key(scope, head.version)
    stored = store.read_snapshot((vkey,))[vkey]
    if stored is None:
        raise Problem(409, "AGENT_ROLE_PROMPT_INVALID", "the current role prompt version is missing")
    value = _parse(stored.payload["data"], AgentRolePromptVersion)
    try:
        capture = RolePromptCapture(version=value.version, sha256=value.sha256, text=value.text)
    except ValueError:
        capture = None
    if capture is None or (value.version, value.sha256) != (head.version, head.sha256):
        raise Problem(409, "AGENT_ROLE_PROMPT_INVALID", "the current role prompt version differs from its head")
    return capture


def captured_role_prompt(store, policy, prompt_profile):
    """The one admission/preflight read: the head capture for the agent-role profiles, None for any other profile.

    v1 requires the role text; v2 takes it when this Agent has one.
    """
    if prompt_profile not in ROLE_PROFILES:
        return None
    capture = current_role_prompt(store, policy)
    if capture is None and prompt_profile == AGENT_ROLE_PROFILE:
        raise Problem(409, "AGENT_ROLE_PROMPT_MISSING", "this agent has no saved role prompt yet")
    return capture
