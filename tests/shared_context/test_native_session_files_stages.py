"""Each file stage of ``session_files``: which check refuses first and what it saves, without any Hosted runtime."""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from shared_context import native_environment, native_environment_files
from shared_context.behavior_native_turn_wire import NativeEnvironmentView
from shared_context.errors import Problem
from shared_context.native_environment import _Following
from shared_context.native_environment_files import session_files
from shared_context.native_files_wire import (
    NativeFileEntry, NativeFilesListing, NativeFilesManifest, NativeFilesPage, NativeFilesProgress, NativeSessionFiles,
)
from shared_context.native_turns import _AgentSession

SCOPE = {
    "agent_id": "agent", "request_id": "rid", "source_session_id": "source", "source_agent_version": "41",
    "target_session_id": "target", "target_agent_version": "42",
}
MANIFEST = NativeFilesManifest(**SCOPE, root_mode=0o755, entries=(
    NativeFileEntry(path="d", kind="directory", mode=0o755),
    NativeFileEntry(path="d/a.txt", kind="file", mode=0o644, byte_length=1, sha256="b" * 64),
))
DONE = NativeFilesListing(directory=2, complete=True)
VIEW = NativeEnvironmentView(
    agent_id="agent", request_id="rid", previous_hosted_session_id="source", previous_agent_version="41",
    hosted_session_id="target", hosted_agent_version="42", state="pending")
UNAVAILABLE, CONFLICT = "NATIVE_FILES_UNAVAILABLE", "NATIVE_FILES_CONFLICT"
ROOT_PAGE = NativeFilesPage(directory=0, paths=("d",))


def progress(**options):
    return NativeFilesProgress(manifest=MANIFEST, **options)


def following(files, *, phase="files"):
    view = VIEW if files is None else VIEW.model_copy(update={"files_manifest_sha256": files.manifest.sha256})
    return _Following(view=view, phase=phase, files_required=True, files=files)


SENT = {"next_index": 2, "source_listing": DONE}
VERIFIED = {**SENT, "source_verified": True}
STOPPING = {**VERIFIED, "stop_started": True}
STOPPED = {**STOPPING, "stopped_index": 2, "stopped_listing": DONE}


class Workspace:
    def __init__(self, log, published):
        self.log, self.is_published = log, published

    def snapshot(self, **scope):
        self.log.append(("snapshot", scope))
        return MANIFEST

    def prepare(self, manifest):
        self.log.append(("prepare", manifest))

    def confirm(self, manifest, index):
        self.log.append(("confirm", manifest, index))

    def verify(self, manifest):
        self.log.append(("verify", manifest))

    def published(self, manifest):
        self.log.append(("published", manifest))
        return self.is_published


class Scene:
    def __init__(self, monkeypatch, files=None, *, phase="files", supported=True, inactive=True, published=False,
                 readback="saved", environment=VIEW):
        self.log = log = []
        self.record = record = following(files, phase=phase)
        self.current = _AgentSession(agent_id="agent", agent_name="name", hosted_session_id="source", environment=VIEW,
                                     following_supported=supported)
        self.saved = record
        self.readback = readback

        async def source_inactive(session_id):
            log.append(("inactive", session_id))
            return inactive

        self.runtime = SimpleNamespace(
            workspace_files=Workspace(log, published),
            bootstrap_source_inactive=None if inactive is None else source_inactive,
            _agent_record=self._agent_record)
        self.fresh = _AgentSession(agent_id="agent", agent_name="name", hosted_session_id="source",
                                   environment=environment)

        async def operation(runtime, command):
            log.append("operation")
            return self.current, record, "etag", "key"

        async def save_progress(runtime, value, etag, *, key=None):
            log.append(("progress", value, etag, key))
            self.saved = value

        async def read(runtime, *, key=None):
            log.append(("read", key))
            if self.readback is None:
                return None, None
            return (self.saved if self.readback == "saved" else self.readback), "etag-2"

        async def save_agent(runtime, original, etag, replacement):
            log.append(("agent", original, etag, replacement))

        def source_matches(current, view):
            log.append(("matches", current, view))

        monkeypatch.setattr(native_environment_files, "_operation", operation)
        monkeypatch.setattr(native_environment, "_progress", save_progress)
        monkeypatch.setattr(native_environment, "_read", read)
        monkeypatch.setattr(native_environment, "_save_agent", save_agent)
        monkeypatch.setattr(native_environment, "_source_matches", source_matches)

    async def _agent_record(self):
        self.log.append("agent_record")
        return self.fresh, "agent-etag"

    def run(self, stage, **options):
        scope = options.pop("scope", SCOPE)
        return asyncio.run(session_files(self.runtime, NativeSessionFiles(**scope, stage=stage, **options)))

    def kinds(self):
        return [entry if isinstance(entry, str) else entry[0] for entry in self.log]


