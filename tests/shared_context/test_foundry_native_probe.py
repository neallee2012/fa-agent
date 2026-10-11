"""N0 contracts with the pinned SDK; all hosted storage and identity I/O is OFFLINE."""

import base64
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec
from urllib.parse import urlsplit

import pytest
from azure.ai.agentserver.core.storage import (
    FoundryStateStore, FoundryStorageApiError, FoundryStorageConflictError,
    FoundryStorageNotFoundError, FoundryStoragePreconditionError,
)
from azure.ai.agentserver.core.tasks import (
    TaskManagerNotInitialized, resilient_tasks_enabled, set_resilient_tasks_enabled,
)
from azure.ai.agentserver.core.tasks import _manager
from azure.core.exceptions import ServiceResponseError
from starlette.testclient import TestClient

from shared_context.behavior_foundry_probe import (
    PROBE_AGENT_ID, PROBE_AGENT_NAME, PROBE_HEADER, PROBE_PROFILE,
    PROBE_PROJECT_ENDPOINT, PROBE_STORE,
)
from shared_context.behavior_hosted import create_host
from shared_context.behavior_wire import HostedIdentity, NATIVE_LAUNCH_PERMIT_HEADER
from shared_context.errors import Problem

PROBE_ID = "0123456789abcdef0123456789abcdef"
HEADERS = {
    PROBE_HEADER: PROBE_PROFILE, "x-agent-session-id": "OFFLINE-session",
    "x-agent-invocation-id": "OFFLINE-invocation", "x-agent-foundry-call-id": "OFFLINE-call",
}
REAL_TASK_MANAGER = _manager.TaskManager
get_task_manager = _manager.get_task_manager


class OfflineStorage:
    """Explicit fake remote boundary; SDK serialization and public methods are real."""

    def __init__(self):
        self.descriptor = None
        self.items = {}
        self.calls = []
        self.status = None
        self.cas_conflict = False
        self.lose_put_response = False

    @staticmethod
    def response(value):
        encoded = json.dumps(value)
        return SimpleNamespace(text=lambda: encoded)

    async def send(self, request):
        assert request.url.startswith(PROBE_PROJECT_ENDPOINT + "/storage/")
        assert request.headers["x-agent-foundry-call-id"] == "OFFLINE-call"
        path = urlsplit(request.url).path.split("/storage/", 1)[1]
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body, dict(request.headers)))
        if self.status is not None:
            raise FoundryStorageApiError("OFFLINE_PRIVATE_DETAILS", status_code=self.status)
        if path == "state_stores":
            assert request.method == "POST" and body["name"] == PROBE_STORE
            assert body["item_ttl_seconds"] == -1 and body["user_isolation"] is False
            self.descriptor = {
                **body, "id": "OFFLINE-store", "object": "state_store", "created_at": 1, "updated_at": 1,
            }
            return self.response(self.descriptor)
        segments = path.split("/")
        name = base64.urlsafe_b64decode(segments[1] + "=" * (-len(segments[1]) % 4)).decode()
        assert name == PROBE_STORE
        if self.descriptor is None:
            raise FoundryStorageNotFoundError("OFFLINE missing store", status_code=404)
        if len(segments) == 2:
            assert request.method == "GET"
            return self.response(self.descriptor)
        if len(segments) == 3:
            assert request.method == "POST" and segments[2] == "items"
            key = body["key"]
            if key in self.items:
                raise FoundryStorageConflictError("OFFLINE duplicate key", status_code=409)
            self.items[key] = {"key": key, "value": body["value"], "etag": '"OFFLINE-created"'}
            return self.response(self.items[key])
        key = base64.urlsafe_b64decode(segments[3] + "=" * (-len(segments[3]) % 4)).decode()
        if key not in self.items:
            raise FoundryStorageNotFoundError("OFFLINE missing item", status_code=404)
        if request.method == "PUT":
            if self.cas_conflict or request.headers.get("If-Match") != self.items[key]["etag"]:
                raise FoundryStoragePreconditionError("OFFLINE stale ETag", status_code=412)
            self.items[key] = {"key": key, "value": body["value"], "etag": '"OFFLINE-updated"'}
            if self.lose_put_response:
                raise ServiceResponseError("OFFLINE_PRIVATE_DETAILS: saved but response lost")
        else:
            assert request.method == "GET"
        return self.response(self.items[key])


