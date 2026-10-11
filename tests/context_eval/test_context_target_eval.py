import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "controller" / "context_target_eval.py"
SPEC = importlib.util.spec_from_file_location("context_target_eval", PATH)
ledger = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ledger)


@pytest.fixture
def spec():
    return ledger.read_spec(ROOT / "docs" / "grok-aligned-target-state.md")


@pytest.fixture
def run():
    return {
        "id": "synthetic-registry-check", "observed_on": "2026-09-24",
        "kind": "product_verification", "execution": "completed", "outcome": "pass",
        "coverage": "subset", "targets": ["TC-11"], "baseline": "git:" + "a" * 40,
        "environment": "local_sdk_offline", "checks": {"passed": 4, "failed": 0, "not_run": 0},
        "human_review": {"scope": "none", "decision": "not_reviewed", "reviewed": None},
        "evidence": [{"kind": "test_report", "ref": "docs/evaluations/synthetic-report.json", "sha256": "b" * 64}],
        "summary": "Synthetic validator fixture; not an actual agent evaluation.",
        "limitations": ["Offline synthetic fixture only."],
        "metrics": {"model_requests": None}, "versions": {"sdk": "1.0.13", "runtime": "1.0.83"},
    }


def record(document, run, spec, recorded_spec=None):
    return ledger.append_run(document, run, spec, recorded_spec_sha256=recorded_spec or ledger.fingerprint(spec))


def accepted_state(spec, run, identity="TC-11"):
    run = copy.deepcopy(run)
    run.update(kind="target_acceptance", coverage="full_target")
    run["targets"] = [identity]
    document = record(ledger.initialize(spec), run, spec)
    states = document["targets"] if identity in ledger.TARGET_IDS else document["scenarios"]
    states[identity].update(
        implementation="implemented", acceptance="accepted",
        current_baseline=run["baseline"], evidence=[run["id"]],
    )
    return document


def test_all_76_targets_are_present_and_unassessed(spec):
    document = ledger.initialize(spec)
    result = ledger.summary(document, spec)
    assert len(spec["targets"]) == result["target_count"] == 76
    assert len(spec["scenarios"]) == result["scenario_count"] == 12
    assert result["implementation"] == {"unassessed": 76}
    assert result["declared_full_target_acceptances"] == 0
    assert result["overall_model_quality"] == "NOT_COMPUTED"
    assert result["evidence_authenticity"] == "NOT_VERIFIED_BY_THIS_VALIDATOR"


def test_spec_change_or_missing_target_cannot_be_silently_accepted(spec):
    document = ledger.initialize(spec)
    changed = copy.deepcopy(spec)
    changed["targets"]["TC-11"]["target"] += " Changed criterion."
    with pytest.raises(ValueError, match="fingerprint"):
        ledger.validate(document, changed)
    del document["targets"]["SP-07"]
    with pytest.raises(ValueError, match="76"):
        ledger.validate(document, spec)