def saved_files(entry):
    assert entry[0] == "progress" and entry[2:] == ("etag", "key")
    return entry[1].files


def refused(scene, code, message, stage, status=409, **options):
    with pytest.raises(Problem) as failure:
        scene.run(stage, **options)
    assert failure.value.status == status
    assert failure.value.body["error"]["code"] == code
    assert failure.value.body["error"]["message"] == message


def test_a_refused_source_is_recorded_first_on_the_operation_then_on_the_agent(monkeypatch):
    scene = Scene(monkeypatch)
    view = scene.run("source_unavailable")
    assert view.progress is None and view.send_allowed is False
    assert scene.kinds() == ["operation", "progress", "agent_record", "matches", "agent"]
    assert scene.log[1][1].view.source_invocation_refused is True and scene.log[1][1].files is None
    assert scene.log[3] == ("matches", scene.fresh, VIEW)
    assert scene.log[4][1:3] == (scene.fresh, "agent-etag")
    assert scene.log[4][3].environment.source_invocation_refused is True
    assert scene.log[1][2:] == ("etag", "key")


def test_a_refused_source_cannot_discard_existing_file_material(monkeypatch):
    scene = Scene(monkeypatch, progress())
    refused(scene, CONFLICT, "existing file material must be reconciled, not discarded", "source_unavailable")
    assert scene.kinds() == ["operation"]


def test_a_refused_source_needs_its_pending_binding_after_the_operation_save(monkeypatch):
    scene = Scene(monkeypatch, environment=None)
    refused(scene, "NATIVE_ENVIRONMENT_CONFLICT", "the original pending binding is missing", "source_unavailable")
    assert scene.kinds() == ["operation", "progress", "agent_record", "matches"]


def test_inspect_fixes_one_manifest_and_returns_it_without_a_readback(monkeypatch):
    scene = Scene(monkeypatch)
    view = scene.run("inspect")
    assert view.progress == progress()
    assert scene.kinds() == ["operation", "snapshot", "progress"]
    assert scene.log[1] == ("snapshot", SCOPE)
    assert saved_files(scene.log[2]) == progress()
    assert scene.log[2][1].view.files_manifest_sha256 == MANIFEST.sha256


def test_inspect_keeps_the_retained_manifest(monkeypatch):
    files = progress(next_index=1)
    scene = Scene(monkeypatch, files)
    assert scene.run("inspect").progress == files
    assert scene.kinds() == ["operation"]


def test_inspect_waits_for_writers_before_it_asks_about_support(monkeypatch):
    scene = Scene(monkeypatch, phase="draining", supported=False)
    refused(scene, UNAVAILABLE, "original writers must drain before the source is fixed", "inspect")
    assert scene.kinds() == ["operation"]


def test_inspect_refuses_a_source_that_cannot_fence_new_writers(monkeypatch):
    scene = Scene(monkeypatch, supported=False)
    refused(scene, "NATIVE_FILES_UNSUPPORTED", "the original source cannot fence new workspace writers", "inspect")
    assert scene.kinds() == ["operation"]


@pytest.mark.parametrize("stage", ["read", "observe"])
def test_a_reader_before_any_manifest_gets_an_empty_view(monkeypatch, stage):
    scene = Scene(monkeypatch)
    view = scene.run(stage)
    assert view.progress is None and view.send_allowed is False
    assert scene.kinds() == ["operation"]


@pytest.mark.parametrize("stage, options", [
    ("begin", {"index": 0, "send_token": "token"}), ("confirm", {"index": 0}), ("confirm_readback", {"index": 0}),
    ("list_source", {"page": ROOT_PAGE}), ("list_stopped", {"page": ROOT_PAGE}), ("verify_source", {}), ("stop", {}),
    ("source_stopped", {}),
])
def test_a_writer_before_any_manifest_is_unavailable(monkeypatch, stage, options):
    scene = Scene(monkeypatch)
    refused(scene, UNAVAILABLE, "the original source manifest is not available", stage, **options)


