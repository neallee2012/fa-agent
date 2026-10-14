"""Personal Teams deployment inputs and deterministic OFFLINE app packaging.

The Bot Service resource contract supports an existing UAMI: msaAppType is
UserAssignedMSI, msaAppId is its CLIENT ID, and msaAppMSIResourceId is its ARM ID.
No Entra application, secret, federated credential, role, install or send is
created here. Metadata and package validation never establish a Connector route.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import struct
import uuid
import zipfile
import zlib
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, field_validator, model_validator

from .behavior_delivery import _Model, _json
from .errors import Problem
from .models import Identifier

TENANT = "00000000-0000-0000-0000-000000000013"
SUBSCRIPTION = "00000000-0000-0000-0000-000000000012"
RESOURCE_GROUP = "rg-faexample"
INTENDED_USER = "00000000-0000-0000-0000-00000000001b"
UAMI_NAME = "id-facontext-api"
UAMI_RESOURCE_ID = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{UAMI_NAME}"
CALLBACK_PATH = "/behavior/v1/teams/activities"
APP_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://fa.invalid/teams-personal/" + TENANT))
MANIFEST_SCHEMA = "https://developer.microsoft.com/json-schemas/teams/v1.19/MicrosoftTeams.schema.json"
MANIFEST_SCHEMA_SHA256 = "cd5815382f0810ff947f94debd4d03a3a3f62a2a671b090ef2b8c12e761de961"
HOST_RESOURCE_ID = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.App/containerApps/ca-fa-behavior-poc"
Guid = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]


def public_https(value: str, *, origin: bool = False) -> str:
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        try:
            ipaddress.ip_address(host)
        except ValueError:
            is_ip = False
        else:
            is_ip = True
        if (
            url.scheme != "https" or url.netloc != host or is_ip or "." not in host
            or host.endswith((".localhost", ".local", ".internal", ".invalid", ".test"))
            or re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) is None
            or ".." in host or url.username or url.password or url.query or url.fragment
            or "\\" in value or any(c.isspace() or ord(c) < 32 for c in value)
            or (origin and (url.path or value != "https://" + host))
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise Problem(422, "TEAMS_CONFIG_URL_DENIED", "an explicit public HTTPS DNS endpoint without credentials, query or fragment is required") from None
    return value


class PersonalTeamsConfig(_Model):
    schema_version: Literal["fa-teams-personal-config-v1"] = "fa-teams-personal-config-v1"
    subscription_id: Literal["00000000-0000-0000-0000-000000000012"] = SUBSCRIPTION
    tenant_id: Literal["00000000-0000-0000-0000-000000000013"] = TENANT
    resource_group: Literal["rg-faexample"] = RESOURCE_GROUP
    intended_user_oid: Literal["00000000-0000-0000-0000-00000000001b"] = INTENDED_USER
    uami_resource_id: Literal[UAMI_RESOURCE_ID] = UAMI_RESOURCE_ID
    uami_client_id: Guid
    uami_principal_id: Guid
    host_resource_id: Literal[HOST_RESOURCE_ID] = HOST_RESOURCE_ID
    bot_name: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")] = "fa-teams-personal-example"
    teams_app_id: Guid = APP_ID
    app_version: Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$", max_length=32)] = "1.0.0"
    callback_origin: Annotated[str, Field(min_length=1, max_length=512)]
    website_url: Annotated[str, Field(min_length=1, max_length=1024)]
    privacy_url: Annotated[str, Field(min_length=1, max_length=1024)]
    terms_url: Annotated[str, Field(min_length=1, max_length=1024)]
    team_id: Identifier
    native_admission_enabled: Literal[False] = False
    route: None = None

    @field_validator("callback_origin")
    @classmethod
    def callback(cls, value):
        return public_https(value, origin=True)

    @field_validator("website_url", "privacy_url", "terms_url")
    @classmethod
    def metadata_url(cls, value):
        return public_https(value)

    @model_validator(mode="after")
    def identity(self):
        ids = (self.uami_client_id, self.uami_principal_id, self.teams_app_id)
        if any(uuid.UUID(value).int == 0 for value in ids) or self.uami_client_id == self.uami_principal_id:
            raise ValueError("distinct nonzero UAMI client/principal IDs are required")
        if INTENDED_USER in ids or TENANT in ids:
            raise ValueError("the test user/tenant is not a bot identity")
        if self.native_admission_enabled is not False:
            raise ValueError("native admission must be explicitly false")
        return self

    @property
    def endpoint(self) -> str:
        return self.callback_origin + CALLBACK_PATH

    @property
    def bot_resource_id(self) -> str:
        return f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.BotService/botServices/{self.bot_name}"

    @property
    def configuration_digest(self) -> str:
        return hashlib.sha256(_json(self.model_dump(mode="json"))).hexdigest()

    def bot_environment(self) -> dict[str, str]:
        return {
            "MicrosoftAppType": "UserAssignedMSI", "MicrosoftAppId": self.uami_client_id,
            "MicrosoftAppTenantId": TENANT, "AZURE_CLIENT_ID": self.uami_client_id,
        }


def load_config(path: Path) -> PersonalTeamsConfig:
    try:
        content = path.read_bytes()
        if len(content) > 16384:
            raise ValueError
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        return PersonalTeamsConfig.model_validate(json.loads(content, object_pairs_hook=unique))
    except (OSError, ValidationError, ValueError, RecursionError):
        raise Problem(422, "TEAMS_CONFIG_INVALID", "complete validated personal Teams configuration is required; raw values are not logged") from None


def manifest(config: PersonalTeamsConfig) -> dict:
    try:
        assets = Path(__file__).resolve().parents[3] / "deploy" / "teams"
        value = json.loads((assets / "manifest.template.json").read_bytes())
    except (IndexError, OSError, ValueError):
        raise Problem(503, "TEAMS_BUILD_ASSETS_REQUIRED", "package building requires the reviewed checkout manifest template") from None
    value["id"], value["version"] = config.teams_app_id, config.app_version
    value["bots"][0]["botId"] = config.uami_client_id
    value["developer"].update(
        websiteUrl=config.website_url, privacyUrl=config.privacy_url, termsOfUseUrl=config.terms_url,
    )
    return value


def validate_manifest_schema(config: PersonalTeamsConfig, schema_file: Path) -> None:
    from jsonschema import Draft4Validator, FormatChecker

    try:
        content = schema_file.read_bytes()
        if hashlib.sha256(content).hexdigest() != MANIFEST_SCHEMA_SHA256:
            raise ValueError
        schema = json.loads(content)
        errors = list(Draft4Validator(schema, format_checker=FormatChecker()).iter_errors(manifest(config)))
        if errors:
            raise ValueError
    except (OSError, ValueError):
        raise Problem(422, "TEAMS_MANIFEST_SCHEMA_DENIED", "the exact pinned official schema and a valid manifest are required") from None


def _icon(size: int, outline: bool) -> bytes:
    # Original geometric FA glyph; no external icons or image dependencies.
    rows = []
    for y in range(size):
        row = bytearray(b"\x00")
        for x in range(size):
            gx, gy = x * 16 // size, y * 16 // size
            glyph = (3 <= gx <= 4 and 4 <= gy <= 11 or 3 <= gx <= 7 and gy in (4, 7)
                     or gx in (9, 13) and 5 <= gy <= 11 or 10 <= gx <= 12 and gy in (4, 8))
            row.extend((255, 255, 255, 255) if glyph else (0, 0, 0, 0) if outline else (29, 78, 216, 255))
        rows.append(bytes(row))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def build_artifacts(config: PersonalTeamsConfig, directory: Path) -> dict:
    config = PersonalTeamsConfig.model_validate(config.model_dump())
    files = {
        "manifest.json": _json(manifest(config)),
        "color.png": _icon(192, False), "outline.png": _icon(32, True),
    }
    parameters = {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {name: {"value": value} for name, value in {
            "botName": config.bot_name, "existingIdentityName": UAMI_NAME,
            "tenantId": TENANT, "endpoint": config.endpoint,
        }.items()},
    }
    try:
        directory.mkdir(parents=False, exist_ok=False)
        for name, data in {**files, "bot.parameters.json": _json(parameters),
                           "personal.config.json": _json(config.model_dump(mode="json")),
                           "bot.environment.json": _json(config.bot_environment())}.items():
            (directory / name).write_bytes(data)
        with zipfile.ZipFile(directory / "fa-teams-personal.zip", "x", zipfile.ZIP_DEFLATED) as package:
            for name, data in files.items():
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 0
                info.external_attr = 0o600 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                package.writestr(info, data)
    except OSError:
        raise Problem(409, "TEAMS_ARTIFACT_WRITE_DENIED", "use a new writable output directory; existing files are never overwritten") from None
    return {
        "status": "offline_artifacts_created", "configuration_digest": config.configuration_digest,
        "package_sha256": hashlib.sha256((directory / "fa-teams-personal.zip").read_bytes()).hexdigest(),
        "native_admission_enabled": False, "route_established": False, "actual_environment": "NOT_RUN",
    }