def test_spec_duplicate_is_not_a_replacement(tmp_path):
    source = (ROOT / "docs" / "grok-aligned-target-state.md").read_text(encoding="utf-8")
    row = next(line for line in source.splitlines() if line.startswith("| TC-11 |"))
    path = tmp_path / "spec.md"
    path.write_text(source + "\n" + row, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        ledger.read_spec(path)


def test_record_is_append_only_and_identical_retry_is_idempotent(spec, run):
    initial = ledger.initialize(spec)
    saved = record(initial, run, spec)
    assert initial["runs"] == []
    assert record(saved, run, spec) == saved
    changed = copy.deepcopy(run)
    changed["summary"] = "Rewritten historical result"
    with pytest.raises(ValueError, match="immutable"):
        record(saved, changed, spec)
    removed = copy.deepcopy(saved)
    removed["runs"] = []
    removed["run_spec_bindings"] = {}
    with pytest.raises(ValueError, match="historical"):
        ledger.validate(removed, spec, saved)


def test_changed_stored_result_fingerprint_is_rejected(spec, run):
    document = record(ledger.initialize(spec), run, spec)
    document["runs"][0]["outcome"] = "fail"
    with pytest.raises(ValueError, match="fingerprint"):
        ledger.validate(document, spec)


def test_subset_pass_never_becomes_full_target_acceptance(spec, run):
    document = record(ledger.initialize(spec), run, spec)
    document["targets"]["TC-11"].update(
        implementation="implemented", acceptance="accepted",
        current_baseline=run["baseline"], evidence=[run["id"]],
    )
    with pytest.raises(ValueError, match="full-target"):
        ledger.validate(document, spec)


def test_full_target_declaration_requires_current_baseline_and_no_blockers(spec, run):
    document = accepted_state(spec, run)
    assert ledger.summary(document, spec)["declared_full_target_acceptances"] == 1
    document["targets"]["TC-11"]["current_baseline"] = "git:" + "c" * 40
    with pytest.raises(ValueError, match="current baseline"):
        ledger.validate(document, spec)
    document = accepted_state(spec, run)
    document["targets"]["TC-11"]["blockers"] = ["Hosted path not verified"]
    with pytest.raises(ValueError, match="unblocked"):
        ledger.validate(document, spec)


def test_new_regression_downgrades_acceptance_without_erasing_history(spec, run):
    document = accepted_state(spec, run)
    regression = copy.deepcopy(run)
    regression.update(id="new-regression", outcome="fail")
    regression["checks"] = {"passed": 3, "failed": 1, "not_run": 0}
    updated = record(document, regression, spec)
    assert updated["targets"]["TC-11"]["acceptance"] == "partial"
    assert updated["targets"]["TC-11"]["blockers"] == ["unresolved evaluation: new-regression"]
    assert updated["runs"][0] == document["runs"][0]
    updated["targets"]["TC-11"].update(acceptance="accepted", blockers=[])
    with pytest.raises(ValueError, match="newer unresolved"):
        ledger.validate(updated, spec)


def test_human_batch_acceptance_is_not_individual_target_gold(spec, run):
    run.update(kind="target_acceptance", coverage="full_target", targets=["SP-02"], environment="hosted")
    run["human_review"] = {"scope": "batch", "decision": "accepted", "reviewed": 12}
    document = record(ledger.initialize(spec), run, spec)
    document["targets"]["SP-02"].update(
        implementation="implemented", acceptance="accepted",
        current_baseline=run["baseline"], evidence=[run["id"]],
    )
    with pytest.raises(ValueError, match="batch review"):
        ledger.validate(document, spec)


@pytest.mark.parametrize("value", [True, -1, "4", 1.5])
def test_invalid_check_counts_are_explicit_errors(spec, run, value):
    run["checks"]["passed"] = value
    with pytest.raises(ValueError, match="integer or null"):
        record(ledger.initialize(spec), run, spec)


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "unknown"])
def test_invalid_metrics_are_not_zero_or_success(spec, run, value):
    run["metrics"]["model_requests"] = value
    with pytest.raises(ValueError, match="finite"):
        record(ledger.initialize(spec), run, spec)


def test_missing_measurements_stay_unknown(spec, run):
    run["checks"] = {"passed": None, "failed": None, "not_run": None}
    run["outcome"] = "not_graded"
    saved = record(ledger.initialize(spec), run, spec)
    assert ledger.summary(saved, spec)["runs"][0]["checks"]["passed"] is None
    assert saved["runs"][0]["metrics"]["model_requests"] is None


def test_unknown_historical_baseline_is_preserved_but_cannot_support_acceptance(spec, run):
    run["baseline"] = None
    saved = record(ledger.initialize(spec), run, spec)
    assert ledger.summary(saved, spec)["runs"][0]["baseline"] is None
    run.update(kind="target_acceptance", coverage="full_target")
    with pytest.raises(ValueError, match="known baseline"):
        record(ledger.initialize(spec), run, spec)


@pytest.mark.parametrize("reference", ["C:\\private\\trace.json", "../trace.json", "ghapp://sessions/test", "/private/trace.json"])
def test_evidence_references_do_not_publish_local_paths(spec, run, reference):
    run["evidence"][0]["ref"] = reference
    with pytest.raises(ValueError, match="repository-relative"):
        record(ledger.initialize(spec), run, spec)


def test_pass_cannot_hide_failed_checks(spec, run):
    run["checks"]["failed"] = 1
    with pytest.raises(ValueError, match="failed checks"):
        record(ledger.initialize(spec), run, spec)


def test_unknown_schema_duplicate_ids_and_na_status_are_rejected(spec, run):
    with pytest.raises(ValueError, match="duplicate"):
        ledger.parse('{"schema_version":1,"schema_version":1}')
    with pytest.raises(ValueError, match="nonfinite"):
        ledger.parse('{"value": NaN}')
    document = ledger.initialize(spec)
    document["targets"]["SP-07"]["acceptance"] = "not_applicable"
    with pytest.raises(ValueError, match="status"):
        ledger.validate(document, spec)
    run["raw_messages"] = []
    with pytest.raises(ValueError, match="fields"):
        record(ledger.initialize(spec), run, spec)