@pytest.mark.parametrize("name, value", [
    ("agent_id", "another"), ("request_id", "another"), ("source_session_id", "another"),
    ("source_agent_version", "40"), ("target_session_id", "another"), ("target_agent_version", "43"),
])
@pytest.mark.parametrize("stage", ["read", "stop"])
def test_a_retained_manifest_of_another_operation_is_refused_before_its_stage(monkeypatch, stage, name, value):
    scene = Scene(monkeypatch, progress())
    refused(scene, CONFLICT, "the retained manifest names another operation", stage, scope={**SCOPE, name: value})


@pytest.mark.parametrize("stage, field", [("list_source", "source_listing"), ("list_stopped", "stopped_listing")])
def test_a_listing_page_advances_its_own_listing_after_the_stage_gates(monkeypatch, stage, field):
    files = progress(**(STOPPING if stage == "list_stopped" else {"next_index": 2}), **(
        {"stopped_index": 2} if stage == "list_stopped" else {}))
    scene = Scene(monkeypatch, files)
    view = scene.run(stage, page=ROOT_PAGE)
    assert getattr(view.progress, field) == NativeFilesListing(directory=1)
    expected = ["operation", "progress", "read"]
    assert scene.kinds() == (["operation", "inactive", "progress", "read"] if stage == "list_stopped" else expected)
    assert scene.log[-1] == ("read", "key")


@pytest.mark.parametrize("stage, options", [
    ("list_source", {"next_index": 1}),
    ("list_source", {**VERIFIED, "stop_started": True}),
    ("list_stopped", {"next_index": 1}),
    ("list_stopped", {"next_index": 2}),
    ("list_stopped", {**STOPPING, "stopped_index": 1}),
])
def test_a_listing_outside_its_phase_is_refused_before_the_source_is_asked(monkeypatch, stage, options):
    scene = Scene(monkeypatch, progress(**options))
    refused(scene, CONFLICT, "this listing is outside its original file phase", stage, page=ROOT_PAGE)
    assert scene.kinds() == ["operation"]


def test_an_empty_workspace_cannot_list_stopped_before_its_stop(monkeypatch):
    # Zero entries make every index check pass, so only ``stop_started`` refuses here.
    empty = NativeFilesProgress(manifest=NativeFilesManifest(**SCOPE, root_mode=0o755, entries=()))
    scene = Scene(monkeypatch, empty)
    refused(scene, CONFLICT, "this listing is outside its original file phase", "list_stopped",
            page=NativeFilesPage(directory=0, paths=()))
    assert scene.kinds() == ["operation"]


@pytest.mark.parametrize("inactive", [None, False])
def test_a_stopped_listing_needs_an_inactive_source(monkeypatch, inactive):
    scene = Scene(monkeypatch, progress(**STOPPING, stopped_index=2), inactive=inactive)
    refused(scene, UNAVAILABLE, "the original source is not inactive", "list_stopped", page=ROOT_PAGE)
    assert scene.log[1:] == ([] if inactive is None else [("inactive", "source")])


def test_a_changed_directory_page_is_refused_without_a_save(monkeypatch):
    scene = Scene(monkeypatch, progress(next_index=2))
    with pytest.raises(Problem) as failure:
        scene.run("list_source", page=NativeFilesPage(directory=1, paths=("d/a.txt",)))
    assert failure.value.body["error"]["code"] == "NATIVE_FILES_SOURCE_CHANGED"
    assert failure.value.body["error"]["message"] == "the original directory page is incomplete or changed"
    assert failure.value.__suppress_context__ is True
    assert scene.kinds() == ["operation"]


def test_begin_prepares_then_claims_the_send_and_allows_it_once(monkeypatch):
    scene = Scene(monkeypatch, progress())
    view = scene.run("begin", index=0, send_token="token")
    assert view.send_allowed is True and view.progress.sending == "token"
    assert scene.kinds() == ["operation", "prepare", "progress", "read"]
    assert scene.log[1] == ("prepare", MANIFEST)
    assert saved_files(scene.log[2]) == progress(sending="token")


