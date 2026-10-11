import copy
import hashlib
import json

import pytest

from shared_context.errors import Problem
from shared_context.m4_result import validate_answer, validate_policy_source


def answer(marker="APPROVED_POLICY=V3"):
    result = {name: {"value": "synthetic fact", "citation": "synthetic source"}
              for name in ("decision_owner", "report_layout", "private_theme")}
    result["policy_marker"] = {"value": marker, "citation": "policy:source@" + "a" * 32}
    return result


def context(text="APPROVED_POLICY=V3"):
    digest = hashlib.sha256(text.encode()).hexdigest()
    return {"approved_policy_context": [{
        "artifact_id": "source", "status": "approved", "text": text,
        "version": {"version_id": "a" * 32, "sha256": digest,
                    "approval": {"decision": "approve", "version_id": "a" * 32, "sha256": digest}},
    }]}


@pytest.mark.parametrize("suffix", ["V3", "R7", "different-version"])
def test_exact_native_failure_and_nonfixture_versions(suffix):
    with pytest.raises(Problem) as exc:
        validate_answer(json.dumps(answer(suffix)))
    assert exc.value.body["error"]["code"] == "M4_RESULT_INVALID"
    marker = "APPROVED_POLICY=" + suffix
    validate_policy_source(validate_answer(json.dumps(answer(marker))), context(marker))


@pytest.mark.parametrize("bad", [
    "not JSON", "[]", '{"policy_marker":NaN}',
    json.dumps({**answer(), "extra": True}),
    json.dumps({**answer(), "decision_owner": {"value": 1, "citation": "source"}}),
    json.dumps({**answer(), "private_theme": {"value": "fact", "citation": ""}}),
    json.dumps(answer())[:-1] + ',"policy_marker":{"value":"APPROVED_POLICY=V3","citation":"other"}}',
])
def test_result_shape_is_strict(bad):
    with pytest.raises(Problem) as exc:
        validate_answer(bad)
    assert exc.value.body["error"]["code"] == "M4_RESULT_INVALID"


@pytest.mark.parametrize("change", [
    "missing", "duplicate", "wrong-citation", "unapproved", "wrong-value",
    "wrong-hash", "wrong-approval-hash", "wrong-approval-version", "multiple-markers",
])
def test_complete_marker_is_not_enough_without_matching_approved_source(change):
    value = validate_answer(json.dumps(answer()))
    source = context()
    policy = source["approved_policy_context"][0]
    if change == "missing":
        source["approved_policy_context"] = []
    elif change == "duplicate":
        source["approved_policy_context"].append(copy.deepcopy(policy))
    elif change == "wrong-citation":
        policy["artifact_id"] = "elsewhere"
    elif change == "unapproved":
        policy["version"]["approval"]["decision"] = "reject"
    elif change == "wrong-value":
        source = context("APPROVED_POLICY=R7")
    elif change == "wrong-hash":
        policy["version"]["sha256"] = "0" * 64
    elif change == "wrong-approval-hash":
        policy["version"]["approval"]["sha256"] = "0" * 64
    elif change == "wrong-approval-version":
        policy["version"]["approval"]["version_id"] = "b" * 32
    elif change == "multiple-markers":
        source = context("APPROVED_POLICY=V3\nAPPROVED_POLICY=R7")
    with pytest.raises(Problem) as exc:
        validate_policy_source(value, source)
    assert exc.value.body["error"]["code"] == "M4_POLICY_SOURCE_MISMATCH"


def test_source_hash_is_not_normalized_when_reading_a_crlf_marker_line():
    source = context("Header\r\nAPPROVED_POLICY=V3\r\n")
    validate_policy_source(validate_answer(json.dumps(answer())), source)