@pytest.mark.parametrize("field", ["kind", "execution", "outcome", "coverage", "environment"])
def test_unhashable_enum_inputs_are_invalid_instead_of_crashing(spec, run, field):
    run[field] = []
    with pytest.raises(ValueError, match="invalid"):
        record(ledger.initialize(spec), run, spec)


def test_huge_integer_metric_is_finite_without_float_conversion(spec, run):
    run["metrics"]["items"] = 10 ** 400
    saved = record(ledger.initialize(spec), run, spec)
    assert saved["runs"][0]["metrics"]["items"] == 10 ** 400


def test_not_run_and_execution_failure_do_not_become_pass(spec, run):
    run["execution"] = "not_run"
    with pytest.raises(ValueError, match="completed execution"):
        record(ledger.initialize(spec), run, spec)
    run["outcome"] = "not_graded"
    with pytest.raises(ValueError, match="not-run"):
        record(ledger.initialize(spec), run, spec)


def test_observation_date_is_explicit_and_valid(spec, run):
    run["observed_on"] = "2026-02-30"
    with pytest.raises(ValueError):
        record(ledger.initialize(spec), run, spec)


def test_single_writer_update_checks_predecessor_and_preserves_valid_json(tmp_path, spec):
    path = tmp_path / "ledger.json"
    document = ledger.initialize(spec)
    ledger.save(path, document)
    before = path.read_bytes()
    updated = copy.deepcopy(document)
    updated["targets"]["TC-11"]["implementation"] = "in_progress"
    ledger.save(path, updated, expected=before)
    assert ledger.parse(path.read_text(encoding="utf-8")) == updated
    with pytest.raises(ValueError, match="changed"):
        ledger.save(path, document, expected=before)
    with pytest.raises(FileExistsError):
        ledger.save(path, document)
    assert list(tmp_path.iterdir()) == [path]


