"""_finished_proof: each rule of the original finish chain refuses, and the first failing rule names the error.

Forgery keeps row and receipt hashes coherent, so a case reaches the named rule, not the generic hash check.
"""
import base64
import json
from types import SimpleNamespace

import pytest

from history_forgery import Forgery, _hash, _material as inline_material
from shared_context.behavior_assistant import key
from shared_context.behavior_recovery import _finished_proof
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_behavior_assistant_acquire_conflicts import installed_assistant, native_acquire
from test_behavior_finished_result_recovery import durable_result
from test_m3_auth import jwk_client, signing_key


INVALID = "RECOVERY_FINISHED_RESULT_INVALID"
ROWS = (INVALID, "original complete result/artifact/criteria differ")
CONTEXT = (INVALID, "original reviewed context receipt is missing")
ORDER = (INVALID, "original claim/assignment/acquire/context/finish order differs")
REVIEWED = (INVALID, "reviewed original finish references changed")
HISTORY = "MIGRATION_RECOVERY_HISTORY_INVALID"
RECEIPT = (HISTORY, "original recovery request/read-set/write-set receipt differs")
RESULT = (HISTORY, "original recovery receipt result differs")
IMAGE = (HISTORY, "recovery receipt write image differs")
CLAIM = ("RECOVERY_CLAIM_UNVERIFIED", "one exact original native claim receipt is required")
ETAG = "synthetic-etag"


@pytest.fixture
def world(durable_result):
    c = durable_result.c
    sid, work = c.assignment.session_id, c.binding.work.work_id
    inventory = c.store.read_inventory()
    return SimpleNamespace(
        c=c, sid=sid, inventory=inventory, launch=c.native.launch(c.launch_id),
        session=key("session", sid), artifact=key("artifact", work), result=key("result", work),
        context=key("context", sid, "current-context"), issue=key("issue", sid),
        acquire=key("acquire", sid, "sdk-start"), finish=key("finish", sid, "finish-task"),
    )


def forged(w):
    return Forgery(w.inventory)


def vouched(w):
    """Every row keeps a second writer, so a forged receipt fails its own rule and not the row's coverage."""
    forgery = forged(w)
    for name in (w.session, w.artifact, w.result):
        forgery._vouch(name, _hash(w.inventory.rows[name].payload))
    return forgery


def prove(w, forgery=None, **options):
    inventory = w.inventory if forgery is None else forgery.inventory()
    return _finished_proof(inventory, w.c.config, options.pop("launch", w.launch), ETAG, **options)


def refusal(w, forgery=None, **options):
    with pytest.raises(Problem) as error:
        prove(w, forgery, **options)
    body = error.value.body["error"]
    return body["code"], body["message"]


def edit(**updates):
    return lambda data: {**data, **updates}


def encoded(value):
    return base64.b64encode(value).decode()


def result_edit(w, **updates):
    return forged(w).data(w.result, edit(**updates), material=True)


def artifact_edit(w, change):
    def edited(data):
        return {**data, "artifacts": change(data["artifacts"])}
    return forged(w).data(w.result, edited, material=True)


def claimed_id(w):
    launch = w.launch
    return next(name for name, record in w.inventory.receipts.items()
                if record.payload["result"] == {"claim_id": launch.claim_id})


def revision(w, name):
    return w.inventory.receipts[name].payload["guard_revision"]


def test_the_original_finish_binds_every_reference(world):
    w = world
    proof = prove(w)
    payload = w.inventory.receipts
    assert (proof.session_image_sha256, proof.artifact_image_sha256, proof.result_image_sha256) == tuple(
        _hash(w.inventory.rows[name].payload) for name in (w.session, w.artifact, w.result))
    claim = payload[claimed_id(w)].payload
    assert (proof.native_claim_request_sha256, proof.native_claim_receipt_sha256) == (digest(claim["request"]), digest(claim))
    for field, name in (("assignment", w.issue), ("acquire", w.acquire), ("finish", w.finish)):
        assert getattr(proof, field + "_request_sha256") == digest(payload[name].payload["request"]), field
        assert getattr(proof, field + "_receipt_sha256") == digest(payload[name].payload), field
    profile = w.c.config.assistant.profiles[0]
    effect = w.inventory.rows[w.artifact].payload["data"]
    assert (proof.artifact_id, proof.artifact_version) == (profile.artifact_id, profile.artifact_version)
    assert proof.artifact_sha256 == effect["artifact_sha256"] and proof.artifact_blob_etag == ETAG
    assert prove(w, expected=proof) == proof


