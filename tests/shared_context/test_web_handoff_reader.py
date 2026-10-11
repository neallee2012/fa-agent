from types import SimpleNamespace
import pytest
from shared_context.behavior_teams_recovery import validate_teams_recovery_history
from shared_context.errors import Problem


def test_web_handoff_cannot_become_history_without_original_cleanup():
    inventory=SimpleNamespace(rows={"orphan":SimpleNamespace(payload={"data":{
        "schema_version":"fa-original-web-handoff-authorization-v1"}})})
    with pytest.raises(Problem) as denied:
        validate_teams_recovery_history(inventory,None)
    assert denied.value.body["error"]["code"]=="DELIVERY_CONTINUATION_HISTORY_INVALID"


def test_web_handoff_permit_cannot_outlive_its_owned_write_scope():
    from shared_context.behavior_web_handoff import OriginalFailureWebHandoff,_WebHandoffAccess
    service=object.__new__(OriginalFailureWebHandoff)
    permit=_WebHandoffAccess(service,None,None,None,"offline",())
    with pytest.raises(Problem):
        service._owned(permit)


def test_verified_original_web_ack_completes_legacy_index_and_replays_once(monkeypatch):
    """Only the prior authorization/receipt verifiers are synthetic; completion uses the real EGT."""
    from datetime import timedelta
    from shared_context.behavior_auth import BehaviorPrincipal
    from shared_context.behavior_delivery import DeliveryBinding, VerifiedDeliveryReceipt
    from shared_context.behavior_migration import MigrationStore
    from shared_context.behavior_operations import control_key
    from shared_context.behavior_web_handoff import OriginalFailureWebHandoff, _WebHandoffAccess
    from shared_context.behavior_wire import Command
    from test_behavior_delivery import CONTEXT, NOW, final_ready
    import json

    h, ob = final_ready()
    h.store = MigrationStore(h.client, h.store._scope)
    h.core.store = h.store
    binding = DeliveryBinding.model_validate_json(json.dumps(h.data("delivery-outbox", "delivery-one")["binding"]))
    principal = BehaviorPrincipal("tenant", "person-one", "team", "human", None,
                                  frozenset({"read", "acknowledge"}))
    proof = VerifiedDeliveryReceipt(evidence_id=control_key("inbox-ack-evidence", "delivery-one"),
        verifier_version=h.config.verifier_version, binding=binding, send_outcome="emitted",
        confirmed_levels=("client_received",), dispatch_authorization_ref="original-fetch",
        proof=b"Preverified offline original ACK fixture, not a live receipt.")
    plan = SimpleNamespace(original_route=binding.route, web_delivery=binding,
                           payload=h.material("delivery-outbox", "delivery-one"))
    auth = SimpleNamespace(plan=plan, approved_at=NOW, expires_at=NOW + timedelta(minutes=5),
                           model_dump=lambda **_: {"synthetic_authorization": "original-web-receipt"})
    service = object.__new__(OriginalFailureWebHandoff)
    service.store, service.core = h.store, h.core
    service.runtime = SimpleNamespace(_now=lambda: h.time)
    service.config = SimpleNamespace(identity=SimpleNamespace(team_id="team"))
    service.scope = SimpleNamespace(handoff_id="legacy-web-completion", not_before=NOW, expires_at=auth.expires_at)
    service.observation = SimpleNamespace(observed_at=NOW, expires_at=auth.expires_at)
    permit = _WebHandoffAccess(service, principal, auth, SimpleNamespace(action="acknowledge", binding=binding),
                              "original-web-ack", ())
    monkeypatch.setattr(service, "_auth", lambda: auth)

    def authorized(caller, value):
        assert caller is principal and value is auth
        caller.require("team", "acknowledge")

    monkeypatch.setattr(service, "_authorized", authorized)
    monkeypatch.setattr(service, "_access", lambda *args: (permit, CONTEXT))
    monkeypatch.setattr(service, "_keys", lambda _: ())
    service.inbox = SimpleNamespace(verify_receipt=lambda identifier: proof
                                    if identifier == proof.evidence_id else pytest.fail("Wrong original ACK"))
    receipt = service.reconcile(principal, Command(request_id="read-original-web-ack"))
    assert receipt.payload["result"]["fulfillment"] == "fulfilled"
    assert h.data("delivery-obligation", ob)["fulfillment"] == "fulfilled"
    assert h.data("delivery-index", "root")["entries"] == []
    assert h.data("delivery-quota", "root", "root")["pending"] == 0
    guard = h.store._guard()
    assert service.reconcile(principal, Command(request_id="same-original-web-ack")) == receipt
    assert h.store._guard() == guard
