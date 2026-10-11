"""Validate an append-only evaluation ledger; never executes an agent or grades prose."""
from __future__ import annotations

import argparse
from collections import Counter
import copy
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile

TARGET_IDS = tuple(
    f"{prefix}-{index:02}"
    for prefix, start, stop in (("SP", 0, 32), ("PA", 1, 17), ("TC", 1, 17), ("SK", 1, 13))
    for index in range(start, stop)
)
SCENARIO_IDS = tuple(f"E-{index:02}" for index in range(1, 13))
ENTRY_IDS = TARGET_IDS + SCENARIO_IDS
IMPLEMENTATION = {"unassessed", "planned", "in_progress", "partial", "implemented", "blocked"}
ACCEPTANCE = {"not_evaluated", "partial", "accepted", "blocked"}
KINDS = {
    "checker_probe", "behavior_baseline", "native_probe", "source_review",
    "product_verification", "target_acceptance", "evaluation_tooling",
}
OUTCOMES = {"pass", "fail", "mixed", "blocked", "not_run", "not_graded", "unknown"}
ENVIRONMENTS = {"offline_checker", "local_sdk_offline", "hosted", "source_only"}
RUN_FIELDS = {
    "id", "observed_on", "kind", "execution", "outcome", "coverage", "targets", "baseline", "environment",
    "checks", "human_review", "evidence", "limitations", "summary", "metrics", "versions",
}
VERSION_FIELDS = {"sdk", "runtime", "model", "api_core", "hosted_core", "hosted_version", "platform", "python", "pytest"}
BASELINE = re.compile(r"(?:git:[a-f0-9]{40}|(?:snapshot|core):[a-f0-9]{64})")


def encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def fingerprint(value: object) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def fields(value: object, expected: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} has invalid fields")
    return value


