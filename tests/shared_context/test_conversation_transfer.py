"""Explicit environment preparation with original checkpoints and offline transports."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from shared_context.behavior_foundry import FoundryExecutor, NativeJobRoute, NativeLaunchAuthority
from assistant_api_helpers import assistant_api
from test_behavior_foundry import TARGET
from instruction_flow_helpers import instruction_models, runtime_path, sdk_io
from test_m3_auth import jwk_client, signing_key
from work_policy_lifecycle_helpers import enabled
from hosted_versions_stub import deployed_versions
from legacy_replay_helpers import legacy_tokens, replayed


@pytest.fixture
def transfer_case(legacy_tokens, assistant_api, monkeypatch):
    deployed_versions(monkeypatch, TARGET.agent_version)
    h, control, human, facts = enabled(assistant_api)
    io = sdk_io(monkeypatch)
    # A succeeded first turn and a second turn of the same conversation refused on its agent version, as the
    # retired job lane left them on 80ec0865 (legacy_replay_helpers).
    seeded = replayed(h, h.artifacts)
    control.refresh()
    creates, reads = session_executor(h, control)
    deployed_versions(monkeypatch, "42")
    return SimpleNamespace(h=h, control=control, human=human, cid=seeded.cid, failed_job_id=seeded.failed_job_id,
                           io=io, creates=creates, reads=reads)


def session_executor(h, control):
    """The concrete Foundry session executor of the failed turn's target, over a session client that records calls."""
    target = TARGET.model_copy(update={
        "executor_id": h.workers.contract.executor_id, "agent_oid": control.policy.template.agent_oid,
    })
    native = NativeLaunchAuthority(
        h.workers, team_id=control.policy.template.team_id, controller_id="internal-controller", targets=(target,),
        jobs=(NativeJobRoute(job_id=h.binding.job_id, api_binding_id=h.binding.binding_id, grant_id="unused"),),
        verifier_version=h.config.verifier_version, clock=lambda: h.time,
    )
    native.assistant = h.assistant
    h.runtime.dep = replace(h.runtime.dep, native_launches=native)
    creates, reads = [], []

    class SessionClient:
        credential = object()

        def __init__(self, selected):
            self.target = selected

        def for_target(self, selected):
            return SessionClient(selected)

        def create_session(self, *, before_send):
            before_send()
            creates.append(self.target)

        def verify_session(self, *, before_send):
            before_send()
            reads.append(self.target)
            return SimpleNamespace(agent_session_id=self.target.session_id, status="idle")

        def start(self, *args, **kwargs):
            pytest.fail("previewing a replacement environment must not invoke")

    h.workers.adapter = FoundryExecutor(native, (SessionClient(target),)).adapter()
    return creates, reads


def test_transfer_preview_reads_the_last_legal_archive_without_creating_compute(transfer_case):
    from shared_context.behavior_conversation_transfer import preview
    c = transfer_case
    before = c.h.transport.backing.rows.copy()
    value = preview(c.control, c.h.assistant, c.human, c.cid)
    assert value.conversation_id == c.cid and value.failed_job_id == c.failed_job_id
    assert value.archive_turn_number == 1 and value.destination_agent_version == "42"
    assert value.state == "available" and value.sdk_restored is False
    assert value.failed_message_resent is False
    assert c.creates == c.reads == [] and c.io == []
    assert c.h.transport.backing.rows == before
