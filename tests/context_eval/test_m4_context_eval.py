import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / "controller" / "m4_context_eval.py"
SPEC = importlib.util.spec_from_file_location("m4_context_eval", PATH)
m4 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m4)


def memory(scope, identity, content):
    return {"scope": scope, "native": {"memories": [
        {"memory_item": {"memory_id": identity, "content": content}},
    ]}}


@pytest.fixture
def observation():
    oracle = m4.fixtures()["oracle"]
    context = {
        "actor": "agent-a", "team": "team-one",
        "work": {"work_id": "m4-work", "revision": 2, "progress": "The confirmed owner is Mira."},
        "team_memory": memory("team:team-one", "team-fact", "Lyra prefers compact report layouts."),
        "private_memory": memory("private:team-one:agent-a", "private-a", "A prefers cobalt."),
        "approved_policy_context": [{
            "artifact_id": "m3-artifacts-01-sop-repair", "status": "approved", "text": m4.POLICY_TEXT,
            "version": {
                "version_id": m4.POLICY_VERSION, "sha256": oracle["policy_sha256"],
                "approval": {"decision": "approve", "version_id": m4.POLICY_VERSION, "sha256": oracle["policy_sha256"]},
            },
        }],
    }
    sources = {
        "work": "work:m4-work@2", "team_memory": "memory:team-fact",
        "private_memory": "memory:private-a",
        "policy": "policy:m3-artifacts-01-sop-repair@" + m4.POLICY_VERSION,
    }
    return {
        "case_id": "short-cold", "suite_sha256": hashlib.sha256(m4.encoded(m4.fixtures())).hexdigest(),
        "context_text": json.dumps(context),
        "answer": {key: {"value": value, "citation": sources[kind]} for key, (value, kind) in m4.GOLD.items()},
        "metrics": {"model_input_tokens": [800, 2400, 2800], "e2e_ms": 2000, "context_ms": 500},
        "binding": {"session_id": "synthetic-hosted", "sdk_session_id": "synthetic-sdk", "process_marker": "first"},
        "pre_start_state": "idle",
        "privacy_control": {
            "b_context_text": json.dumps({
                "actor": "agent-b", "team": "team-one",
                "private_memory": memory("private:team-one:agent-b", "private-b", "B prefers saffron."),
            }),
            "cross_private_denial": {"http_status": 403, "code": "PRIVATE_TARGET_DENIED"},
        },
    }


def test_fixtures_are_repeatable_and_long_history_is_measured():
    first, second = m4.fixtures(), m4.fixtures()
    assert m4.encoded(first) == m4.encoded(second)
    assert len(first["seed_data"]["long_team"]) == 128
    assert 32768 <= len(m4.encoded(first["seed_data"]["long_team"])) <= 65536
    assert len(m4.encoded(first["seed_data"]["short_team"])) < 4096
    for case in first["cases"]:
        for value, _ in m4.GOLD.values():
            assert value.casefold() not in case["prompt"].casefold()


def test_shared_fixture_is_the_approved_source():
    from shared_context.m4_fixtures import fixtures

    assert m4.fixtures is fixtures
    assert hashlib.sha256(m4.encoded(fixtures())).hexdigest() == (
        "8f37192bd3c9aa93d4f7855eb5ec133ba368d136772bba96147c4cf19178c090"
    )
    assert len(m4.encoded(fixtures()["seed_data"]["short_team"])) == 272
    assert len(m4.encoded(fixtures()["seed_data"]["long_team"])) == 52147


def test_valid_fixture_is_not_cloud_or_ten_x_proof(observation):
    result = m4.evaluate("short-cold", observation)
    assert result["all_checks_passed"]
    assert result["cloud_evidence_verified"] is False
    assert result["ten_x_performance"] == "NOT_EVALUATED"


@pytest.mark.parametrize("mutation", [
    lambda o: o["answer"]["decision_owner"].update(value="owen"),
    lambda o: o["answer"]["decision_owner"].update(citation="memory:team-fact"),
    lambda o: o["answer"]["report_layout"].update(citation="memory:invented"),
    lambda o: o["answer"]["private_theme"].update(value="saffron"),
    lambda o: o["metrics"].update(model_input_tokens=[8193]),
    lambda o: o.update(context_text=o["context_text"] + " " * 32768),
    lambda o: o["privacy_control"]["cross_private_denial"].update(http_status=200),
    lambda o: o["privacy_control"].update(b_context_text=o["privacy_control"]["b_context_text"].replace("saffron", "missing")),
    lambda o: o.update(pre_start_state="active"),
])
def test_quality_or_boundary_failure_is_not_pass(observation, mutation):
    mutation(observation)
    assert not m4.evaluate("short-cold", observation)["all_checks_passed"]