def text(value: object, label: str, maximum: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be bounded text")
    return value


def count(value: object, label: str) -> None:
    require(value is None or type(value) is int and value >= 0, f"{label} must be a nonnegative integer or null")


def choice(value: object, values: set[str], label: str) -> None:
    require(isinstance(value, str) and value in values, f"{label} is invalid")


def unique_strings(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a string array")
    require(len(value) == len(set(value)), f"{label} contains duplicates")
    return value


def parse(raw: str) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("nonfinite JSON number")

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    require(isinstance(value, dict), "document must be an object")
    return value


def scenario_targets(value: str) -> list[str]:
    result = []
    prefix = None
    for part in re.split(r"[、,]", value):
        match = re.fullmatch(r"(?:(SP|PA|TC|SK)-)?(\d{2})(?:[～~-](\d{2}))?", part.strip())
        if match is None:
            raise ValueError("scenario target reference is invalid")
        prefix = match.group(1) or prefix
        require(prefix is not None, "scenario target prefix is missing")
        start = int(match.group(2))
        end = int(match.group(3) or match.group(2))
        require(start <= end, "scenario target range is reversed")
        result.extend(f"{prefix}-{index:02}" for index in range(start, end + 1))
    require(set(result) <= set(TARGET_IDS) and len(result) == len(set(result)), "scenario target references are unknown or duplicated")
    return result


def scenario_judgment(targets: list[str], definitions: dict) -> str:
    kinds = {definitions[identity]["judgment"] for identity in targets}
    return next(iter(kinds)) if len(kinds) == 1 else "M"


def validate_spec(spec: object) -> dict:
    spec = fields(spec, {"version", "common", "targets", "scenarios"}, "specification snapshot")
    text(spec["version"], "specification version", 128)
    text(spec["common"], "common specification", 1000000)
    require(isinstance(spec["targets"], dict) and set(spec["targets"]) == set(TARGET_IDS), "target specification must contain all 76 fixed IDs")
    require(isinstance(spec["scenarios"], dict) and set(spec["scenarios"]) == set(SCENARIO_IDS), "specification must retain all 12 scenarios")
    for definition in spec["targets"].values():
        fields(definition, {"topic", "target", "criteria", "judgment"}, "target definition")
        for key in ("topic", "target", "criteria"):
            text(definition[key], "target definition")
        choice(definition["judgment"], {"C", "H", "M"}, "target judgment")
        require(definition["criteria"].endswith(definition["judgment"]), "target judgment differs from its criterion marker")
    for definition in spec["scenarios"].values():
        fields(definition, {"topic", "criteria", "targets", "judgment"}, "scenario definition")
        text(definition["topic"], "scenario topic")
        text(definition["criteria"], "scenario criteria")
        references = unique_strings(definition["targets"], "scenario targets")
        require(bool(references) and set(references) <= set(TARGET_IDS), "scenario targets are invalid")
        require(definition["judgment"] == scenario_judgment(references, spec["targets"]), "scenario judgment differs from its requirements")
    return spec


def read_spec(path: Path) -> dict:
    source = path.read_text(encoding="utf-8-sig")
    version = re.search(r"(?m)^版本：`([^`]+)`", source)
    if version is None:
        raise ValueError("specification version is missing")
    targets, scenarios, common = {}, {}, []
    section = 0
    for line in source.splitlines():
        heading = re.match(r"^## (\d+)\.", line)
        if heading:
            section = int(heading.group(1))
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line)[1:-1]] if line.startswith("|") else []
        if cells and re.fullmatch(r"(?:SP|PA|TC|SK)-\d{2}", cells[0]):
            require(len(cells) == 4, "target specification row must have four cells")
            identity, topic, target, criteria = cells
            require(identity not in targets, "duplicate target specification ID")
            judgment = re.search(r"([CHM])$", criteria)
            if judgment is None:
                raise ValueError("target specification needs C/H/M judgment")
            targets[identity] = {"topic": topic, "target": target, "criteria": criteria, "judgment": judgment.group(1)}
            continue
        scenario = re.match(r"^(E-\d{2})\s+(.+)$", cells[0]) if cells else None
        if scenario is not None:
            require(len(cells) == 3, "scenario row must have three cells")
            identity, topic = scenario.groups()
            require(identity not in scenarios, "duplicate scenario ID")
            scenarios[identity] = {"topic": topic, "criteria": cells[1], "targets": scenario_targets(cells[2])}
            continue
        # Baseline history (section 2), preamble status, and source index (11) are not normative rules.
        if section == 1 or 3 <= section <= 10:
            common.append(line.rstrip())
    require(set(targets) == set(TARGET_IDS), "target specification must contain all 76 fixed IDs")
    for definition in scenarios.values():
        definition["judgment"] = scenario_judgment(definition["targets"], targets)
    return validate_spec({"version": version.group(1), "common": "\n".join(common).strip(),
                          "targets": targets, "scenarios": scenarios})


def definition(spec: dict, identity: str) -> dict:
    return spec["targets"][identity] if identity in TARGET_IDS else spec["scenarios"][identity]


def entry_fingerprint(spec: dict, identity: str) -> str:
    item = definition(spec, identity)
    dependencies = {target: entry_fingerprint(spec, target) for target in item["targets"]} if identity in SCENARIO_IDS else {}
    return fingerprint({"version": spec["version"], "common": fingerprint(spec["common"]),
                        "definition": item, "dependencies": dependencies})


def empty_states(identities: tuple[str, ...]) -> dict:
    return {identity: {"implementation": "unassessed", "acceptance": "not_evaluated",
                       "current_baseline": None, "evidence": [], "blockers": []} for identity in identities}


def initialize(spec: dict) -> dict:
    validate_spec(spec)
    sha = fingerprint(spec)
    return {"schema_version": 2, "spec_sha256": sha, "specifications": {sha: copy.deepcopy(spec)},
            "targets": empty_states(TARGET_IDS), "scenarios": empty_states(SCENARIO_IDS),
            "runs": [], "run_spec_bindings": {}, "migration": None}