@pytest.fixture
def sdk_state(monkeypatch, tmp_path):
    old_switch = resilient_tasks_enabled()
    try:
        old_manager = get_task_manager()
    except TaskManagerNotInitialized:
        old_manager = None
    set_resilient_tasks_enabled(False)
    _manager.set_task_manager(None)
    monkeypatch.delenv("FA_N0_STATE_PROBE", raising=False)
    monkeypatch.delenv("FOUNDRY_HOSTING_ENVIRONMENT", raising=False)
    monkeypatch.delenv("AGENTSERVER_TASKS_BACKEND", raising=False)
    monkeypatch.setenv("AGENTSERVER_STATE_ROOT", str(tmp_path / "OFFLINE-sdk-state"))
    yield
    _manager.set_task_manager(old_manager)
    set_resilient_tasks_enabled(old_switch)


@pytest.fixture
def offline(sdk_state, monkeypatch):
    import azure.identity.aio

    storage, managers, credentials = OfflineStorage(), [], []
    settings = SimpleNamespace(startup_error=None)

    class OfflineCredential:
        def __init__(self):
            self.closed = False
            credentials.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        async def get_token(self, *args, **kwargs):
            pytest.fail("N0 offline tests must never request a token")

    async def send(client, request):
        return await storage.send(request)

    def manager(**kwargs):
        instance = create_autospec(REAL_TASK_MANAGER, instance=True)
        instance.startup.side_effect = settings.startup_error
        managers.append(instance)
        return instance

    monkeypatch.setattr(azure.identity.aio, "ManagedIdentityCredential", OfflineCredential)
    monkeypatch.setattr(FoundryStateStore, "_send_storage_request", send)
    monkeypatch.setattr(_manager, "TaskManager", manager)
    monkeypatch.setenv("FOUNDRY_HOSTING_ENVIRONMENT", "OFFLINE-hosted-boundary")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", PROBE_PROJECT_ENDPOINT)
    monkeypatch.setenv("FA_N0_STATE_PROBE", PROBE_PROFILE)
    identity = HostedIdentity(
        agent_id=PROBE_AGENT_ID, agent_name=PROBE_AGENT_NAME, agent_version="4", session_id="OFFLINE-session",
    )
    for name, value in {
        "FOUNDRY_AGENT_ID": identity.agent_id, "FOUNDRY_AGENT_NAME": identity.agent_name,
        "FOUNDRY_AGENT_VERSION": identity.agent_version, "FOUNDRY_AGENT_SESSION_ID": identity.session_id,
    }.items():
        monkeypatch.setenv(name, value)
    runtime = SimpleNamespace(identity=identity, start=Mock(return_value={"status": "accepted"}))
    return SimpleNamespace(storage=storage, managers=managers, credentials=credentials,
                           runtime=runtime, settings=settings)


def invoke(runtime, phase="write", **fields):
    with TestClient(create_host(runtime)) as client:
        return client.post("/invocations", headers=HEADERS,
                           json={"phase": phase, "probe_id": PROBE_ID, **fields})


def test_pinned_invocations_host_owns_real_local_task_manager_lifecycle(sdk_state, monkeypatch):
    events = []
    startup, shutdown = REAL_TASK_MANAGER.startup, REAL_TASK_MANAGER.shutdown

    async def start(manager):
        events.append("startup")
        await startup(manager)

    async def stop(manager):
        events.append("shutdown")
        await shutdown(manager)

    monkeypatch.setattr(REAL_TASK_MANAGER, "startup", start)
    monkeypatch.setattr(REAL_TASK_MANAGER, "shutdown", stop)
    set_resilient_tasks_enabled(True)
    identity = HostedIdentity(
        agent_id=PROBE_AGENT_ID, agent_name=PROBE_AGENT_NAME, agent_version="4", session_id="OFFLINE-session",
    )
    with TestClient(create_host(SimpleNamespace(identity=identity))):
        assert isinstance(get_task_manager(), REAL_TASK_MANAGER)
        assert events == ["startup"]
    assert events == ["startup", "shutdown"]
    with pytest.raises(TaskManagerNotInitialized):
        get_task_manager()


