"""從 test_web_test_console_frozen_interface.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

from types import SimpleNamespace
from fastapi.testclient import TestClient
from shared_context.errors import Problem
from web_test_console.bridge import create_app


def open_ui(c):
    app=create_app(c.config,c.journal,backend=c.backend,token_provider=lambda:c.h.headers()["Authorization"][7:],
        clock=lambda:c.h.time,environment="OFFLINE_TEST",instruction_connection=c.connection)
    ui=TestClient(app,base_url="http://127.0.0.1:8765")
    ui.get("/")
    headers={"Origin":"http://127.0.0.1:8765","X-Console-CSRF":ui.get("/api/state").json()["csrf"]}
    authenticated=ui.post("/api/authenticate",json={},headers=headers)
    assert authenticated.status_code==200,authenticated.text
    return SimpleNamespace(app=app,ui=ui,headers=headers)


def post(c,path,body=None):
    return c.browser.ui.post(path,json=body or {},headers=c.browser.headers)


def complete(c,binding_id):
    try:
        state=c.h.runtime.controller.step(binding_id)
    except Problem:
        if c.sdk_errors:raise c.sdk_errors[0]
        if c.errors:raise c.errors[0]
        raise
    if c.errors:raise c.errors[0]
    assert state.state=="succeeded" and state.result_forwarded
    return state
