"""T2 control/ordering seam only; the T1 receipt consumer is injected, not reimplemented."""

from threading import Event

import pytest
from pydantic import SecretBytes

from shared_context.behavior_agent_worker import (
    AgentTurnAddress,
    AgentTurnDelivery,
    AgentVmBinding,
    AgentWorkerControl,
)
from shared_context.errors import Problem


def vm(agent="agent-one"):
    return AgentVmBinding(
        team_id="team-one", agent_id=agent, hosted_session_id="hosted-" + agent,
        hosted_agent_version="1", vm_generation="original-vm-generation",
    )


def delivery(cid, rid):
    return AgentTurnDelivery(
        address=AgentTurnAddress(conversation_id=cid, sdk_session_id="sdk-" + cid, request_id=rid),
        payload=SecretBytes(("original-ticket-" + rid).encode()),
    )


def control(binding, consumer):
    return AgentWorkerControl(
        binding, consumer, process_id="process-" + binding.agent_id, caller_oid="verified-" + binding.agent_id,
    )


class OriginalConsumer:
    def __init__(self):
        self.entered = Event()
        self.release = Event()
        self.second_entered = Event()
        self.second_saved = Event()
        self.calls = []
        self.starts = []
        self.receipts = {}
        self.cancelled = []

    def consume(self, context, request):
        self.starts.append((context, request))
        return ("original-start-" + request.address.request_id).encode()

    def execute(self, context, request, start):
        assert start == ("original-start-" + request.address.request_id).encode()
        self.calls.append((context.vm, request))
        if request.address in self.cancelled:
            return self.receipts[request.address]
        if request.address.request_id == "rid-one":
            self.entered.set()
            if not self.release.wait(5):
                raise RuntimeError("OFFLINE test did not release the first turn")
        else:
            self.second_entered.set()
        receipt = ("original-receipt-" + request.address.request_id).encode()
        self.receipts[request.address] = receipt
        if request.address.request_id == "rid-two":
            self.second_saved.set()
        return receipt

    def read(self, binding, address):
        return self.receipts[address]

    def cancel(self, binding, address):
        self.cancelled.append(address)
        self.receipts[address] = ("original-cancel-" + address.request_id).encode()
        return self.receipts[address]


def test_an_existing_ticket_consumption_reads_its_receipt_without_executing_again():
    class OnceConsumer(OriginalConsumer):
        def consume(self, context, request):
            if request.address in self.receipts:
                raise Problem(409, "TURN_ALREADY_CONSUMED", "read the original receipt")
            return super().consume(context, request)

    binding, consumer = vm(), OnceConsumer()
    worker = control(binding, consumer)
    connection = worker.connect(binding)
    request = delivery("cid-two", "rid-two")
    try:
        assert connection.deliver(request).result(timeout=2) == b"original-receipt-rid-two"
        with pytest.raises(Problem) as duplicate:
            connection.deliver(request).result(timeout=2)
        assert duplicate.value.body["error"]["code"] == "TURN_ALREADY_CONSUMED"
        assert connection.read(request.address) == b"original-receipt-rid-two"
        assert len(consumer.starts) == len(consumer.calls) == 1
    finally:
        worker.close()


def test_one_agent_orders_two_conversations_and_preserves_each_original_receipt():
    binding, consumer = vm(), OriginalConsumer()
    worker = control(binding, consumer)
    connection = worker.connect(binding)
    first, second = delivery("cid-one", "rid-one"), delivery("cid-two", "rid-two")
    try:
        pending_one = connection.deliver(first)
        assert consumer.entered.wait(2)
        pending_two = connection.deliver(second)
        assert not consumer.second_entered.wait(0.1)
        consumer.release.set()
        assert pending_one.result(timeout=2) == b"original-receipt-rid-one"
        assert pending_two.result(timeout=2) == b"original-receipt-rid-two"
        assert consumer.calls == [(binding, first), (binding, second)]
        assert connection.read(first.address) == b"original-receipt-rid-one"
        assert connection.read(second.address) == b"original-receipt-rid-two"
        assert second.payload.get_secret_value() == b"original-ticket-rid-two"
    finally:
        consumer.release.set()
        worker.close()


def test_cancelling_a_queued_cid_bypasses_the_running_turn_without_stopping_it():
    binding, consumer = vm(), OriginalConsumer()
    worker = control(binding, consumer)
    connection = worker.connect(binding)
    first, second = delivery("cid-one", "rid-one"), delivery("cid-two", "rid-two")
    try:
        pending_one = connection.deliver(first)
        assert consumer.entered.wait(2)
        pending_two = connection.deliver(second)
        assert connection.cancel(second.address) == b"original-cancel-rid-two"
        assert not pending_one.done()
        assert not consumer.second_entered.is_set()
        assert consumer.cancelled == [second.address]
        consumer.release.set()
        assert pending_one.result(timeout=2) == b"original-receipt-rid-one"
        assert pending_two.result(timeout=2) == b"original-cancel-rid-two"
        assert not consumer.second_entered.is_set()
    finally:
        consumer.release.set()
        worker.close()


