"""Read and check a saved cancellation-result continuation; nothing writes one any more."""
from typing import Literal

from . import behavior_delivery as d
from .behavior_operations import control_key
from .behavior_runtime import runtime_key
from .behavior_store import ControlRequest
from .errors import Problem
from .models import Identifier


class OriginalResultDelivery(d._Model):
    kind: Literal["original_result_delivery"] = "original_result_delivery"
    runtime_binding_id: Identifier
    job_id: Identifier
    original_fetch_request_id: Identifier
    original_access_evidence_id: Identifier
    previous: d.DeliveryBinding
    current: d.DeliveryBinding
    original_obligation: d._Obligation
    request: ControlRequest


def continuation_key(binding_id):
    return runtime_key("result-continuation", binding_id)


def require(condition, message):
    if not condition:
        raise Problem(409, "RESULT_CONTINUATION_DENIED", message)


def require_continuation(runtime, binding_id, command, principal):
    binding, _, context = runtime.authorize(binding_id, principal, "read")
    row = runtime.store.read_snapshot((continuation_key(binding_id),))[continuation_key(binding_id)]
    if row is None:
        raise Problem(409, "RESULT_CONTINUATION_NOT_READY", "original retained result continuation is not prepared")
    value = d._parse(row.payload["data"], OriginalResultDelivery)
    require(value.runtime_binding_id == binding_id and value.job_id == binding.job_id
            and value.original_fetch_request_id == command.request_id
            and value.current.route == binding.route and value.current.obligation_id == binding.obligation_id,
            "continue only the original failed fetch ID and recipient")
    require(row.payload["material"] == d._material(value.model_dump(mode="json")).model_dump(mode="json"),
            "original continuation material differs")
    runtime.store.reconcile(value.request)
    require(runtime.store.read_snapshot((control_key("inbox-exchange", value.previous.delivery_id),))[
        control_key("inbox-exchange", value.previous.delivery_id)] is None,
        "original delivery exchange exists; continuation denied")
    return value, binding, context