def test_begin_of_a_claimed_entry_never_allows_a_second_send(monkeypatch):
    files = progress(sending="first")
    scene = Scene(monkeypatch, files)
    view = scene.run("begin", index=0, send_token="second")
    assert view.send_allowed is False and view.progress == files
    assert scene.kinds() == ["operation", "read"]


@pytest.mark.parametrize("index, options", [(1, {}), (0, {"next_index": 1}), (2, VERIFIED)])
def test_begin_only_starts_the_next_unsent_entry(monkeypatch, index, options):
    scene = Scene(monkeypatch, progress(**options))
    refused(scene, CONFLICT, "only the next unsent original entry may start", "begin", index=index, send_token="token")
    assert scene.kinds() == ["operation"]


def test_begin_past_the_last_entry_prepares_then_fails_progress_validation(monkeypatch):
    scene = Scene(monkeypatch, progress(**SENT))
    with pytest.raises(ValidationError):
        scene.run("begin", index=2, send_token="token")
    assert scene.kinds() == ["operation", "prepare"]


@pytest.mark.parametrize("stage", ["confirm", "confirm_readback"])
def test_a_confirmed_entry_returns_the_operation_copy_without_any_read(monkeypatch, stage):
    files = progress(next_index=1)
    scene = Scene(monkeypatch, files, readback=None)
    view = scene.run(stage, index=0)
    assert view.progress == files and view.send_allowed is False
    assert scene.kinds() == ["operation"]


@pytest.mark.parametrize("stage", ["confirm", "confirm_readback"])
def test_an_old_confirm_of_another_operation_is_refused_by_scope_before_it_returns_progress(monkeypatch, stage):
    scene = Scene(monkeypatch, progress(next_index=1), readback=None)
    refused(scene, CONFLICT, "the retained manifest names another operation", stage, index=0,
            scope={**SCOPE, "request_id": "another"})
    assert scene.kinds() == ["operation"]


@pytest.mark.parametrize("stage", ["confirm", "confirm_readback"])
def test_confirm_moves_the_in_flight_entry_on_after_the_destination_agrees(monkeypatch, stage):
    scene = Scene(monkeypatch, progress(sending="token"))
    view = scene.run(stage, index=0)
    assert view.progress == progress(next_index=1) and view.send_allowed is False
    assert scene.kinds() == ["operation", "confirm", "progress", "read"]
    assert scene.log[1] == ("confirm", MANIFEST, 0)


@pytest.mark.parametrize("stage", ["confirm", "confirm_readback"])
@pytest.mark.parametrize("index, options", [(1, {"sending": "token"}), (0, {})], ids=["skips", "not-sent"])
def test_confirm_needs_the_in_flight_next_entry(monkeypatch, stage, index, options):
    scene = Scene(monkeypatch, progress(**options))
    refused(scene, CONFLICT, "only an original in-flight file may be confirmed", stage, index=index)
    assert scene.kinds() == ["operation"]


def test_verify_source_checks_then_records_its_complete_verification(monkeypatch):
    scene = Scene(monkeypatch, progress(**SENT))
    assert scene.run("verify_source").progress == progress(**VERIFIED)
    assert scene.kinds() == ["operation", "verify", "progress", "read"]
    assert scene.log[1] == ("verify", MANIFEST)


@pytest.mark.parametrize("options", [
    {"next_index": 1, "source_listing": DONE},
    {"next_index": 1, "source_listing": DONE, "sending": "token"},
    STOPPING,
    {"next_index": 2},
], ids=["unsent", "in-flight", "stopping", "unlisted"])
def test_verify_source_needs_every_entry_and_listing_before_any_stop(monkeypatch, options):
    scene = Scene(monkeypatch, progress(**options))
    refused(scene, UNAVAILABLE, "all original destination entries must first be confirmed", "verify_source")
    assert scene.kinds() == ["operation"]


def test_a_finished_entry_count_cannot_also_be_sending():
    # Why verify_source needs no ``sending`` check: every decoded or changed progress passes this validator first.
    with pytest.raises(ValidationError):
        progress(**SENT, sending="token")


def test_stop_records_that_it_started(monkeypatch):
    scene = Scene(monkeypatch, progress(**VERIFIED))
    assert scene.run("stop").progress == progress(**STOPPING)
    assert scene.kinds() == ["operation", "progress", "read"]


