"""Every admission and receipt gate, one fault at a time, with its exact refusal."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ResourceNotFoundError

from shared_context.behavior_delivery import DeliveryCore, ReceiptRequest, obligation_id_for
from shared_context.behavior_inbox import InboxRegistration, PullInbox
from shared_context.behavior_operations import control_key
from shared_context.behavior_store import ControlWrite
from shared_context.errors import Problem
from test_behavior_delivery import CONTEXT, NOW, Harness, final_ready
from test_behavior_inbox import InboxHarness
from test_behavior_store import code


def refused(action):
    with pytest.raises(Problem) as error:
        action()
    return code(error), error.value.body["error"]["message"]


def instruction(h, writes=()):
    h.core.work_policy = SimpleNamespace(
        _admission_record=lambda _: SimpleNamespace(profile=SimpleNamespace(instruction=True)),
        admission_keys=lambda _: (), admission_observation=lambda _: None,
        admission_writes=lambda *args: writes,
        admission_commit=lambda command, observation, commit: commit(),
    )


def instruction_command(h, **updates):
    command = h.command().model_copy(update={"deadline": None, **updates})
    h.attest(command)
    return command


def ingress(h, command, **updates):
    h.ingress[command.ingress_evidence_id] = h.ingress[command.ingress_evidence_id].model_copy(update=updates)


def readiness(h, **updates):
    proof = h.readiness().model_copy(update=updates)
    h.core = h.broker(h.store, replace(h.registration, verify_readiness=lambda: proof))


def repoint(h, command, **updates):
    changed = command.model_copy(update=updates)
    h.attest(changed)
    return changed


def test_admission_requires_an_installed_ingress_verifier():
    h = Harness()
    core = h.broker(h.store, replace(h.registration, verify_ingress=None))
    assert refused(lambda: core.admit(h.command(), CONTEXT))[0] == "DELIVERY_INGRESS_DISABLED"


@pytest.mark.parametrize("change", [
    "evidence", "digest", "verifier", "principal", "conversation", "channel",
])
def test_each_ingress_binding_is_checked_before_any_write(change):
    h = Harness()
    command = h.command()
    if change == "conversation":
        command = repoint(h, command, event=command.event.model_copy(update={"conversation_id": "other"}))
    elif change == "channel":
        command = repoint(h, command, route=command.route.model_copy(update={"channel_id": "other"}))
    else:
        ingress(h, command, **{
            "evidence": {"evidence_id": "other"}, "digest": {"admission_digest": "0" * 64},
            "verifier": {"verifier_version": "other"}, "principal": {"on_behalf_of_principal_id": "other"},
        }[change])
    count = h.submissions()
    assert refused(lambda: h.core.admit(command, CONTEXT))[0] == "DELIVERY_INGRESS_DENIED"
    assert h.submissions() == count


@pytest.mark.parametrize("row,ids,updates", [
    ("policy", ("policy",), {"policy_id": "other"}),
    ("policy", ("policy",), {"revision": 2}),
    ("policy", ("policy",), {"active": False}),
    ("budget", ("root",), {"root_budget_id": "other"}),
    ("budget", ("root",), {"active": False}),
])
def test_current_policy_and_budget_are_read_inside_the_admission(row, ids, updates):
    h = Harness()
    h.mutate(row, *ids, **updates)
    assert refused(lambda: h.core.admit(h.command(), CONTEXT))[0] == "DELIVERY_ADMISSION_DENIED"


@pytest.mark.parametrize("updates", [
    {"observed_at": NOW + timedelta(seconds=1)}, {"expires_at": NOW},
])
def test_ingress_evidence_must_be_inside_its_window(updates):
    h = Harness()
    command = h.command()
    ingress(h, command, **updates)
    assert refused(lambda: h.core.admit(command, CONTEXT))[0] == "DELIVERY_ADMISSION_DENIED"


def test_readiness_evidence_cannot_come_from_the_future():
    h = Harness()
    readiness(h, observed_at=NOW + timedelta(seconds=1))
    assert refused(lambda: h.core.admit(h.command(), CONTEXT))[0] == "DELIVERY_ADMISSION_DENIED"


@pytest.mark.parametrize("deadline", [None, NOW, NOW + timedelta(seconds=301)])
def test_ordinary_admission_needs_a_deadline_inside_the_queue_window(deadline):
    h = Harness()
    command = repoint(h, h.command(), deadline=deadline)
    assert refused(lambda: h.core.admit(command, CONTEXT))[0] == "DELIVERY_ADMISSION_DENIED"


def test_the_last_queue_second_is_admitted():
    h = Harness()
    command = repoint(h, h.command(), deadline=NOW + timedelta(seconds=300))
    assert h.core.admit(command, CONTEXT).payload["result"]["status"] == "queued"


def test_instruction_admission_refuses_a_deadline_and_keeps_no_control_budget():
    h = Harness()
    instruction(h)
    assert refused(lambda: h.core.admit(h.command(), CONTEXT))[0] == "DELIVERY_ADMISSION_DENIED"
    receipt = h.core.admit(instruction_command(h), CONTEXT)
    ob = h.data("delivery-obligation", receipt.payload["result"]["obligation_id"])
    assert ob["normal_control_remaining"] is None and ob["deadline"] is None
    assert ob["review_at"] == (NOW + timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    assert h.data("delivery-quota", "root", "root")["pending"] == 0
    assert h.data("delivery-quota", "team", "team")["pending"] == 0


def test_the_index_ceiling_counts_instruction_work_the_quotas_do_not():
    h = Harness()
    entry = {
        "owner_record_id": CONTEXT.owner_record_id, "conversation_id": CONTEXT.conversation_id,
        "review_at": NOW.isoformat(), "generation": 0, "automation": "active", "due_pending": False, "instruction": True,
    }
    h.mutate("delivery-index", "root", entries=[{**entry, "obligation_id": f"held-{number}"} for number in range(32)])
    assert refused(lambda: h.core.admit(h.command(), CONTEXT))[0] == "DELIVERY_ADMISSION_FULL"


def test_review_never_lands_after_the_deadline():
    h = Harness()
    deadline = NOW + timedelta(seconds=5)
    ob = h.core.admit(repoint(h, h.command(), deadline=deadline), CONTEXT).payload["result"]["obligation_id"]
    assert h.data("delivery-obligation", ob)["review_at"] == deadline.isoformat().replace("+00:00", "Z")


def test_admission_receipt_reports_the_new_sequence():
    assert Harness().admit()[2].payload["result"]["sequence"] == 1


def test_work_policy_rows_join_the_admission_transaction():
    h = Harness()
    row = control_key("synthetic-policy-row", "e1")
    instruction(h, (ControlWrite(row_id=row, mode="create", data={"synthetic": True}),))
    h.core.admit(instruction_command(h), CONTEXT)
    assert h.store.read_snapshot((row,))[row].payload["data"] == {"synthetic": True}


@pytest.mark.parametrize("limit,expected", [
    ("actor", NOW + timedelta(minutes=1)), ("readiness", NOW + timedelta(minutes=2)), ("deadline", NOW + timedelta(minutes=3)),
])
def test_admission_is_valid_until_its_earliest_limit(limit, expected):
    h = Harness()
    readiness(h, expires_at=expected if limit == "readiness" else NOW + timedelta(hours=1))
    command = repoint(h, h.command(), deadline=expected if limit == "deadline" else NOW + timedelta(minutes=4))
    if limit == "actor":
        ingress(h, command, expires_at=expected)
    assert h.core.admit(command, CONTEXT).payload["result"]["valid_until"] == expected.isoformat()


def test_instruction_admission_is_valid_until_its_ingress_expires():
    h = Harness()
    instruction(h)
    receipt = h.core.admit(instruction_command(h), CONTEXT)
    assert receipt.payload["result"]["valid_until"] == (NOW + timedelta(minutes=10)).isoformat()


def test_replay_with_rewritten_ingress_evidence_conflicts():
    h = Harness()
    _, command, _ = h.admit()
    ingress(h, command, proof=b"rewritten scoped ingress evidence")
    assert refused(lambda: h.core.admit(command, CONTEXT))[0] == "DELIVERY_SOURCE_CONFLICT"


def test_replay_after_the_window_returns_the_original_receipt():
    h = Harness()
    _, command, first = h.admit()
    h.time = NOW + timedelta(hours=1)
    assert h.core.admit(command, CONTEXT) == first


def test_commit_observed_before_admitted_at_stays_unknown():
    h = Harness()
    command = h.command()
    request = h.core.request_for(command, CONTEXT)
    h.client.before_submit = lambda: setattr(h, "time", NOW - timedelta(seconds=1))
    with pytest.raises(Problem) as error:
        h.core.admit(command, CONTEXT)
    assert code(error) == "DELIVERY_ADMISSION_UNKNOWN"
    assert error.value.body["error"]["request_id"] == request.request_id
    assert error.value.body["error"]["request_digest"] == request.request_digest


def test_refused_admission_leaves_nothing_behind():
    h = Harness()
    h.mutate("policy", "policy", active=False)
    with pytest.raises(Problem):
        h.core.admit(h.command(), CONTEXT)
    with pytest.raises(ResourceNotFoundError):
        h.raw("delivery-obligation", obligation_id_for("e1"))


def alias(h, ob, **updates):
    h.receipt(ob, evidence_id="alias", outcome="unknown", levels=())
    h.receipts["rebound"] = h.receipts["alias"].model_copy(update=updates)
    return ReceiptRequest(obligation_id=ob, delivery_id="delivery-one", evidence_id="rebound")


def test_native_receipts_come_from_the_channel_adapter_only():
    h = InboxHarness()
    inbox = PullInbox(h.store, InboxRegistration("offline-auth-v1", lambda key: None, verify_deployment=lambda key: None))
    core = DeliveryCore(h.store, clock=lambda: h.time, registration=replace(
        h.registration, mode="native", verify_receipt=None, channel_adapter=inbox))
    inbox.attach(core)
    asked = []
    def verify(key):
        asked.append(key)
        raise Problem(403, "SYNTHETIC_ADAPTER", "the adapter answered")
    inbox.verify_receipt = verify
    command = ReceiptRequest(obligation_id="ob", delivery_id="delivery", evidence_id="evidence")
    assert refused(lambda: core.record_receipt(command, CONTEXT))[0] == "SYNTHETIC_ADAPTER"
    assert asked == ["evidence"]


@pytest.mark.parametrize("updates", [{"evidence_id": "other"}, {"evidence_id": "rebound", "verifier_version": "other"}])
def test_receipt_evidence_must_be_the_one_requested_from_this_verifier(updates):
    h, ob = final_ready()
    command = alias(h, ob, **updates)
    count = h.submissions()
    assert refused(lambda: h.core.record_receipt(command, CONTEXT)) == (
        "DELIVERY_RECEIPT_DENIED", "receipt identity or channel evidence level differs")
    assert h.submissions() == count


@pytest.mark.parametrize("field,value", [
    ("obligation_id", "other"), ("delivery_id", "other"), ("channel_version", "other"),
    ("route", {"tenant_id": "t", "account_id": "a", "recipient_id": "other", "channel_id": "c", "thread_id": "x"}),
])
def test_a_tampered_outbox_cannot_vouch_for_itself(field, value):
    h, ob = final_ready()
    binding = {**h.data("delivery-outbox", "delivery-one")["binding"], field: value}
    h.mutate("delivery-outbox", "delivery-one", binding=binding)
    assert refused(lambda: h.receipt(ob)) == (
        "DELIVERY_RECEIPT_DENIED", "receipt digest, route, version or association differs")
    assert h.data("delivery-obligation", ob)["fulfillment"] == "pending"


def test_a_receipt_must_match_the_retained_payload():
    h, ob = final_ready()
    binding = {**h.data("delivery-outbox", "delivery-one")["binding"], "payload_digest": "0" * 64}
    h.mutate("delivery-outbox", "delivery-one", binding=binding)
    assert refused(lambda: h.receipt(ob)) == ("DELIVERY_RECEIPT_DENIED", "saved payload differs")


def test_receipt_for_another_version_of_the_final_does_not_fulfill():
    h, ob = final_ready()
    h.mutate("delivery-obligation", ob, final_version=9)
    h.receipt(ob)
    assert h.data("delivery-obligation", ob)["fulfillment"] == "pending"


def test_receipt_for_another_delivery_at_the_final_version_is_not_current():
    h = Harness()
    ob, _, _ = h.admit()
    h.message(ob, "ack", "ack")
    h.work_result(ob)
    h.message(ob, "final", "final")
    h.mutate("delivery-obligation", ob, final_version=1)
    h.receipt(ob, "ack", outcome="unknown", levels=())
    assert h.data("delivery-obligation", ob)["delivery_state"] == "final_prepared"


def test_only_a_final_or_failure_outbox_can_fulfill():
    h = Harness()
    ob, _, _ = h.admit()
    h.message(ob, "ack", "ack")
    h.mutate("delivery-obligation", ob, final_delivery_id="ack", final_version=1)
    h.receipt(ob, "ack")
    assert h.data("delivery-obligation", ob)["fulfillment"] == "pending"


def test_late_emitted_receipt_is_recorded_without_fulfilling_again():
    h, ob = final_ready(max_receipts_per_delivery=2)
    h.receipt(ob)
    h.receipt(ob, evidence_id="late", levels=("client_received", "human_read"))
    state = h.data("delivery-obligation", ob)
    assert state["fulfillment_receipt_id"] == "receipt-one" and state["confirmed_levels"] == ["client_received"]
    assert h.data("delivery-outbox", "delivery-one")["receipt_count"] == 2
    assert h.data("delivery-outbox", "delivery-one")["confirmed_levels"] == ["client_received", "human_read"]
    assert h.data("delivery-quota", "root", "root")["pending"] == 0


def test_late_unknown_cannot_rewrite_a_fulfilled_delivery_state():
    h, ob = final_ready()
    h.receipt(ob)
    h.receipt(ob, evidence_id="late", outcome="unknown", levels=())
    assert h.data("delivery-obligation", ob)["delivery_state"] == "fulfilled"


def test_instruction_receipts_have_no_receipt_ceiling():
    h = Harness(max_receipts_per_delivery=1)
    instruction(h)
    ob = h.core.admit(instruction_command(h), CONTEXT).payload["result"]["obligation_id"]
    h.work_result(ob)
    h.message(ob)
    for number in range(3):
        h.receipt(ob, evidence_id=f"unknown-{number}", outcome="unknown", levels=())
    assert h.data("delivery-outbox", "delivery-one")["receipt_count"] == 3


def test_fulfilling_needs_its_reserved_credit():
    h, ob = final_ready()
    h.mutate("delivery-obligation", ob, fulfillment_credit=0)
    assert refused(lambda: h.receipt(ob))[0] == "DELIVERY_CONTROL_EXHAUSTED"


def test_fulfilling_stops_automation_and_clears_pending_review():
    h, ob = final_ready()
    h.due(ob)
    assert h.data("delivery-obligation", ob)["due_pending"] is True
    h.receipt(ob)
    state = h.data("delivery-obligation", ob)
    assert state["due_pending"] is False and state["automation"] == "stopped"


@pytest.mark.parametrize("scope,ids,pending,expected", [
    ("team", ("team",), 0, "DELIVERY_QUOTA_INVALID"),
    ("entry", ("entry",), 0, "DELIVERY_QUOTA_INVALID"),
    ("root", ("root",), 0, "DELIVERY_QUOTA_INVALID"),
    ("root", ("root",), 2, "DELIVERY_INDEX_INVALID"),
])
def test_fulfilling_refuses_quota_rows_that_disagree(scope, ids, pending, expected):
    h, ob = final_ready()
    h.mutate("delivery-quota", scope, *ids, pending=pending)
    assert refused(lambda: h.receipt(ob))[0] == expected


@pytest.mark.parametrize("outcome,state", [
    ("unknown", "delivery_unknown"), ("failed", "delivery_failed"), ("suppressed", "final_prepared"),
])
def test_current_final_outcome_sets_the_delivery_state(outcome, state):
    h, ob = final_ready()
    h.receipt(ob, outcome=outcome, levels=())
    assert h.data("delivery-obligation", ob)["delivery_state"] == state


def test_old_ack_outcomes_cannot_touch_the_prepared_final():
    h = Harness()
    ob, _, _ = h.admit()
    h.message(ob, "ack", "ack")
    h.work_result(ob)
    h.message(ob, "final", "final")
    h.receipt(ob, "ack", evidence_id="ack-unknown", outcome="unknown", levels=())
    h.receipt(ob, "ack", evidence_id="ack-emitted")
    assert h.data("delivery-obligation", ob)["delivery_state"] == "final_prepared"


@pytest.mark.parametrize("kind,outcome", [("progress", "emitted"), ("ack", "unknown")])
def test_only_an_emitted_ack_marks_the_obligation_acked(kind, outcome):
    h = Harness()
    ob, _, _ = h.admit()
    h.message(ob, kind, "note")
    h.receipt(ob, "note", outcome=outcome, levels=())
    assert h.data("delivery-obligation", ob)["delivery_state"] == "pending"


def rows_written(h):
    names = [operation[1]["RowKey"] for operation in h.client.submissions[-1]]
    return [name.removeprefix("behavior:v1:row:") for name in names if name.startswith("behavior:v1:row:")]


QUOTAS = [control_key("delivery-quota", kind, value) for kind, value in (("team", "team"), ("entry", "entry"), ("root", "root"))]
INDEX = control_key("delivery-index", "root")


def admission_rows(ob):
    return [
        control_key("delivery-conversation", CONTEXT.conversation_id), control_key("delivery-event", "e1"),
        control_key("delivery-work", "work-e1"), control_key("delivery-work-state", ob),
        control_key("delivery-obligation", ob),
        control_key("delivery-intent", control_key("delivery-intent-id", "e1")), INDEX,
    ]


def test_admission_writes_in_its_fixed_order():
    h = Harness()
    ob = h.admit()[0]
    assert rows_written(h) == [*admission_rows(ob), *QUOTAS]


def test_instruction_admission_writes_policy_rows_last_and_no_quota():
    h = Harness()
    row = control_key("synthetic-policy-row", "e1")
    instruction(h, (ControlWrite(row_id=row, mode="create", data={"synthetic": True}),))
    ob = h.core.admit(instruction_command(h), CONTEXT).payload["result"]["obligation_id"]
    assert rows_written(h) == [*admission_rows(ob), row]


def test_fulfilling_receipt_writes_in_its_fixed_order():
    h, ob = final_ready()
    h.receipt(ob)
    assert rows_written(h) == [
        *QUOTAS, INDEX, control_key("delivery-obligation", ob), control_key("delivery-outbox", "delivery-one"),
        control_key("delivery-receipt", "delivery-one", "receipt-one"),
    ]


def test_nonfulfilling_receipt_writes_in_its_fixed_order():
    h, ob = final_ready()
    h.receipt(ob, outcome="unknown", levels=())
    assert rows_written(h) == [
        control_key("delivery-obligation", ob), control_key("delivery-outbox", "delivery-one"),
        control_key("delivery-receipt", "delivery-one", "receipt-one"),
    ]


def test_only_the_root_quota_must_equal_the_index():
    h, ob = final_ready()
    h.mutate("delivery-quota", "team", "team", pending=2)
    h.receipt(ob)
    assert h.data("delivery-quota", "team", "team")["pending"] == 1
    assert h.data("delivery-obligation", ob)["fulfillment"] == "fulfilled"


def test_receipt_association_answers_before_the_saved_payload():
    h, ob = final_ready()
    binding = {**h.data("delivery-outbox", "delivery-one")["binding"], "obligation_id": "other", "payload_digest": "0" * 64}
    h.mutate("delivery-outbox", "delivery-one", binding=binding)
    assert refused(lambda: h.receipt(ob))[1] == "receipt digest, route, version or association differs"


def test_receipt_ceiling_answers_before_the_control_credit():
    h, ob = final_ready(max_receipts_per_delivery=2)
    h.receipt(ob, outcome="unknown", levels=())
    h.mutate("delivery-obligation", ob, normal_control_remaining=0)
    assert refused(lambda: h.receipt(ob, evidence_id="extra", outcome="unknown", levels=()))[0] == "DELIVERY_RECEIPT_LIMIT"


def test_sequence_answers_before_an_event_without_a_receipt():
    h = Harness()
    h.put([(control_key("delivery-event", "e1"), h.config)])
    command = h.command(sequence=1)
    assert refused(lambda: h.core.admit(command, CONTEXT))[0] == "DELIVERY_SEQUENCE_CONFLICT"
