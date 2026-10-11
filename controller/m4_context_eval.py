"""Offline M4 fixtures and observation checks; never calls Azure or a model."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "fa-shared-context-poc"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from shared_context.m4_fixtures import (
    ALIASES, CASES, CONTEXT_BYTES_LIMIT, GOLD, POLICY_ARTIFACT, POLICY_TEXT,
    POLICY_VERSION, PROMPT, PROMPT_TOKENS_LIMIT, SUITE, encoded, fixtures,
)


def object_value(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def text_value(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be nonempty text")
    return value


def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse(text: str) -> dict:
    return object_value(json.loads(text, object_pairs_hook=reject_duplicate_keys), "document")


def memory_sources(context: dict, field: str, expected_scope: str) -> dict[str, tuple[str, str]]:
    block = object_value(context.get(field), field)
    if block.get("scope") != expected_scope:
        raise ValueError(f"{field} scope mismatch")
    native = object_value(block.get("native"), field + ".native")
    items = native.get("memories")
    if not isinstance(items, list) or len(items) > 10:
        raise ValueError(f"{field} needs a bounded memories array")
    result = {}
    for entry in items:
        item = object_value(object_value(entry, "memory entry").get("memory_item"), "memory_item")
        identity = text_value(item.get("memory_id"), "memory_id")
        content = text_value(item.get("content"), "memory content")
        citation = "memory:" + identity
        if citation in result:
            raise ValueError("duplicate memory citation")
        result[citation] = (field, content)
    return result


def evaluate(case_id: str, observation: dict) -> dict:
    if case_id not in CASES:
        raise ValueError("unknown M4 case")
    if observation.get("case_id") != case_id:
        raise ValueError("observation case mismatch")
    suite_sha = hashlib.sha256(encoded(fixtures())).hexdigest()
    if observation.get("suite_sha256") != suite_sha:
        raise ValueError("observation fixture digest mismatch")
    context_text = text_value(observation.get("context_text"), "context_text")
    context = parse(context_text)
    if context.get("actor") != "agent-a" or context.get("team") != "team-one":
        raise ValueError("context actor/team mismatch")
    answer = object_value(observation.get("answer"), "answer")
    metrics = object_value(observation.get("metrics"), "metrics")
    tokens = metrics.get("model_input_tokens")
    if not isinstance(tokens, list) or not tokens or any(type(n) is not int or n <= 0 for n in tokens):
        raise ValueError("model_input_tokens must contain native positive integer counts")
    for field in ("e2e_ms", "context_ms"):
        value = metrics.get(field)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{field} must be a finite nonnegative measurement")
    if metrics["e2e_ms"] <= 0 or metrics["context_ms"] > metrics["e2e_ms"]:
        raise ValueError("context timing must fit inside the active end-to-end interval")

    work = object_value(context.get("work"), "work")
    work_id = text_value(work.get("work_id"), "work_id")
    revision = work.get("revision")
    if type(revision) is not int or revision < 1:
        raise ValueError("work revision must be a positive integer")
    progress = text_value(work.get("progress"), "work progress")
    sources = {f"work:{work_id}@{revision}": ("work", progress)}
    sources.update(memory_sources(context, "team_memory", "team:team-one"))
    private_sources = memory_sources(context, "private_memory", "private:team-one:agent-a")
    if sources.keys() & private_sources.keys():
        raise ValueError("ambiguous cross-scope memory citation")
    sources.update(private_sources)

    policies = context.get("approved_policy_context")
    if not isinstance(policies, list):
        raise ValueError("approved_policy_context must be an array")
    policy_match = False
    for entry in policies:
        entry = object_value(entry, "policy")
        if entry.get("status") != "approved":
            continue
        version = object_value(entry.get("version"), "policy version")
        approval = object_value(version.get("approval"), "policy approval")
        text = text_value(entry.get("text"), "policy text")
        artifact_id = text_value(entry.get("artifact_id"), "policy artifact_id")
        identity = text_value(version.get("version_id"), "policy version_id")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        valid = (
            approval.get("decision") == "approve" and approval.get("version_id") == identity
            and approval.get("sha256") == digest == version.get("sha256")
        )
        pinned = valid and artifact_id == POLICY_ARTIFACT and identity == POLICY_VERSION and text == POLICY_TEXT
        if pinned:
            sources[f"policy:{artifact_id}@{identity}"] = ("policy", text)
        policy_match |= pinned

    control = object_value(observation.get("privacy_control"), "privacy_control")
    b_context = parse(text_value(control.get("b_context_text"), "b_context_text"))
    if b_context.get("actor") != "agent-b" or b_context.get("team") != "team-one":
        raise ValueError("privacy positive control needs agent B context")
    memory_sources(b_context, "private_memory", "private:team-one:agent-b")
    b_memories = b_context["private_memory"]["native"]["memories"]
    b_seen = any("saffron" in item["memory_item"]["content"].casefold() for item in b_memories)
    denial = object_value(control.get("cross_private_denial"), "cross_private_denial")
    checks = {
        "exact_answer_fields": set(answer) == set(GOLD),
        "approved_policy_version": bool(policy_match),
        "private_positive_control": b_seen,
        "cross_private_denied": denial.get("http_status") == 403 and denial.get("code") == "PRIVATE_TARGET_DENIED",
        "no_private_b_disclosure": "saffron" not in (context_text + json.dumps(answer)).casefold(),
        "context_within_limit": len(context_text.encode("utf-8")) <= CONTEXT_BYTES_LIMIT,
        "model_input_within_limit": max(tokens) <= PROMPT_TOKENS_LIMIT,
    }
    for key, (_, kind) in GOLD.items():
        item = object_value(answer.get(key), "answer." + key)
        value = text_value(item.get("value"), "answer value")
        citation = text_value(item.get("citation"), "answer citation")
        checks[key + "_correct"] = value.strip().casefold() in ALIASES[key]
        source = sources.get(citation)
        checks[key + "_source_reference"] = source is not None and source[0] == kind
        checks[key + "_source_terms"] = source is not None and any(
            re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", source[1].casefold())
            for term in ALIASES[key]
        )

    binding = object_value(observation.get("binding"), "binding")
    for key in ("session_id", "sdk_session_id", "process_marker"):
        text_value(binding.get(key), key)
    lifecycle = CASES[case_id][1]
    if lifecycle == "cold":
        checks["cold_started_from_idle"] = observation.get("pre_start_state") == "idle"
    else:
        previous = object_value(observation.get("previous_binding"), "previous_binding")
        for key in ("session_id", "sdk_session_id", "process_marker"):
            text_value(previous.get(key), "previous_binding." + key)
        checks["same_hosted_session"] = binding["session_id"] == previous["session_id"]
        if lifecycle == "resumed":
            checks["same_sdk_session"] = binding["sdk_session_id"] == previous["sdk_session_id"]
            checks["changed_process"] = binding["process_marker"] != previous["process_marker"]
        else:
            checks["same_warm_process"] = binding["process_marker"] == previous["process_marker"]
            checks["new_sdk_conversation"] = binding["sdk_session_id"] != previous["sdk_session_id"]
    return {
        "case_id": case_id, "suite_sha256": suite_sha, "scope": "offline_observation_check",
        "all_checks_passed": all(checks.values()), "checks": checks,
        "context_bytes": len(context_text.encode("utf-8")), "max_model_input_tokens": max(tokens),
        "e2e_ms": metrics["e2e_ms"], "context_ms": metrics["context_ms"],
        "cloud_evidence_verified": False, "ten_x_performance": "NOT_EVALUATED",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fixture_parser = sub.add_parser("fixtures")
    fixture_parser.add_argument("--part", choices=("manifest", "seed_data", "cases", "oracle"), default="manifest")
    check = sub.add_parser("evaluate")
    check.add_argument("--case", choices=CASES, required=True)
    check.add_argument("--observation", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "fixtures":
            bundle = fixtures()
            result = {
                "schema": 1, "suite_id": SUITE, "cloud_status": "NOT_RUN",
                "suite_sha256": hashlib.sha256(encoded(bundle)).hexdigest(),
                "case_count": len(CASES),
                "short_history_bytes": len(encoded(bundle["seed_data"]["short_team"])),
                "long_history_bytes": len(encoded(bundle["seed_data"]["long_team"])),
            } if args.part == "manifest" else bundle[args.part]
        else:
            result = evaluate(args.case, parse(args.observation.read_text(encoding="utf-8-sig")))
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "INVALID_INPUT", "error": str(exc)}), file=sys.stderr)
        return 2
    sys.stdout.write(encoded(result).decode("utf-8"))
    return 0 if args.command == "fixtures" or result["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