def test_each_session_rule_refuses(world):
    w = world
    session = w.inventory.rows[w.session].payload["data"]
    assignment, execution = session["assignment"], session["execution"]
    for name, updates in {
        "state": {"state": "running"},
        "other reason": {"reason": "operation_unknown"},
        "context unread": {"context_read": False},
        "execution": {"execution": {**execution, "generation": execution["generation"] + 1}},
        "work": {"assignment": {**assignment, "task": {**assignment["task"], "work_id": "another-work"}}},
    }.items():
        assert refusal(w, forged(w).data(w.session, edit(**updates))) == ROWS, name


@pytest.mark.parametrize("reason", [None, "sdk_failed"])
def test_a_finished_session_may_carry_no_reason_or_an_sdk_failure(world, reason):
    w = world
    assert prove(w, forged(w).data(w.session, edit(reason=reason)))


def test_each_artifact_rule_refuses(world):
    w = world
    effect = w.inventory.rows[w.artifact].payload["data"]
    execution = effect["execution"]
    for name, updates in {
        "execution": {"execution": {**execution, "generation": execution["generation"] + 1}},
        "stage": {"stage": "created"},
    }.items():
        assert refusal(w, forged(w).data(w.artifact, edit(**updates))) == ROWS, name


def test_the_artifact_digests_answer_for_its_own_content(world):
    """The result quotes both digests, so it is forged to agree and only the artifact row's own rule can refuse."""
    w = world
    proof = json.loads(base64.b64decode(w.inventory.rows[w.result].payload["data"]["proof"]))
    forgery = forged(w).data(w.artifact, edit(description_digest="e" * 64))
    forgery.data(w.result, edit(proof=inline_material({**proof, "description_digest": "e" * 64})["data"]), material=True)
    assert refusal(w, forgery) == ROWS
    forgery = forged(w).data(w.artifact, edit(artifact_sha256="e" * 64))
    forgery.data(w.result, lambda data: {**data, "artifacts": [{**data["artifacts"][0], "sha256": "e" * 64}]}, material=True)
    assert refusal(w, forgery) == ROWS


def test_each_result_rule_refuses(world):
    w = world
    for name, updates in {
        "evidence": {"evidence_id": "another-evidence"},
        "state": {"state": "failed"},
        "work": {"work_id": "another-work"},
        "verifier": {"verifier_version": "another-verifier"},
        "definition": {"definition_digest": "e" * 64},
        "checks": {"passed_checks": []},
        "known effects": {"known_effects": ["another-effect"]},
        "unresolved": {"unresolved_operations": ["another-operation"]},
        "content": {"content": encoded(b"another content")},
        "proof": {"proof": encoded(b"another proof")},
    }.items():
        assert refusal(w, result_edit(w, **updates)) == ROWS, name


def test_each_delivered_artifact_rule_refuses(world):
    w = world
    for name, change in {
        "none": lambda items: [],
        "two": lambda items: [*items, items[0]],
        "id": lambda items: [{**items[0], "artifact_id": "another-artifact"}],
        "version": lambda items: [{**items[0], "version": "another-version"}],
        "sha": lambda items: [{**items[0], "sha256": "e" * 64}],
    }.items():
        assert refusal(w, artifact_edit(w, change)) == ROWS, name


def test_each_row_refuses_while_missing_in_read_order(world):
    w = world
    assert refusal(w, forged(w).drop(w.session).drop(w.artifact).drop(w.result)) == (
        HISTORY, "required recovery history row is missing: " + w.session)
    assert refusal(w, forged(w).drop(w.artifact).drop(w.result)) == (
        HISTORY, "required recovery history row is missing: " + w.artifact)
    assert refusal(w, forged(w).drop(w.result)) == (HISTORY, "required recovery history row is missing: " + w.result)


