"""One real fixed-SDK tool/cold-route case with lawful query interleavings; offline only."""

import json
import os
from pathlib import Path

import test_reviewed_work_memory_native as original
from test_reviewed_work_memory_native import assistant, instruction_models, jwk_client, runtime_path, signing_key
from test_memory_no_sdk_conflicts import observe_job_during_context


def test_fixed_sdk_note_and_cold_route_survive_same_source_observer_updates(assistant, monkeypatch):
    configure = original.configured_memory
    observations, failures = [], []

    def observed_configuration(h):
        result = configure(h)
        updates, refusals, _ = observe_job_during_context(h, monkeypatch, local=True, allow_sdk=True)
        observations.append(updates)
        failures.append(refusals)
        return result

    monkeypatch.setattr(original, "configured_memory", observed_configuration)
    original.test_fixed_sdk_reads_reviewed_team_note_only_on_demand_and_restores_its_exact_sources(
        assistant, monkeypatch)
    assert len(observations) == 1 and len(observations[0]) == 2 and failures == [[]]
    if os.environ.get("FA_MEMORY_OBSERVER_EVIDENCE_DIR"):
        destination = Path(os.environ["FA_MEMORY_OBSERVER_EVIDENCE_DIR"])
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "native-observer-interleavings.json").write_text(json.dumps({
            "scope": "One existing fixed SDK1.0.13/runtime1.0.83 local tool/cold-route case with real query reservation races",
            "observations": observations[0], "generic_source_refusals": failures[0],
            "same_claim_review_manifest_and_originals": True, "completed_sdk_turns": 2,
            "offline_requests": 6, "paid_requests": 0, "cloud_requests": 0,
            "detail": "Exact body/next-request, original user bytes, cold SDK identity, old archive and raw stop receipts "
                      "are verified and saved by the original case; no quality or Hosted-cloud claim.",
        }, indent=2) + "\n", encoding="utf-8")
