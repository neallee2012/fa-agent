"""API-MI Session Files transport for the one retained follow operation; no fallback."""

from contextlib import contextmanager
import hashlib
import logging
from pathlib import PurePosixPath
from time import monotonic
from uuid import uuid4

from azure.core.exceptions import AzureError

from .errors import Problem
from .native_files_wire import (
    FILES_PROFILE, MAX_FILE_BYTES, WORKSPACE_ROOT,
    NativeFilesPage, NativeSessionFiles, listing_directories, relative_path, staging_relative_path,
)

MAX_FILE_STEPS = 16
MAX_FILE_PAGES = 128
FILE_STEP_SECONDS = 10
log = logging.getLogger(__name__)


def _failure(error, *, write=False):
    status = getattr(error, "status_code", None)
    denied = status in {401, 403}
    log.warning("NATIVE_SESSION_FILES_UNCONFIRMED status=%s write=%s",
                status if type(status) is int else None, write)
    return Problem(status if denied else 503,
                   "NATIVE_FILES_ACCESS_DENIED" if denied else "NATIVE_FILES_UNKNOWN",
                   "the original Session Files operation is unconfirmed; do not upload again")


class SessionFiles:
    def __init__(self, client):
        self.client = client

    @contextmanager
    def _project(self):
        from azure.ai.projects import AIProjectClient
        try:
            with AIProjectClient(
                    endpoint=self.client.target.project_endpoint, credential=self.client.credential,
                    retry_total=0, retry_connect=0, retry_read=0, retry_status=0,
                    connection_timeout=5, read_timeout=10) as project:
                yield project.agents
        except (AzureError, OSError) as error:
            raise _failure(error) from None

    def download(self, sid, path, entry):
        raw = bytearray()
        deadline = monotonic() + FILE_STEP_SECONDS
        with self._project() as agents:
            stream = agents.download_session_file(self.client.target.agent_name, sid, path=path)
            for chunk in stream:
                if (not isinstance(chunk, bytes) or monotonic() > deadline
                        or len(raw) + len(chunk) > min(entry.byte_length, MAX_FILE_BYTES)):
                    raise Problem(409, "NATIVE_FILES_CONFLICT", "the original file stream differs or exceeds its bound")
                raw.extend(chunk)
        if len(raw) != entry.byte_length or hashlib.sha256(raw).hexdigest() != entry.sha256:
            raise Problem(409, "NATIVE_FILES_CONFLICT", "the original file bytes do not match their saved manifest")
        return bytes(raw)

    def upload(self, sid, path, data):
        # The fixed 2.3.0 synchronous API takes bytes; a missing ACK never authorizes another PUT.
        with self._project() as agents:
            agents.upload_session_file(self.client.target.agent_name, sid, data, path=path)

    def list_page(self, manifest, listing):
        expected = {entry.path: entry for entry in manifest.entries}
        parent = listing_directories(manifest)[listing.directory]
        path = WORKSPACE_ROOT + ("/" + parent if parent else "")
        paths = []
        with self._project() as agents:
            pages = agents.list_session_files(
                self.client.target.agent_name, manifest.source_session_id, path=path, limit=100
            ).by_page(continuation_token=listing.after)
            try:
                page = next(pages)
            except StopIteration:
                raise Problem(503, "NATIVE_FILES_UNKNOWN", "the original directory page is missing") from None
            for item in page:
                name = item.name
                if (not isinstance(name, str) or "/" in name or "\\" in name
                        or type(item.is_directory) is not bool or type(item.size) is not int or len(paths) >= 100):
                    raise Problem(409, "NATIVE_FILES_UNSAFE", "the Session Files entry is not canonical")
                relative = str(PurePosixPath(parent) / name)
                try:
                    relative_path(relative)
                except ValueError:
                    raise Problem(409, "NATIVE_FILES_UNSAFE", "the source listing leaves its fixed workspace root") from None
                entry = expected.get(relative)
                if (entry is None or item.is_directory != (entry.kind == "directory")
                        or item.size != entry.byte_length):
                    raise Problem(409, "NATIVE_FILES_SOURCE_CHANGED", "the original source listing changed")
                paths.append(relative)
            try:
                return NativeFilesPage(directory=listing.directory, after=listing.after,
                                       next_after=pages.continuation_token, paths=tuple(paths))
            except ValueError:
                raise Problem(503, "NATIVE_FILES_UNKNOWN", "the original directory cursor is invalid") from None


