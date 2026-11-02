"""_multi_models: each original intent-set rule refuses, and the first failing rule names the error.

Forgery keeps row and receipt hashes coherent, so a case reaches the named rule, not the generic hash check.
"""
from types import SimpleNamespace

import pytest

from history_forgery import Forgery
from shared_context.behavior_assistant import key
from shared_context.behavior_recovery import ModelIntentState, _multi_models
from shared_context.errors import Problem
from test_behavior_assistant_acquire_conflicts import installed_assistant, native_acquire
from test_behavior_finished_result_recovery import durable_result, service_for as finished_service
from test_behavior_multi_model_recovery import mixed_models, service_for
from test_m3_auth import jwk_client, signing_key


SET = ("RECOVERY_MODEL_SET_CHANGED", "complete original model/session set differs")
CONTEXT = ("RECOVERY_MODEL_RECEIPT_INVALID", "original context read lacks its bound producer receipt")
RESERVE = ("RECOVERY_MODEL_RECEIPT_INVALID", "original reservation/image/consume absence differs")
CONSUME = ("RECOVERY_MODEL_RECEIPT_INVALID", "original consume request/image/causal receipt differs")
REVIEWED = ("RECOVERY_MODEL_RECEIPT_INVALID", "original reviewed model receipts changed")


def refusal(forged, config, launch, expected, **options):
    inventory = forged.inventory() if isinstance(forged, Forgery) else forged
    with pytest.raises(Problem) as error:
        _multi_models(inventory, config, launch, expected, **options)
    body = error.value.body["error"]
    return body["code"], body["message"]


def receipt_edit(**updates):
    def change(payload):
        return {**payload, **updates}
    return change


def request_edit(**updates):
    def change(payload):
        return {**payload, "request": {**payload["request"], **updates}}
    return change


@pytest.fixture
def world(mixed_models):
    h, c = mixed_models, mixed_models.c
    service, _ = service_for(h)
    sid = c.assignment.session_id
    inventory = c.store.read_inventory()
    return SimpleNamespace(
        c=c, sid=sid, inventory=inventory, launch=c.native.launch(c.launch_id), intents=service.scope.model_intents,
        session=key("session", sid), consumed=key("model-intent", sid, h.first), reserved=key("model-intent", sid, h.second),
        context=key("context", sid, "current-context"), first_reserve=key("model", sid, h.first),
        second_reserve=key("model", sid, h.second), first_consume=key("model-send", sid, h.first), h=h,
    )


def forged(w):
    return Forgery(w.inventory)


def session_edit(**updates):
    return lambda session: {**session, **updates}


def unread(w):
    """A session that never read context, so a reservation answers to the base reads alone."""
    return forged(w).drop_receipt(w.context).data(w.session, session_edit(context_read=False))


def extra_intent(w):
    extra = key("model-intent", w.sid, "e" * 64)
    return forged(w).copy(w.consumed, extra).data(extra, lambda intent: {**intent, "request_sha256": "e" * 64}, later=True)


def read_set(w, name):
    return list(w.inventory.receipts[name].payload["request"]["read_set"])


def test_the_produced_set_passes_and_binds_its_receipts(world):
    w = world
    proofs = _multi_models(w.inventory, w.c.config, w.launch, w.intents)
    assert [proof.request_sha256 for proof in proofs] == [item.request_sha256 for item in w.intents]
    consumed = {proof.request_sha256: proof for proof in proofs}
    assert consumed[w.h.first].consumed and consumed[w.h.first].consume_receipt_sha256
    assert not consumed[w.h.second].consumed and consumed[w.h.second].consume_receipt_sha256 is None
    assert _multi_models(w.inventory, w.c.config, w.launch, proofs) == proofs