def test_write_uses_real_sdk_create_get_conditional_set_and_closes_clients(offline):
    result = invoke(offline.runtime)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["item_ttl_seconds"] == -1 and body["resilient_tasks_enabled"] is True
    assert body["created_etag"] == '"OFFLINE-created"'
    assert body["item"]["etag"] == '"OFFLINE-updated"'
    assert body["item"]["value"]["stage"] == "updated"
    assert body["cross_version_verified"] is False
    assert [call[0] for call in offline.storage.calls] == ["GET", "POST", "GET", "POST", "GET", "PUT", "GET"]
    assert offline.storage.calls[5][3]["If-Match"] == body["created_etag"]
    offline.managers[0].startup.assert_awaited_once()
    offline.managers[0].shutdown.assert_awaited_once()
    assert len(offline.credentials) == 1 and offline.credentials[0].closed
    offline.runtime.start.assert_not_called()


def test_new_version_only_reads_same_store_key_value_and_etag(offline):
    written = invoke(offline.runtime).json()
    before = copy.deepcopy(offline.storage.items)
    offline.storage.calls.clear()
    offline.runtime.identity = offline.runtime.identity.model_copy(update={"agent_version": "5"})
    result = invoke(offline.runtime, "read", baseline={
        "agent_version": written["identity"]["agent_version"], "etag": written["item"]["etag"],
    })
    assert result.status_code == 200, result.text
    assert result.json()["cross_version_verified"] is True
    assert result.json()["store"] == written["store"] and result.json()["key"] == written["key"]
    assert result.json()["item"] == written["item"]
    assert offline.storage.items == before
    assert [call[0] for call in offline.storage.calls] == ["GET", "GET"]


@pytest.mark.parametrize("existing_store", [False, True])
def test_read_never_creates_missing_store_or_item(offline, existing_store):
    if existing_store:
        invoke(offline.runtime)
        offline.storage.items.clear()
        offline.storage.calls.clear()
    result = invoke(offline.runtime, "read")
    assert result.status_code == 404
    assert all(call[0] == "GET" for call in offline.storage.calls)
    assert not offline.storage.items
    assert existing_store or offline.storage.descriptor is None


@pytest.mark.parametrize("phase", ["write", "read"])
def test_existing_store_ttl_is_read_back_and_never_repaired(offline, phase):
    invoke(offline.runtime)
    offline.storage.descriptor["item_ttl_seconds"] = 30 * 24 * 60 * 60
    before = copy.deepcopy((offline.storage.descriptor, offline.storage.items))
    offline.storage.calls.clear()
    result = invoke(offline.runtime, phase)
    assert result.status_code == 409 and result.json()["error"]["code"] == "N0_STATE_TTL_INVALID"
    assert all(call[0] == "GET" for call in offline.storage.calls)
    assert (offline.storage.descriptor, offline.storage.items) == before


def test_duplicate_write_and_stale_cas_do_not_overwrite_original_key(offline):
    offline.storage.cas_conflict = True
    first = invoke(offline.runtime)
    assert first.status_code == 412
    before = copy.deepcopy(offline.storage.items)
    duplicate = invoke(offline.runtime)
    assert duplicate.status_code == 409 and offline.storage.items == before
    recovered = invoke(offline.runtime, "read")
    assert recovered.status_code == 200
    assert recovered.json()["item"]["value"]["stage"] == "created"
    assert recovered.json()["cross_version_verified"] is False


def test_lost_update_response_stays_unknown_and_recovery_is_read_only(offline):
    offline.storage.lose_put_response = True
    failed = invoke(offline.runtime)
    assert failed.status_code == 503
    assert failed.json()["error"]["key"] == f"probe:{PROBE_ID}"
    assert "OFFLINE_PRIVATE_DETAILS" not in failed.text
    offline.storage.calls.clear()
    recovered = invoke(offline.runtime, "read")
    assert recovered.status_code == 200 and recovered.json()["item"]["value"]["stage"] == "updated"
    assert recovered.json()["cross_version_verified"] is False
    assert [call[0] for call in offline.storage.calls] == ["GET", "GET"]


@pytest.mark.parametrize("status", [401, 403])
def test_access_denied_stops_without_exposing_private_error_or_retrying(offline, status):
    offline.storage.status = status
    result = invoke(offline.runtime)
    assert result.status_code == status
    assert result.json()["error"]["code"] == "N0_STATE_ACCESS_DENIED"
    assert result.json()["error"]["upstream_status"] == status
    assert "OFFLINE_PRIVATE_DETAILS" not in result.text
    assert len(offline.storage.calls) == 1 and not offline.storage.items
    assert all(credential.closed for credential in offline.credentials)


