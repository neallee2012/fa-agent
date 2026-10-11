"""The existing relay keeps only a bounded, one-item root allowance, not messages or receipts."""

from conftest import InMemoryRepository
import pytest

from shared_context.errors import Problem


def test_root_counts_ten_new_messages_once_without_storing_contents_or_results():
    from shared_context.native_relay_quota import NativeRelayQuota

    repository = InMemoryRepository()
    quota = NativeRelayQuota(repository, tenant_id="tenant", team_id="team")
    root = "a" * 64
    for number in range(1, 11):
        identifier, payload = f"{number:064x}", f"{number + 20:064x}"
        first = quota.reserve_message(root, identifier, payload)
        assert quota.reserve_message(root, identifier, payload) == first
    assert len(quota.read(root).messages) == 10
    final = quota.read(root)
    assert len(final.messages) == 10
    assert all(set(message.model_dump()) == {"message_id", "payload_sha256"} for message in final.messages)
    rows, more = repository.list_prefix(quota.partition_key, "root:", 2)
    assert len(rows) == 1 and not more
    assert len(final.model_dump_json().encode("utf-8")) <= 4096


def test_eleventh_message_and_changed_material_do_not_consume_another_slot():
    from shared_context.native_relay_quota import NativeRelayQuota

    quota = NativeRelayQuota(InMemoryRepository(), tenant_id="tenant", team_id="team")
    for number in range(1, 11):
        quota.reserve_message("a" * 64, f"{number:064x}", f"{number + 20:064x}")
    before = quota.read("a" * 64)
    for message_id, payload, code in (
        (f"{11:064x}", "b" * 64, "AGENT_MESSAGE_ALLOWANCE"),
        (f"{1:064x}", "f" * 64, "AGENT_MESSAGE_CONFLICT"),
    ):
        with pytest.raises(Problem) as refused:
            quota.reserve_message("a" * 64, message_id, payload)
        assert refused.value.body["error"]["code"] == code
        assert quota.read("a" * 64) == before


@pytest.mark.parametrize("existing", [False, True], ids=["create", "replace"])
def test_a_lost_quota_ack_reads_the_same_item_without_repeating_the_write(existing, monkeypatch):
    from shared_context.native_relay_quota import NativeRelayQuota

    repository = InMemoryRepository()
    quota = NativeRelayQuota(repository, tenant_id="tenant", team_id="team")
    if existing:
        quota.reserve_message("a" * 64, "1" * 64, "2" * 64)
    name = "replace" if existing else "create"
    write = getattr(repository, name)
    writes = []

    def lost(*args):
        writes.append(args)
        write(*args)
        raise Problem(503, "UPSTREAM_UNKNOWN", "synthetic lost write response")

    monkeypatch.setattr(repository, name, lost)
    accepted = quota.reserve_message("a" * 64, "3" * 64, "4" * 64)
    assert accepted.message_id == "3" * 64 and len(writes) == 1
    assert quota.reserve_message("a" * 64, "3" * 64, "4" * 64) == accepted
    assert len(writes) == 1 and len(quota.read("a" * 64).messages) == 1 + int(existing)


def test_two_callers_competing_for_the_last_slot_cannot_exceed_ten(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock
    from shared_context.native_relay_quota import NativeRelayQuota

    repository = InMemoryRepository()
    quota = NativeRelayQuota(repository, tenant_id="tenant", team_id="team")
    for number in range(1, 10):
        quota.reserve_message("a" * 64, f"{number:064x}", f"{number + 20:064x}")
    barrier, lock, counter = Barrier(2), Lock(), [0]
    get = repository.get

    def together(*args):
        value = get(*args)
        with lock:
            counter[0] += 1
            wait = counter[0] <= 2
        if wait:
            barrier.wait(timeout=5)
        return value

    monkeypatch.setattr(repository, "get", together)

    def reserve(number):
        try:
            return quota.reserve_message("a" * 64, f"{number:064x}", f"{number + 20:064x}").message_id
        except Problem as error:
            return error.body["error"]["code"]

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(reserve, (10, 11)))
    assert sum(value in {f"{10:064x}", f"{11:064x}"} for value in outcomes) == 1
    assert "NATIVE_RELAY_QUOTA_UNKNOWN" in outcomes
    assert len(quota.read("a" * 64).messages) == 10