def test_cli_invalid_run_preserves_registry_and_reports_error(tmp_path, spec, run):
    path = tmp_path / "ledger.json"
    ledger.save(path, ledger.initialize(spec))
    before = path.read_bytes()
    run["checks"]["failed"] = 1
    packet = tmp_path / "run.json"
    packet.write_text(json.dumps(run), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(PATH), "record", "--registry", str(path), "--run", str(packet),
         "--recorded-spec", ledger.fingerprint(spec)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert json.loads(result.stderr)["status"] == "INVALID_INPUT"
    assert path.read_bytes() == before


def test_committed_ledger_preserves_the_historical_strict_failure_and_batch_review(spec):
    path = ROOT / "docs" / "context-evaluation-status.json"
    document = ledger.parse(path.read_text(encoding="utf-8"))
    ledger.validate(document, spec)
    round2 = next(run for run in document["runs"] if run["id"] == "contrast-round2-20260923")
    assert round2["checks"] == {"passed": 8, "failed": 1, "not_run": 0}
    assert round2["human_review"] == {"scope": "batch", "decision": "accepted", "reviewed": 12}
    assert round2["coverage"] == "subset" and round2["outcome"] == "mixed"
    assert round2["metrics"]["user_turns"] == 12
    assert round2["metrics"]["model_requests"] == 13


@pytest.mark.parametrize("position", ["reverse", "prepend"])
def test_previous_rejects_reordered_or_inserted_history(spec, run, position):
    before = accepted_state(spec, run)
    failed = copy.deepcopy(run)
    failed.update(id="later-failure", outcome="fail")
    failed["checks"]["failed"] = 1
    after = record(before, failed, spec)
    previous = after if position == "reverse" else before
    forged = copy.deepcopy(after)
    forged["runs"].reverse()
    forged["targets"]["TC-11"].update(acceptance="accepted", blockers=[])
    with pytest.raises(ValueError, match="sequence|historical run prefix"):
        ledger.validate(forged, spec, previous)
    with pytest.raises(ValueError, match="sequence"):
        ledger.validate(forged, spec)


@pytest.mark.parametrize("outcome", ["pass", "not_graded"])
def test_later_human_rejection_reopens_acceptance_without_rewriting_objective_result(spec, run, outcome):
    original = copy.deepcopy(run)
    original["human_review"] = {"scope": "target", "decision": "accepted", "reviewed": 1}
    document = accepted_state(spec, original, "SP-02")
    rejected = copy.deepcopy(run)
    rejected.update(id="human-rejected", targets=["SP-02"], outcome=outcome)
    rejected["human_review"] = {"scope": "target", "decision": "rejected", "reviewed": 1}
    updated = record(document, rejected, spec)
    assert updated["runs"][-1]["outcome"] == outcome
    assert updated["targets"]["SP-02"]["acceptance"] == "partial"
    updated["targets"]["SP-02"].update(acceptance="accepted", blockers=[])
    with pytest.raises(ValueError, match="fresh full-target"):
        ledger.validate(updated, spec, document)


def test_rejected_human_review_cannot_support_a_c_target_acceptance(spec, run):
    run["human_review"] = {"scope": "target", "decision": "rejected", "reviewed": 1}
    with pytest.raises(ValueError, match="fresh full-target"):
        ledger.validate(accepted_state(spec, run), spec)


@pytest.mark.parametrize("identity", ["SP-02", "TC-11", "SP-04"])
def test_pre_regression_human_or_programmatic_acceptance_cannot_be_reused(spec, run, identity):
    first = copy.deepcopy(run)
    first["human_review"] = {"scope": "target", "decision": "accepted", "reviewed": 1}
    document = accepted_state(spec, first, identity)
    failed = copy.deepcopy(run)
    failed.update(id="regression", targets=[identity], outcome="fail")
    failed["checks"]["failed"] = 1
    after_failure = record(document, failed, spec)
    incomplete = copy.deepcopy(run)
    incomplete.update(id="incomplete-reacceptance", targets=[identity], kind="target_acceptance", coverage="full_target")
    if identity != "SP-02":
        incomplete["checks"] = {"passed": None, "failed": None, "not_run": None}
        incomplete["human_review"] = {"scope": "target", "decision": "accepted", "reviewed": 1}
    after = record(after_failure, incomplete, spec)
    after["targets"][identity].update(acceptance="accepted", blockers=[])
    with pytest.raises(ValueError, match="fresh"):
        ledger.validate(after, spec, after_failure)


def test_target_human_review_cannot_be_applied_to_three_targets(spec, run):
    run.update(kind="target_acceptance", coverage="full_target", targets=["SP-02", "SP-19", "SP-28"])
    run["human_review"] = {"scope": "target", "decision": "accepted", "reviewed": 1}
    with pytest.raises(ValueError, match="exactly one"):
        record(ledger.initialize(spec), run, spec)


@pytest.mark.parametrize("heading", ["### 5.3", "### 9.1", "判定標記：", "### 1.2"])
def test_shared_normative_changes_invalidate_entry_fingerprints(tmp_path, spec, heading):
    source = (ROOT / "docs" / "grok-aligned-target-state.md").read_text(encoding="utf-8")
    line = next(line for line in source.splitlines() if line.startswith(heading))
    path = tmp_path / "changed-spec.md"
    path.write_text(source.replace(line, line + "\nA new normative requirement.", 1), encoding="utf-8")
    changed = ledger.read_spec(path)
    assert ledger.fingerprint(changed) != ledger.fingerprint(spec)
    assert ledger.entry_fingerprint(changed, "SP-02") != ledger.entry_fingerprint(spec, "SP-02")
    assert ledger.entry_fingerprint(changed, "E-09") != ledger.entry_fingerprint(spec, "E-09")


def test_scenario_criteria_and_acceptance_are_tracked_separately(tmp_path, spec, run):
    source = (ROOT / "docs" / "grok-aligned-target-state.md").read_text(encoding="utf-8")
    line = next(line for line in source.splitlines() if line.startswith("| E-09 "))
    cells = line.split("|")
    cells[2] += " Additional scenario requirement."
    path = tmp_path / "scenario-spec.md"
    path.write_text(source.replace(line, "|".join(cells)), encoding="utf-8")
    changed = ledger.read_spec(path)
    assert ledger.entry_fingerprint(changed, "E-09") != ledger.entry_fingerprint(spec, "E-09")
    assert ledger.entry_fingerprint(changed, "SP-02") == ledger.entry_fingerprint(spec, "SP-02")
    document = accepted_state(spec, run, "E-03")
    result = ledger.summary(document, spec)
    assert result["scenario_count"] == 12
    assert result["declared_full_scenario_acceptances"] == 1
    assert result["declared_full_target_acceptances"] == 0


def test_spec_revision_keeps_history_but_cannot_reuse_old_criteria(spec, run):
    document = accepted_state(spec, run)
    changed = copy.deepcopy(spec)
    changed["targets"]["TC-11"]["target"] += " New requirement."
    revised = ledger.sync_spec(document, changed)
    ledger.validate(revised, changed, document)
    assert revised["runs"] == document["runs"]
    assert revised["targets"]["TC-11"]["acceptance"] == "not_evaluated"
    assert ledger.fingerprint(spec) in revised["specifications"]
    assert ledger.entry_fingerprint(changed, "E-05") != ledger.entry_fingerprint(spec, "E-05")
    old_result = copy.deepcopy(run)
    old_result.update(id="late-old-spec-result", kind="target_acceptance", coverage="full_target")
    appended = record(revised, old_result, changed, ledger.fingerprint(spec))
    appended["targets"]["TC-11"].update(implementation="implemented", acceptance="accepted", blockers=[])
    with pytest.raises(ValueError, match="specification"):
        ledger.validate(appended, changed, revised)
    forged = copy.deepcopy(document)
    forged["specifications"][ledger.fingerprint(changed)] = changed
    forged["spec_sha256"] = ledger.fingerprint(changed)
    with pytest.raises(ValueError, match="specification"):
        ledger.validate(forged, changed, document)


def legacy_document(spec, run):
    document = {
        "schema_version": 1, "spec_sha256": ledger.fingerprint(spec["targets"]),
        "targets": ledger.empty_states(ledger.TARGET_IDS),
        "runs": [{**copy.deepcopy(run), "sha256": ledger.fingerprint(run)}],
    }
    document["targets"]["TC-11"].update(
        implementation="implemented", acceptance="accepted", current_baseline=run["baseline"], evidence=[run["id"]],
    )
    return document


def test_legacy_migration_preserves_exact_runs_without_inventing_spec_bindings(spec, run):
    run.update(kind="target_acceptance", coverage="full_target")
    legacy = legacy_document(spec, run)
    migrated = ledger.migrate(legacy, spec)
    ledger.validate(migrated, spec, legacy)
    assert migrated["runs"] == legacy["runs"]
    assert migrated["run_spec_bindings"] == {run["id"]: None}
    assert migrated["targets"]["TC-11"]["acceptance"] == "partial"
    assert len(migrated["scenarios"]) == 12
    with pytest.raises(ValueError, match="immutable"):
        record(migrated, run, spec)
    changed = copy.deepcopy(spec)
    changed["targets"]["TC-11"]["target"] += " New requirement."
    revised = ledger.sync_spec(migrated, changed)
    ledger.validate(revised, changed, legacy)


def test_recording_requires_an_archived_specification(spec, run):
    with pytest.raises(ValueError, match="archived"):
        record(ledger.initialize(spec), run, spec, "f" * 64)


@pytest.mark.parametrize("identity", ["TC-11", "SP-02"])
def test_late_recorded_older_acceptance_cannot_cross_a_newer_observed_failure(spec, run, identity):
    accepted = copy.deepcopy(run)
    accepted["observed_on"] = "2026-09-20"
    accepted["human_review"] = {"scope": "target", "decision": "accepted", "reviewed": 1}
    document = accepted_state(spec, accepted, identity)
    rejected = copy.deepcopy(run)
    rejected.update(id="observed-new-failure", observed_on="2026-09-26", targets=[identity], outcome="fail")
    rejected["checks"]["failed"] = 1
    if identity == "SP-02":
        rejected["human_review"] = {"scope": "target", "decision": "rejected", "reviewed": 1}
    after_failure = record(document, rejected, spec)
    late = copy.deepcopy(accepted)
    late.update(id="late-older-pass", targets=[identity], kind="target_acceptance", coverage="full_target")
    forged = record(after_failure, late, spec)
    forged["targets"][identity].update(acceptance="accepted", blockers=[])
    with pytest.raises(ValueError, match="fresh full-target"):
        ledger.validate(forged, spec, after_failure)


def test_latest_observed_failure_is_not_replaced_by_a_late_older_failure(spec, run):
    document = accepted_state(spec, run)
    for identity, observed in (("newer-failure", "2026-09-26"), ("late-older-failure", "2026-09-21")):
        failed = copy.deepcopy(run)
        failed.update(id=identity, observed_on=observed, outcome="fail")
        failed["checks"]["failed"] = 1
        document = record(document, failed, spec)
    late = copy.deepcopy(run)
    late.update(id="intermediate-pass", observed_on="2026-09-24", kind="target_acceptance", coverage="full_target")
    rejected = record(document, late, spec)
    rejected["targets"]["TC-11"].update(acceptance="accepted", blockers=[])
    with pytest.raises(ValueError, match="fresh full-target"):
        ledger.validate(rejected, spec)
    same_day = copy.deepcopy(late)
    same_day.update(id="same-day-new-assessment", observed_on="2026-09-26")
    accepted = record(document, same_day, spec)
    accepted["targets"]["TC-11"].update(acceptance="accepted", blockers=[])
    ledger.validate(accepted, spec, document)