def test_stop_needs_the_complete_verification(monkeypatch):
    scene = Scene(monkeypatch, progress(**SENT))
    refused(scene, UNAVAILABLE, "source stop requires the original complete verification", "stop")
    assert scene.kinds() == ["operation"]


def test_source_stopped_verifies_entries_one_at_a_time(monkeypatch):
    scene = Scene(monkeypatch, progress(**STOPPING))
    assert scene.run("source_stopped", index=0).progress.stopped_index == 1
    assert scene.log[1:] == [("inactive", "source"), scene.log[2], ("read", "key")]


def test_an_already_verified_stopped_entry_saves_the_same_progress_again(monkeypatch):
    files = progress(**STOPPING, stopped_index=1)
    scene = Scene(monkeypatch, files)
    assert scene.run("source_stopped", index=0).progress == files
    assert saved_files(scene.log[2]) == files


def test_source_stopped_finishes_by_preparing_then_recording_the_verification(monkeypatch):
    scene = Scene(monkeypatch, progress(**STOPPED))
    assert scene.run("source_stopped").progress.source_stopped_verified is True
    assert scene.kinds() == ["operation", "inactive", "prepare", "progress", "read"]


@pytest.mark.parametrize("options, inactive, message, seen", [
    (VERIFIED, True, "the original source verification is missing", []),
    (STOPPING, None, "the original source verification is missing", []),
    (STOPPING, False, "the original source is not inactive", ["inactive"]),
], ids=["not-started", "no-reader", "active"])
@pytest.mark.parametrize("index", [None, 0])
def test_source_stopped_needs_a_started_stop_and_an_inactive_source(monkeypatch, options, inactive, message, seen, index):
    scene = Scene(monkeypatch, progress(**options), inactive=inactive)
    refused(scene, UNAVAILABLE, message, "source_stopped", index=index)
    assert scene.kinds() == ["operation", *seen]


@pytest.mark.parametrize("options", [
    {**STOPPING, "stopped_index": 1, "stopped_listing": DONE},
    {**STOPPING, "stopped_index": 2},
], ids=["unverified-entry", "unlisted"])
def test_source_stopped_finish_needs_every_stopped_check(monkeypatch, options):
    scene = Scene(monkeypatch, progress(**options))
    refused(scene, UNAVAILABLE, "the stopped source has not been completely verified", "source_stopped")
    assert scene.kinds() == ["operation", "inactive"]


def test_source_stopped_cannot_skip_an_entry(monkeypatch):
    scene = Scene(monkeypatch, progress(**STOPPING))
    refused(scene, CONFLICT, "stopped source verification cannot skip an original entry", "source_stopped", index=1)
    assert scene.kinds() == ["operation", "inactive"]


def test_observe_records_a_real_publication_once(monkeypatch):
    publishing = {**STOPPED, "source_stopped_verified": True, "publishing": True}
    scene = Scene(monkeypatch, progress(**publishing), published=True)
    assert scene.run("observe").progress.published is True
    assert scene.kinds() == ["operation", "published", "progress", "read"]


@pytest.mark.parametrize("options, published, seen", [
    ({"source_stopped_verified": True, "publishing": True}, False, ["published"]),
    ({"source_stopped_verified": True, "publishing": True, "published": True}, True, []),
    ({"source_stopped_verified": True}, True, []),
], ids=["not-yet", "already", "not-publishing"])
def test_observe_otherwise_only_reads_back(monkeypatch, options, published, seen):
    scene = Scene(monkeypatch, progress(**STOPPED, **options), published=published)
    scene.run("observe")
    assert scene.kinds() == ["operation", *seen, "read"]


def test_read_only_reads_back_the_saved_progress(monkeypatch):
    newer = following(progress(next_index=1))
    scene = Scene(monkeypatch, progress(), readback=newer)
    view = scene.run("read")
    assert view.progress == newer.files and view.send_allowed is False
    assert scene.kinds() == ["operation", "read"]


def test_a_missing_readback_is_unknown_after_the_step(monkeypatch):
    scene = Scene(monkeypatch, progress(**VERIFIED), readback=None)
    refused(scene, "NATIVE_FILES_UNKNOWN", "the original file progress did not read back", "stop", status=503)
    assert scene.kinds() == ["operation", "progress", "read"]
