"""Validate the M4 result contract without supplying fixture answers."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated

from pydantic import Field, ValidationError

from .errors import Problem
from .models import StrictModel


class AnswerField(StrictModel):
    value: Annotated[str, Field(min_length=1, max_length=2048)]
    citation: Annotated[str, Field(min_length=1, max_length=2048)]


class PolicyField(AnswerField):
    value: Annotated[str, Field(pattern=r"^APPROVED_POLICY=[A-Za-z0-9._-]+$", max_length=2048)]


class M4Answer(StrictModel):
    decision_owner: AnswerField
    report_layout: AnswerField
    private_theme: AnswerField
    policy_marker: PolicyField


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate result field")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("nonfinite result value")


def validate_answer(text: str) -> M4Answer:
    try:
        return M4Answer.model_validate(json.loads(
            text, object_pairs_hook=_unique, parse_constant=_constant,
        ))
    except (ValueError, TypeError, RecursionError, ValidationError) as exc:
        raise Problem(
            422, "M4_RESULT_INVALID",
            "result must contain exactly decision_owner, report_layout, private_theme, policy_marker; "
            "each needs string value and citation. policy_marker.value must preserve the complete "
            "APPROVED_POLICY=<version> token from the approved source, not only its version suffix. "
            "No output was saved by this failed validation.",
        ) from exc


def validate_policy_source(answer: M4Answer, context: dict) -> None:
    policies = context.get("approved_policy_context")
    if not isinstance(policies, list):
        raise Problem(409, "M4_POLICY_SOURCE_REQUIRED", "read the current authorized policy context")
    matches = []
    for policy in policies:
        if not isinstance(policy, dict) or not isinstance(policy.get("version"), dict):
            continue
        version = policy["version"]
        reference = f'policy:{policy.get("artifact_id")}@{version.get("version_id")}'
        if reference == answer.policy_marker.citation:
            matches.append(policy)
    if len(matches) != 1:
        raise Problem(422, "M4_POLICY_SOURCE_MISMATCH", "policy citation must identify one approved source version")
    policy = matches[0]
    version, text = policy["version"], policy.get("text")
    approval = version.get("approval")
    if not isinstance(text, str) or not isinstance(approval, dict):
        raise Problem(422, "M4_POLICY_SOURCE_MISMATCH", "policy source lacks approved immutable content")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    markers = [line for line in text.splitlines() if re.fullmatch(r"APPROVED_POLICY=[A-Za-z0-9._-]+", line)]
    if (policy.get("status") != "approved" or approval.get("decision") != "approve"
            or version.get("sha256") != digest or approval.get("sha256") != digest
            or approval.get("version_id") != version.get("version_id")
            or markers != [answer.policy_marker.value]):
        raise Problem(
            422, "M4_POLICY_SOURCE_MISMATCH",
            "preserve the exact marker and citation from the approved, digest-bound policy source",
        )