def validate_run(run: object, *, stored: bool, legacy: bool = False) -> dict:
    run = fields(run, RUN_FIELDS | ({"sha256"} if stored else set()), "run")
    require(isinstance(run["id"], str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,95}", run["id"]) is not None, "run ID is invalid")
    observed = text(run["observed_on"], "observation date", 10)
    require(date.fromisoformat(observed).isoformat() == observed, "observation date must be ISO date")
    choice(run["kind"], KINDS, "run kind")
    choice(run["execution"], {"completed", "blocked", "failed", "not_run", "unknown"}, "run execution")
    choice(run["outcome"], OUTCOMES, "run outcome")
    choice(run["coverage"], {"none", "subset", "full_target"}, "run coverage")
    choice(run["environment"], ENVIRONMENTS, "run environment")
    require(run["baseline"] is None or isinstance(run["baseline"], str) and BASELINE.fullmatch(run["baseline"]) is not None,
            "run baseline must be exact or explicitly unknown")
    targets = unique_strings(run["targets"], "run targets")
    require(set(targets) <= set(TARGET_IDS if legacy else ENTRY_IDS), "run refers to an unknown target or scenario")
    require(bool(targets) or run["coverage"] == "none", "covered run needs targets")
    if run["coverage"] == "full_target":
        require(run["kind"] == "target_acceptance", "only explicit target acceptance can claim full coverage")
        require(run["environment"] != "source_only", "source inspection is not full acceptance")
        require(run["baseline"] is not None, "full acceptance needs a known baseline")
    checks = fields(run["checks"], {"passed", "failed", "not_run"}, "checks")
    for key, value in checks.items():
        count(value, "checks." + key)
    if run["outcome"] == "pass":
        require(checks["failed"] in (None, 0), "passing run contains failed checks")
        require(run["execution"] == "completed", "passing run must have completed execution")
    if run["execution"] == "not_run":
        require(run["outcome"] in {"not_run", "not_graded"} and all(value is None for value in checks.values()),
                "not-run evaluation cannot claim observed checks")
    human = fields(run["human_review"], {"scope", "decision", "reviewed"}, "human review")
    choice(human["scope"], {"none", "batch", "target"}, "human review scope")
    choice(human["decision"], {"not_reviewed", "accepted", "rejected"}, "human review decision")
    count(human["reviewed"], "human reviewed")
    if human["scope"] == "none":
        require(human["decision"] == "not_reviewed" and human["reviewed"] is None, "absent human review cannot imply acceptance")
    else:
        require(human["decision"] != "not_reviewed" and type(human["reviewed"]) is int and human["reviewed"] > 0, "human review needs an explicit decision and count")
        if not legacy and human["scope"] == "target":
            require(len(targets) == 1, "target-specific human review must identify exactly one target or scenario")
    text(run["summary"], "run summary")
    limitations = unique_strings(run["limitations"], "limitations")
    for item in limitations:
        text(item, "limitation")
    require(isinstance(run["metrics"], dict), "metrics must be an object")
    for key, value in run["metrics"].items():
        require(isinstance(key, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) is not None, "metric name is invalid")
        require(value is None or type(value) is int and value >= 0
                or type(value) is float and math.isfinite(value) and value >= 0,
                "metric must be finite, nonnegative or null")
    require(isinstance(run["versions"], dict) and set(run["versions"]) <= VERSION_FIELDS, "version fields are invalid")
    for value in run["versions"].values():
        if value is not None:
            text(value, "version", 128)
    require(isinstance(run["evidence"], list) and bool(run["evidence"]), "run needs evidence references")
    for reference in run["evidence"]:
        reference = fields(reference, {"kind", "ref", "sha256"}, "evidence reference")
        choice(reference["kind"], {"repository_report", "withheld_artifact", "test_report"}, "evidence kind")
        ref = text(reference["ref"], "evidence reference", 512)
        if reference["kind"] == "withheld_artifact":
            require(re.fullmatch(r"[a-zA-Z0-9_.-]+", ref) is not None, "withheld reference must be an opaque local-free name")
        else:
            path = ref.split("#", 1)[0]
            require(bool(path) and "\\" not in path and ":" not in path and "?" not in path
                    and not any(ord(char) < 32 for char in ref) and not PurePosixPath(path).is_absolute()
                    and all(part not in {"", ".", ".."} for part in path.split("/")), "report reference must be repository-relative")
        sha = reference["sha256"]
        require(sha is None or isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{64}", sha) is not None, "evidence hash is invalid")
        require(reference["kind"] == "repository_report" or sha is not None, "artifact evidence requires its hash")
    if stored:
        payload = {key: value for key, value in run.items() if key != "sha256"}
        require(run["sha256"] == fingerprint(payload), "run content fingerprint differs")
    return run


def digest_value(value: object, label: str) -> None:
    require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None, f"{label} must be a SHA-256")


def validate_state(value: object, identity: str, runs: dict) -> dict:
    state = fields(value, {"implementation", "acceptance", "current_baseline", "evidence", "blockers"}, "target")
    choice(state["implementation"], IMPLEMENTATION, "target implementation status")
    choice(state["acceptance"], ACCEPTANCE, "target acceptance status")
    baseline = state["current_baseline"]
    require(baseline is None or isinstance(baseline, str) and BASELINE.fullmatch(baseline) is not None, "target baseline is invalid")
    for run_id in unique_strings(state["evidence"], "target evidence"):
        require(run_id in runs and identity in runs[run_id]["targets"], "target evidence is missing or belongs elsewhere")
    for blocker in unique_strings(state["blockers"], "target blockers"):
        text(blocker, "blocker")
    if state["implementation"] in {"partial", "implemented"}:
        require(baseline is not None, "implemented or partial target needs its current baseline")
    if state["acceptance"] == "partial":
        require(bool(state["evidence"]), "partial acceptance needs scoped evidence")
    return state


def validate_legacy(document: object) -> dict:
    document = fields(document, {"schema_version", "spec_sha256", "targets", "runs"}, "legacy registry")
    require(type(document["schema_version"]) is int and document["schema_version"] == 1, "legacy registry version is unsupported")
    digest_value(document["spec_sha256"], "legacy specification fingerprint")
    require(isinstance(document["targets"], dict) and set(document["targets"]) == set(TARGET_IDS), "legacy registry must retain 76 targets")
    require(isinstance(document["runs"], list), "legacy runs must be an array")
    runs = {}
    for item in document["runs"]:
        run = validate_run(item, stored=True, legacy=True)
        require(run["id"] not in runs, "duplicate legacy run ID")
        runs[run["id"]] = run
    for identity, state in document["targets"].items():
        validate_state(state, identity, runs)
    return document


def unresolved(run: dict) -> bool:
    return (run["human_review"]["decision"] == "rejected"
            or run["kind"] in {"product_verification", "target_acceptance"}
            and run["outcome"] in {"fail", "mixed", "blocked", "unknown"})


def matches_entry(document: dict, run: dict, identity: str, spec: dict, baseline: str | None) -> bool:
    binding = document["run_spec_bindings"][run["id"]]
    return (identity in run["targets"] and run["baseline"] == baseline and binding is not None
            and binding["entry_sha256"][identity] == entry_fingerprint(spec, identity))


def validate(document: object, spec: dict | None = None, previous: dict | None = None) -> dict:
    if isinstance(document, dict) and document.get("schema_version") == 1:
        raise ValueError("registry schema 1 requires explicit migrate")
    document = fields(document, {"schema_version", "spec_sha256", "specifications", "targets", "scenarios",
                                "runs", "run_spec_bindings", "migration"}, "registry")
    require(type(document["schema_version"]) is int and document["schema_version"] == 2, "registry schema version is unsupported; migrate explicitly")
    archive = document["specifications"]
    require(isinstance(archive, dict) and bool(archive), "specification archive is required")
    for sha, snapshot in archive.items():
        validate_spec(snapshot)
        require(sha == fingerprint(snapshot), "archived specification fingerprint differs")
    current_sha = document["spec_sha256"]
    require(isinstance(current_sha, str) and current_sha in archive, "current specification is not archived")
    current = archive[current_sha]
    if spec is not None:
        validate_spec(spec)
        require(current_sha == fingerprint(spec), "target specification fingerprint differs; use sync-spec")
    require(isinstance(document["targets"], dict) and set(document["targets"]) == set(TARGET_IDS), "registry must retain all 76 targets")
    require(isinstance(document["scenarios"], dict) and set(document["scenarios"]) == set(SCENARIO_IDS), "registry must retain all 12 scenarios")
    require(isinstance(document["runs"], list), "runs must be an array")
    migration = document["migration"]
    legacy_ids = []
    if migration is not None:
        fields(migration, {"from_schema", "registry_sha256", "legacy_spec_sha256", "legacy_run_ids"}, "migration")
        require(type(migration["from_schema"]) is int and migration["from_schema"] == 1, "migration source version is invalid")
        digest_value(migration["registry_sha256"], "migration registry fingerprint")
        digest_value(migration["legacy_spec_sha256"], "migration specification fingerprint")
        legacy_ids = unique_strings(migration["legacy_run_ids"], "legacy run IDs")
    bindings = document["run_spec_bindings"]
    require(isinstance(bindings, dict), "run specification bindings must be an object")
    runs = {}
    for item in document["runs"]:
        require(isinstance(item, dict), "run must be an object")
        run = validate_run(item, stored=True, legacy=item.get("id") in legacy_ids)
        require(run["id"] not in runs, "duplicate run ID")
        runs[run["id"]] = run
    require(list(runs)[:len(legacy_ids)] == legacy_ids, "legacy run prefix was reordered")
    require(set(bindings) == set(runs), "each run needs its immutable specification binding")
    for sequence, (run_id, run) in enumerate(runs.items()):
        binding = bindings[run_id]
        if run_id in legacy_ids:
            require(binding is None, "legacy unbound evidence cannot be retroactively rebound")
            continue
        fields(binding, {"spec_sha256", "entry_sha256", "sequence"}, "run specification binding")
        require(type(binding["sequence"]) is int and binding["sequence"] == sequence, "recorded run sequence differs")
        recorded_sha = binding["spec_sha256"]
        require(isinstance(recorded_sha, str) and recorded_sha in archive, "recorded specification is missing")
        expected = {identity: entry_fingerprint(archive[recorded_sha], identity) for identity in run["targets"]}
        require(binding["entry_sha256"] == expected, "run clause/scenario fingerprints differ from its recorded specification")
    if previous is not None:
        if previous.get("schema_version") == 1:
            validate_legacy(previous)
            require(migration is not None and migration["registry_sha256"] == fingerprint(previous)
                    and migration["legacy_spec_sha256"] == previous["spec_sha256"]
                    and legacy_ids == [run["id"] for run in previous["runs"]], "migration predecessor differs")
        else:
            validate(previous)
            require(migration == previous["migration"], "historical migration cannot change")
            for sha, snapshot in previous["specifications"].items():
                require(archive.get(sha) == snapshot, "historical specifications cannot be removed or rewritten")
            for run_id, binding in previous["run_spec_bindings"].items():
                require(bindings.get(run_id) == binding, "historical run specification binding changed")
        require(document["runs"][:len(previous["runs"])] == previous["runs"],
                "historical run prefix cannot be removed, reordered, inserted into or rewritten")
    for identity, value in {**document["targets"], **document["scenarios"]}.items():
        state = validate_state(value, identity, runs)
        if state["acceptance"] != "accepted":
            continue
        baseline = state["current_baseline"]
        require(state["implementation"] == "implemented" and not state["blockers"] and baseline is not None,
                "accepted target must be implemented, unblocked and versioned")
        relevant = [(index, run) for index, run in enumerate(document["runs"])
                    if matches_entry(document, run, identity, current, baseline)]
        cutoff = max((index for index, run in relevant if unresolved(run)), default=-1)
        observed_cutoff = max((run["observed_on"] for _, run in relevant if unresolved(run)), default="0001-01-01")
        candidates = [run for index, run in relevant if index > cutoff and run["observed_on"] >= observed_cutoff
                      and run["id"] in state["evidence"]
                      and run["kind"] == "target_acceptance" and run["coverage"] == "full_target"
                      and run["outcome"] == "pass" and not unresolved(run)]
        require(bool(candidates), "accepted target needs fresh full-target evidence at its current baseline and specification; newer unresolved results cannot be ignored")
        judgment = definition(current, identity)["judgment"]
        if judgment in {"C", "M"}:
            require(any(type(run["checks"]["passed"]) is int and run["checks"]["passed"] > 0
                        and run["checks"]["failed"] == 0 and run["checks"]["not_run"] == 0
                        for run in candidates), "accepted target lacks fresh complete programmatic checks")
        if judgment in {"H", "M"}:
            require(any(run["human_review"]["scope"] == "target"
                        and run["human_review"]["decision"] == "accepted" for run in candidates),
                    "batch review or old review is not fresh target-specific human acceptance")
    return document


def migrate(document: dict, spec: dict) -> dict:
    validate_legacy(document)
    validate_spec(spec)
    require(document["spec_sha256"] == fingerprint(spec["targets"]),
            "migrate legacy data using its original target tables, then sync-spec")
    result = initialize(spec)
    result["targets"] = copy.deepcopy(document["targets"])
    result["runs"] = copy.deepcopy(document["runs"])
    result["run_spec_bindings"] = {run["id"]: None for run in result["runs"]}
    result["migration"] = {"from_schema": 1, "registry_sha256": fingerprint(document),
                           "legacy_spec_sha256": document["spec_sha256"], "legacy_run_ids": list(result["run_spec_bindings"])}
    for state in result["targets"].values():
        if state["acceptance"] == "accepted":
            state["acceptance"] = "partial"
            state["blockers"].append("legacy evidence has no complete specification binding")
    return validate(result, spec, document)


def sync_spec(document: dict, spec: dict) -> dict:
    validate(document)
    validate_spec(spec)
    result = copy.deepcopy(document)
    old = result["specifications"][result["spec_sha256"]]
    sha = fingerprint(spec)
    result["specifications"][sha] = copy.deepcopy(spec)
    result["spec_sha256"] = sha
    for identity, state in {**result["targets"], **result["scenarios"]}.items():
        if entry_fingerprint(old, identity) != entry_fingerprint(spec, identity):
            state["acceptance"] = "not_evaluated"
            state["implementation"] = "unassessed"
            reason = "specification changed: " + sha
            if reason not in state["blockers"]:
                state["blockers"].append(reason)
    return validate(result, spec, document)


def append_run(document: dict, run: dict, spec: dict, *, recorded_spec_sha256: str) -> dict:
    validate(document, spec)
    validate_run(run, stored=False)
    require(isinstance(recorded_spec_sha256, str) and recorded_spec_sha256 in document["specifications"],
            "recorded specification must already be archived")
    recorded = document["specifications"][recorded_spec_sha256]
    binding = {"spec_sha256": recorded_spec_sha256,
               "entry_sha256": {identity: entry_fingerprint(recorded, identity) for identity in run["targets"]}}
    stored = {**copy.deepcopy(run), "sha256": fingerprint(run)}
    existing = next((item for item in document["runs"] if item["id"] == run["id"]), None)
    if existing is not None:
        binding["sequence"] = document["runs"].index(existing)
        require(existing == stored and document["run_spec_bindings"][run["id"]] == binding,
                "run ID and recorded specification are immutable; use a new ID")
        return copy.deepcopy(document)
    result = copy.deepcopy(document)
    binding["sequence"] = len(result["runs"])
    result["runs"].append(stored)
    result["run_spec_bindings"][run["id"]] = binding
    for identity in run["targets"]:
        state = result["targets"][identity] if identity in TARGET_IDS else result["scenarios"][identity]
        state["evidence"].append(run["id"])
        if state["acceptance"] == "accepted" and unresolved(run) and matches_entry(result, stored, identity, spec, state["current_baseline"]):
            state["acceptance"] = "partial"
            state["blockers"].append("unresolved evaluation: " + run["id"])
    return validate(result, spec, document)


def summary(document: dict, spec: dict) -> dict:
    validate(document, spec)
    return {
        "target_count": len(TARGET_IDS),
        "scenario_count": len(SCENARIO_IDS),
        "spec_sha256": document["spec_sha256"],
        "specification_revisions": len(document["specifications"]),
        "declared_full_target_acceptances": sum(t["acceptance"] == "accepted" for t in document["targets"].values()),
        "declared_full_scenario_acceptances": sum(t["acceptance"] == "accepted" for t in document["scenarios"].values()),
        "implementation": dict(sorted(Counter(t["implementation"] for t in document["targets"].values()).items())),
        "acceptance": dict(sorted(Counter(t["acceptance"] for t in document["targets"].values()).items())),
        "scenario_acceptance": dict(sorted(Counter(t["acceptance"] for t in document["scenarios"].values()).items())),
        "runs": [{"id": run["id"], "observed_on": run["observed_on"], "kind": run["kind"],
                  "execution": run["execution"], "outcome": run["outcome"],
                  "coverage": run["coverage"], "baseline": run["baseline"], "environment": run["environment"],
                  "checks": run["checks"], "human_review": run["human_review"],
                  "specification_bound": document["run_spec_bindings"][run["id"]] is not None}
                 for run in document["runs"]],
        "overall_model_quality": "NOT_COMPUTED",
        "evidence_authenticity": "NOT_VERIFIED_BY_THIS_VALIDATOR",
    }


def save(path: Path, document: dict, *, expected: bytes | None = None) -> None:
    content = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if expected is None:
        with path.open("xb") as stream:
            stream.write(content)
        return
    require(path.read_bytes() == expected, "registry changed while preparing the update")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        # One writer owns the ledger; replacement prevents a partial JSON file, not a distributed CAS.
        require(path.read_bytes() == expected, "registry changed while preparing the update")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=Path("docs/grok-aligned-target-state.md"))
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--registry", type=Path, required=True)
    for name in ("validate", "summary", "record", "migrate", "sync-spec"):
        command = sub.add_parser(name)
        command.add_argument("--registry", type=Path, required=True)
        if name == "validate":
            command.add_argument("--previous", type=Path)
        if name == "record":
            command.add_argument("--run", type=Path, required=True)
            command.add_argument("--recorded-spec", required=True, help="Fingerprint of the specification used for this evaluation")
    args = parser.parse_args()
    try:
        spec = read_spec(args.spec)
        if args.command == "init":
            document = initialize(spec)
            save(args.registry, document)
        else:
            raw = args.registry.read_bytes()
            document = parse(raw.decode("utf-8-sig"))
            previous = None
            if args.command == "validate" and args.previous is not None:
                previous = parse(args.previous.read_text(encoding="utf-8-sig"))
            if args.command == "migrate":
                updated = migrate(document, spec)
            elif args.command == "sync-spec":
                updated = sync_spec(document, spec)
            else:
                validate(document, spec, previous)
                updated = document
            if args.command == "record":
                updated = append_run(document, parse(args.run.read_text(encoding="utf-8-sig")), spec,
                                     recorded_spec_sha256=args.recorded_spec)
            if updated != document:
                save(args.registry, updated, expected=raw)
                document = updated
        result = summary(document, spec)
    except (ValueError, OSError) as error:
        print(json.dumps({"status": "INVALID_INPUT", "error": str(error)}), file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
