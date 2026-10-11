"""Current Team reference access on the existing two-Agent authority."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_agent_message_wire import PeerTeamReference
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_agent_runtime import peer_runtime
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key


def test_api_reference_checker_does_not_import_the_hosted_sdk():
    source = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"
    script = ("import sys; sys.modules['copilot']=None; sys.path.insert(0,sys.argv[1]); "
              "import shared_context.behavior_agent_message_refs; print('api-refs-ok')")
    result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", script, str(source)],
                            capture_output=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr


def principal(composition):
    policy = composition.runtime.work_policy.policy
    actor = composition.config.identity.auth().actors[policy.template.agent_oid]
    return BehaviorPrincipal(composition.config.identity.tenant_id, actor.oid, actor.team, "agent", actor.actor,
                             frozenset({"read", "submit", "execute", "consume"}))


def register(composition, human, request_id):
    command = InstructionSubmit(request_id=request_id, content="Synthetic peer reference check.")
    return composition.runtime.work_policy.register(human, request_id,
        hashlib.sha256(command.model_dump_json().encode()).hexdigest(), instruction=command)


def test_registry_template_cannot_replace_a_registered_receiver_read_binding(peer_runtime):
    peer = peer_runtime
    actor = principal(peer.b)
    with pytest.raises(Problem) as error:
        peer.b.runtime.authorize(peer.policy.template.binding_id, actor, "read")
    assert error.value.status == 403
    registered = register(peer.b, peer.c.human, "receiver-read-binding")
    binding, _, _ = peer.b.runtime.authorize(registered.binding.binding_id, actor, "read")
    assert binding == registered.binding
    assert peer.c.sessions.calls == []


@pytest.fixture
def shared_peer_context(request, monkeypatch):
    import test_agent_runtime
    original = test_agent_runtime.peer_policy

    def shared_sources(config, parent):
        policy = original(config, parent)
        template = policy.template.model_copy(update={
            "source_ids": (*policy.template.source_ids, *parent.template.source_ids)})
        return policy.model_copy(update={"template": template, "legacy_template_policy":
            policy.legacy_template_policy.model_copy(update={"binding_digest": digest(
                ["fa-behavior-api-v1", policy.evidence_mode, template.model_dump(mode="json")])})})

    monkeypatch.setattr(test_agent_runtime, "peer_policy", shared_sources)
    peer = request.getfixturevalue("peer_runtime")
    a = register(peer.a, peer.c.human, "sender-reference-work")
    b = register(peer.b, peer.c.human, "receiver-reference-work")
    return SimpleNamespace(peer=peer, a=a, b=b, sender=principal(peer.a), receiver=principal(peer.b))


def source_reference(case):
    source = case.peer.a.runtime.work_policy.policy.profile.source
    return PeerTeamReference(resource_kind="source", visibility="team", tenant_id=case.a.binding.tenant_id,
        team_id=case.a.binding.team_id, resource_id=source.source_id, version=str(source.version), sha256=source.sha256)


def checked(case, refs, allowed_refs=None, **changes):
    from shared_context.behavior_agent_message_refs import validate_team_refs
    values = dict(sender_authority=case.peer.a.assistant.authority, sender_binding=case.a.binding,
                  sender_principal=case.sender, receiver_authority=case.peer.b.assistant.authority,
                  receiver_binding=case.b.binding, receiver_principal=case.receiver,
                  refs=refs, allowed_refs=refs if allowed_refs is None else allowed_refs)
    return validate_team_refs(**{**values, **changes})


def test_team_source_is_checked_against_both_current_scopes_without_loading_private_memory(shared_peer_context, monkeypatch):
    c = shared_peer_context
    reference = source_reference(c)

    def no_memory(*args, **kwargs):
        pytest.fail("reference validation cannot load private memory or assemble another Agent context")

    monkeypatch.setattr(c.peer.a.runtime.dep.memory, "assemble", no_memory)
    before = c.peer.a.runtime.store._guard()
    assert checked(c, (reference,)) == (reference,)
    assert c.peer.a.runtime.store._guard() == before and c.peer.c.sessions.calls == []
    for ref in (
        reference.model_copy(update={"sha256": "f"*64}),
        reference.model_copy(update={"version": "999"}),
        reference.model_copy(update={"team_id": "another-team"}),
        reference.model_copy(update={"resource_kind": None}),
    ):
        with pytest.raises(Problem):
            checked(c, (ref,), allowed_refs=(reference,))
    with pytest.raises(Problem):
        checked(c, (reference,), allowed_refs=())
    for changed in (reference.model_copy(update={"sha256":"f"*64}), reference.model_copy(update={"version":"999"})):
        with pytest.raises(Problem):
            checked(c, (changed,))
    c.peer.b.runtime.work_policy.revoke(c.peer.c.human, "receiver-read-revoked")
    with pytest.raises(Problem):
        checked(c, (reference,))
    assert c.peer.c.sessions.calls == []


def test_root_allowlist_does_not_expand_the_receivers_original_source_scope(peer_runtime):
    p = peer_runtime
    c = SimpleNamespace(peer=p, a=register(p.a, p.c.human, "a-unshared"), b=register(p.b, p.c.human, "b-unshared"),
                        sender=principal(p.a), receiver=principal(p.b))
    reference = source_reference(c)
    assert reference.resource_id not in c.b.binding.source_ids
    with pytest.raises(Problem) as denied:
        checked(c, (reference,))
    assert denied.value.body["error"]["code"] == "BEHAVIOR_SOURCE_DENIED"
    assert p.c.sessions.calls == []


def test_ref_read_reuses_an_existing_read_scope_and_denies_sender_revocation(shared_peer_context):
    c = shared_peer_context
    ref = source_reference(c)
    store = c.peer.a.runtime.store
    assert store.consistent_read(lambda: checked(c, (ref,))) == (ref,)
    c.peer.a.runtime.work_policy.revoke(c.peer.c.human, "sender-read-revoked")
    with pytest.raises(Problem):
        checked(c, (ref,))
    assert c.peer.c.sessions.calls == []


def test_receiver_revocation_during_ref_read_prevents_a_successful_result(shared_peer_context, monkeypatch):
    c = shared_peer_context
    reference = source_reference(c)
    read = c.peer.b.runtime.read_source
    changed = []

    def revoke_after_read(*args, **kwargs):
        result = read(*args, **kwargs)
        if not changed:
            changed.append(True)
            with ThreadPoolExecutor(max_workers=1) as worker:
                worker.submit(c.peer.b.runtime.work_policy.revoke, c.peer.c.human, "receiver-raced-revoke").result()
        return result

    monkeypatch.setattr(c.peer.b.runtime, "read_source", revoke_after_read)
    with pytest.raises(Problem) as denied:
        checked(c, (reference,))
    assert denied.value.status in {403, 412}
    assert changed == [True] and c.peer.c.sessions.calls == []


def test_source_expiring_during_the_two_peer_reads_cannot_return_a_current_reference(shared_peer_context, monkeypatch):
    from shared_context import behavior_wire as wire
    c = shared_peer_context
    reference = source_reference(c)
    runtime = c.peer.a.runtime
    monkeypatch.setattr(runtime.dep.sources, "_clock", lambda: c.peer.c.h.time)
    monkeypatch.setattr(c.peer.b.runtime.dep.sources, "_clock", lambda: c.peer.c.h.time)
    source = runtime.read_source(c.a.binding.binding_id, reference.resource_id, 1, c.sender)
    expiry = c.peer.c.h.time + timedelta(seconds=1)
    command = wire.SourcePrepare(request_id="expiring-team-ref", version=2, original=source.original, expires_at=expiry)
    runtime.prepare_source(c.a.binding.binding_id, reference.resource_id, command, c.peer.c.human)
    runtime.commit_source(c.a.binding.binding_id, reference.resource_id,
                          wire.Command(request_id=command.request_id), c.peer.c.human)
    reference = reference.model_copy(update={"version": "2"})
    read = c.peer.b.runtime.read_source

    def advance_after_read(*args, **kwargs):
        result = read(*args, **kwargs)
        c.peer.c.h.time = expiry
        return result

    monkeypatch.setattr(c.peer.b.runtime, "read_source", advance_after_read)
    with pytest.raises(Problem) as expired:
        checked(c, (reference,))
    assert expired.value.body["error"]["code"] == "SOURCE_EXPIRED"


def test_withdrawn_source_does_not_survive_a_former_successful_check(shared_peer_context):
    from shared_context import behavior_wire as wire
    c = shared_peer_context
    ref = source_reference(c)
    assert checked(c, (ref,)) == (ref,)
    source = c.peer.a.runtime.read_source(c.a.binding.binding_id, ref.resource_id, int(ref.version), c.sender)
    command = wire.SourceWithdrawal(request_id="withdraw-shared", withdrawal_id="withdraw-shared",
        reference=wire.ExactSourceRef(source_id=ref.resource_id, version=int(ref.version),
            sha256=ref.sha256, byte_length=source.event.reference.byte_length), expected_access_revision=source.access_revision)
    c.peer.a.runtime.withdraw_source(c.a.binding.binding_id, ref.resource_id, command, c.peer.c.human)
    with pytest.raises(Problem) as denied:
        checked(c, (ref,))
    assert denied.value.body["error"]["code"] == "SOURCE_WITHDRAWN"
    assert c.peer.c.sessions.calls == []


def test_private_foreign_malformed_or_untyped_refs_fail_before_any_data_reader(shared_peer_context, monkeypatch):
    from dataclasses import replace
    c = shared_peer_context
    reference = source_reference(c)

    def no_read(*args, **kwargs):
        pytest.fail("invalid reference data must be refused before content reads")

    monkeypatch.setattr(c.peer.a.runtime, "read_source", no_read)
    monkeypatch.setattr(c.peer.b.runtime, "read_source", no_read)
    for changes in (
        {"visibility":"private"}, {"resource_kind":None}, {"resource_kind":"url"},
        {"resource_id":"../source"}, {"version":"https://example.org/file"}, {"version":"01"},
        {"tenant_id":"other"}, {"team_id":"other"}, {"resource_kind":"artifact","version":"1"},
    ):
        ref = reference.model_copy(update=changes)
        with pytest.raises(Problem):
            checked(c, (ref,), allowed_refs=(ref,))
    with pytest.raises(Problem):
        checked(c, (reference, reference))
    with pytest.raises(Problem):
        checked(c, tuple(reference for _ in range(9)))
    with pytest.raises(Problem):
        checked(c, (reference,), receiver_principal=replace(c.receiver, kind="human", actor_id=None))
    with pytest.raises(Problem):
        checked(c, (reference,), receiver_principal=replace(c.receiver, oid=c.sender.oid))


def test_fixed_artifact_bytes_need_current_read_on_both_sides_without_loading_memory(shared_peer_context, monkeypatch):
    from conftest import InMemoryRepository
    from shared_context.m3_models import CreateArtifact, VersionInput
    from shared_context.m3_store import InMemoryArtifactStore
    c = shared_peer_context
    sender = c.peer.a.assistant.authority
    receiver = c.peer.b.assistant.authority
    artifacts = sender.artifacts
    memory_store = InMemoryArtifactStore(c.a.binding.tenant_id)
    repository = InMemoryRepository()
    monkeypatch.setattr(artifacts, "store", memory_store)
    monkeypatch.setattr(receiver.artifacts, "store", memory_store)
    monkeypatch.setattr(artifacts, "repository", repository)
    monkeypatch.setattr(receiver.artifacts, "repository", repository)
    actor = sender.actors[c.sender.oid]
    artifact_id, version_id, content = "shared-report", "1"*32, b"Exact shared report.\r\n"
    doc = artifacts.create(actor, actor.team, artifact_id, CreateArtifact(
        kind="deliverable", title="Shared report",
        source_work=c.peer.a.runtime.work_policy.policy.profile.legacy_source_work))
    artifacts.upload(actor, actor.team, artifact_id, version_id, doc.etag, VersionInput(
        filename="report.txt", media_type="text/plain", sha256=hashlib.sha256(content).hexdigest()), content)
    reference = PeerTeamReference(resource_kind="artifact", visibility="team", tenant_id=c.a.binding.tenant_id,
        team_id=actor.team, resource_id=artifact_id, version=version_id, sha256=hashlib.sha256(content).hexdigest())
    before = c.peer.a.runtime.store._guard()
    assert checked(c, (reference,)) == (reference,)
    assert c.peer.a.runtime.store._guard() == before
    for ref in (reference.model_copy(update={"sha256":"f"*64}), reference.model_copy(update={"version":"2"*32})):
        with pytest.raises(Problem):
            checked(c, (ref,))
    assert c.peer.c.sessions.calls == []