@pytest.mark.parametrize("change", ["version", "etag", "value", "key"])
def test_cross_version_comparison_rejects_changed_baseline_or_item(offline, change):
    written = invoke(offline.runtime).json()
    baseline = {"agent_version": "4", "etag": written["item"]["etag"]}
    offline.runtime.identity = offline.runtime.identity.model_copy(update={"agent_version": "5"})
    if change == "version":
        baseline["agent_version"] = "5"
    elif change == "etag":
        baseline["etag"] = '"OFFLINE-wrong"'
    elif change == "value":
        offline.storage.items[f"probe:{PROBE_ID}"]["value"]["stage"] = "created"
    else:
        offline.storage.items[f"probe:{PROBE_ID}"]["key"] = "probe:another"
    offline.storage.calls.clear()
    result = invoke(offline.runtime, "read", baseline=baseline)
    assert result.status_code == 409
    assert all(call[0] == "GET" for call in offline.storage.calls)


@pytest.mark.parametrize("field,value", [
    ("agent_id", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"), ("agent_name", "OFFLINE-other-agent"),
])
def test_enabled_probe_refuses_any_other_agent_before_startup(offline, field, value):
    offline.runtime.identity = offline.runtime.identity.model_copy(update={field: value})
    with pytest.raises(Problem, match="approved test agent"):
        create_host(offline.runtime)
    assert not offline.managers and not offline.storage.calls and not offline.credentials


@pytest.mark.parametrize("name,value", [
    ("FOUNDRY_HOSTING_ENVIRONMENT", ""), ("FOUNDRY_PROJECT_ENDPOINT", "https://OFFLINE.invalid/api/projects/other"),
    ("AGENTSERVER_TASKS_BACKEND", "local"), ("FA_N0_STATE_PROBE", "unknown"),
])
def test_invalid_probe_environment_is_not_silently_enabled(offline, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(Problem):
        create_host(offline.runtime)
    assert not offline.managers and not offline.storage.calls and not offline.credentials


def test_disabled_probe_keeps_default_host_inactive_and_refuses_probe_requests(offline, monkeypatch):
    monkeypatch.delenv("FA_N0_STATE_PROBE")
    result = invoke(offline.runtime)
    assert result.status_code == 403 and result.json()["error"]["code"] == "N0_PROBE_DISABLED"
    assert not resilient_tasks_enabled() and not offline.managers and not offline.storage.calls
    offline.runtime.start.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True])
def test_normal_invocation_uses_original_runtime_even_when_probe_is_available(offline, monkeypatch, enabled):
    if not enabled:
        monkeypatch.delenv("FA_N0_STATE_PROBE")
    headers = {key: value for key, value in HEADERS.items() if key != PROBE_HEADER}
    headers[NATIVE_LAUNCH_PERMIT_HEADER] = "a" * 64
    with TestClient(create_host(offline.runtime)) as client:
        result = client.post("/invocations", headers=headers, json={"binding_id": "original", "launch_id": "launch"})
    assert result.status_code == 202 and result.json() == {"status": "accepted"}
    offline.runtime.start.assert_called_once()
    assert not offline.storage.calls and not offline.credentials


@pytest.mark.parametrize("body", [
    '{"phase":"write","probe_id":"' + PROBE_ID + '","value":"untrusted"}',
    '{"phase":"write","phase":"read","probe_id":"' + PROBE_ID + '"}',
    '{"phase":"write","probe_id":"arbitrary/key"}',
    '{"phase":"write","probe_id":"' + PROBE_ID + '","baseline":{"agent_version":"4","etag":"x"}}',
    "x" * 4097,
], ids=["extra-field", "duplicate-field", "invalid-key", "write-baseline", "oversized"])
def test_invalid_or_oversized_body_never_reaches_storage(offline, body):
    with TestClient(create_host(offline.runtime)) as client:
        result = client.post("/invocations", headers=HEADERS, content=body)
    assert result.status_code == (413 if len(body) > 4096 else 422)
    assert not offline.storage.calls and not offline.credentials


def test_sdk_startup_failure_cleans_manager_and_fails_host_lifespan(offline):
    offline.settings.startup_error = RuntimeError("OFFLINE startup failed")
    with pytest.raises(RuntimeError, match="OFFLINE startup failed"):
        with TestClient(create_host(offline.runtime)):
            pytest.fail("host must not start without its requested manager")
    offline.managers[0].startup.assert_awaited_once()
    offline.managers[0].shutdown.assert_awaited_once()
    with pytest.raises(TaskManagerNotInitialized):
        get_task_manager()
    assert not offline.storage.calls
