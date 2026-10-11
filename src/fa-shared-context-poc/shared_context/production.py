from __future__ import annotations

import os
import re
from contextlib import ExitStack
from urllib.parse import urlparse

from azure.ai.projects import AIProjectClient
from azure.data.tables import TableClient
from azure.identity import ManagedIdentityCredential

from .api import create_app
from .auth import AuthSettings, JwtVerifier
from .backup_models import MAX_BACKUP_BYTES
from .backup_store import BlobBackupStore
from .config import BackupSettings
from .memory import FoundryMemory
from .m2_gateway import M2Gateway, M2Settings
from .service import ContextService
from .storage import TableRepository


def configured_url(name: str, host_suffix: str | None = None) -> str:
    value = os.environ[name].rstrip("/")
    parsed = urlparse(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.port not in (None, 443)):
        raise ValueError(f"{name} must be a HTTPS URL without credentials, query, or fragment")
    if host_suffix and not parsed.hostname.endswith(host_suffix):
        raise ValueError(f"{name} has an unexpected host suffix")
    return value


def create_production_app():
    # Deliberately no DefaultAzureCredential, test header, local auth or anonymous fallback.
    from .m3_config import M3Settings, model_settings_from_env
    from .m4_config import M4Settings
    from .m4_fixtures import POLICY_ARTIFACT

    auth = AuthSettings.from_env()
    backup_settings = BackupSettings.from_env()
    m2_settings = M2Settings.from_env()
    m3_settings = M3Settings.from_env()
    m3_model_settings = model_settings_from_env()
    m4_settings = M4Settings.from_env()
    if m4_settings is not None and (
        m3_settings is None or POLICY_ARTIFACT not in m3_settings.policy_artifacts
    ):
        raise ValueError("M4 requires the existing M3 artifact module with its pinned policy ID")
    if m3_model_settings is not None and m3_settings is None:
        raise ValueError("M3 model settings require the separately configured artifact module")
    if m3_settings is not None and backup_settings == m3_settings.storage:
        raise ValueError("M3 artifacts require a container separate from private M1 backups")
    table_endpoint = configured_url("CONTEXT_TABLE_ENDPOINT", ".table.core.windows.net")
    project_endpoint = configured_url("CONTEXT_MEMORY_ENDPOINT", ".services.ai.azure.com")
    table_name = os.environ["CONTEXT_TABLE_NAME"]
    memory_store = os.environ["CONTEXT_MEMORY_STORE"]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{2,62}", table_name):
        raise ValueError("invalid CONTEXT_TABLE_NAME")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", memory_store):
        raise ValueError("invalid CONTEXT_MEMORY_STORE")
    if m4_settings is not None and memory_store == m4_settings.memory_store:
        raise ValueError("M4 memory store must be separate from the existing memory store")
    with ExitStack() as resources:
        credential = ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID") or None)
        resources.callback(credential.close)
        table = TableClient(endpoint=table_endpoint, table_name=table_name, credential=credential,
                            retry_total=0, connection_timeout=10, read_timeout=20)
        resources.callback(table.close)
        project = AIProjectClient(
            endpoint=project_endpoint, credential=credential, api_version="v1",
            credential_scopes=["https://ai.azure.com/.default"],
            retry_total=0, connection_timeout=10, read_timeout=20,
        )
        resources.callback(project.close)
        backup_store = None
        if backup_settings is not None:
            from azure.storage.blob import ContainerClient

            container = ContainerClient(
                account_url=backup_settings.endpoint, container_name=backup_settings.container,
                credential=credential, retry_total=0, retry_connect=0, retry_read=0, retry_status=0,
                connection_timeout=10, read_timeout=20,
                max_single_put_size=MAX_BACKUP_BYTES, max_single_get_size=MAX_BACKUP_BYTES,
            )
            resources.callback(container.close)
            backup_store = BlobBackupStore(container, auth.tenant_id)
        service = ContextService(TableRepository(table), FoundryMemory(project, project_endpoint, memory_store), auth)
        artifacts, artifact_identity = None, None
        if m3_settings is not None:
            from azure.storage.blob import ContainerClient
            from .m3_artifacts import ArtifactService
            from .m3_auth import M3JwtVerifier
            from .m3_models import MAX_ARTIFACT_BYTES
            from .m3_store import BlobArtifactStore

            artifact_identity = M3JwtVerifier(auth, m3_settings.approval)
            container = ContainerClient(
                account_url=m3_settings.storage.endpoint, container_name=m3_settings.storage.container,
                credential=credential, retry_total=0, retry_connect=0, retry_read=0, retry_status=0,
                connection_timeout=10, read_timeout=20,
                max_single_put_size=MAX_ARTIFACT_BYTES, max_single_get_size=MAX_ARTIFACT_BYTES,
            )
            resources.callback(container.close)
            artifacts = ArtifactService(service, BlobArtifactStore(container, auth.tenant_id), m3_settings.approval)

        m4_service = None
        if m4_settings is not None:
            from .m4_context import M4ContextService, M4FoundryMemory

            m4_service = M4ContextService(
                service, M4FoundryMemory(project, project_endpoint, m4_settings.memory_store), m4_settings, artifacts,
            )
        model_gateway = M2Gateway(service, m2_settings, project_endpoint, credential) if m2_settings is not None else None
        m3_model_gateway = M2Gateway(
            service, m3_model_settings, project_endpoint, credential, milestone="m3",
        ) if m3_model_settings is not None else None
        m4_model_gateway = M2Gateway(
            service, m4_settings.model_settings, project_endpoint, credential, milestone="m4",
        ) if m4_settings is not None else None

        def close():
            owned_clients.close()

        app = create_app(
            service, JwtVerifier(auth), close, backup_store, model_gateway=model_gateway, artifact_service=artifacts,
            verified_artifact_identity_dependency=artifact_identity,
            artifact_policy_ids=m3_settings.policy_artifacts if m3_settings else (),
            m3_model_gateway=m3_model_gateway,
            m4_service=m4_service, m4_model_gateway=m4_model_gateway,
        )
        owned_clients = resources.pop_all()
        return app
