"""Short-lived operator-only observation of an exact expired source.

This is NOT RuntimePolicy renewal, recovery consent, admission or bootstrap.
Construct with the operator app's MigrationStore, exact installed NEW
ServerConfiguration and a trusted ExpiredSourceVerificationPermission file.
The file is server configuration, never HTTP input or proof of human approval.
Each HTTP call still requires the real delegated human Read + Approve JWT.

Only SourceMaterials.read_exact raising SOURCE_EXPIRED can produce a safe
proof. Source bytes, underlying exception details and receipts are never
returned. No Blob credentials, authority writes, SDK or Hosted entry imports
are added. Migration history/code, immutable source metadata/receipt, current
access and the permission window are rechecked before releasing the proof.

Configuration workflow (no permission is generated from the clock):
1. Freeze the complete module set before the real plan/approval/apply.
2. Use the exact applied MigrationCurrent.history_key/history_sha256,
   MigrationHistory.plan.code_sha256 and NEW ServerConfiguration.sha256.
3. Pin the real operator OID and SourceReference (including private scope),
   accepted source purpose and immutable expires_at. Set a UTC window <=20min.
4. Inject that separately approved local JSON through the operator CLI's
   --expired-source-permission PATH. Omission leaves the route absent.
--print-expired-source-permission-example prints INVALID placeholders only;
it performs no approval, authority lookup or write. Do not use the expired
recovery catalog deadline as the source's immutable applicability deadline.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator

from .behavior_auth import BehaviorPrincipal
from .behavior_migration import (
    MigrationCurrent, MigrationHistory, MigrationStore, code_sha256, installed_origin, migration_key,
)
from .behavior_server_config import ServerConfiguration
from .behavior_wire import Digest, Identifier, WireModel
from .errors import Problem
from .source_materials import (
    AuthenticatedSourceContext, SourceEvent, SourceMaterials, SourceReference, authorize,
    canonical, control_row, digest, require_human_context, require_source_head, utc,
)

MAX_VERIFICATION_TTL = timedelta(minutes=20)


def permission_example() -> dict:
    """Intentionally invalid shape; never a generated approval or ready-to-use grant."""
    return {
        "schema_version": "fa-expired-source-verification-permission-v1",
        "permission_id": "<unique-operator-permission-id>",
        "action": "verify-expired-source",
        "operator_oid": "<configured-delegated-human-oid>",
        "authority_domain": "<exact-authority-domain>",
        "control_binding": "<exact-control-binding-sha256>",
        "new_configuration_sha256": "<applied-new-configuration-sha256>",
        "migration_history_key": "<exact-applied-history-key>",
        "migration_history_sha256": "<exact-applied-history-sha256>",
        "code_sha256": "<approved-frozen-migration-plan-code-sha256>",
        "reference": {
            "scope": {
                "tenant_id": "<source-tenant-id>", "team_id": "<source-team-id>",
                "visibility": "private", "owner_actor": "human-<configured-delegated-human-oid>",
            },
            "source_id": "<exact-source-id>", "version": 2,
            "sha256": "<exact-original-sha256>", "byte_length": 49,
        },
        "purpose": "<exact-accepted-source-purpose>",
        "expected_immutable_expiry": "<exact-source-expires_at-UTC>",
        "not_before": "<separately-authorized-UTC-window-start>",
        "expires_at": "<UTC-window-end-at-most-20-minutes-later>",
    }


class ExpiredSourceVerificationPermission(WireModel):
    schema_version: Literal["fa-expired-source-verification-permission-v1"] = (
        "fa-expired-source-verification-permission-v1"
    )
    permission_id: Identifier
    action: Literal["verify-expired-source"]
    operator_oid: Identifier
    authority_domain: Identifier
    control_binding: Digest
    new_configuration_sha256: Digest
    migration_history_key: Identifier
    migration_history_sha256: Digest
    code_sha256: Digest
    reference: SourceReference
    purpose: Identifier
    expected_immutable_expiry: datetime
    not_before: datetime
    expires_at: datetime

    @field_validator("expected_immutable_expiry", "not_before", "expires_at")
    @classmethod
    def explicit_utc(cls, value):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("verification times require explicit UTC")
        return value

    @model_validator(mode="after")
    def short_window(self):
        if not timedelta(0) < self.expires_at - self.not_before <= MAX_VERIFICATION_TTL:
            raise ValueError("verification permission must last at most 20 minutes")
        return self


class ExpiredSourceVerificationProof(WireModel):
    schema_version: Literal["fa-expired-source-verification-proof-v1"] = "fa-expired-source-verification-proof-v1"
    action: Literal["verify-expired-source"] = "verify-expired-source"
    outcome: Literal["PASS"] = "PASS"
    observed_code: Literal["SOURCE_EXPIRED"] = "SOURCE_EXPIRED"
    permission_id: Identifier
    permission_sha256: Digest
    operator_oid: Identifier
    authority_domain: Identifier
    control_binding: Digest
    new_configuration_sha256: Digest
    migration_history_key: Identifier
    migration_history_sha256: Digest
    code_sha256: Digest
    reference: SourceReference
    expected_immutable_expiry: datetime
    checked_at: datetime
    activation_performed: Literal[False] = False
    recovery_authorized: Literal[False] = False


class ExpiredSourceVerification:
    def __init__(self, store: MigrationStore, configuration: ServerConfiguration,
                 permission: ExpiredSourceVerificationPermission, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        if (not isinstance(store, MigrationStore) or not isinstance(configuration, ServerConfiguration)
                or not isinstance(permission, ExpiredSourceVerificationPermission) or not callable(clock)):
            raise ValueError("explicit server-pinned verification dependencies are required")
        configuration = ServerConfiguration.model_validate_json(configuration.model_dump_json())
        permission = ExpiredSourceVerificationPermission.model_validate_json(permission.model_dump_json())
        endpoint = urlsplit(configuration.table.endpoint)
        if (permission.operator_oid != configuration.identity.human_oid
                or permission.authority_domain != configuration.table.scope.authority_domain
                or permission.control_binding != configuration.workers.control_binding
                or permission.new_configuration_sha256 != configuration.sha256
                or store._scope != configuration.table.scope or store._binding != permission.control_binding
                or (endpoint.scheme, endpoint.netloc, endpoint.path.rstrip("/")) != store._client_url):
            raise ValueError("verification permission must bind the exact new configuration and authority")
        self.store, self.configuration, self.permission = store, configuration, permission
        self._clock = clock
        self._sources = SourceMaterials(store, clock=clock)

    def require_operator(self, principal: BehaviorPrincipal) -> None:
        identity = self.configuration.identity
        if (not isinstance(principal, BehaviorPrincipal) or principal.kind != "human"
                or principal.actor_id is not None or principal.oid != self.permission.operator_oid
                or principal.tenant_id != identity.tenant_id):
            raise Problem(403, "MIGRATION_OPERATOR_REQUIRED", "configured delegated human operator required")
        principal.require(identity.team_id, "read")
        principal.require(identity.team_id, "approve")

    def _window(self) -> datetime:
        now = utc(self._clock())
        if not self.permission.not_before <= now < self.permission.expires_at:
            raise Problem(403, "EXPIRED_SOURCE_PERMISSION_INACTIVE", "verification permission window is closed")
        return now

    def _installed(self) -> None:
        permission, configuration = self.permission, self.configuration
        try:
            installed_origin(self.store, configuration)
            current_key = migration_key(permission.authority_domain, "current")
            rows = self.store.read_snapshot((current_key, permission.migration_history_key))
            pointer = MigrationCurrent.model_validate_json(canonical(rows[current_key].payload["data"]))
            data = rows[permission.migration_history_key].payload["data"]
            history = MigrationHistory.model_validate_json(canonical(data))
            if (pointer.configuration_sha256 != permission.new_configuration_sha256
                    or pointer.history_key != permission.migration_history_key
                    or pointer.history_sha256 != permission.migration_history_sha256
                    or digest(data) != permission.migration_history_sha256
                    or history.plan.code_sha256 != permission.code_sha256
                    or code_sha256() != permission.code_sha256):
                raise ValueError("different installed history or code")
        except (Problem, ValueError, TypeError, KeyError, AttributeError):
            raise Problem(409, "EXPIRED_SOURCE_INSTALLATION_MISMATCH",
                          "exact migrated installation, history and frozen code are required") from None

    def _source_metadata(self, context):
        permission = self.permission
        reference = permission.reference
        rows = self.store.read_snapshot((reference.key, reference.head_key))
        event = control_row(rows, reference.key, SourceEvent)
        authorize(context, event.reference.scope, event.purpose)
        if event.reference != reference or event.purpose != permission.purpose:
            raise Problem(409, "SOURCE_REFERENCE_MISMATCH", "exact permitted source and purpose are required")
        if event.expires_at != permission.expected_immutable_expiry:
            raise Problem(409, "EXPIRED_SOURCE_EXPIRY_MISMATCH", "immutable source expiry differs")
        require_source_head(rows, reference)
        row = rows[reference.key]
        self._sources.verify_receipt(row, event.request, reference.key)
        return row

    def verify(self, principal: BehaviorPrincipal) -> ExpiredSourceVerificationProof:
        """Observe only; trusted principals must originate at the JWT boundary."""
        self.require_operator(principal)
        try:
            self._window()
            self._installed()
            permission = self.permission
            context = AuthenticatedSourceContext(
                principal.tenant_id, principal.team, principal.source_actor_id,
                permission.permission_id, (permission.purpose,),
            )
            require_human_context(principal, context, permission.reference.scope)
            before = self._source_metadata(context)
            self._window()
            try:
                self._sources.read_exact(context, permission.reference)
            except Problem as error:
                if error.status != 409 or error.body["error"]["code"] != "SOURCE_EXPIRED":
                    raise
            else:
                raise Problem(409, "EXPIRED_SOURCE_NOT_EXPIRED",
                              "exact source remained readable; no bytes are returned")
            if self._source_metadata(context) != before:
                raise Problem(409, "EXPIRED_SOURCE_CHANGED", "source changed during verification")
            self._installed()
            now = self._window()
            if now < permission.expected_immutable_expiry:
                raise Problem(409, "EXPIRED_SOURCE_NOT_EXPIRED", "trusted clock does not confirm source expiry")
            return ExpiredSourceVerificationProof(
                permission_id=permission.permission_id, permission_sha256=digest(permission.model_dump(mode="json")),
                operator_oid=principal.oid, authority_domain=permission.authority_domain,
                control_binding=permission.control_binding,
                new_configuration_sha256=permission.new_configuration_sha256,
                migration_history_key=permission.migration_history_key,
                migration_history_sha256=permission.migration_history_sha256, code_sha256=permission.code_sha256,
                reference=permission.reference, expected_immutable_expiry=permission.expected_immutable_expiry,
                checked_at=now,
            )
        except Problem as error:
            code = error.body["error"]["code"]
            allowed = {
                "EXPIRED_SOURCE_PERMISSION_INACTIVE", "EXPIRED_SOURCE_INSTALLATION_MISMATCH",
                "EXPIRED_SOURCE_EXPIRY_MISMATCH", "EXPIRED_SOURCE_NOT_EXPIRED", "EXPIRED_SOURCE_CHANGED",
                "SOURCE_REFERENCE_MISMATCH", "SOURCE_SCOPE_DENIED", "SOURCE_REQUIRED_MISSING",
                "SOURCE_WITHDRAWN", "SOURCE_ACCESS_CHANGED", "SOURCE_RECEIPT_MISMATCH",
                "BEHAVIOR_ACCESS_DENIED",
            }
            if code in allowed:
                raise Problem(error.status, code, "expired-source verification did not pass") from None
            raise Problem(503, "EXPIRED_SOURCE_VERIFICATION_UNKNOWN",
                          "expired-source verification did not pass") from None
        except Exception:
            raise Problem(503, "EXPIRED_SOURCE_VERIFICATION_UNKNOWN",
                          "expired-source verification did not pass") from None
