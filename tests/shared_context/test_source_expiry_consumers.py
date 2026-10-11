"""Source expiry through real review/governance interfaces, offline storage only."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from shared_context.errors import Problem
from shared_context.memory_governance import MemoryGovernance
from shared_context.memory_projection import NativeMemoryProjection
from shared_context.source_reviews import DurableHumanReviewVerifier
from test_memory_governance import VERSIONS, candidate, projection
from test_memory_projection import QUERY, Transport, client, completed, setup
from test_source_expiry import expiring_service
from test_source_materials import CONTEXT, code, input_source
from test_source_reviews import HUMAN, HUMAN_CONTEXT, POLICY, claimed


def governance(*, token_counter=len, transitive=False):
    sources, table, clock = expiring_service()
    expiry = clock.value + timedelta(seconds=60)
    source = sources.plan(CONTEXT, input_source(expires_at=expiry), b"Exact expiring support.")
    sources.commit(CONTEXT, source)
    if transitive:
        source = sources.plan(CONTEXT, input_source(source_id="unexpired-root", necessary=(source.reference,)), b"Root original.")
        sources.commit(CONTEXT, source)
    reviewer = DurableHumanReviewVerifier(sources, POLICY, clock=clock)
    memory = MemoryGovernance(sources, clock=clock, token_counter=token_counter, review_verifier=reviewer)
    proposed = candidate(source.reference, visibility="team")
    claim = claimed(reviewer, memory, proposed)
    return sources, table, clock, expiry, reviewer, memory, proposed, claim


def assemble(memory, proposed, **options):
    return memory.assemble(
        CONTEXT, scope=CONTEXT.scope("team"), subject=proposed.subject,
        required_claim_ids=(proposed.claim_id,), versions=VERSIONS, **options,
    )


def test_expiry_during_context_token_count_never_releases_required_source():
    state = {}

    def count(raw):
        if state.get("armed"):
            state["clock"].value = state["expiry"]
        return len(raw)

    _, _, clock, expiry, _, memory, proposed, _ = governance(token_counter=count)
    assert assemble(memory, proposed).originals
    state.update(armed=True, clock=clock, expiry=expiry)
    with pytest.raises(Problem) as expired:
        assemble(memory, proposed)
    assert code(expired) == "SOURCE_EXPIRED"


def test_same_authority_review_snapshot_rechecks_source_expiry():
    sources, _, clock, expiry, reviewer, _, proposed, claim = governance()
    originals = sources.read_graph(CONTEXT, proposed.supporting)
    keys = (*reviewer.predicate_keys(CONTEXT, proposed, claim.review), *sources.source_read_keys(originals))
    rows = sources.read_snapshot(keys)
    clock.value = expiry
    with pytest.raises(Problem) as expired:
        reviewer.verify_snapshot(CONTEXT, proposed, claim.review, rows)
    assert code(expired) == "SOURCE_EXPIRED"


@pytest.mark.parametrize("operation", ["current", "context", "claim-plan", "claim-commit", "review", "projection"])
def test_expired_source_invalidates_even_a_claim_without_its_own_expiry(operation):
    sources, _, clock, expiry, reviewer, memory, proposed, claim = governance()
    assert proposed.expires_at is None
    clock.value = expiry
    actions = {
        "current": lambda: memory.get_current(CONTEXT, scope=CONTEXT.scope("team"), subject=proposed.subject,
                                              claim_id=proposed.claim_id),
        "context": lambda: assemble(memory, proposed),
        "claim-plan": lambda: memory.plan_claim(CONTEXT, proposed),
        "claim-commit": lambda: memory.commit_claim(CONTEXT, claim),
        "review": lambda: reviewer.preview(HUMAN, HUMAN_CONTEXT, proposed),
        "projection": lambda: memory.record_projection(CONTEXT, projection(memory, claim, visibility="team")),
    }
    with pytest.raises(Problem) as expired:
        actions[operation]()
    assert code(expired) == "SOURCE_EXPIRED"
    assert memory.reconcile_claim(CONTEXT, claim).payload["result"]["claim"] == claim.reference.model_dump(mode="json")


def test_missing_optional_projection_never_hides_an_expired_necessary_source():
    _, _, clock, expiry, _, memory, proposed, claim = governance()
    summary = projection(memory, claim, visibility="team")
    assert assemble(memory, proposed, projection_id=summary.projection_id).originals
    clock.value = expiry
    with pytest.raises(Problem) as expired:
        assemble(memory, proposed, projection_id=summary.projection_id)
    assert code(expired) == "SOURCE_EXPIRED"


def test_nonexpiring_claim_and_root_cannot_skip_expired_transitive_support():
    sources, _, clock, expiry, _, memory, proposed, _ = governance(transitive=True)
    assert proposed.expires_at is None
    assert len(assemble(memory, proposed).originals) == 2
    clock.value = expiry
    assert sources.read_exact(CONTEXT, proposed.supporting[0]).event.expires_at is None
    with pytest.raises(Problem) as expired:
        assemble(memory, proposed)
    assert code(expired) == "SOURCE_EXPIRED"


@pytest.mark.parametrize("operation", ["plan", "start", "search", "context"])
def test_native_projection_adapter_never_uses_expired_source_or_sends_new_requests(operation):
    sources, _, clock, expiry, _, memory, _, claim = governance()
    transport = Transport([])
    with client(transport) as project:
        native = NativeMemoryProjection(memory, project, setup(memory))
        planned = native.plan(CONTEXT).payload["result"]
        h = SimpleNamespace(service=native, claim=claim, transport=transport,
                            pid=planned["projection_id"], scope=planned["native_scope"])
        if operation in {"search", "context"}:
            completed(h)
        clock.value = expiry
        count = len(transport.requests)
        actions = {
            "plan": lambda: native.plan(CONTEXT),
            "start": lambda: native.start(CONTEXT, h.pid),
            "search": lambda: native.search(CONTEXT, h.pid, QUERY),
            "context": lambda: native.assemble(CONTEXT, h.pid),
        }
        with pytest.raises(Problem) as expired:
            actions[operation]()
        assert code(expired) == "SOURCE_EXPIRED"
        assert len(transport.requests) == count