def test_each_set_rule_refuses(world):
    w = world
    c, sid, work = w.c, w.sid, w.c.binding.work.work_id
    execution = w.inventory.rows[w.session].payload["data"]["execution"]
    assignment = w.inventory.rows[w.session].payload["data"]["assignment"]
    cases = {
        "extra intent": extra_intent(w),
        "missing intent": forged(w).drop(w.reserved),
        "row names another digest": forged(w).data(w.consumed, lambda intent: {**intent, "request_sha256": "e" * 64}),
        "request count": forged(w).data(w.session, session_edit(model_requests=len(w.intents) + 1)),
        "state": forged(w).data(w.session, session_edit(state="running")),
        "no failure reason": forged(w).data(w.session, session_edit(reason=None)),
        "other reason": forged(w).data(w.session, session_edit(reason="operation_unknown")),
        "execution": forged(w).data(w.session, session_edit(execution={**execution, "generation": 2})),
        "resume": forged(w).data(w.session, session_edit(assignment={**assignment, "resume": True})),
        "archive": forged(w).copy(w.consumed, key("archive", sid)),
        "restore": forged(w).copy(w.consumed, key("restore", sid)),
        "artifact": forged(w).copy(w.consumed, key("artifact", work)),
        "result": forged(w).copy(w.consumed, key("result", work)),
    }
    for name, forgery in cases.items():
        assert refusal(forgery, c.config, w.launch, w.intents) == SET, name
    resumed = w.launch.model_copy(update={"start": w.launch.start.model_copy(update={"checkpoint": b"checkpoint"})})
    assert refusal(w.inventory, c.config, resumed, w.intents) == SET
    binding = c.config.bindings[0]
    other = binding.model_copy(update={"route": binding.route.model_copy(update={"channel_id": "another-channel"})})
    config = SimpleNamespace(**{name: getattr(c.config, name) for name in ("table", "workers", "delivery", "sha256")},
                             bindings=(other,))
    assert refusal(w.inventory, config, w.launch, w.intents) == SET


def test_each_context_rule_refuses_and_an_unread_context_needs_no_receipt(world):
    w = world
    c = w.c
    reads = read_set(w, w.context)
    for name, forgery in {
        "missing": forged(w).drop_receipt(w.context),
        "digest": forged(w).receipt(w.context, request_edit(request_digest="0" * 64)),
        "reads": forged(w).receipt(w.context, request_edit(read_set=["another-key", *reads[1:]])),
        "result": forged(w).receipt(w.context, receipt_edit(result={"state": "denied"})),
        "writes": forged(w).receipt(w.context, receipt_edit(writes=[])),
    }.items():
        assert refusal(forgery, c.config, w.launch, w.intents) == CONTEXT, name
    unread = forged(w).drop_receipt(w.context).data(w.session, session_edit(context_read=False))
    assert len(_multi_models(unread.inventory(), c.config, w.launch, w.intents)) == len(w.intents)


def test_each_reservation_rule_refuses(world):
    w = world
    c = w.c
    reads = read_set(w, w.first_reserve)
    writes = w.inventory.receipts[w.first_reserve].payload["writes"]
    flipped = tuple(ModelIntentState(request_sha256=item.request_sha256, consumed=not item.consumed)
                    if item.request_sha256 == w.h.first else item for item in w.intents)
    assert refusal(w.inventory, c.config, w.launch, flipped) == RESERVE
    only_base = reads[:9]  # the nine original producer reads; a reservation must read more
    for name, forgery in {
        "missing": forged(w).drop_receipt(w.first_reserve),
        "digest": forged(w).receipt(w.first_reserve, request_edit(request_digest="0" * 64)),
        "context reads": forged(w).receipt(w.first_reserve, request_edit(read_set=[*reads, "another-key"])),
        "base reads": unread(w).receipt(w.first_reserve, request_edit(read_set=["another-key", *reads[1:]])),
        "only base reads": unread(w).receipt(w.first_reserve, request_edit(read_set=only_base)),
        "result": forged(w).receipt(w.first_reserve, receipt_edit(result={"state": "denied"})),
        "writes miss the intent": forged(w).receipt(w.first_reserve, receipt_edit(
            writes=[item for item in writes if item[0] != w.consumed])),
        "write image": forged(w).receipt(w.first_reserve, receipt_edit(
            writes=[[item[0], "0" * 64 if item[0] == w.consumed else item[1]] for item in writes])),
        "consumed without its receipt": forged(w).drop_receipt(w.first_consume).data(
            w.consumed, lambda intent: intent, later=True),
    }.items():
        assert refusal(forgery, c.config, w.launch, w.intents) == RESERVE, name