def test_unknown_or_unapproved_policy_fails(observation):
    context = json.loads(observation["context_text"])
    context["approved_policy_context"][0]["version"]["approval"]["decision"] = "reject"
    observation["context_text"] = json.dumps(context)
    result = m4.evaluate("short-cold", observation)
    assert not result["checks"]["approved_policy_version"]
    assert not result["checks"]["policy_marker_source_reference"]


def test_scope_mismatch_is_explicit_invalid_input(observation):
    context = json.loads(observation["context_text"])
    context["private_memory"]["scope"] = "private:team-one:agent-b"
    observation["context_text"] = json.dumps(context)
    with pytest.raises(ValueError, match="scope mismatch"):
        m4.evaluate("short-cold", observation)


@pytest.mark.parametrize("tokens", [[], [True], [-1], [None], "2400"])
def test_native_token_counts_are_required(observation, tokens):
    observation["metrics"]["model_input_tokens"] = tokens
    with pytest.raises(ValueError, match="native positive integer"):
        m4.evaluate("short-cold", observation)


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), -1, True])
def test_invalid_latency_is_not_silently_accepted(observation, duration):
    observation["metrics"]["context_ms"] = duration
    with pytest.raises(ValueError, match="finite nonnegative"):
        m4.evaluate("short-cold", observation)


def test_missing_answer_is_invalid_instead_of_success_shaped(observation):
    del observation["answer"]["decision_owner"]
    with pytest.raises(ValueError, match="answer.decision_owner"):
        m4.evaluate("short-cold", observation)


def test_duplicate_json_key_is_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        m4.parse('{"actor":"agent-b","actor":"agent-a"}')


def test_fixture_mismatch_is_rejected(observation):
    observation["suite_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="fixture digest mismatch"):
        m4.evaluate("short-cold", observation)


def test_citation_must_contain_supporting_terms(observation):
    context = json.loads(observation["context_text"])
    context["team_memory"]["native"]["memories"][0]["memory_item"]["content"] = "An unrelated packing list."
    observation["context_text"] = json.dumps(context)
    result = m4.evaluate("short-cold", observation)
    assert result["checks"]["report_layout_source_reference"]
    assert not result["checks"]["report_layout_source_terms"]
    assert not result["all_checks_passed"]


def test_known_paraphrase_is_not_a_format_failure(observation):
    observation["answer"]["report_layout"]["value"] = "concise"
    context = json.loads(observation["context_text"])
    context["team_memory"]["native"]["memories"][0]["memory_item"]["content"] = "Lyra prefers concise reports."
    observation["context_text"] = json.dumps(context)
    assert m4.evaluate("short-cold", observation)["all_checks_passed"]


def test_policy_citation_is_bound_to_configured_artifact(observation):
    context = json.loads(observation["context_text"])
    context["approved_policy_context"][0]["artifact_id"] = "different-sop"
    observation["context_text"] = json.dumps(context)
    observation["answer"]["policy_marker"]["citation"] = "policy:different-sop@" + m4.POLICY_VERSION
    result = m4.evaluate("short-cold", observation)
    assert not result["checks"]["approved_policy_version"]
    assert not result["checks"]["policy_marker_source_reference"]


@pytest.mark.parametrize("case", ["short-warm", "long-warm", "long-new-conversation", "long-resumed"])
def test_lifecycle_requires_native_binding_relationships(observation, case):
    observation["case_id"] = case
    observation["previous_binding"] = copy.deepcopy(observation["binding"])
    if case == "long-resumed":
        observation["previous_binding"]["process_marker"] = "before-stop"
    else:
        observation["previous_binding"]["sdk_session_id"] = "previous-sdk"
    assert m4.evaluate(case, observation)["all_checks_passed"]
    observation["previous_binding"]["session_id"] = "different-hosted"
    assert not m4.evaluate(case, observation)["all_checks_passed"]


def test_limits_include_exact_boundary(observation):
    observation["metrics"]["model_input_tokens"] = [8192]
    size = len(observation["context_text"].encode("utf-8"))
    observation["context_text"] += " " * (32768 - size)
    result = m4.evaluate("short-cold", observation)
    assert result["all_checks_passed"] and result["context_bytes"] == 32768
