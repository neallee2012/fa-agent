"""Explicit environment preparation with original checkpoints and offline transports."""

from dataclasses import replace
import hashlib
from types import SimpleNamespace

import pytest

from shared_context.behavior_assistant_wire import InstructionSubmit, PROFILE
from shared_context.behavior_foundry import (
    FoundryExecutor, NativeExecutionGrant, NativeJobRoute, NativeLaunchAuthority, native_key,
)
from shared_context.behavior_instruction import Conversation, conversation_key
from shared_context.behavior_delivery import _parse
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_behavior_foundry import TARGET
from test_instruction_flow import instruction_models, runtime_path, sdk_io
from test_m3_auth import jwk_client, signing_key
from test_work_policy_lifecycle import enabled
from test_sdk_compaction_product import checkpoint_case, open_turn, settle
from hosted_versions_stub import deployed_versions


def retained_history(c):
    from shared_context.behavior_migration import MigrationStore
    from shared_context.behavior_recovery_history import _Inventory
    from test_behavior_migration_store import InventoryTable
    table = InventoryTable()
    table.backing = c.h.transport.backing
    table.page_size = 1000
    store = MigrationStore(table, c.h.store._scope)
    inventory = store.read_inventory()
    config = SimpleNamespace(table=SimpleNamespace(scope=store._scope), workers=c.h.workers.contract,
                             delivery=c.h.config, sha256="0" * 64)
    return _Inventory(inventory, config)


@pytest.fixture
def compacted_transfer_source(monkeypatch, request):
    deployed_versions(monkeypatch, TARGET.agent_version)
    return request.getfixturevalue("checkpoint_case")


