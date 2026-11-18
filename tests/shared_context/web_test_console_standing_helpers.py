"""從 test_web_test_console_standing.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

from types import SimpleNamespace
from fastapi.testclient import TestClient
from shared_context.behavior_work_policy import derive_work
from web_test_console.bridge import create_app


def configuration(h, control, ids=("fixed-standing-A", "fixed-standing-B")):
    policy = control.policy
    tasks = []
    for n, request_id in enumerate(ids):
        _, b, _ = derive_work(policy, request_id, h.runtime.mode)
        tasks.append({
            "task_id": "A" if n == 0 else "B", "label": "OFFLINE 常態工作 "+str(n+1),
            "description": "正式backend ASGI/JWT/producers；非原生驗收", "mode": "normal_pull", "submission": "assistant",
            "binding_id": b.binding_id, "job_id": b.job_id, "work_id": b.work.work_id, "event_id": b.event_id,
            "root_request_id": b.root_request_id, "obligation_id": b.obligation_id, "owner_record_id": b.owner.owner_record_id,
            "route": b.route.model_dump(mode="json"), "channel_version": h.config.channel.version,
            "submit_request_id": request_id, "fetch_request_id": "fetch-"+str(n), "ack_request_id": "ack-"+str(n),
            "allowed_actions": ["status", "submit", "fetch", "acknowledge"],
        })
    return {
        "schema_version": "fa-web-test-console-standing-v3", "authorization_mode": "backend-per-work",
        "console_id": "offline-standing-ui", "backend_origin": "https://standing.example.org",
        "tenant_id": h.verifier.auth.tenant_id, "human_oid": h.verifier.settings.human_oid,
        "client_id": h.verifier.settings.client_id, "audience": h.verifier.auth.audience,
        "backend_configuration_sha256": policy.configuration_sha256, "backend_core_sha256":
            "3996484c7333d2a0679da2b225b53b7f68c122f0b84aca2f75d36a264e1c7da8",
        "standing_policy": policy.model_dump(mode="json"), "standing_policy_sha256": policy.sha256, "tasks": tasks,
    }


def open_ui(case, path, config=None):
    app = create_app(config or case.config, path, backend=case.backend,
        token_provider=lambda: case.h.headers()["Authorization"].removeprefix("Bearer "),
        clock=lambda: case.h.time, environment="OFFLINE_TEST")
    ui = TestClient(app, base_url="http://127.0.0.1:8765")
    ui.get("/")
    headers = {"Origin": "http://127.0.0.1:8765", "X-Console-CSRF": ui.get("/api/state").json()["csrf"]}
    assert ui.post("/api/authenticate", json={}, headers=headers).status_code == 200
    return SimpleNamespace(app=app, ui=ui, headers=headers)
