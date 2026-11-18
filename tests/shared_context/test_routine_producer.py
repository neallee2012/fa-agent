"""Approved routine occurrences enter the original work/receipt path."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from conftest import A
from shared_context.behavior_assistant import AssistantAuthority, AssistantRuntime
from local_http_helpers import _LocalHttp
from shared_context.behavior_inbox import PullInbox
from shared_context.behavior_delivery import DeliveryCore
from assistant_api_helpers import assistant_api
from instruction_flow_helpers import instruction_models, runtime_path, sdk_io
from test_m3_auth import jwk_client, signing_key
from work_cost_policy_helpers import standing


def test_server_policy_retains_a_disabled_approved_daily_routine(assistant_api):
    _, policy, _, _, human = standing(assistant_api)
    document = policy.model_dump(mode="json")
    document["routines"] = [{
        "routine_id": "daily-check",
        "owner_oid": human.oid,
        "approved_version": "daily-check-v1",
        "time_zone": "Asia/Taipei",
        "local_time": "09:00",
        "content": "Summarize the approved source.",
        "trigger_data": {"topic": "daily"},
        "notification_policy": "original-conversation",
    }]
    configured = type(policy).model_validate_json(json.dumps(document))
    assert configured.routines[0].enabled is False
    assert configured.routines[0].owner_oid == human.oid
    assert configured.routines[0].approved_version == "daily-check-v1"


def reject_one_storage_batch(c, monkeypatch, kind):
    from azure.core.exceptions import ResourceModifiedError
    from test_behavior_store import parse_batch

    send = c.h.transport.send
    rejected = []
    def transmit(request, **kwargs):
        if request.method == "POST" and not rejected:
            operations, _ = parse_batch(request)
            rows = [json.loads(operation[1]["Payload"]) for operation in operations]
            if any(row.get("data", {}).get("kind") == kind for row in rows):
                rejected.append(kind)
                error = ResourceModifiedError("OFFLINE rejected original storage batch before apply")
                error.status_code = 412
                raise error
        return send(request, **kwargs)
    monkeypatch.setattr(c.h.transport, "send", transmit)
    return rejected
