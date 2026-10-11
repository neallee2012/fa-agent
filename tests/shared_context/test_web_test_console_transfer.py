"""P06: legacy workbench environment transfer routes are no longer exposed."""

import pytest

from test_web_test_console_native import native_console, first
from test_native_turn_api import native_api
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_conversation_transfer_startup import synthetic_standing_v1


@pytest.mark.parametrize("suffix", ["environment-prepare", "environment-reconcile"])
def test_environment_transfer_routes_are_removed(native_console, suffix):
    c = native_console
    root = "/api/instructions/" + first(c)["thread_id"]
    response = c.ui.client.post(root + "/" + suffix, json={}, headers=c.ui.headers)
    assert response.status_code == 404
    assert not any("/environment" in path for _, path, _ in c.calls)