@pytest.fixture
def transfer_case(assistant, monkeypatch, request):
    deployed_versions(monkeypatch, TARGET.agent_version)
    h, control, human, facts = enabled(assistant)
    io = sdk_io(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Original reply and context remain available."}])
    first, _ = control.submit(human, InstructionSubmit(request_id="transfer-first", content="Keep the original context."))
    assert h.runtime.controller.step(first.binding_id).state == "succeeded"
    first_record = control.records[first.binding_id]
    cid = first_record.profile.instruction.conversation_id
    case = failed_environment(h, control, human, cid, monkeypatch, disposition=getattr(request, "param", None))
    case.first, case.io, case.facts = first, io, facts
    return case


def failed_environment(h, control, human, cid, monkeypatch, *, disposition=None):
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
    failed, _ = control.submit(human, InstructionSubmit(
        request_id="transfer-original-failed", conversation_id=cid, content="Do not resend this automatically."))
    grant_id = native_key("bootstrap-grant", failed.job_id)
    h.seed([(native_key("grant", grant_id), NativeExecutionGrant(
        grant_id=grant_id, controller_id=native.controller_id, owner=failed.owner, policy_revision=1,
        job_id=failed.job_id, definition=failed.work,
        input_digest=hashlib.sha256(h.runtime.job_input(failed)).hexdigest(),
        targets=native.targets_for(failed.job_id), active=True, remaining_uses=1,
        not_before=h.time, expires_at=None, workload_version=PROFILE,
    ))])
    def refuse(start):
        native.prepare(start)
        if disposition == "unknown":
            return None
        return native.fence_unclaimed(start.binding, observed_agent_version="999")
    h.workers.adapter = replace(h.workers.adapter, start=refuse, verify=native.verify)
    h.workers.start(failed.job_id, failed.owner)
    if disposition not in {"unknown", "not_forwarded"}:
        h.workers.forward_result(failed.job_id, failed.owner, h.runtime.dep.delivery)
    creates, reads = [], []
    platform = {"created": False}

    class SessionClient:
        credential = object()

        def __init__(self, selected):
            self.target = selected

        def for_target(self, selected):
            return SessionClient(selected)

        def create_session(self, *, before_send):
            before_send()
            creates.append(self.target)
            platform["created"] = True
            if platform.get("lose_create"):
                raise Problem(503, "FOUNDRY_SESSION_UNKNOWN", "OFFLINE original creation response lost")

        def verify_session(self, *, before_send):
            before_send()
            reads.append(self.target)
            if platform.get("read_unavailable"):
                raise Problem(409, "CONVERSATION_SESSION_UNAVAILABLE", "OFFLINE original new session unreadable")
            return SimpleNamespace(agent_session_id=self.target.session_id, status="idle")

        def start(self, *args, **kwargs):
            pytest.fail("preparing a replacement environment must not invoke")

    executor = FoundryExecutor(native, (SessionClient(target),))
    h.workers.adapter = executor.adapter()
    deployed_versions(monkeypatch, "42")
    return SimpleNamespace(h=h, control=control, human=human, failed=failed, cid=cid,
                           creates=creates, reads=reads, platform=platform)


def test_transfer_preview_reads_the_last_legal_archive_without_creating_compute(transfer_case):
    from shared_context.behavior_conversation_transfer import preview
    c = transfer_case
    before = c.h.transport.backing.rows.copy()
    value = preview(c.control, c.h.assistant, c.human, c.cid)
    assert value.conversation_id == c.cid and value.failed_job_id == c.failed.job_id
    assert value.archive_turn_number == 1 and value.destination_agent_version == "42"
    assert value.state == "available" and value.sdk_restored is False
    assert value.failed_message_resent is False
    assert c.creates == c.reads == [] and len(c.io) == 1
    assert c.h.transport.backing.rows == before


def test_explicit_environment_preparation_creates_once_without_resending_the_failed_message(transfer_case):
    from shared_context.behavior_conversation_transfer import prepare, preview, TransferCommand
    c = transfer_case
    key = conversation_key(c.cid)
    old = _parse(c.h.store.read_snapshot((key,))[key].payload["data"], Conversation)
    job = c.h.workers.inspect(c.failed.job_id)
    reply = c.control.read_reply(c.human, c.cid, 1)
    command = TransferCommand(request_id="explicit-environment-1", failed_job_id=c.failed.job_id, expected_agent_version="42")
    result = prepare(c.control, c.h.assistant, c.human, c.cid, command)
    assert result.state == "ready" and result.sdk_restored is False and result.failed_message_resent is False
    assert result.hosted_session_id != old.hosted_session_id and result.destination_agent_version == "42"
    new = _parse(c.h.store.read_snapshot((key,))[key].payload["data"], Conversation)
    assert new.sdk_session_id == old.sdk_session_id and new.jobs == old.jobs and new.archive_id == old.archive_id
    assert new.hosted_session_id == result.hosted_session_id and new.active_job_id is None
    assert c.h.workers.inspect(c.failed.job_id) == job
    assert c.control.read_reply(c.human, c.cid, 1) == reply
    assert prepare(c.control, c.h.assistant, c.human, c.cid, command) == result
    assert preview(c.control, c.h.assistant, c.human, c.cid) == result
    assert len(c.creates) == len(c.reads) == len(c.io) == 1


def test_lost_environment_create_response_only_reads_the_original_new_session(transfer_case, monkeypatch):
    from shared_context.behavior_conversation_transfer import prepare, preview, TransferCommand
    c = transfer_case
    key = conversation_key(c.cid)
    original = c.h.store.read_snapshot((key,))[key]
    command = TransferCommand(request_id="unknown-environment", failed_job_id=c.failed.job_id, expected_agent_version="42")
    c.platform["lose_create"] = True
    with pytest.raises(Problem) as unknown:
        prepare(c.control, c.h.assistant, c.human, c.cid, command)
    assert unknown.value.body["error"]["code"] == "CONVERSATION_TRANSFER_UNKNOWN"
    assert c.h.store.read_snapshot((key,))[key] == original and len(c.creates) == 1
    pending = preview(c.control, c.h.assistant, c.human, c.cid)
    assert pending.state == "pending" and pending.hosted_session_id == c.creates[0].session_id
    deployed_versions(monkeypatch, "43")
    completed = prepare(c.control, c.h.assistant, c.human, c.cid, command, reconcile=True)
    assert completed.state == "ready" and completed.hosted_session_id == pending.hosted_session_id
    assert completed.destination_agent_version == "42" and len(c.creates) == 1 and len(c.reads) == 1
    assert c.reads[0] == c.creates[0] and len(c.io) == 1


@pytest.mark.parametrize("transfer_case", ["unknown", "not_forwarded"], indirect=True)
def test_unknown_or_unforwarded_failure_cannot_prepare_an_environment(transfer_case):
    from shared_context.behavior_conversation_transfer import prepare, TransferCommand
    c = transfer_case
    before = c.h.transport.backing.rows.copy()
    with pytest.raises(Problem) as denied:
        prepare(c.control, c.h.assistant, c.human, c.cid,
                TransferCommand(request_id="no-new-environment", failed_job_id=c.failed.job_id, expected_agent_version="42"))
    assert denied.value.body["error"]["code"] == "CONVERSATION_TRANSFER_NOT_READY"
    assert c.h.transport.backing.rows == before and c.creates == c.reads == []


def test_missing_archive_part_refuses_before_compute_or_transfer_reservation(transfer_case, monkeypatch):
    from shared_context.behavior_conversation_transfer import prepare, TransferCommand
    c = transfer_case
    before = c.h.transport.backing.rows.copy()
    def missing(*args, **kwargs):
        raise Problem(404, "ARTIFACT_BLOB_NOT_FOUND", "OFFLINE required checkpoint part is missing")
    monkeypatch.setattr(c.h.assistant.artifacts.store, "read", missing)
    with pytest.raises(Problem) as denied:
        prepare(c.control, c.h.assistant, c.human, c.cid,
                TransferCommand(request_id="no-empty-restore", failed_job_id=c.failed.job_id, expected_agent_version="42"))
    assert denied.value.status == 409
    assert denied.value.body["error"]["code"] == "CONVERSATION_TRANSFER_ARCHIVE_INVALID"
    assert c.h.transport.backing.rows == before and c.creates == c.reads == []


def test_unreadable_prepared_environment_never_changes_conversation_or_recreates(transfer_case):
    from shared_context.behavior_conversation_transfer import prepare, TransferCommand
    c = transfer_case
    key = conversation_key(c.cid)
    original = c.h.store.read_snapshot((key,))[key]
    command = TransferCommand(request_id="original-unreadable-new-sid", failed_job_id=c.failed.job_id, expected_agent_version="42")
    c.platform["read_unavailable"] = True
    for reconcile in (False, True, True):
        with pytest.raises(Problem) as unknown:
            prepare(c.control, c.h.assistant, c.human, c.cid, command, reconcile=reconcile)
        assert unknown.value.body["error"]["code"] == "CONVERSATION_TRANSFER_UNKNOWN"
        assert c.h.store.read_snapshot((key,))[key] == original
    assert len(c.creates) == 1 and len(c.reads) == 3
    assert all(value == c.creates[0] for value in c.reads)
    assert len(c.io) == 1


def test_new_human_message_restores_the_last_legal_archive_without_replaying_the_failed_turn(transfer_case):
    from shared_context.behavior_conversation_transfer import prepare, TransferCommand
    c = transfer_case
    original_failed = c.control.records[c.failed.binding_id]
    prepared = prepare(c.control, c.h.assistant, c.human, c.cid,
                       TransferCommand(request_id="human-chosen-new-environment", failed_job_id=c.failed.job_id,
                                       expected_agent_version="42"))
    projection = c.control.conversation(c.human, c.cid)
    assert projection["can_follow_up"] is True and projection["follow_up_blocker"] is None
    assert len(c.io) == 1 and len(c.creates) == 1
    c.h.workers.adapter = c.h.executor.adapter()
    c.h.endpoint.replies = iter([{"content": "Continued from the original saved context."}])
    third, _ = c.control.submit(c.human, InstructionSubmit(
        request_id="new-human-message", conversation_id=c.cid, content="This is a new explicitly sent message."))
    registered = c.control.records[third.binding_id]
    assert registered.hosted_session_id == prepared.hosted_session_id
    assert registered.environment_transfer_id == prepared.transfer_id
    assert registered.profile.instruction.turn_number == 3
    assert registered.profile.instruction.hosted_agent_version == "42"
    assert c.h.runtime.controller.step(third.binding_id).state == "succeeded"
    assert len(c.io) == 2 and c.io[1]["resume"] is True
    assert c.io[0]["sdk_session_id"] == c.io[1]["sdk_session_id"]
    messages = [message["content"] for message in c.io[1]["messages"]]
    assert any("Original reply and context remain available." in message for message in messages)
    assert all("Do not resend this automatically." not in message for message in messages)
    assert c.control.records[c.failed.binding_id] == original_failed
    assert c.h.workers.inspect(c.failed.job_id).state == "failed"
    assert c.control.read_reply(c.human, c.cid, 1).text == "Original reply and context remain available."
    assert c.control.conversation(c.human, c.cid)["can_follow_up"] is True
    from shared_context.behavior_instruction import validate_instruction_history
    history = retained_history(c)
    validate_instruction_history(history.inventory, history.config,
                                 tuple(record.binding for record in c.control.records.values()))


@pytest.mark.parametrize("unknown", [False, True])
def test_transfer_reservation_and_readiness_remain_classified_in_the_history_reader(transfer_case, unknown):
    from shared_context.behavior_conversation_transfer import (
        prepare, TransferCommand, validate_history, EnvironmentTransfer, EnvironmentHead,
    )
    from shared_context.behavior_instruction import validate_instruction_history
    from shared_context.behavior_migration import _kind_models
    c = transfer_case
    command = TransferCommand(request_id="retain-explicit-transfer", failed_job_id=c.failed.job_id, expected_agent_version="42")
    c.platform["lose_create"] = unknown
    if unknown:
        with pytest.raises(Problem):
            prepare(c.control, c.h.assistant, c.human, c.cid, command)
    else:
        prepare(c.control, c.h.assistant, c.human, c.cid, command)
    view = retained_history(c)
    transfers = validate_history(view, tuple(c.control.records.values()))
    assert len(transfers) == 1
    kinds = _kind_models()
    assert kinds["conversation_environment_transfer"] is EnvironmentTransfer
    assert kinds["conversation_environment_head"] is EnvironmentHead
    checked = validate_instruction_history(view.inventory, view.config,
                                           tuple(record.binding for record in c.control.records.values()))
    assert set(transfers) <= checked


def test_environment_http_routes_only_prepare_after_explicit_post_and_reconcile_the_original(transfer_case):
    from shared_context.behavior_work_policy_api import install_work_policy_routes
    c = transfer_case
    install_work_policy_routes(c.h.app, SimpleNamespace(
        runtime=c.h.runtime, verifier=c.h.verifier, installation=lambda: None,
        assistant=SimpleNamespace(authority=c.h.assistant),
    ))
    path = f"/work-policy/v1/conversations/{c.cid}/environment"
    viewed = c.h.client.get(path, headers=c.h.headers())
    assert viewed.status_code == 200, viewed.text
    assert viewed.json()["state"] == "available" and c.creates == c.reads == []
    command = {"request_id": "explicit-http-transfer", "failed_job_id": c.failed.job_id, "expected_agent_version": "42"}
    c.platform["lose_create"] = True
    result = c.h.client.post(path, json=command, headers=c.h.headers())
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "CONVERSATION_TRANSFER_UNKNOWN"
    assert result.json()["error"]["reconcile_original"] is True
    response = c.h.client.post(path + "-reconcile", json=command, headers=c.h.headers())
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "ready" and response.json()["sdk_restored"] is False
    assert len(c.creates) == 1 and len(c.reads) == 1 and len(c.io) == 1


def test_changed_destination_and_client_authority_fields_cannot_select_the_environment(transfer_case, monkeypatch):
    from shared_context.behavior_work_policy_api import install_work_policy_routes
    c = transfer_case
    install_work_policy_routes(c.h.app, SimpleNamespace(
        runtime=c.h.runtime, verifier=c.h.verifier, installation=lambda: None,
        assistant=SimpleNamespace(authority=c.h.assistant),
    ))
    path = f"/work-policy/v1/conversations/{c.cid}/environment"
    command = {"request_id": "displayed-old-version", "failed_job_id": c.failed.job_id, "expected_agent_version": "42"}
    before = c.h.transport.backing.rows.copy()
    deployed_versions(monkeypatch, "43")
    changed = c.h.client.post(path, json=command, headers=c.h.headers())
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "CONVERSATION_TRANSFER_CHANGED"
    for extra in ("agent_oid", "hosted_session_id", "archive_id"):
        denied = c.h.client.post(path, json={**command, extra: "untrusted-choice"}, headers=c.h.headers())
        assert denied.status_code == 422
    assert c.h.transport.backing.rows == before and c.creates == c.reads == []


def test_transferred_compacted_archive_keeps_its_original_compaction_lineage(compacted_transfer_source, monkeypatch):
    from shared_context.behavior_conversation_transfer import prepare, TransferCommand
    from shared_context.behavior_sdk_files import SdkSessionFiles
    from shared_context.behavior_sdk_state import CheckpointProfile
    from test_instruction_flow import check_history
    original = compacted_transfer_source
    state = original.fs.snapshot()
    reference = settle(original, state, compaction=original.fs.compaction)
    cid = original.assignment.task.instruction.conversation_id
    c = failed_environment(original.h, original.control, original.human, cid, monkeypatch)
    prepared = prepare(c.control, c.h.assistant, c.human, cid, TransferCommand(
        request_id="preserve-compacted-context", failed_job_id=c.failed.job_id, expected_agent_version="42"))
    assert prepared.archive_turn_number == 2
    c.h.workers.adapter = c.h.executor.adapter()
    following = open_turn(c.h, c.control, c.human, "new-human-after-compacted-transfer", cid)
    try:
        restored = following.client.restore()
        assert restored == state
        fs = SdkSessionFiles(restored.sdk_session_id, restored, checkpoint_profile=CheckpointProfile())
        saved = settle(following, fs.snapshot())
        assert saved.source.execution == reference.source.execution
        assert saved.source.previous_job_id == original.binding.job_id
        assert saved.source.execution.job_id != following.binding.job_id
        check_history(c.h)
    finally:
        following.client.close()


def test_two_clients_cannot_create_two_environments_for_the_same_failed_turn(transfer_case):
    from concurrent.futures import ThreadPoolExecutor
    from shared_context.behavior_conversation_transfer import prepare, preview, TransferCommand
    c = transfer_case
    command = TransferCommand(request_id="same-explicit-choice", failed_job_id=c.failed.job_id, expected_agent_version="42")
    def choose():
        try:
            return prepare(c.control, c.h.assistant, c.human, c.cid, command)
        except Problem as error:
            assert error.body["error"]["code"] in {
                "BEHAVIOR_CONFLICT", "CONVERSATION_TRANSFER_CHANGED", "CONVERSATION_TRANSFER_SCOPE",
            }
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        choices = [pool.submit(choose) for _ in range(2)]
        results = [choice.result() for choice in choices]
    assert any(result is not None and result.state == "ready" for result in results)
    assert len(c.creates) == 1
    assert preview(c.control, c.h.assistant, c.human, c.cid).state == "ready"
    assert len(c.io) == 1


@pytest.mark.parametrize("identity", ["another-human", "another-team", "read-only"])
def test_current_human_submit_authority_is_required_for_environment_preparation(transfer_case, identity):
    from shared_context.behavior_conversation_transfer import prepare, TransferCommand
    c = transfer_case
    principal = replace(c.human, **(
        {"oid": "another-human"} if identity == "another-human" else
        {"team": "another-team"} if identity == "another-team" else
        {"actions": frozenset({"read"})}
    ))
    before = c.h.transport.backing.rows.copy()
    with pytest.raises(Problem) as denied:
        prepare(c.control, c.h.assistant, principal, c.cid,
                TransferCommand(request_id="no-foreign-choice", failed_job_id=c.failed.job_id, expected_agent_version="42"))
    assert denied.value.status == 403
    assert c.h.transport.backing.rows == before and c.creates == c.reads == []
