"""P06: version following is server-owned; the page/bridge do not call environment-auto."""

from test_web_test_console_native import native_console, first
from test_native_turn_api import native_api
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_conversation_transfer_startup import synthetic_standing_v1


def test_environment_auto_route_is_removed(native_console):
    c = native_console
    root = "/api/instructions/" + first(c)["thread_id"]
    response = c.ui.client.post(root + "/environment-auto", json={}, headers=c.ui.headers)
    assert response.status_code == 404
    assert not any(path.endswith("/environment/advance") or path.endswith("/environment/observe-automatic")
                   for _, path, _ in c.calls)
