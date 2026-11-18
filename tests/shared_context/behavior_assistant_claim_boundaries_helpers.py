"""從 test_behavior_assistant_claim_boundaries.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

from shared_context import behavior_wire as wire
from test_behavior_api import METRICS


def reviewed(c):
    runtime, b = c.composition.runtime, c.binding
    profile = c.config.assistant.profiles[0]
    source = wire.SourcePrepare(request_id="startup-source", version=1, original=METRICS)
    runtime.prepare_source(b.binding_id, profile.source.source_id, source, c.principal)
    runtime.commit_source(b.binding_id, profile.source.source_id, wire.Command(request_id=source.request_id), c.principal)
    draft = wire.ClaimDraft(request_id="startup-claim", version=1, text="The accepted metrics source has been reviewed.",
                           supporting=(profile.source,), effective_from=c.clock[0])
    preview = runtime.preview_claim(b.binding_id, profile.claim_id, draft, c.principal)
    command = wire.ClaimReview(**draft.model_dump(), decision_id="startup-reviewed",
        candidate_sha256=preview.candidate_sha256, evidence_sha256=preview.evidence_sha256,
        policy_version=preview.policy_version,
        reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"))
    runtime.review_claim(b.binding_id, profile.claim_id, command, c.principal)
    runtime.commit_claim(b.binding_id, profile.claim_id, wire.Command(request_id=command.request_id), c.principal)
