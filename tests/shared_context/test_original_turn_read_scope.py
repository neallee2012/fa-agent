"""Warm original-turn reads revalidate their own work, not unrelated conversation histories."""

import pytest

from shared_context.behavior_operations import control_key
from shared_context.behavior_work_policy import work_policy_key
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_instruction_text_files import configured_files
from test_m3_auth import jwk_client, signing_key
from stored_turn_context_helpers import load_case


def stored_prepared(h, saved=False):
    """One admitted file turn and 8 unrelated instructions (`saved`: the target also completed), as stored."""
    h, control, _ = configured_files(h)
    route = load_case(h, "context-read-scope-" + ("saved" if saved else "admitted"))["route"]
    control.refresh()
    by_request = {record.request_id: record for record in control.records.values()}
    unrelated = [work_policy_key("registration", by_request[f"unrelated-file-read-{index}"].admission_id)
                 for index in range(8)]
    return h, control, by_request["original-file-read"], unrelated, route


@pytest.mark.parametrize("kind", ["file", "reply", "context"])
def test_warm_file_read_uses_one_original_registration_and_no_unrelated_work(assistant, monkeypatch, kind):
    h, _, target, unrelated, route = stored_prepared(assistant, saved=kind != "file")
    if kind != "file":
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
    h, control, target, _, route = stored_prepared(assistant)
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
    h, control, _, _, route = stored_prepared(assistant)
    control.records.clear()
    result = h.client.get(route, headers=h.headers("read-only"))
    assert result.status_code == 200 and result.content == b"Original file bytes."