def test_a_repeated_read_never_reaches_the_rules(world):
    w = world
    doubled = request_edit(read_set=[*read_set(w, w.first_reserve), read_set(w, w.first_reserve)[-1]])
    code, _ = refusal(forged(w).receipt(w.first_reserve, doubled), w.c.config, w.launch, w.intents)
    assert code == "MIGRATION_RECORD_INVALID"


def test_each_consume_rule_refuses(world):
    w = world
    c = w.c
    payload = w.inventory.receipts[w.first_consume].payload
    reads = list(payload["request"]["read_set"])
    reserve_revision = w.inventory.receipts[w.first_reserve].payload["guard_revision"]
    for name, forgery in {
        "reads": forged(w).receipt(w.first_consume, request_edit(read_set=reads[:-1])),
        "result": forged(w).receipt(w.first_consume, receipt_edit(result={"state": "denied"})),
        "extra write": forged(w).receipt(w.first_consume, receipt_edit(writes=[*payload["writes"], ["another-row", "0" * 64]])),
        "not after its reservation": forged(w).receipt(w.first_consume, receipt_edit(guard_revision=reserve_revision)),
    }.items():
        assert refusal(forgery, c.config, w.launch, w.intents) == CONSUME, name


def test_each_reviewed_field_refuses(world):
    w = world
    c = w.c
    proofs = _multi_models(w.inventory, c.config, w.launch, w.intents)
    position = next(index for index, proof in enumerate(proofs) if proof.consumed)
    for field in ("image_sha256", "reserve_request_sha256", "reserve_receipt_sha256",
                  "consume_request_sha256", "consume_receipt_sha256"):
        changed = list(proofs)
        changed[position] = proofs[position].model_copy(update={field: "0" * 64})
        assert refusal(w.inventory, c.config, w.launch, tuple(changed)) == REVIEWED, field


def test_two_faults_name_the_earlier_rule(world):
    w = world
    c = w.c
    proofs = _multi_models(w.inventory, c.config, w.launch, w.intents)
    position = next(index for index, proof in enumerate(proofs) if proof.consumed)
    bad_first = list(proofs)
    bad_first[position] = proofs[position].model_copy(update={"image_sha256": "0" * 64})
    denied_result = receipt_edit(result={"state": "denied"})
    for name, forgery, expected in (
        ("set before context", forged(w).data(w.session, session_edit(state="running")).drop_receipt(w.context), SET),
        ("context before reservation", forged(w).drop_receipt(w.context).receipt(w.first_reserve, denied_result), CONTEXT),
        ("reservation before consume", forged(w).receipt(w.first_reserve, denied_result).receipt(w.first_consume, denied_result), RESERVE),
        ("consume before reviewed", forged(w).receipt(w.first_consume, denied_result), CONSUME),
    ):
        assert refusal(forgery, c.config, w.launch, tuple(bad_first)) == expected, name
    with pytest.raises(Problem, match="required recovery history row is missing"):
        _multi_models(extra_intent(w).drop(w.session).inventory(), c.config, w.launch, w.intents)


def test_intents_are_checked_in_the_expected_order(world):
    w = world
    c = w.c
    proofs = _multi_models(w.inventory, c.config, w.launch, w.intents)
    first = proofs[0].model_copy(update={"image_sha256": "0" * 64})
    second_reserve = key("model", w.sid, proofs[1].request_sha256)
    broken = forged(w).receipt(second_reserve, receipt_edit(result={"state": "denied"}))
    assert refusal(broken, c.config, w.launch, (first, proofs[1])) == REVIEWED


def test_finished_mode_requires_its_own_session_shape(durable_result):
    h, c = durable_result, durable_result.c
    service, _ = finished_service(h)
    inventory = c.store.read_inventory()
    launch = c.native.launch(c.launch_id)
    expected = service.scope.model_intents
    sid = c.assignment.session_id
    session = key("session", sid)
    assert len(_multi_models(inventory, c.config, launch, expected, finished=True)) == len(expected)
    assert refusal(inventory, c.config, launch, expected) == SET
    clean = Forgery(inventory).data(session, session_edit(reason=None))
    assert len(_multi_models(clean.inventory(), c.config, launch, expected, finished=True)) == len(expected)
    for name, change in {"reason": {"reason": "finish_missing"}, "state": {"state": "waiting"}}.items():
        assert refusal(Forgery(inventory).data(session, session_edit(**change)), c.config, launch, expected,
                       finished=True) == SET, name