def test_different_agents_can_execute_while_the_first_agent_is_busy():
    first_consumer, second_consumer = OriginalConsumer(), OriginalConsumer()
    first_worker = control(vm(), first_consumer)
    second_worker = control(vm("agent-two"), second_consumer)
    try:
        pending = first_worker.connect(vm()).deliver(delivery("cid-one", "rid-one"))
        assert first_consumer.entered.wait(2)
        other = second_worker.connect(vm("agent-two")).deliver(delivery("other-cid", "other-rid"))
        assert other.result(timeout=2) == b"original-receipt-other-rid"
        assert not pending.done()
    finally:
        first_consumer.release.set()
        first_worker.close()
        second_worker.close()


def test_one_connection_per_vm_and_a_late_close_cannot_close_its_replacement():
    binding, consumer = vm(), OriginalConsumer()
    worker = control(binding, consumer)
    original = worker.connect(binding)
    try:
        with pytest.raises(Problem) as duplicate:
            worker.connect(binding)
        assert duplicate.value.body["error"]["code"] == "AGENT_WORKER_CONNECTION_BUSY"
        for field, value in (
            ("team_id", "other-team"), ("agent_id", "other-agent"),
            ("hosted_session_id", "other-session"), ("hosted_agent_version", "2"),
            ("vm_generation", "other-generation"),
        ):
            with pytest.raises(Problem) as changed:
                worker.connect(binding.model_copy(update={field: value}))
            assert changed.value.body["error"]["code"] == "AGENT_WORKER_BINDING_CHANGED"
        original.close()
        replacement = worker.connect(binding)
        original.close()
        request = delivery("cid-two", "rid-two")
        assert replacement.deliver(request).result(timeout=2) == b"original-receipt-rid-two"
        with pytest.raises(Problem) as stale:
            original.deliver(request)
        assert stale.value.body["error"]["code"] == "AGENT_WORKER_UNAVAILABLE"
        assert len(consumer.calls) == 1
    finally:
        consumer.release.set()
        worker.close()


def test_losing_the_reply_waiter_does_not_drop_or_resend_the_queued_original_turn():
    binding, consumer = vm(), OriginalConsumer()
    worker = control(binding, consumer)
    connection = worker.connect(binding)
    first, second = delivery("cid-one", "rid-one"), delivery("cid-two", "rid-two")
    try:
        pending_one = connection.deliver(first)
        assert consumer.entered.wait(2)
        pending_two = connection.deliver(second)
        assert pending_two.cancel()
        connection.close()
        replacement = worker.connect(binding)
        consumer.release.set()
        assert pending_one.result(timeout=2) == b"original-receipt-rid-one"
        assert consumer.second_saved.wait(2), "a lost transport waiter is not a user cancellation"
        assert consumer.calls == [(binding, first), (binding, second)]
        assert replacement.read(second.address) == b"original-receipt-rid-two"
    finally:
        consumer.release.set()
        worker.close()


def test_queued_turn_consumes_with_the_replacement_connection_and_verified_worker_identity():
    binding, consumer = vm(), OriginalConsumer()
    worker = AgentWorkerControl(binding, consumer, process_id="process-one", caller_oid="verified-agent-oid")
    original = worker.connect(binding)
    first, second = delivery("cid-one", "rid-one"), delivery("cid-two", "rid-two")
    try:
        pending_one = original.deliver(first)
        assert consumer.entered.wait(2)
        pending_two = original.deliver(second)
        original.close()
        replacement = worker.connect(binding)
        consumer.release.set()
        pending_one.result(timeout=2)
        pending_two.result(timeout=2)
        assert len(consumer.starts) == 2
        first_context, second_context = (value[0] for value in consumer.starts)
        assert first_context.control_connection_id != second_context.control_connection_id
        assert second_context == replacement.context
        assert second_context.vm == binding
        assert second_context.process_id == "process-one"
        assert second_context.caller_oid == "verified-agent-oid"
        assert consumer.starts[1][1] == second
    finally:
        consumer.release.set()
        worker.close()


def test_hosted_runtime_binds_only_its_actual_agent_session_and_keeps_the_original_pool(tmp_path):
    from shared_context import behavior_wire as wire
    from shared_context.behavior_assistant_hosted import AssistantHostedRuntime, AssistantSupervisor

    binding = vm().model_copy(update={"agent_id": "11111111-1111-1111-1111-111111111111"})
    identity = wire.HostedIdentity(
        agent_id=binding.agent_id, agent_name="offline-agent",
        agent_version=binding.hosted_agent_version, session_id=binding.hosted_session_id,
    )
    supervisor = AssistantSupervisor(tmp_path / "hosted", runtime_path=tmp_path / "unused-runtime")
    runtime = AssistantHostedRuntime(identity, supervisor, object())
    consumer = OriginalConsumer()
    original_pool = supervisor.resident
    worker = runtime.bind_agent_worker(binding, consumer, caller_oid="verified-agent-oid")
    try:
        assert worker is runtime.bind_agent_worker(binding, consumer, caller_oid="verified-agent-oid")
        assert supervisor.resident is original_pool
        connection = worker.connect(binding)
        request = delivery("cid-two", "rid-two")
        assert connection.deliver(request).result(timeout=2) == b"original-receipt-rid-two"
        assert consumer.starts[0][0].process_id == supervisor.supervisor_id
        for field, value in (
            ("agent_id", "other-agent"), ("hosted_session_id", "other-session"),
            ("hosted_agent_version", "2"), ("vm_generation", "another-logical-generation"),
        ):
            with pytest.raises(Problem):
                runtime.bind_agent_worker(binding.model_copy(update={field: value}), consumer,
                                          caller_oid="verified-agent-oid")
    finally:
        consumer.release.set()
        worker.close()
