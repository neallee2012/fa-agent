"""Warm original-turn reads revalidate their own work, not unrelated conversation histories."""

import pytest

from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_operations import control_key
from shared_context.behavior_text_files import UserTextFile
from shared_context.behavior_work_policy import work_policy_key
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path, sdk_io
from test_instruction_text_files import configured_files, post
from test_m3_auth import jwk_client, signing_key


def prepared(h):
    h, control, _ = configured_files(h)
    command = InstructionSubmit(request_id="original-file-read", content="Keep the original input.",
                                files=(UserTextFile(name="input.txt", text="Original file bytes."),))
    accepted = post(h, command)
    assert accepted.status_code == 200, accepted.text
    value = accepted.json()
    target = control.records[value["binding_id"]]
    unrelated = []
    for index in range(8):
        response = post(h, InstructionSubmit(request_id=f"unrelated-file-read-{index}",
                                             content="Unrelated retained instruction."))
        assert response.status_code == 200
        record = control.records[response.json()["binding_id"]]
        unrelated.append(work_policy_key("registration", record.admission_id))
    route = f"/work-policy/v1/conversations/{value['conversation_id']}/turns/1/files/input.txt"
    return h, control, target, unrelated, route


@pytest.mark.parametrize("kind", ["file", "reply", "context"])
def test_warm_file_read_uses_one_original_registration_and_no_unrelated_work(assistant, monkeypatch, kind):
    h, _, target, unrelated, route = prepared(assistant)
    if kind != "file":
        sdk_io(monkeypatch)
        h.endpoint.unbounded = True
        h.endpoint.replies = iter([{"content": "Original saved result."}])
        assert h.runtime.controller.step(target.binding.binding_id).state == "succeeded"
        route = route.rsplit("/files/", 1)[0] + "/" + kind
    h.transport.requests.clear()
    result = h.client.get(route, headers=h.headers("read-only"))
    assert result.status_code == 200, result.text
    if kind == "file":
        assert result.content == b"Original file bytes."
    target_key = work_policy_key("registration", target.admission_id)
    target_reads = [item for item in h.transport.requests if item.method == "GET" and target_key in str(item.url)]
    other_reads = [item for item in h.transport.requests if item.method == "GET"
                   and any(key in str(item.url) for key in unrelated)]
    assert len(target_reads) == 1 and not other_reads, (len(target_reads), len(other_reads))


@pytest.mark.parametrize("change", ["owner", "registration", "registry"])
def test_cached_original_work_is_never_cached_read_authority(assistant, change):
    h, control, target, _, route = prepared(assistant)
    assert h.client.get(route, headers=h.headers("read-only")).status_code == 200
    if change == "owner":
        h.mutate(control_key("owner", target.binding.owner.owner_record_id), active=False)
    elif change == "registration":
        h.mutate(work_policy_key("registration", target.admission_id), submit_sha256="f" * 64)
    else:
        current = h.store.read_snapshot((control.registry_key,))[control.registry_key].payload["data"]
        h.mutate(control.registry_key, entries=[item for item in current["entries"] if item != target.admission_id])
    before = h.store._guard()
    if change == "registry":
        # 7c66d6a9 (PR #264): an admitted turn keeps its identity after its index row goes; checked at next admission.
        kept = h.client.get(route, headers=h.headers("read-only"))
        assert kept.status_code == 200, kept.text
        assert kept.content == b"Original file bytes." and h.store._guard() == before
        return
    denied = h.client.get(route, headers=h.headers("read-only"))
    assert denied.status_code in {403, 404, 409}
    assert b"Original file bytes." not in denied.content and h.store._guard() == before


def test_cold_cache_loads_original_registration_without_inventing_a_replacement(assistant):
    h, control, _, _, route = prepared(assistant)
    control.records.clear()
    result = h.client.get(route, headers=h.headers("read-only"))
    assert result.status_code == 200 and result.content == b"Original file bytes."
