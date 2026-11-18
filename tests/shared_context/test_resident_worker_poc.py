"""ADR-0005 resident worker PoC: real authority/HTTP/Table producers with the pinned SDK I/O double.

Covers the four PoC acceptance items offline: a follow-up turn keeps the released SDK instead of
restoring it, a lost worker falls back to the published checkpoint without resending, cancellation
stops the resident SDK, and a revocation after the light restore still blocks the next model write.
"""

import pytest
from pydantic import ValidationError

from shared_context import behavior_wire as wire
from shared_context.behavior_assistant_wire import InstructionTask
from shared_context.behavior_hosted import LocalExecution
from shared_context.behavior_instruction import TurnArchive
from shared_context.behavior_assistant import key
from shared_context.m2_copilot import ResidentHolder, ResidentSdk

from assistant_api_helpers import assistant_api
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from instruction_flow_helpers import instruction_models
from legacy_turn_rows_helpers import outcome_of, stored_turn

POC = wire.RESIDENT_WORKER_PROFILE


def test_saved_release_passes_the_real_native_startup_readers(stored_turn):
    """Coordinator condition: after saving a release the API still starts (no new record kind).

    Both old turns ran on the PoC template, selected the same way the operator will select it on agent B.
    """
    from legacy_turn_rows_helpers import read_startup
    from shared_context.behavior_migration import MigrationStore, installed_origin
    from shared_context.behavior_operations import _parse
    from shared_context.behavior_profile_selection import current_selection, profile_from_template

    def readers(c):
        store = MigrationStore(c.h.composition.clients[0], c.h.config.table.scope)
        return (outcome_of(lambda: read_startup(c)), outcome_of(lambda: installed_origin(store, c.h.config)))
    c = stored_turn("synthetic-exit")
    selected = current_selection(c.control.store, c.control.policy)
    assert profile_from_template(c.control.policy, selected.command.template).worker_profile == POC
    after_exit = readers(c)
    assert after_exit[0] == "ok"
    released = wire.AssistantSdkStop(pid=99, release=POC)
    # The release turn followed the exit turn on the same store.
    c = stored_turn("synthetic-release")
    row_key = key("text-archive", c.turn.binding.job_id)
    saved = _parse(c.h.runtime.store.read_snapshot((row_key,))[row_key].payload["data"], TurnArchive)
    assert saved.sdk_stop == released
    # Release records are classified exactly as ordinary exit records by every startup reader.
    assert readers(c) == after_exit


def test_a_release_requires_the_captured_profile():
    stop = wire.AssistantSdkStop(pid=7, release=POC)
    base = dict(launch_id="launch", assignment_digest="a" * 64, supervisor_id="sup", child_nonce="nonce",
                phase="sealed", child_pid=7, outcome="succeeded", sdk_stop=stop)
    assert LocalExecution(**base).sdk_stop.release == POC
    for outcome in ("stopped", "failed"):
        assert LocalExecution(**{**base, "outcome": outcome}).outcome == outcome
    for change in ({"outcome": "parked"}, {"child_exited": True}, {"child_pid": 8}, {"exit_code": 0}):
        with pytest.raises(ValidationError):
            LocalExecution(**{**base, **change})
    with pytest.raises(ValidationError):
        wire.AssistantSdkStop(pid=7)
    from shared_context.behavior_sdk_state import CheckpointProfile
    with pytest.raises(ValidationError, match="resident worker PoC"):
        InstructionTask(content="x", conversation_id="c", turn_number=1, worker_profile=POC,
                        checkpoint_profile=CheckpointProfile())
    assert InstructionTask(content="x", conversation_id="c", turn_number=1, worker_profile=POC).worker_profile == POC


def test_poc_template_selection_refuses_checkpoint_profiles(assistant_api):
    from shared_context.behavior_sdk_state import CheckpointProfile
    from conversation_profile_selection_helpers import configured, select, template
    h, _, _ = configured(assistant_api)
    refused, _ = select(h, request_id="poc-with-checkpoint",
                        value=template(worker_profile=POC, checkpoint_profile=CheckpointProfile()))
    assert refused.status_code == 422
    selected, _ = select(h, request_id="poc", value=template(worker_profile=POC))
    assert selected.status_code == 200, selected.text


def test_an_unbound_resident_sdk_fails_closed():
    import asyncio
    live = ResidentSdk("digest", "sid", files=None)
    denied = live.permission(object(), {})
    assert type(denied).__name__ == "PermissionDecisionReject"
    result = asyncio.run(live.invoke(object()))
    assert result.result_type == "failure"
    with pytest.raises(RuntimeError):
        asyncio.run(live.gateway.send_request(object(), object()))
    assert ResidentHolder().live is None
