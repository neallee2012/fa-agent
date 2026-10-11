"""Explicit API-MI bootstrap; normal turn traffic never creates or changes a session."""

from contextlib import contextmanager
import hashlib
import json
import logging
import re
from threading import Lock, RLock
from time import monotonic

from azure.core.exceptions import AzureError, ResourceNotFoundError

from .behavior_foundry import PLATFORM_READ_RETRY, FoundryTarget
from .behavior_native_turn_wire import (
    NATIVE_STATE_PROFILE, NativeEnvironmentMetadata, NativeEnvironmentReplacementPreview, NativeEnvironmentView, NativeSessionMetadata,
    NativeSessionFollow, NativeSessionRead, NativeSessionPrepare, NativeSessionRetarget, NativeSessionFilesOmission,
    NativeEnvironmentCommand, NativeEnvironmentRetargetCommand, material_digest,
)
from .errors import Problem


class _RetiredSession(Problem):
    """A known terminal platform session is not a candidate for new execution."""


# Foundry deletes a session only after 30 days without activity (Learn: manage-hosted-sessions); a cached binding
# unused for a day is read again, so a kept ID never invokes an expired session.
READER_IDLE_SECONDS = 24 * 3600
# Refusals meaning the cached binding moved: the platform is read once more (reads are idempotent).
_MOVED = frozenset({"NATIVE_SESSION_CHANGED", "AGENT_VERSION_FOLLOWING", "FOUNDRY_NATIVE_BINDING_MISMATCH"})


def _scope(target):
    return target.executor_id, target.project_endpoint, target.agent_id, target.agent_name, target.agent_oid


def native_session_id(target, client_id, *, version=None):
    if not isinstance(client_id, str) or not client_id:
        raise Problem(503, "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE", "the original API managed identity is required")
    material = ["fa-native-api-session-v1", target.project_endpoint, target.agent_id, client_id]
    if version is not None:
        if not isinstance(version, str) or re.fullmatch(r"[1-9][0-9]{0,15}", version) is None:
            raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "one concrete version is required")
        material = ["fa-native-version-session-v1", *material[1:], version]
    return "fa-native-" + hashlib.sha256(json.dumps(material, separators=(",", ":")).encode()).hexdigest()[:48]


def _target(original, **changes):
    return FoundryTarget.model_validate_json(original.model_copy(update=changes).model_dump_json())


def _session_metadata(client):
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import VersionRefIndicator

    try:
        with AIProjectClient(endpoint=client.target.project_endpoint, credential=client.credential,
                             **PLATFORM_READ_RETRY,
                             connection_timeout=5, read_timeout=10) as project:
            session = project.agents.get_session(client.target.agent_name, client.target.session_id)
    except ResourceNotFoundError:
        return None
    except AzureError as error:
        status = getattr(error, "status_code", None)
        logging.getLogger(__name__).warning("NATIVE_BOOTSTRAP_PLATFORM_READ status=%s",
                                           status if type(status) is int else None)
        denied = status in {401, 403}
        raise Problem(status if denied else 503,
                      "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE" if denied else "NATIVE_BOOTSTRAP_UNKNOWN",
                      "the original platform session could not be read") from None
    indicator = session.version_indicator
    if (session.agent_session_id != client.target.session_id or not isinstance(indicator, VersionRefIndicator)
            or not isinstance(indicator.agent_version, str)
            or re.fullmatch(r"[1-9][0-9]{0,15}", indicator.agent_version) is None):
        raise Problem(409, "NATIVE_SESSION_CHANGED", "the platform session identity or concrete version differs")
    return session


def _read_platform_session(client):
    session = _session_metadata(client)
    if session is None:
        return None
    if session.status in {"failed", "deleted", "expired"}:
        raise _RetiredSession(503, "NATIVE_BOOTSTRAP_UNKNOWN",
                              "the original session is retired; no replacement was created")
    if session.status not in {"active", "idle"}:
        raise Problem(503, "NATIVE_BOOTSTRAP_UNKNOWN", "the original session is not ready; no replacement was created")
    return _target(client.target, agent_version=session.version_indicator.agent_version)


