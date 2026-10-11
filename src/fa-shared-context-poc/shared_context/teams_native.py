"""Server-only native Teams installation gate; no bootstrap, HTTP enable or Azure IO.

The current verifier is an independent deployment observer, not a caller's JSON.
require_current must retain the parent's installed-configuration/NativeGuard
checks. Route proof is read from the original signed capture in this authority.
Publishing only retains channel evidence; it never alters quotas or installation.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import field_validator

from .behavior_delivery import (
    AdmissionRequest, DeliveryCore, ReadinessEvidence, _Model, _check, _material, _parse, _utc, authority_snapshot,
)
from .behavior_operations import control_key
from .behavior_store import ControlChanges, ControlRequest, ControlWrite, Digest, InlineMaterial
from .errors import Problem
from .models import Identifier
from .teams_deployment import PersonalTeamsConfig
from .teams_identity import TEAMS_VERSION, digest

NATIVE_SENDER_VERSION = "fa-teams-native-sender-v1"
COVERAGE = {
    "atomic_control", "inline_retention", "durable_consumer",
    "timer_redrive", "bounded_scan", "ownership_recovery",
}


class TeamsNativeEvidence(_Model):
    schema_version: Literal["fa-teams-native-installation-v1"] = "fa-teams-native-installation-v1"
    evidence_id: Identifier
    evidence_mode: Literal["native"] = "native"
    sender_version: Literal["fa-teams-native-sender-v1"] = NATIVE_SENDER_VERSION
    channel_version: Literal["fa-teams-connector-v1"] = TEAMS_VERSION
    configuration_digest: Digest
    server_configuration_sha256: Digest
    control_binding: Digest
    personal_configuration_digest: Digest
    settings_digest: Digest
    installation_row_id: Identifier
    installation_data_sha256: Digest
    route_witness_sha256: Digest
    capture_binding_sha256: Digest
    bot_registration_sha256: Digest
    personal_installation_sha256: Digest
    package_sha256: Digest
    callback_url: str
    bot_app_id: Identifier
    bot_principal_id: Identifier
    readiness: ReadinessEvidence
    observed_at: datetime
    expires_at: datetime

    _times = field_validator("observed_at", "expires_at")(_utc)


@dataclass(frozen=True)
class TeamsNativeRegistration:
    personal: PersonalTeamsConfig
    server_configuration_sha256: str
    installation_row_id: str
    evidence_id: str
    verify_current: Callable[[str], TeamsNativeEvidence]
    require_current: Callable[[], None]

    def __post_init__(self):
        if not callable(self.verify_current) or not callable(self.require_current):
            raise Problem(403, "TEAMS_NATIVE_DISABLED", "current native observation and installed-authority guard are required")


class TeamsDeliveryCore(DeliveryCore):
    """Add native installation predicates to the ORIGINAL admission transaction.

    DeliveryCore's admission always checks _quotas inside its guarded decision.
    Pin native evidence there, not just in a readiness callback before the EGT.
    Other transitions, especially historical receipts, keep their original lane.
    """
    def __init__(self, *args, shared_channels=None, **kwargs):
        from .behavior_channels import InstalledChannels

        if shared_channels is not None and type(shared_channels) is not InstalledChannels:
            raise Problem(403, "CHANNELS_CONFIGURATION_DENIED", "an explicit installed channel registry is required")
        self.shared_channels = shared_channels
        super().__init__(*args, **kwargs)

    def configuration_digest(self):
        if self.shared_channels is not None:
            return self.shared_channels.configuration_digest(self)
        return super().configuration_digest()

    def request_for(self, command, context):
        cleanup = getattr(self.registration.channel_adapter, "cleanup_authority", None)
        if cleanup is not None:
            cleanup.validate_command(command, context)
        request = super().request_for(command, context)
        if isinstance(command, AdmissionRequest):
            keys = self.shared_channels.admission_keys if self.shared_channels is not None else ()
            if self.registration.mode == "native":
                keys = (*keys, *self.registration.channel_adapter.native.admission_keys)
            return request.model_copy(update={"read_set": tuple(sorted(set((*request.read_set, *keys))))})
        return request

    def _quotas(self, snapshot, context):
        if self.shared_channels is not None and self.shared_channels.admission_keys[0] in snapshot:
            self.shared_channels.check_admission(self, snapshot, context)
        if self.registration.mode == "native":
            adapter = self.registration.channel_adapter
            if adapter.native.current_key in snapshot:
                adapter.check_installation(self, snapshot)
        return super()._quotas(snapshot, context)


class TeamsNativeGate:
    def __init__(self, adapter, registration: TeamsNativeRegistration):
        self.adapter, self.registration = adapter, registration
        personal = registration.personal
        self.current_key = control_key("teams-native-current", personal.configuration_digest)
        self.evidence_key = control_key("teams-native-evidence", registration.evidence_id)
        self.route_key = control_key("teams-personal-route", personal.configuration_digest)
        self.capture_key = control_key("teams-capture-route-binding", personal.configuration_digest)
        self.keys = (
            self.current_key, self.evidence_key, registration.installation_row_id,
            self.route_key, self.capture_key,
        )
        self.admission_keys = (
            self.current_key, registration.installation_row_id, self.route_key, self.capture_key,
        )

    def _row(self, rows, key):
        row = rows.get(key)
        if row is None or row.payload["binding"] != self.adapter.core.contract.control_binding:
            raise Problem(403, "TEAMS_NATIVE_INSTALLATION_REQUIRED", "native evidence is absent from the exact authority")
        return row

    def _retained(self, rows, key, model):
        row = self._row(rows, key)
        value = _parse(row.payload["data"], model)
        if _parse(row.payload["material"], InlineMaterial) != _material(value.model_dump(mode="json")):
            raise Problem(403, "TEAMS_NATIVE_EVIDENCE_CHANGED", "retained native evidence differs from its fixed material")
        return value

    def _validate(self, evidence, rows):
        from .teams_personal import _RouteWitness

        adapter, registration = self.adapter, self.registration
        core, personal = adapter.core, registration.personal
        config, now = core.contract, _utc(adapter.clock())
        readiness = evidence.readiness
        if (evidence.evidence_id != registration.evidence_id
                or evidence.configuration_digest != core.configuration_digest()
                or evidence.server_configuration_sha256 != registration.server_configuration_sha256
                or evidence.control_binding != config.control_binding
                or evidence.personal_configuration_digest != personal.configuration_digest
                or evidence.settings_digest != digest(adapter.settings.model_dump(mode="json"))
                or evidence.installation_row_id != registration.installation_row_id
                or evidence.callback_url != personal.endpoint
                or evidence.bot_app_id != personal.uami_client_id
                or evidence.bot_principal_id != personal.uami_principal_id
                or readiness.configuration_digest != evidence.configuration_digest
                or readiness.control_binding != config.control_binding
                or (readiness.protection_version, readiness.consumer_version, readiness.timer_version)
                != (config.protection_version, config.consumer_version, config.timer_version)
                or set(readiness.coverage) != COVERAGE or len(readiness.coverage) != len(COVERAGE)
                or not evidence.observed_at <= now < evidence.expires_at
                or not readiness.observed_at <= now < readiness.expires_at):
            raise Problem(403, "TEAMS_NATIVE_INSTALLATION_DENIED", "current native configuration, identity or freshness differs")
        installation = self._row(rows, registration.installation_row_id).payload["data"]
        if (installation.get("configuration_sha256") != registration.server_configuration_sha256
                or installation.get("control_binding") != config.control_binding
                or digest(installation) != evidence.installation_data_sha256):
            raise Problem(403, "TEAMS_NATIVE_INSTALLATION_DENIED", "the registered server configuration is not currently installed")
        witness = self._retained(rows, self.route_key, _RouteWitness)
        capture_row = self._row(rows, self.capture_key)
        capture = capture_row.payload["data"]
        if (witness.evidence_mode != "connector_http"
                or witness.configuration_digest != personal.configuration_digest
                or adapter.settings.routes != (witness.route,)
                or witness.route.conversation_type != "personal" or witness.route.thread_id is not None
                or witness.route.tenant_id != personal.tenant_id
                or witness.route.aad_object_id != personal.intended_user_oid
                or witness.route.bot_app_id != personal.uami_client_id
                or witness.route.bot_id != "28:" + personal.uami_client_id
                or _material(witness.model_dump(mode="json")).sha256 != evidence.route_witness_sha256
                or digest(capture) != evidence.capture_binding_sha256
                or _parse(capture_row.payload["material"], InlineMaterial) != _material(capture)
                or capture.get("route_row_id") != self.route_key
                or capture.get("route_material_sha256") != evidence.route_witness_sha256
                or capture.get("control_binding") != config.control_binding
                or capture.get("personal_configuration_sha256") != personal.configuration_digest):
            raise Problem(403, "TEAMS_NATIVE_ROUTE_DENIED", "native requires the original signed personal route and capture binding")
        return evidence

    def observed(self):
        self.registration.require_current()
        return _check(self.registration.verify_current(self.registration.evidence_id), TeamsNativeEvidence)

    def check(self, rows=None):
        evidence = self.observed()
        rows = authority_snapshot(self.adapter.store, self.keys) if rows is None else rows
        if self.evidence_key not in rows:
            # The stable current pointer pins this immutable history row. Keep
            # admission request read-sets stable across evidence-only renewal.
            rows = {**rows, **authority_snapshot(self.adapter.store, (self.evidence_key,))}
        current = self._row(rows, self.current_key).payload["data"]
        retained = self._retained(rows, self.evidence_key, TeamsNativeEvidence)
        if (current.get("active") is not True
                or current.get("evidence_id") != evidence.evidence_id
                or current.get("evidence_digest") != digest(evidence.model_dump(mode="json"))
                or retained != evidence):
            raise Problem(403, "TEAMS_NATIVE_INSTALLATION_DENIED", "installed native evidence was revoked or replaced")
        return self._validate(evidence, rows)

    def check_wire(self, send):
        # Full observer/NativeGuard work already ran inside consumption. Recheck
        # only published revocation/config predicates in the tiny wire window.
        rows = authority_snapshot(self.adapter.store, (self.current_key, self.registration.installation_row_id))
        current = self._row(rows, self.current_key).payload["data"]
        installation = self._row(rows, self.registration.installation_row_id).payload["data"]
        if (current.get("active") is not True or current.get("evidence_id") != send.native_evidence_id
                or current.get("evidence_digest") != send.native_evidence_digest
                or installation.get("configuration_sha256") != self.registration.server_configuration_sha256
                or digest(installation) != send.native_installation_data_sha256):
            raise Problem(403, "TEAMS_NATIVE_INSTALLATION_DENIED", "consumed native installation was revoked or changed")

    def publish(self):
        evidence = self.observed()
        data = evidence.model_dump(mode="json")
        request = ControlRequest(
            request_id=control_key("teams-native-install", evidence.evidence_id),
            request_digest=digest(data), read_set=self.keys,
        )

        def decide(rows):
            if self.observed() != evidence:
                raise Problem(403, "TEAMS_NATIVE_EVIDENCE_CHANGED", "current deployment observation changed during publication")
            self._validate(evidence, rows)
            current = rows[self.current_key]
            if current is not None:
                old = current.payload["data"]
                if (old.get("active") is not True or "observed_at" not in old
                        or _utc(datetime.fromisoformat(old["observed_at"])) >= evidence.observed_at):
                    raise Problem(409, "TEAMS_NATIVE_ALREADY_INSTALLED", "revoked or newer native installation cannot be republished")
            return ControlChanges(writes=(
                ControlWrite(row_id=self.current_key, mode="create" if current is None else "replace", data={
                    "evidence_id": evidence.evidence_id, "evidence_digest": digest(data), "active": True,
                    "observed_at": evidence.observed_at.isoformat(),
                }),
                ControlWrite(row_id=self.evidence_key, mode="create", data=data, material=_material(data)),
            ), result={"status": "teams_native_evidence_retained", "enables_admission": False,
                       "evidence_id": evidence.evidence_id})

        result = self.adapter.store.transact(request, decide)
        self.check()
        return result


def compose_installed_personal_channel(
    parent, enrollment, seams, *, evidence_id, verify_current,
    artifact_links=None, artifact_origins=(),
):
    """Use the original server/store/guard; never relabel a pull installation.

    This deliberately rejects today's authenticated-pull SDK configuration.
    A legal multi-channel configuration transition is separate work, not an
    excuse to create new root quotas, replace NativeGuard or reuse pull ACKs.
    """
    from .teams_personal import compose_personal_channel

    from dataclasses import replace
    from .behavior_channels import InstalledChannels
    from .behavior_server_config import ServerConfigurationV2

    delivery = parent.runtime.dep.delivery
    shared = InstalledChannels(delivery, parent.config) if type(parent.config) is ServerConfigurationV2 else None
    if (delivery.registration is None or delivery.registration.mode != "native"
            or shared is None and delivery.contract.channel.channel_id != "msteams"):
        raise Problem(403, "TEAMS_PARENT_CHANNEL_INCOMPATIBLE",
                      "installed parent is not a native Teams channel; no sideplane quota or receipt relabelling is allowed")
    if (enrollment.store is not parent.runtime.store or delivery.store is not enrollment.store
            or parent.config.delivery != delivery.contract):
        raise Problem(403, "TEAMS_PERSONAL_COMPOSITION_DENIED", "the exact installed parent authority and delivery contract are required")
    registration = TeamsNativeRegistration(
        personal=enrollment.config, server_configuration_sha256=parent.config.sha256,
        installation_row_id=control_key("server-installation", parent.config.table.scope.authority_domain),
        evidence_id=evidence_id, verify_current=verify_current,
        require_current=parent.require_current_native_evidence,
    )
    runtime_registration = delivery.registration if shared is None else replace(
        delivery.registration, contract=shared.contract,
        verify_readiness=lambda: verify_current(evidence_id).readiness,
    )
    return compose_personal_channel(
        enrollment, runtime_registration, seams, native_registration=registration, shared_channels=shared,
        artifact_links=artifact_links, artifact_origins=artifact_origins,
    )