class FollowFiles:
    def __init__(self, destination, view, before_send):
        from .behavior_native_turn_api import NativeTurnGateway
        self.destination, self.view, self.before_send = destination, view, before_send
        self.invoke = NativeTurnGateway._invoke
        self.transport = SessionFiles(destination)
        self.scope = dict(
            agent_id=view.agent_id, request_id=view.request_id,
            source_session_id=view.previous_hosted_session_id, source_agent_version=view.previous_agent_version,
            target_session_id=view.hosted_session_id, target_agent_version=view.hosted_agent_version)

    def step(self, stage, *, client=None, **fields):
        client = self.destination if client is None else client
        self.before_send()
        value, _ = self.invoke(client, NativeSessionFiles(
            **self.scope, stage=stage, **fields))
        return value

    def _source(self):
        from .behavior_native_sessions import _session_metadata, _target
        if self.view.source_invocation_refused:
            raise Problem(409, "NATIVE_FILES_SOURCE_UNAVAILABLE", "the original source invocation was refused; do not invoke it again")
        if self.view.previous_files_profile != FILES_PROFILE and self.view.files_manifest_sha256 is None:
            raise Problem(409, "NATIVE_FILES_UNSUPPORTED", "the original FA binding has no verified source helper; no source was invoked")
        client = self.destination.for_target(_target(
            self.destination.target, session_id=self.view.previous_hosted_session_id,
            agent_version=self.view.previous_agent_version))
        actual = _session_metadata(client)
        if actual is None or actual.status not in {"active", "idle"}:
            raise Problem(409, "NATIVE_FILES_SOURCE_UNAVAILABLE", "the original source is not available for a file snapshot")
        if actual.version_indicator.agent_version != self.view.previous_agent_version:
            raise Problem(409, "NATIVE_FILES_SOURCE_UNAVAILABLE", "the source version differs from its FA binding; no source was invoked")
        return client

    def require_source_version(self):
        from .behavior_native_sessions import _session_metadata, _target
        source = self.destination.for_target(_target(
            self.destination.target, session_id=self.view.previous_hosted_session_id,
            agent_version=self.view.previous_agent_version))
        metadata = _session_metadata(source)
        if metadata is None or metadata.version_indicator.agent_version != self.view.previous_agent_version:
            raise Problem(409, "NATIVE_FILES_SOURCE_UNAVAILABLE",
                          "the original source version is unavailable; no source file read or stop was sent")

    def copy_step(self):
        observed = self.step("read")
        if observed.progress is None:
            try:
                observed = self.step("inspect", client=self._source())
            except Problem as error:
                code = error.body["error"]["code"]
                if not (error.status == 409 and code in {"NATIVE_SESSION_CHANGED", "NATIVE_FILES_SOURCE_UNAVAILABLE"}
                        or error.body["error"].get("upstream_status") == 409):
                    raise
                if not self.view.source_invocation_refused:
                    self.step("source_unavailable")
                raise Problem(409, "NATIVE_FILES_SOURCE_UNAVAILABLE",
                              "the original source refused inspection; confirm without files instead of invoking it again") from None
        files = observed.progress
        if files is None:
            raise Problem(503, "NATIVE_FILES_UNKNOWN", "the original source manifest did not read back")
        if files.source_verified:
            return
        self.require_source_version()
        deadline = monotonic() + FILE_STEP_SECONDS
        for _ in range(MAX_FILE_STEPS):
            if files.next_index == len(files.manifest.entries) or monotonic() > deadline:
                break
            index, manifest = files.next_index, files.manifest
            entry = manifest.entries[index]
            if files.sending is None:
                data = (None if entry.kind == "directory" else self.transport.download(
                    manifest.source_session_id, WORKSPACE_ROOT + "/" + entry.path, entry))
                claim = self.step("begin", index=index, send_token=uuid4().hex)
                files = claim.progress
                if claim.send_allowed and entry.kind == "file":
                    try:
                        self.before_send()
                        self.transport.upload(manifest.target_session_id, staging_relative_path(manifest, index), data)
                    except Problem as error:
                        if error.body["error"]["code"] not in {"NATIVE_FILES_UNKNOWN", "NATIVE_FILES_ACCESS_DENIED"}:
                            raise
                        log.warning("NATIVE_SESSION_FILES_UPLOAD_READBACK code=%s", error.body["error"]["code"])
            # Includes restart/lost ACK: only GET this original target and staging path.
            if entry.kind == "file":
                self.transport.download(manifest.target_session_id, staging_relative_path(manifest, index), entry)
            files = self.step("confirm", index=index).progress
        if files.next_index == len(files.manifest.entries):
            files = self._listing(files, stopped=False)
            if files.source_listing.complete:
                self.step("verify_source", client=self._source())

    def _listing(self, files, *, stopped):
        field = "stopped_listing" if stopped else "source_listing"
        deadline = monotonic() + FILE_STEP_SECONDS
        for _ in range(MAX_FILE_PAGES):
            listing = getattr(files, field)
            if listing.complete or monotonic() > deadline:
                break
            page = self.transport.list_page(files.manifest, listing)
            files = self.step("list_stopped" if stopped else "list_source", page=page).progress
        return files

    def before_stop(self):
        files = self.step("read").progress
        if files is None or not files.source_verified:
            raise Problem(409, "NATIVE_FILES_UNAVAILABLE", "the original workspace has not been verified")
        if not files.stop_started:
            self.step("verify_source", client=self._source())
            self.step("stop")

    def stopped_step(self):
        self.require_source_version()
        files = self.step("read").progress
        if files is None or not files.source_verified:
            raise Problem(409, "NATIVE_FILES_UNAVAILABLE", "the original transfer has not been verified")
        if files.source_stopped_verified:
            return True
        deadline = monotonic() + FILE_STEP_SECONDS
        for _ in range(MAX_FILE_STEPS):
            if files.stopped_index == len(files.manifest.entries) or monotonic() > deadline:
                break
            index, manifest = files.stopped_index, files.manifest
            entry = manifest.entries[index]
            if entry.kind == "file":
                self.transport.download(manifest.source_session_id, WORKSPACE_ROOT + "/" + entry.path, entry)
            files = self.step("source_stopped", index=index).progress
        if files.stopped_index != len(files.manifest.entries):
            return False
        files = self._listing(files, stopped=True)
        if not files.stopped_listing.complete:
            return False
        return self.step("source_stopped").progress.source_stopped_verified

    def observe(self):
        files = self.step("observe").progress
        if files is not None and files.sending is not None:
            entry, index = files.manifest.entries[files.next_index], files.next_index
            if entry.kind == "file":
                self.transport.download(files.manifest.target_session_id,
                                        staging_relative_path(files.manifest, index), entry)
            files = self.step("confirm_readback", index=index).progress
        return files