def _source_session_status(client, phase):
    session = _session_metadata(client)
    if session is None or not isinstance(session.status, str) or session.status not in {
            "active", "idle", "creating", "updating", "deleting", "failed", "deleted", "expired"}:
        raise Problem(503, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the original source session status is unconfirmed")
    observed_version = session.version_indicator.agent_version
    if observed_version != client.target.agent_version:
        logging.getLogger(__name__).warning(
            "NATIVE_ENVIRONMENT_SOURCE_VERSION_CHANGED phase=%s session_id=%s recorded_version=%s observed_version=%s",
            phase, client.target.session_id, client.target.agent_version, observed_version)
    return session.status


def _require_native_version(client):
    from azure.ai.projects import AIProjectClient
    try:
        with AIProjectClient(endpoint=client.target.project_endpoint, credential=client.credential,
                             **PLATFORM_READ_RETRY,
                             connection_timeout=5, read_timeout=10) as project:
            version = project.agents.get_version(client.target.agent_name, client.target.agent_version)
    except AzureError as error:
        status = getattr(error, "status_code", None)
        raise Problem(status if status in {401, 403} else 503, "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE",
                      "the selected Hosted version could not be read") from None
    definition = version.definition
    environment = definition.get("environment_variables") if definition is not None else None
    if (version.name != client.target.agent_name or version.version != client.target.agent_version
            or definition is None or definition.get("kind") != "hosted"
            or not isinstance(environment, dict) or environment.get("FA_NATIVE_TURNS") != NATIVE_STATE_PROFILE):
        raise Problem(409, "NATIVE_TURNS_DISABLED", "the selected build version must explicitly enable native turns")


def _latest_version_reader(client, client_id):
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.models import VersionRefIndicator

    try:
        with AIProjectClient(endpoint=client.target.project_endpoint, credential=client.credential,
                             **PLATFORM_READ_RETRY,
                             connection_timeout=5, read_timeout=10) as project:
            for index, session in enumerate(project.agents.list_sessions(client.target.agent_name, limit=100, order="desc")):
                if index >= 100:
                    raise Problem(503, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the native locator is outside the bounded session page")
                indicator = session.version_indicator
                if session.status not in {"active", "idle"} or not isinstance(indicator, VersionRefIndicator):
                    continue
                version = indicator.agent_version
                if not isinstance(version, str) or re.fullmatch(r"[1-9][0-9]{0,15}", version) is None:
                    continue
                if session.agent_session_id == native_session_id(client.target, client_id, version=version):
                    return _target(client.target, session_id=session.agent_session_id, agent_version=version)
    except AzureError as error:
        status = getattr(error, "status_code", None)
        raise Problem(status if status in {401, 403} else 503, "NATIVE_ENVIRONMENT_UNAVAILABLE",
                      "the original native environment locator could not be read") from None
    return None


def _metadata_record(session):
    from azure.ai.projects.models import VersionRefIndicator

    try:
        if not isinstance(session.version_indicator, VersionRefIndicator):
            raise ValueError("a concrete observed version is required")
        return NativeSessionMetadata(
            hosted_session_id=session.agent_session_id,
            hosted_agent_version=session.version_indicator.agent_version, status=session.status)
    except (AttributeError, TypeError, ValueError):
        raise Problem(502, "NATIVE_STATE_INVALID", "the platform session metadata is invalid") from None


def _metadata_sessions(client):
    from azure.ai.projects import AIProjectClient

    observed = {}
    truncated = False
    try:
        with AIProjectClient(endpoint=client.target.project_endpoint, credential=client.credential,
                             **PLATFORM_READ_RETRY,
                             connection_timeout=5, read_timeout=10) as project:
            for index, session in enumerate(project.agents.list_sessions(
                    client.target.agent_name, limit=100, order="desc")):
                if index == 100:
                    truncated = True
                    break
                if not isinstance(session.agent_session_id, str):
                    raise Problem(502, "NATIVE_STATE_INVALID", "a platform session has no valid identity")
                if not session.agent_session_id.startswith("fa-native-"):
                    continue
                item = _metadata_record(session)
                if item.hosted_session_id in observed and observed[item.hosted_session_id] != item:
                    raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the platform session listing changed")
                observed[item.hosted_session_id] = item
    except AzureError as error:
        status = getattr(error, "status_code", None)
        logging.getLogger(__name__).warning("NATIVE_ENVIRONMENT_METADATA_READ status=%s",
                                           status if type(status) is int else None)
        raise Problem(status if status in {401, 403} else 503, "NATIVE_ENVIRONMENT_UNAVAILABLE",
                      "the platform session metadata could not be read") from None
    return observed, truncated


class NativeSessions:
    def __init__(self, composition):
        self.composition = composition
        self.lock = RLock()
        self.prepared = None
        self.binding = None
        self.environment = None
        self.environment_creates = set()
        self.following_candidate = None
        self.following = None
        self.desired_version = None
        self.admission_request_id = None
        self._reader = None
        self._reader_generation = 0
        self._reader_lock = Lock()

    def forget(self):
        """The next read selects the version and session from the platform again."""
        with self._reader_lock:
            self._reader_generation += 1
            self._reader = None

    def reader_cached(self):
        return self._reader is not None

    def cached_reader(self, client):
        """The kept reader for ``client``'s Agent without any platform read; None when none is kept."""
        with self._reader_lock:
            cached = self._reader
            if (cached is not None and cached[0] == _scope(client.target)
                    and monotonic() - cached[2] < READER_IDLE_SECONDS):
                return client.for_target(cached[1])
        return None

    @staticmethod
    def binding_moved(error):
        """True when the refusal says the Agent's binding moved (reads nothing and forgets nothing)."""
        detail = error.body["error"]
        return detail["code"] in _MOVED or (
            detail["code"] == "FOUNDRY_INVOCATION_UNKNOWN" and detail.get("upstream_status") == 404)

    def moved(self, error):
        """Forget the cached reader after a refusal; True when the refusal says the Agent's binding moved."""
        moved = self.binding_moved(error)
        if moved or error.body["error"]["code"].startswith("FOUNDRY_"):
            self.forget()
        return moved

    @contextmanager
    def _changing(self):
        """One environment operation; reads meanwhile and afterwards select the binding from the platform again."""
        with self.lock:
            # A cached reader may be the source this operation stops; a read must not resume it.
            self.forget()
            try:
                yield
            finally:
                self.forget()

    def candidate(self, client, *, version=None):
        client_id = self.composition.config.identity.managed_identity_client_id
        changes = {"session_id": native_session_id(client.target, client_id, version=version)}
        if version is not None:
            changes["agent_version"] = version
        return client.for_target(_target(client.target, **changes))

    def selected_version(self, client):
        from .behavior_agent_versions import current_hosted_version
        self.desired_version = current_hosted_version(self.composition.runtime, client.target)
        return self.desired_version

    def _scoped(self, client, target):
        fields = ("project_endpoint", "agent_id", "agent_name", "agent_oid", "executor_id")
        client_id = self.composition.config.identity.managed_identity_client_id
        if (any(getattr(client.target, name) != getattr(target, name) for name in fields)
                or target.session_id not in {
                    native_session_id(client.target, client_id),
                    native_session_id(client.target, client_id, version=target.agent_version),
                }):
            raise Problem(409, "NATIVE_SESSION_CHANGED", "the native locator is outside the original server scope")
        return client.for_target(target)

    @staticmethod
    def _state(client, *, environment_request_id=None):
        from .behavior_native_turn_api import NativeTurnGateway
        value, _ = NativeTurnGateway._invoke(client, NativeSessionRead(environment_request_id=environment_request_id))
        return value

    def _remember(self, client, state):
        value = state.preparation
        if (value is None or state.hosted_session_id != client.target.session_id
                or value.agent_id != client.target.agent_id
                or value.hosted_session_id != client.target.session_id
                or value.hosted_agent_version != client.target.agent_version):
            raise Problem(409, "NATIVE_SESSION_CHANGED", "explicit preparation of the original native session is required")
        self.prepared = value
        self.binding = client.target
        return value

    def _remember_state(self, client, state):
        if state.agent_id != client.target.agent_id or state.preparation is None:
            raise Problem(409, "NATIVE_SESSION_CHANGED", "the original prepared Agent binding is unavailable")
        value = state.environment
        if value is None:
            sid, version = state.preparation.hosted_session_id, state.preparation.hosted_agent_version
        elif value.state == "ready":
            sid, version = value.hosted_session_id, value.hosted_agent_version
        else:
            sid, version = value.previous_hosted_session_id, value.previous_agent_version
        if sid != state.hosted_session_id:
            raise Problem(409, "NATIVE_SESSION_CHANGED", "the published Agent environment differs")
        self.prepared, self.environment, self.following = state.preparation, value, state.following
        self.admission_request_id = state.admission_request_id
        self.binding = _target(client.target, session_id=sid, agent_version=version)
        return self._scoped(client, self.binding)

    def current(self, client, *, refresh=False):
        with self.lock:
            return self._current(client)

    def _current(self, client):
        retry = self.reader_cached()
        while True:
            reader = self.reader(client)
            try:
                current = self._remember_state(reader, self._state(reader))
            except Problem as error:
                if self.moved(error) and retry:
                    retry = False
                    continue
                raise
            if (current.target.session_id, current.target.agent_version) == (
                    reader.target.session_id, reader.target.agent_version):
                self.following_candidate = None
                return current
            if retry:
                # A cached reader that is no longer the Agent's environment: read the platform once more.
                self.forget()
                retry = False
                continue
            self.following_candidate = reader.target
            raise Problem(409, "NATIVE_SESSION_CHANGED",
                          "the published version has not become the Agent's current environment")

    def admission(self, client, *, request_id=None, resolved=False):
        """``resolved=True``: this request just got ``client`` from ``current()``; it is not read again."""
        with self.lock:
            current = client if resolved else self._current(client)
            if request_id is not None and self.admission_request_id == request_id:
                return current
            if (self.environment is not None and self.environment.state == "pending"
                    or self.following is not None and self.following.state == "pending"
                    or self.following_candidate is not None
                    and current.target.session_id != self.following_candidate.session_id):
                raise Problem(503, "AGENT_VERSION_FOLLOWING",
                              "the entire Agent is changing environments; this new request was not accepted")
            return current

    def reader(self, client):
        """The selected version's existing session, kept until an environment change or a refusal says otherwise.

        A session is bound to one version when it is created and the endpoint selector never moves it (Learn:
        manage-hosted-sessions, configure-agent); like charter-agent's saved ``agent_session_id``, a known binding
        is invoked directly. Hosted's expected-version and replaced-session preconditions refuse a moved one."""
        scope = _scope(client.target)
        with self._reader_lock:
            cached, generation = self._reader, self._reader_generation
            if cached is not None and cached[0] == scope and monotonic() - cached[2] < READER_IDLE_SECONDS:
                self._reader = (scope, cached[1], monotonic())
                return client.for_target(cached[1])
        selected = self._platform_reader(client)
        with self._reader_lock:
            if self._reader_generation == generation:
                self._reader = (scope, selected.target, monotonic())
        return selected

    def _platform_reader(self, client):
        version = self.selected_version(client)
        selected = self.candidate(client, version=version)
        actual = _read_platform_session(selected)
        if actual is not None:
            if (actual.session_id, actual.agent_version) != (selected.target.session_id, version):
                raise Problem(409, "NATIVE_SESSION_CHANGED",
                              "the current reader's platform version differs; this read invokes no old session")
            return selected.for_target(actual)
        raise Problem(409, "NATIVE_SESSION_CHANGED",
                      "the current version needs a reader; this read creates nothing and invokes no old session")

    def environment_view(self, client):
        version = self.selected_version(client)
        destination = self.candidate(client, version=version)
        observed, truncated = _metadata_sessions(client)
        actual = _session_metadata(destination)
        observed.pop(destination.target.session_id, None)
        item = None if actual is None else _metadata_record(actual)
        if item is not None:
            observed[item.hosted_session_id] = item
        return NativeEnvironmentMetadata(
            agent_id=client.target.agent_id, hosted_session_id=destination.target.session_id,
            hosted_agent_version=version, observed_sessions=tuple(observed.values()), truncated=truncated,
            state="unverified" if item is not None and item.hosted_agent_version == version
            and item.status in {"active", "idle"} else "needs_follow",
        )

    def _confirm_environment(self, client, value):
        state = self._state(client)
        if state.environment != value or state.hosted_session_id != value.hosted_session_id:
            raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original environment result did not read back")
        self._remember_state(client, state)
        return value

    def observe_environment(self, client, command):
        reader = self.candidate(client, version=command.expected_agent_version)
        actual = _read_platform_session(reader)
        if actual is None:
            raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original destination reader is unavailable; nothing was created")
        if actual.agent_version != command.expected_agent_version:
            raise Problem(409, "NATIVE_SESSION_CHANGED",
                          "the original destination version changed; this reconciliation invokes no original session")
        reader = reader.for_target(actual)
        state = self._state(reader)
        if (state.environment is not None and state.environment.state == "pending"
                and state.environment.hosted_session_id != reader.target.session_id):
            state = self._state(reader, environment_request_id=command.request_id)
        target = self.candidate(client, version=command.expected_agent_version).target
        values = ((state.environment, state.following) if state.environment is not None
                  and state.environment.state == "ready" else (state.following, state.environment))
        for value in values:
            if (value is not None and value.request_id == command.request_id
                    and value.hosted_session_id == target.session_id
                    and value.hosted_agent_version == command.expected_agent_version
                    and (command.expected_session_id is None
                         or value.previous_hosted_session_id == command.expected_session_id)):
                if (value.state == "pending" and value.files_manifest_sha256 is not None
                        and value.reason in {"files_pending", "source_stop_required"}):
                    from .behavior_session_files import FollowFiles
                    files = FollowFiles(reader, value, lambda: None).observe()
                    state = self._state(reader)
                    observed = state.following or state.environment
                    if observed is None or any(getattr(observed, name) != getattr(value, name) for name in (
                            "agent_id", "request_id", "previous_hosted_session_id", "hosted_session_id",
                            "hosted_agent_version")):
                        raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original file operation changed during readback")
                    if (observed.state == "pending" and files is not None and files.sending is None
                            and (not files.publishing or files.published)):
                        source = client.for_target(_target(
                            client.target, session_id=value.previous_hosted_session_id,
                            agent_version=value.previous_agent_version))
                        metadata = _session_metadata(source)
                        if (metadata is not None
                                and metadata.version_indicator.agent_version != value.previous_agent_version):
                            return observed.model_copy(update={"reason": "source_version_changed"})
                        return observed.model_copy(update={"reason": "files_pending"})
                    return observed
                return value
        current = self._current_environment(reader, state, command)
        if current is not None:
            return current
        raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN",
                      "the original environment operation has no confirmed observation; do not resubmit it")

    @staticmethod
    def _current_environment(client, state, command):
        if (command.expected_session_id is None and state.environment is None and state.following is None
                and state.preparation is not None and state.hosted_session_id == client.target.session_id
                and client.target.agent_version == command.expected_agent_version
                and state.preparation.hosted_agent_version == command.expected_agent_version):
            return NativeEnvironmentView(
                agent_id=client.target.agent_id, request_id=command.request_id,
                previous_hosted_session_id=state.hosted_session_id,
                previous_agent_version=command.expected_agent_version,
                hosted_session_id=state.hosted_session_id, hosted_agent_version=command.expected_agent_version,
                state="current")
        return None

    def _destination(self, client, expected_version, before_send):
        version = self.selected_version(client)
        if version != expected_version:
            raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the server-selected version changed")
        destination = self.candidate(client, version=version)
        actual = _read_platform_session(destination)
        if actual is None:
            _require_native_version(destination)
            if destination.target.session_id in self.environment_creates:
                raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original version session create is unconfirmed")

            def creating():
                before_send()
                self.environment_creates.add(destination.target.session_id)

            try:
                destination.create_session(before_send=creating)
            except Problem as error:
                if error.body["error"].get("upstream_status") in {401, 403}:
                    raise Problem(error.body["error"]["upstream_status"], "NATIVE_BOOTSTRAP_SOURCE_UNAVAILABLE",
                                  "the original API identity cannot create this session") from None
                if error.body["error"]["code"] not in {"FOUNDRY_SESSION_UNKNOWN", "FOUNDRY_SESSION_UNAVAILABLE"}:
                    raise
                actual = _read_platform_session(destination)
                if actual is None:
                    raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "read the same version session after creation") from None
            if actual is None:
                actual = _read_platform_session(destination)
        if actual is None:
            raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original version session create has no confirmed readback")
        if actual.agent_version != version:
            raise Problem(409, "NATIVE_SESSION_CHANGED", "the original version session did not read back")
        self.following_candidate = destination.target
        return destination

    def retarget(self, client, command, *, before_send):
        from .behavior_native_turn_api import NativeTurnGateway

        with self._changing():
            destination = self._destination(client, command.expected_agent_version, before_send)
            state = self._state(destination)
            before_send()
            value, _ = NativeTurnGateway._invoke(destination, NativeSessionRetarget(
                request_id=command.request_id, expected_session_id=command.expected_session_id,
                expected_etag=state.etag, expected_agent_version=command.expected_agent_version,
                pending_request_id=command.pending_request_id,
                pending_session_id=command.pending_session_id, idle_only=command.idle_only))
            if value.state == "ready":
                return self._confirm_environment(destination, value)
            if value.reason != "source_stop_required":
                return value
            return self.follow(client, command, before_send=before_send)

    def follow(self, client, command, *, before_send):
        from .behavior_native_turn_api import NativeTurnGateway
        from .behavior_session_files import FollowFiles
        from .native_files_wire import FILES_PROFILE

        with self._changing():
            destination = self._destination(client, command.expected_agent_version, before_send)
            state = self._state(destination)
            existing = state.environment
            if (existing is not None and existing.hosted_session_id == destination.target.session_id
                    and existing.state == "ready" and (command.expected_session_id is None
                    or existing.previous_hosted_session_id == command.expected_session_id)):
                self._remember_state(destination, state)
                return existing
            current = self._current_environment(destination, state, command)
            if current is not None:
                return current
            omitted = state.following
            confirmed_omission = (omitted is not None and omitted.request_id == command.request_id
                                  and omitted.hosted_session_id == destination.target.session_id
                                  and omitted.files_migrated is False and omitted.files_skip_request_id is not None)
            if state.files_profile != FILES_PROFILE and not confirmed_omission:
                raise Problem(409, "NATIVE_FILES_UNSUPPORTED", "the selected environment cannot safely migrate workspace files")
            before_send()
            internal = NativeSessionFollow(
                request_id=command.request_id,
                expected_session_id=command.expected_session_id or state.hosted_session_id,
                expected_etag=state.etag, stage="prepare", idle_only=command.idle_only, files_required=True)
            try:
                value, _ = NativeTurnGateway._invoke(destination, internal)
            except Problem as error:
                if (not command.idle_only or error.status != 409
                        or error.body["error"]["code"] != "NATIVE_ENVIRONMENT_CONFLICT"
                        or error.body["error"].get("files_check") is not None or state.following is not None
                        or state.environment is not None and state.environment.state != "ready"):
                    raise
                observed = self._state(destination)
                # Admission may mark and clear the Agent ETag after the read; no environment intent may change.
                fields = {"etag", "admission_request_id"}
                if (observed.etag == state.etag or observed.hosted_session_id != internal.expected_session_id
                        or observed.model_dump(exclude=fields) != state.model_dump(exclude=fields)
                        or self.selected_version(client) != command.expected_agent_version):
                    raise
                before_send()
                internal = internal.model_copy(update={"expected_etag": observed.etag})
                value, _ = NativeTurnGateway._invoke(destination, internal)
            if value.state == "ready":
                return self._confirm_environment(destination, value)
            if value.reason == "files_pending":
                FollowFiles(destination, value, before_send).copy_step()
                before_send()
                value, _ = NativeTurnGateway._invoke(destination, internal)
            if value.reason != "source_stop_required":
                return value
            skip_files = value.files_migrated is False and value.files_skip_request_id is not None
            if value.files_manifest_sha256 is None and not skip_files:
                raise Problem(409, "NATIVE_FILES_UNSUPPORTED", "the original follow has no verified workspace manifest")
            files = None if skip_files else FollowFiles(destination, value, before_send)
            if files is not None:
                files.require_source_version()
                files.before_stop()
            source = client.for_target(_target(
                client.target, session_id=value.previous_hosted_session_id, agent_version=value.previous_agent_version))
            before_send()
            phase = "source_status"
            try:
                status = _source_session_status(source, phase)
                if status not in {"idle", "failed", "deleted", "expired"}:
                    phase = "stop_session"
                    source.stop_session()
                phase = "source_readback"
                status = _source_session_status(source, phase)
            except Problem as error:
                upstream = error.body["error"].get("upstream_status")
                logging.getLogger(__name__).warning(
                    "NATIVE_ENVIRONMENT_PENDING stage=%s status=%s code=%s upstream_status=%s",
                    phase, error.status, error.body["error"]["code"], upstream if type(upstream) is int else None)
                return value.model_copy(update={"reason": "source_unavailable"})
            if status not in {"idle", "failed", "deleted", "expired"}:
                return value.model_copy(update={"reason": "source_active"})
            if files is not None and not files.stopped_step():
                return value.model_copy(update={"reason": "files_pending"})
            before_send()
            value, _ = NativeTurnGateway._invoke(destination, internal.model_copy(update={"stage": "commit"}))
            if value.state == "ready":
                return self._confirm_environment(destination, value)
            return value

    def _automatic_decision(self, client, command, destination, state):
        from .behavior_native_turn_wire import NativeEnvironmentWithoutFiles, automatic_omission_id

        original = state.following or state.environment
        if original is not None and original.state == "pending":
            follow = NativeEnvironmentCommand(
                request_id=original.request_id, expected_session_id=original.previous_hosted_session_id,
                expected_agent_version=original.hosted_agent_version,
                idle_only=original.operation_idle_only if original.operation_idle_only is not None else command.idle_only)
        else:
            follow = NativeEnvironmentCommand(
                request_id=command.request_id, expected_session_id=state.hosted_session_id,
                expected_agent_version=command.expected_agent_version, idle_only=command.idle_only)
        if command.expected_session_id is not None and command.expected_session_id != follow.expected_session_id:
            raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the requested original source changed")
        existing = (original.files_confirmation_saved if original is not None
                    and original.hosted_session_id == destination.target.session_id else None)
        return NativeEnvironmentWithoutFiles(
            request_id=existing or automatic_omission_id(
                client.target.agent_id, follow.expected_session_id, destination.target.session_id, command.expected_agent_version),
            follow=follow, expected_agent_version=command.expected_agent_version,
            confirmation="do-not-transfer-workspace-files")

    def advance(self, client, command, *, before_send):
        from .native_files_wire import FILES_ERRORS

        with self._changing():
            try:
                return self.follow(client, command, before_send=before_send)
            except Problem as error:
                code = error.body["error"]["code"]
                if code not in FILES_ERRORS | {"NATIVE_SESSION_CHANGED", "NATIVE_ENVIRONMENT_CONFLICT"}:
                    raise
                failure = error
            destination = self._destination(client, command.expected_agent_version, before_send)
            state = self._state(destination)
            if state.following is not None and state.following.files_reconcile_required:
                raise failure
            pending = state.environment
            if (pending is not None and pending.state == "pending"
                    and pending.hosted_session_id != destination.target.session_id
                    and int(pending.hosted_agent_version) < int(command.expected_agent_version)
                    and command.request_id != pending.request_id):
                source = _session_metadata(client.for_target(_target(
                    client.target, session_id=pending.previous_hosted_session_id,
                    agent_version=pending.previous_agent_version)))
                if (source is not None and source.status in {"active", "idle"} and not pending.source_invocation_refused
                        and source.version_indicator.agent_version == pending.previous_agent_version):
                    retarget = NativeEnvironmentRetargetCommand(
                        request_id=command.request_id, expected_agent_version=command.expected_agent_version,
                        expected_session_id=command.expected_session_id or pending.previous_hosted_session_id,
                        pending_request_id=pending.request_id, pending_session_id=pending.hosted_session_id,
                        idle_only=command.idle_only)
                    try:
                        return self.retarget(client, retarget, before_send=before_send)
                    except Problem as error:
                        code = error.body["error"]["code"]
                        if code not in FILES_ERRORS or code == "NATIVE_FILES_UNKNOWN":
                            raise
                        failure, state = error, self._state(destination)
            decision = self._automatic_decision(client, command, destination, state)
            try:
                return self.without_files(client, decision, before_send=before_send,
                                          refusal_code=code if code in FILES_ERRORS else None, system_policy=True)
            except Problem as error:
                category = error.body["error"].get("files_check", {}).get("category")
                waiting = {"active_waiting": "accepted_or_started", "admission_pending": "admission_pending",
                           "execution_active": "accepted_or_started", "task_active": "task_active",
                           "task_unconfirmed": "task_unconfirmed"}
                original = state.following or state.environment
                if category in waiting and original is not None:
                    return original.model_copy(update={"reason": waiting[category]})
                raise

    def observe_automatic(self, client, command, *, before_send):
        with self._changing():
            try:
                value = self.observe_environment(client, command)
                if value.files_reconcile_required:
                    raise Problem(503, "NATIVE_FILES_UNKNOWN", "the original file outcome is still unconfirmed")
                if value.state in {"ready", "current"} or value.files_migrated is False or value.progress_token is not None:
                    return self._observe_source_progress(client, value)
            except Problem as error:
                if error.body["error"]["code"] not in {"NATIVE_ENVIRONMENT_UNKNOWN", "NATIVE_SESSION_CHANGED"}:
                    raise
            destination = self.candidate(client, version=command.expected_agent_version)
            actual = _read_platform_session(destination)
            if actual is None or actual.agent_version != command.expected_agent_version:
                raise Problem(409, "NATIVE_SESSION_CHANGED", "the original automatic reader is unavailable; nothing was created")
            state = self._state(destination)
            decision = self._automatic_decision(client, command, destination, state)
            value = self.without_files(client, decision, before_send=before_send, reconcile=True, observe_only=True,
                                       system_policy=True)
            return self._observe_source_progress(client, value)

    @staticmethod
    def _observe_source_progress(client, value):
        if value.state != "pending":
            return value
        source = client.for_target(_target(client.target, session_id=value.previous_hosted_session_id,
                                           agent_version=value.previous_agent_version))
        metadata = _session_metadata(source)
        state = None if metadata is None else {
            "version": metadata.version_indicator.agent_version, "status": metadata.status}
        return value.model_copy(update={"progress_token": material_digest({
            "original": value.progress_token or material_digest(value.model_dump(mode="json")), "source": state})})

    def without_files(self, client, command, *, before_send, reconcile=False, preview=False, resume=False,
                      observe_only=False, refusal_code=None, system_policy=False):
        from .behavior_native_turn_api import NativeTurnGateway, _OmissionBeforeDispatch
        from .native_files_wire import FILES_PROFILE

        with self._changing():
            follow = command.follow
            target_version = command.expected_agent_version or follow.expected_agent_version
            if (preview or resume) and self.selected_version(client) != target_version:
                raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the displayed destination version changed")
            original_destination_changed = False
            if not reconcile and target_version != follow.expected_agent_version:
                original = self.candidate(client, version=follow.expected_agent_version)
                metadata = _session_metadata(original)
                original_destination_changed = (metadata is not None
                    and metadata.version_indicator.agent_version != follow.expected_agent_version)
                if preview and metadata is not None and (
                        _read_platform_session(self.candidate(client, version=target_version)) is None):
                    return NativeEnvironmentReplacementPreview(
                        agent_id=client.target.agent_id,
                        hosted_session_id=self.candidate(client, version=target_version).target.session_id,
                        hosted_agent_version=target_version, original_request_id=follow.request_id,
                        original_hosted_session_id=original.target.session_id,
                        original_hosted_agent_version=follow.expected_agent_version,
                        observed_agent_version=metadata.version_indicator.agent_version,
                        reason="destination_version_changed" if original_destination_changed else "reader_required")
            if reconcile or preview or resume:
                destination = self.candidate(client, version=target_version)
                actual = _read_platform_session(destination)
                if actual is None or actual.agent_version != target_version:
                    raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "read the original omission destination; nothing was created")
                destination = destination.for_target(actual)
            else:
                destination = self._destination(client, target_version, before_send)
            state = self._state(destination)
            if state.files_omission_profile != "fa-files-omission-v1":
                raise Problem(409, "NATIVE_FILES_CONFIRMATION_UNSUPPORTED",
                              "this destination image predates explicit file omission; use a compatible image")
            changed_target = target_version != follow.expected_agent_version
            original_target_sid = self.candidate(client, version=follow.expected_agent_version).target.session_id
            fresh_request_id = material_digest({
                "kind": "files-omission-follow", "confirmation": command.request_id,
                "original": follow.request_id, "source": original_target_sid, "target": destination.target.session_id})
            original_ready = (changed_target and state.environment is not None and state.environment.state == "ready"
                and state.environment.request_id == follow.request_id
                and state.environment.hosted_session_id == original_target_sid
                and state.environment.hosted_agent_version == follow.expected_agent_version)
            fresh_follow = changed_target and (original_ready or any(
                value is not None and value.request_id == fresh_request_id
                and value.previous_hosted_session_id == original_target_sid
                and value.hosted_session_id == destination.target.session_id
                and value.hosted_agent_version == target_version
                for value in (state.environment, state.following)))
            if original_ready and follow.expected_session_id is not None and (
                    follow.expected_session_id != state.environment.previous_hosted_session_id):
                raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the referenced ready operation has another source")
            source_sid = follow.expected_session_id or state.hosted_session_id
            retained = state.following or state.environment
            if fresh_follow:
                source_sid = original_target_sid
            elif retained is not None and (
                    retained.request_id == follow.request_id or retained.files_skip_request_id == command.request_id):
                if follow.expected_session_id is not None and follow.expected_session_id != retained.previous_hosted_session_id:
                    raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the displayed source differs from the original file operation")
                source_sid = retained.previous_hosted_session_id
            pending_request_id = pending_session_id = None
            if changed_target:
                if not fresh_follow:
                    pending_request_id = follow.request_id
                    pending_session_id = original_target_sid
                follow = NativeEnvironmentCommand(
                    request_id=fresh_request_id if fresh_follow else material_digest({
                        "kind": "files-omission-retarget", "confirmation": command.request_id, "original": follow.request_id,
                        "source": source_sid, "target": destination.target.session_id}),
                    expected_session_id=source_sid, expected_agent_version=target_version, idle_only=True)
                if state.environment is not None and state.environment.request_id == follow.request_id:
                    source_sid = state.environment.previous_hosted_session_id
            if source_sid == destination.target.session_id:
                raise Problem(409, "NATIVE_FILES_CONFIRMATION_REQUIRED", "there is no original environment transition to confirm")
            already_ready = (retained is not None and retained.state == "ready" and retained.request_id == follow.request_id
                             and retained.files_skip_request_id == command.request_id)
            missing = None
            valid_reasons = set()
            if not reconcile and not already_ready:
                source = client.for_target(_target(client.target, session_id=source_sid))
                actual = _session_metadata(source)
                original = state.environment or state.preparation
                pending = state.environment is not None and state.environment.state == "pending"
                profile = (original.previous_files_profile if pending else original.files_profile) if original is not None else None
                version = (original.previous_agent_version if pending else original.hosted_agent_version) if original is not None else None
                refused = (state.source_invocation_refused and state.hosted_session_id == source_sid or
                           retained is not None and retained.previous_hosted_session_id == source_sid
                           and retained.source_invocation_refused)
                if actual is not None and version is not None and actual.version_indicator.agent_version != version:
                    valid_reasons.add("source_changed")
                if original_destination_changed and not fresh_follow:
                    valid_reasons.add("original_destination")
                if state.files_profile != FILES_PROFILE:
                    valid_reasons.add("destination")
                if not (profile == FILES_PROFILE and not refused and actual is not None
                        and actual.status in {"active", "idle"} and actual.version_indicator.agent_version == version):
                    valid_reasons.add("source")
                if system_policy and refusal_code is not None:
                    valid_reasons.add("source_not_migratable")
                if "source_changed" in valid_reasons:
                    missing = "source_changed"
                elif "original_destination" in valid_reasons:
                    missing = "original_destination"
                elif "destination" in valid_reasons:
                    missing = "destination"
                elif "source" in valid_reasons:
                    missing = "source"
                elif "source_not_migratable" in valid_reasons:
                    missing = "source_not_migratable"
                else:
                    if not preview and not resume:
                        raise _OmissionBeforeDispatch(
                            command.request_id, "NATIVE_FILES_CONFLICT",
                            "both helpers are available; no omission confirmation was dispatched")
                    raise Problem(409, "NATIVE_FILES_CONFLICT", "both helpers are available; keep the original migration")
            before_send()
            internal = NativeSessionFilesOmission(
                request_id=command.request_id,
                follow=NativeSessionFollow(
                    request_id=follow.request_id, expected_session_id=source_sid, expected_etag=state.etag,
                    stage="prepare", idle_only=follow.idle_only, files_required=True),
                stage="read" if reconcile or already_ready else "check" if preview else "activate" if resume else "confirm",
                missing=missing,
                pending_request_id=pending_request_id, pending_session_id=pending_session_id,
                confirmation_digest=None if system_policy else material_digest(command),
                decision_basis="system_policy" if system_policy else None)

            def original_reason(observed):
                reason = observed.files_confirmation_reason
                if observed.files_confirmation_saved != command.request_id or reason not in valid_reasons:
                    raise Problem(409, "NATIVE_FILES_CONFLICT", "the saved omission reason is no longer confirmed")
                return internal.model_copy(update={"missing": reason})

            if internal.stage == "activate":
                observed, _ = NativeTurnGateway._invoke(
                    destination, internal.model_copy(update={"stage": "read", "missing": None}))
                if observed.files_migrated is False and observed.files_skip_request_id == command.request_id:
                    return self.follow(client, follow, before_send=before_send)
                internal = original_reason(observed)
            if internal.stage == "confirm":
                try:
                    checked, _ = NativeTurnGateway._invoke(destination, internal.model_copy(update={"stage": "check"}))
                    if checked.files_migrated is False and checked.files_skip_request_id == command.request_id:
                        return self.follow(client, follow, before_send=before_send)
                    if checked.reason == "scan_pending":
                        return checked.model_copy(update={
                            "files_confirmation_saved": None, "files_confirmation_reason": None,
                            "files_confirmation_absent": None,
                            "files_confirmation_pending": command.request_id})
                    if system_policy and checked.reason in {
                            "accepted_or_started", "admission_pending", "task_active", "task_unconfirmed", "task_access_denied"}:
                        return checked
                    if checked.files_confirmation_saved is not None:
                        internal = original_reason(checked)
                except Problem as error:
                    if error.status == 409 and error.body["error"]["code"] in {
                            "NATIVE_FILES_CONFLICT", "NATIVE_FILES_CONFIRMATION_REQUIRED", "NATIVE_ENVIRONMENT_CONFLICT"}:
                        raise _OmissionBeforeDispatch(command.request_id, error.body["error"]["code"],
                                                     "the read-only check refused; no omission confirmation was dispatched",
                                                     files_check=error.body["error"].get("files_check")) from None
                    raise
                before_send()
            value, _ = NativeTurnGateway._invoke(destination, internal)
            if observe_only:
                return value
            if reconcile:
                if value.state == "ready":
                    return value
                if command.request_id in {value.files_confirmation_absent, value.files_confirmation_saved}:
                    before_send()
                    return self.without_files(client, command, before_send=before_send, system_policy=system_policy,
                                              refusal_code=refusal_code)
                if value.files_migrated is False and value.files_skip_request_id == command.request_id:
                    return self.follow(client, follow, before_send=before_send)
                return value
            if preview or already_ready or value.files_confirmation_pending == command.request_id:
                return value
            return self.follow(client, follow, before_send=before_send)

    def prepare(self, client, command, *, before_send):
        from .behavior_native_turn_api import NativeTurnGateway

        with self._changing():
            version = self.selected_version(client)
            selected = self.candidate(client)
            if self.prepared is not None:
                self.current(client)
                return self.prepared
            version_reader = self.candidate(client, version=version)
            current_version = _read_platform_session(version_reader)
            if current_version is not None:
                if current_version.agent_version != version:
                    raise Problem(409, "NATIVE_SESSION_CHANGED", "the version reader is bound to another image")
                selected = version_reader
            else:
                actual = _read_platform_session(selected)
                if actual is None:
                    reader = _latest_version_reader(client, self.composition.config.identity.managed_identity_client_id)
                    if reader is not None:
                        selected = self._scoped(client, reader)
                        actual = _read_platform_session(selected)
                        if actual is None:
                            raise Problem(503, "NATIVE_BOOTSTRAP_UNKNOWN", "the original version reader disappeared")
                        if actual.agent_version != reader.agent_version:
                            raise Problem(409, "NATIVE_SESSION_CHANGED", "the original version reader has another version")
                if actual is not None:
                    selected = selected.for_target(actual)
                    state = self._state(selected)
                    if state.preparation is not None:
                        self._remember_state(selected, state)
                        return self.prepared
                try:
                    selected = self._destination(client, version, before_send)
                except Problem as error:
                    if error.body["error"]["code"] == "NATIVE_ENVIRONMENT_UNKNOWN":
                        raise Problem(503, "NATIVE_BOOTSTRAP_UNKNOWN", "read the original version session after creation") from None
                    raise
            state = self._state(selected)
            if state.preparation is not None:
                self._remember_state(selected, state)
                return self.prepared
            before_send()
            prepared, _ = NativeTurnGateway._invoke(selected, NativeSessionPrepare(
                request_id=command.request_id, expected_session_id=state.hosted_session_id,
                expected_etag=state.etag,
            ))
            state = self._state(selected)
            if state.preparation != prepared:
                raise Problem(503, "NATIVE_BOOTSTRAP_UNKNOWN", "the original preparation did not read back")
            return self._remember(selected, state)


def native_sessions(composition):
    value = getattr(composition, "_native_sessions", None)
    if value is None:
        value = composition.__dict__.setdefault("_native_sessions", NativeSessions(composition))
    return value
