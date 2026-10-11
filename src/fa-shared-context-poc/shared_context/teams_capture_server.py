"""Standalone capture-only server over an ALREADY migrated authority.

No bootstrap, reset, migration apply, work/approval/model/sender construction,
Blob client, token probe or operator-supplied route. Startup verifies the exact
retained migration lineage. Each route transaction repeats that verification at
MigrationStore's final-send seam; the same EGT guard fences later races.

One transaction retains the original personal route witness and its immutable
server-installation/history binding. Existing guard/budget/work/protection rows
are never reset. Any old, unbound or differently installed route fails closed.
Native send remains NOT_RUN. Injected clients/transports are offline test seams.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from azure.data.tables import TableClient
from azure.identity import ManagedIdentityCredential
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .behavior_delivery import _Model, _json, _material, _parse, _utc
from .behavior_migration import (
    MigrationCurrent, MigrationStore, installed_origin, installation_record,
    migration_key, protection_keys,
)
from .behavior_operations import control_key
from .behavior_server_config import (
    ServerConfiguration, load_configuration, parse_document, read_document,
)
from .behavior_store import ControlChanges, ControlWrite, Digest, InlineMaterial
from .errors import Problem
from .models import Identifier
from .teams_deployment import PersonalTeamsConfig, RESOURCE_GROUP, SUBSCRIPTION
from .teams_personal import PersonalRouteEnrollment, PersonalTeamsEntry, ROUTE_HANDSHAKE, _RouteWitness, _activity
from .teams_notices import notice_router
from .teams_runtime import retained

_TABLE_OPTIONS = {
    "retry_total": 0, "retry_connect": 0, "retry_read": 0, "retry_status": 0,
    "retry_to_secondary": False, "logging_enable": False,
    "connection_timeout": 10, "read_timeout": 20,
}


def utc_now():
    return datetime.now(timezone.utc)


class _Installation(_Model):
    schema_version: str = "fa-teams-capture-installation-v1"
    server_configuration_sha256: Digest
    personal_configuration_sha256: Digest
    original_configuration_sha256: Digest
    control_binding: Digest
    installation_row_id: Identifier
    migration_current_row_id: Identifier
    history_row_id: Identifier
    history_sha256: Digest
    authorization_row_id: Identifier


class _RouteBinding(_Installation):
    route_row_id: Identifier
    route_material_sha256: Digest


class _CaptureStore(MigrationStore):
    """The enrollment writer can only CREATE its route plus installation binding."""
    def __init__(self, client, server, personal):
        super().__init__(client, server.table.scope)
        self.server, self.personal = server, personal
        self.route_key = control_key("teams-personal-route", personal.configuration_digest)
        self.binding_key = control_key("teams-capture-route-binding", personal.configuration_digest)

    def initialize(self):
        raise Problem(403, "TEAMS_CAPTURE_BOOTSTRAP_DISABLED", "capture cannot initialize or reset authority")

    def current_installation(self) -> _Installation:
        installation_key, _ = installation_record(self.server)
        current_key = migration_key(self.server.table.scope.authority_domain, "current")
        row = self.read_snapshot((current_key,))[current_key]
        if row is None:
            raise Problem(503, "TEAMS_CAPTURE_MIGRATION_REQUIRED", "protection migration must be applied; a plan or legacy installation is insufficient")
        pointer = _parse(row.payload["data"], MigrationCurrent)
        original = installed_origin(self, self.server)
        if original.sha256 == self.server.sha256:
            raise Problem(503, "TEAMS_CAPTURE_MIGRATION_REQUIRED", "a verified applied migration history is required")
        self.bind_installation(
            installation_key, self.server.sha256,
            protected_rows=(*protection_keys(self.server), current_key, pointer.history_key, pointer.authorization_key),
        )
        return _Installation(
            server_configuration_sha256=self.server.sha256,
            personal_configuration_sha256=self.personal.configuration_digest,
            original_configuration_sha256=original.sha256,
            control_binding=self.server.workers.control_binding,
            installation_row_id=installation_key, migration_current_row_id=current_key,
            history_row_id=pointer.history_key, history_sha256=pointer.history_sha256,
            authorization_row_id=pointer.authorization_key,
        )

    def validate_capture(self) -> _Installation:
        installation = self.current_installation()
        rows = self.read_snapshot((self.route_key, self.binding_key))
        if rows[self.route_key] is None and rows[self.binding_key] is None:
            return installation
        if rows[self.route_key] is None or rows[self.binding_key] is None:
            raise Problem(409, "TEAMS_CAPTURE_ROUTE_BINDING_REQUIRED", "route and installation binding must have committed together")
        witness = retained(self, self.route_key, _RouteWitness)
        binding = retained(self, self.binding_key, _RouteBinding)
        expected = _RouteBinding(
            **installation.model_dump(), route_row_id=self.route_key,
            route_material_sha256=_material(witness.model_dump(mode="json")).sha256,
        )
        if binding != expected or witness.configuration_digest != self.personal.configuration_digest:
            raise Problem(409, "TEAMS_CAPTURE_ROUTE_BINDING_DENIED", "route belongs to a different installation, history or configuration")
        return installation

    def transact(self, request, decide):
        installation = self.validate_capture()
        if (request.request_id != control_key("teams-personal-enroll", self.personal.configuration_digest)
                or request.read_set != (self.route_key,)):
            raise Problem(403, "TEAMS_CAPTURE_WRITE_DENIED", "only the bound personal enrollment transaction is permitted")

        def checked(snapshot):
            changes = decide(snapshot)
            if (type(changes) is not ControlChanges or len(changes.writes) != 1
                    or changes.writes[0].row_id != self.route_key or changes.writes[0].mode != "create"):
                raise Problem(403, "TEAMS_CAPTURE_WRITE_DENIED", "capture cannot update other authority records")
            write = changes.writes[0]
            witness = _parse(write.data, _RouteWitness)
            if (witness.configuration_digest != self.personal.configuration_digest
                    or write.material != _material(witness.model_dump(mode="json"))):
                raise Problem(403, "TEAMS_CAPTURE_WRITE_DENIED", "route witness must retain its exact compliant material")
            binding = _RouteBinding(
                **installation.model_dump(), route_row_id=self.route_key,
                route_material_sha256=write.material.sha256,
            )
            data = binding.model_dump(mode="json")
            return ControlChanges(writes=(*changes.writes, ControlWrite(
                row_id=self.binding_key, mode="create", data=data, material=_material(data),
            )), result=changes.result)

        def before_send():
            if self.validate_capture() != installation:
                raise Problem(409, "TEAMS_CAPTURE_INSTALLATION_CHANGED", "capture installation changed before the authority write")

        return super().transact(request, checked, before_send=before_send)


class _CaptureEntry(PersonalTeamsEntry):
    def __init__(self, enrollment, validate):
        super().__init__(enrollment)
        self.validate = validate

    def attach_runtime(self, runtime):
        raise Problem(403, "TEAMS_CAPTURE_WORK_DISABLED", "this process cannot attach a business runtime")

    def receive(self, authorization, body):
        try:
            self.validate()
            if self.runtime is not None:
                raise Problem(403, "TEAMS_CAPTURE_WORK_DISABLED", "capture-only process has no business runtime")
            return super().receive(authorization, body)
        except Problem as error:
            _log_rejection(body, error)
            raise


def _log_rejection(body, error):
    try:
        activity = _activity(body)
    except Problem:
        activity = {}
    entities = activity.get("entities")
    attachments = activity.get("attachments")
    kind = activity.get("type")
    text_format = activity.get("textFormat", "plain")
    metadata = {
        "problem": error.body["error"]["code"],
        "body_sha256": hashlib.sha256(body).hexdigest() if isinstance(body, bytes) else None,
        "activity_type": kind if isinstance(kind, str) and kind in {
            "message", "conversationUpdate", "typing", "event", "messageReaction",
        } else "other",
        "text_format": text_format if isinstance(text_format, str) and text_format in {"plain", "xml", "markdown"} else "other",
        "text_matches_handshake": activity.get("text") == ROUTE_HANDSHAKE,
        "text_matches_html_handshake": activity.get("text") == "<p>" + ROUTE_HANDSHAKE + "</p>",
        "entity_count": len(entities) if isinstance(entities, list) else 0,
        "client_info_entities": sum(isinstance(item, dict) and item.get("type") == "clientInfo"
                                    for item in entities) if isinstance(entities, list) else 0,
        "attachment_count": len(attachments) if isinstance(attachments, list) else 0,
        "html_attachments": sum(isinstance(item, dict) and item.get("contentType") == "text/html"
                                for item in attachments) if isinstance(attachments, list) else 0,
        "exact_handshake_mirror_attachments": sum(
            isinstance(item, dict) and item.get("contentType") == "text/html"
            and item.get("content") in (ROUTE_HANDSHAKE, "<p>" + ROUTE_HANDSHAKE + "</p>")
            for item in attachments
        ) if isinstance(attachments, list) else 0,
        "value_present": bool(activity.get("value")),
    }
    logging.getLogger(__name__).warning("DEBUG-teams-capture-0917 %s", json.dumps(metadata, sort_keys=True))


@dataclass
class CaptureComposition:
    server: ServerConfiguration
    personal: PersonalTeamsConfig
    store: _CaptureStore
    entry: _CaptureEntry
    resources: ExitStack
    closed: bool = False

    def health(self):
        if self.closed:
            raise Problem(503, "TEAMS_CAPTURE_CLOSED", "capture composition is closed")
        installation = self.store.validate_capture()
        return {
            "status": "ready", "mode": "capture_only",
            "server_configuration_sha256": installation.server_configuration_sha256,
            "personal_configuration_sha256": installation.personal_configuration_sha256,
            "migration_history_sha256": installation.history_sha256,
            "work_enabled": False, "approval_enabled": False,
            "model_enabled": False, "sender_enabled": False,
            "native_send": "NOT_RUN",
        }

    def close(self):
        if not self.closed:
            self.closed = True
            self.resources.close()


def _checked_configurations(server, personal):
    try:
        server = ServerConfiguration.model_validate_json(server.model_dump_json())
        personal = PersonalTeamsConfig.model_validate(personal.model_dump())
    except (ValueError, TypeError, ValidationError):
        raise Problem(503, "TEAMS_CAPTURE_CONFIGURATION_DENIED", "validated server-owned configuration is required") from None
    if (
        server.identity.tenant_id != personal.tenant_id or server.identity.human_oid != personal.intended_user_oid
        or server.identity.team_id != personal.team_id
        or server.identity.managed_identity_client_id != personal.uami_client_id
        or server.api_resource_id.lower() != personal.host_resource_id.lower()
    ):
        raise Problem(503, "TEAMS_CAPTURE_CONFIGURATION_DENIED", "server and personal tenant, admin, team, host or UAMI differ")
    prefix = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Storage/storageAccounts/".lower()
    if any(not item.account_resource_id.lower().startswith(prefix)
           for item in (server.table, server.sources, server.backups)):
        raise Problem(503, "TEAMS_CAPTURE_CONFIGURATION_DENIED", "the existing authority must remain in the approved subscription/resource group")
    return server, personal


def build_capture_server(
    server: ServerConfiguration, personal: PersonalTeamsConfig, *,
    credential=None, table_transport=None, jwks_transport: httpx.BaseTransport | None = None, clock=utc_now,
) -> CaptureComposition:
    """Create only a UAMI/Table client; all construction is free of network I/O."""
    server, personal = _checked_configurations(server, personal)
    resources = ExitStack()
    try:
        if credential is None:
            credential = ManagedIdentityCredential(
                client_id=server.identity.managed_identity_client_id, retry_total=0, logging_enable=False,
            )
            resources.callback(credential.close)
        options = {} if table_transport is None else {"transport": table_transport}
        table = TableClient(
            server.table.endpoint, server.table.scope.table_name, credential=credential,
            **_TABLE_OPTIONS, **options,
        )
        resources.callback(table.close)
        store = _CaptureStore(table, server, personal)
        if store._binding != server.workers.control_binding:
            raise Problem(503, "TEAMS_CAPTURE_CONFIGURATION_DENIED", "Table endpoint/scope differs from the installed control binding")
        enrollment = PersonalRouteEnrollment(store, personal, clock=clock, jwks_transport=jwks_transport)
        entry = _CaptureEntry(enrollment, store.validate_capture)
        return CaptureComposition(server, personal, store, entry, resources)
    except BaseException:
        resources.close()
        raise


def capture_application(composition: CaptureComposition) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        try:
            composition.health()
            yield
        finally:
            composition.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(composition.entry.router())
    app.include_router(notice_router())

    @app.get("/healthz")
    def healthz():
        try:
            return composition.health()
        except Problem as error:
            return JSONResponse(error.body, status_code=503)

    return app


def load_capture_configurations(*, server_path=None, personal_path=None, environ=None):
    environment = os.environ if environ is None else environ
    selected = personal_path if personal_path is not None else environment.get("TEAMS_PERSONAL_CONFIG")
    raw = environment.get("TEAMS_PERSONAL_CONFIG_JSON")
    try:
        if bool(selected) == bool(raw):
            raise ValueError("one personal configuration source required")
        server = load_configuration(server_path, environ=environment)
        personal = (read_document(selected, PersonalTeamsConfig) if selected
                    else parse_document(raw.encode("utf-8"), PersonalTeamsConfig))
        return _checked_configurations(server, personal)
    except (OSError, ValueError, TypeError, ValidationError):
        raise Problem(503, "TEAMS_CAPTURE_CONFIGURATION_DENIED",
                      "exactly one valid server-owned configuration source is required for each profile") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve signed personal route capture only; all business work is disabled")
    parser.add_argument("action", choices=("serve",))
    parser.add_argument("--server-config")
    parser.add_argument("--personal-config")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    server, personal = load_capture_configurations(server_path=args.server_config, personal_path=args.personal_config)
    composition = build_capture_server(server, personal)
    try:
        import uvicorn
        uvicorn.run(capture_application(composition), host=args.host, port=args.port, access_log=False)
    finally:
        composition.close()


if __name__ == "__main__":
    main()
