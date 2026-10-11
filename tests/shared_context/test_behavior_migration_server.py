"""Migration fencing in the existing server assembly; all SDK transports offline."""

from urllib.parse import unquote

import pytest

from shared_context.behavior_migration import MigrationStore, installation_record
from shared_context.behavior_server import bootstrap, build_server
from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
from shared_context.errors import Problem
from shared_context.source_materials import control_key
from test_behavior_server import Harness, workspace
from test_m3_auth import jwk_client, signing_key


def test_bootstrap_is_fenced_if_installation_moves_after_its_initial_check(workspace, jwk_client):
    h = Harness(workspace, jwk_client)
    bootstrap(h.composition, h.approval, stage="records")
    old_process = build_server(h.config, **h.options)
    original_send = h.table.send
    installation_key, installed = installation_record(h.config)
    target_receipt = "behavior:v1:receipt:" + control_key("server-installation-init", h.config.table.scope.authority_domain)
    armed = [True]
    operator = MigrationStore(old_process.clients[0], h.config.table.scope)

    def send(request, **kwargs):
        response = original_send(request, **kwargs)
        if armed[0] and request.method == "GET" and target_receipt in unquote(request.url):
            armed[0] = False
            operator.transact(
                ControlRequest(request_id="operator-pointer-change", request_digest="f" * 64, read_set=(installation_key,)),
                lambda _: ControlChanges(writes=(ControlWrite(
                    row_id=installation_key, mode="replace", data={**installed, "configuration_sha256": "f" * 64},
                ),), result={}),
            )
        return response

    h.table.send = send
    count = len(h.table.parts)
    try:
        with pytest.raises(Problem) as fenced:
            bootstrap(old_process, h.approval, stage="records")
        assert fenced.value.body["error"]["code"] == "MIGRATION_INSTALLATION_CHANGED"
        assert not armed[0]
        assert len(h.table.parts) == count + 1
    finally:
        old_process.close()
        h.composition.close()
