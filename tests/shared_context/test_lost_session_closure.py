"""A closed lost-session or version-refused turn keeps its own evidence id and failure notice."""

import json
from types import SimpleNamespace

import pytest

from shared_context.behavior_foundry import (
    SESSION_LOST, SESSION_VERSION_CHANGED, _lost_session_result, _version_refusal_result, native_key,
)
from shared_context.behavior_delivery import _digest, _json


@pytest.mark.parametrize("build,kind,code", [
    (_lost_session_result, "lost-session-result", SESSION_LOST),
    (_version_refusal_result, "version-refusal-result", SESSION_VERSION_CHANGED),
])
def test_a_closed_turn_keeps_its_own_evidence_id_and_failure_notice(build, kind, code):
    launch = SimpleNamespace(launch_id="launch-1", start=SimpleNamespace(binding=SimpleNamespace(work_id="work-1")),
                             definition=SimpleNamespace(model_dump=lambda mode: {"mode": mode}))
    result = build(launch, {"proof": 1}, "verifier-1")
    assert result.evidence_id == native_key(kind, "launch-1") and result.verifier_version == "verifier-1"
    assert (result.work_id, result.state, result.passed_checks) == ("work-1", "failed", ())
    assert result.definition_digest == _digest({"mode": "json"}) and result.proof == _json({"proof": 1})
    assert json.loads(result.content) == {"schema_version": "fa-assistant-notice-v1", "state": "failed",
                                          "code": code, "sdk_reply_available": False}
