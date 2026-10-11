"""Explicit channel registry over one existing delivery authority and root pool."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from .behavior_delivery import ChannelContract, _Model, _utc
from .behavior_store import Digest
from .errors import Problem
from .models import Identifier
from .teams_deployment import PersonalTeamsConfig
from .teams_identity import TEAMS_VERSION


class TeamsChannelConfiguration(_Model):
    schema_version: Literal["fa-teams-metrics-channel-v1"] = "fa-teams-metrics-channel-v1"
    personal: PersonalTeamsConfig
    route_id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    template_binding_id: Identifier
    command: Literal["FA metrics"] = "FA metrics"
    max_requests: Annotated[int, Field(ge=1, le=16)]
    max_model_requests: Annotated[int, Field(ge=1, le=6)]
    native_max_generations: Annotated[int, Field(ge=1, le=8)]
    valid_until: datetime
    native_evidence_id: Identifier
    native_evidence_path: Annotated[str, Field(min_length=1, max_length=1024)]
    artifact_origin: Annotated[str, Field(min_length=1, max_length=1024)]
    required_confirmation_level: Literal["provider_accepted"] = "provider_accepted"
    channel: ChannelContract = ChannelContract(
        channel_id="msteams", version=TEAMS_VERSION,
        supported_levels=("provider_accepted",), max_confirmation_level="provider_accepted",
    )

    _time = field_validator("valid_until")(_utc)

    @field_validator("artifact_origin")
    @classmethod
    def origin(cls, value):
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment or parsed.port not in (None, 443)):
            raise ValueError("one exact HTTPS artifact API origin is required")
        return value

    @model_validator(mode="after")
    def confirmation_contract(self):
        expected = ChannelContract(channel_id="msteams", version=TEAMS_VERSION,
                                   supported_levels=("provider_accepted",), max_confirmation_level="provider_accepted")
        if self.channel != expected:
            raise ValueError("Teams supports only its pinned provider_accepted receipt contract")
        return self


class InstalledChannels:
    """An explicit v2 registry, not a caller-provided configuration-digest alias."""
    def __init__(self, primary, config):
        from .behavior_server_config import ServerConfigurationV2

        if (type(config) is not ServerConfigurationV2 or primary.contract != config.delivery
                or primary.contract.channel.channel_id != "authenticated-pull"
                or primary.store._binding != config.delivery.control_binding):
            raise Problem(403, "CHANNELS_CONFIGURATION_DENIED", "the installed v2 primary authority is required")
        self.primary, self.config = primary, config
        self.contract = config.delivery.model_copy(update={"channel": config.channels.channel})

    def check_contract(self, view):
        if (view.store is not self.primary.store or view.contract != self.contract
                or view.registration.mode != self.primary.registration.mode):
            raise Problem(403, "CHANNELS_AUTHORITY_DENIED", "channel views must share the original authority and mode")

    def configuration_digest(self, view):
        self.check_contract(view)
        # Existing quota/index/receipt bytes remain bound to the primary
        # contract. The separate v2 installation pins the channel registry.
        return self.primary.configuration_digest()

    @property
    def current_keys(self):
        from .behavior_channels_migration import channel_key

        return (channel_key("current", self.config.table.scope.authority_domain),
                channel_key("revocation", self.config.sha256))

    @property
    def admission_keys(self):
        from .behavior_operations import control_key

        return (*self.current_keys, control_key("teams-route-access", self.config.channels.route_id))

    def check_current(self, view, rows=None):
        from .behavior_channels_migration import parse_channel_installation, check_window_interval
        from .teams_identity import digest

        self.check_contract(view)
        rows = view.store.read_snapshot(self.current_keys) if rows is None else rows
        current_key, revoked_key = self.current_keys
        if rows[current_key] is None:
            raise Problem(403, "CHANNELS_NOT_INSTALLED", "同一 authority 的通道安裝不存在")
        current = parse_channel_installation(rows[current_key].payload["data"])
        check_window_interval(current, now=view._now())
        if (not current.active or current.configuration_sha256 != self.config.sha256
                or current.registry_sha256 != digest(self.config.channels.model_dump(mode="json"))
                or view._now() >= self.config.channels.valid_until
                or rows[revoked_key] is not None):
            raise Problem(403, "CHANNELS_RULE_DENIED", "通道規則已撤銷、過期或配置不同")
        return current

    def check_admission(self, view, rows, context):
        from .behavior_delivery import _parse
        from .teams_delivery import TeamsRouteAccess

        self.check_current(view, rows)
        access_key = self.admission_keys[-1]
        if rows[access_key] is None:
            raise Problem(403, "CHANNELS_NOT_INSTALLED", "個人路由授權不存在")
        access = _parse(rows[access_key].payload["data"], TeamsRouteAccess)
        if (not access.active or access.route_id != self.config.channels.route_id
                or access.recipient_id != self.config.identity.human_oid
                or context.owner_record_id not in access.owner_record_ids):
            raise Problem(403, "CHANNELS_ADMISSION_DENIED", "通道規則或個人路由已失效，不能接受新工作")


class ChannelRevocation(_Model):
    schema_version: Literal["fa-channel-revocation-v1"] = "fa-channel-revocation-v1"
    configuration_sha256: Digest
    registry_sha256: Digest
    operator_oid: Identifier
    revoked_at: datetime
    _time = field_validator("revoked_at")(_utc)