def test_the_result_material_is_part_of_the_row(world):
    w = world
    forgery = forged(w).material(w.result, lambda _: inline_material({"another": True}), later=False)
    assert refusal(w, forgery) == (HISTORY, "recovery history material differs")


def test_context_is_required_and_keeps_the_base_reads(world):
    w = world
    assert refusal(w, forged(w).drop_receipt(w.context)) == CONTEXT

    def moved(payload):
        reads = payload["request"]["read_set"]
        return {**payload, "request": {**payload["request"], "read_set": ["another-read", *reads[1:]]}}

    assert refusal(w, forged(w).receipt(w.context, moved)) == CONTEXT

    def moved_last_base(payload):  # the base has 9 reads; the 9th must still match
        reads = payload["request"]["read_set"]
        return {**payload, "request": {**payload["request"], "read_set": [*reads[:8], "another-read", *reads[9:]]}}

    assert refusal(w, forged(w).receipt(w.context, moved_last_base)) == CONTEXT


def test_each_receipt_rule_refuses(world):
    w = world

    def another(field, value):
        def change(payload):
            return {**payload, "request": {**payload["request"], field: value}}
        return change

    for name in (w.issue, w.acquire, w.finish):
        assert refusal(w, vouched(w).drop_receipt(name)) == RECEIPT, name
        assert refusal(w, vouched(w).receipt(name, another("request_digest", "e" * 64))) == RECEIPT, name
        assert refusal(w, vouched(w).receipt(name, another("read_set", ["another-read"]))) == RECEIPT, name
        assert refusal(w, vouched(w).receipt(name, edit(result={"state": "denied"}))) == RESULT, name
        written = w.inventory.receipts[name].payload["writes"]
        assert refusal(w, vouched(w).receipt(name, edit(writes=written[:-1]))) == RECEIPT, name


def test_the_finish_receipt_wrote_these_exact_images(world):
    w = world
    for name in (w.session, w.result):
        def change(payload, name=name):
            return {**payload, "writes": [[item[0], "e" * 64 if item[0] == name else item[1]] for item in payload["writes"]]}
        assert refusal(w, vouched(w).receipt(w.finish, change)) == IMAGE, name


def test_the_claim_must_be_the_original_one(world):
    w = world
    assert refusal(w, forged(w).drop_receipt(claimed_id(w))) == CLAIM


def test_each_step_must_follow_the_one_before_it(world):
    w = world
    claim = claimed_id(w)
    for earlier, later in ((claim, w.issue), (w.issue, w.acquire), (w.acquire, w.context), (w.context, w.finish)):
        forgery = forged(w).receipt(later, edit(guard_revision=revision(w, earlier)))
        assert refusal(w, forgery) == ORDER, later


def test_a_changed_reference_refuses_the_review(world):
    w = world
    proof = prove(w)
    assert refusal(w, expected=proof.model_copy(update={"finish_receipt_sha256": "e" * 64})) == REVIEWED


def test_rules_speak_in_chain_order(world):
    w = world
    late = lambda forgery: forgery.receipt(w.finish, edit(guard_revision=revision(w, w.context)))  # noqa: E731
    bad_row = forged(w).data(w.session, edit(state="running"))
    assert refusal(w, bad_row.drop_receipt(w.context)) == ROWS
    assert refusal(w, forged(w).drop_receipt(w.context).drop_receipt(w.issue)) == CONTEXT
    assert refusal(w, forged(w).drop_receipt(w.issue).drop_receipt(claimed_id(w))) == RECEIPT
    assert refusal(w, late(forged(w)).drop_receipt(claimed_id(w))) == CLAIM
    proof = prove(w)
    changed = proof.model_copy(update={"finish_receipt_sha256": "e" * 64})
    assert refusal(w, late(forged(w)), expected=changed) == ORDER


def test_an_unknown_authority_source_cannot_be_read(world):
    w = world
    forgery = forged(w).data(w.artifact, edit(authority_source="another-authority"))
    assert refusal(w, forgery)[0] == "MIGRATION_RECORD_INVALID"
