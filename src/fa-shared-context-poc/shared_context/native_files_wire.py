"""Session Files migration metadata; no Hosted SDK, credentials or arbitrary roots."""

from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .behavior_wire import Digest, Identifier, WireModel

WORKSPACE_ROOT = ".fa-assistant-v12/desktop/files"
FILES_PROFILE = "fa-workspace-files-v1"
MAX_FILE_ENTRIES = 2000
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
FILES_ERRORS = frozenset({
    "NATIVE_FILES_UNSUPPORTED", "NATIVE_FILES_UNSAFE", "NATIVE_FILES_LIMIT",
    "NATIVE_FILES_SOURCE_CHANGED", "NATIVE_FILES_CONFLICT", "NATIVE_FILES_UNKNOWN",
    "NATIVE_FILES_UNAVAILABLE", "NATIVE_FILES_ACCESS_DENIED",
    "NATIVE_FILES_SOURCE_UNAVAILABLE", "NATIVE_FILES_TIMEOUT",
})
AgentVersion = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]


def relative_path(value: str) -> str:
    if (not value or len(value.encode("utf-8")) > 1024 or "\\" in value or ":" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("a bounded canonical relative workspace path is required")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("workspace paths cannot be absolute, aliases or traversals")
    return value


def check_tree(entries) -> None:
    """Unique sorted paths, every parent listed, and the total byte bound; the manifest byte bound is the caller's."""
    paths = [entry.path for entry in entries]
    if paths != sorted(set(paths)):
        raise ValueError("workspace entries must be unique and sorted")
    directories = {entry.path for entry in entries if entry.kind == "directory"}
    for entry in entries:
        parent = str(PurePosixPath(entry.path).parent)
        if parent != "." and parent not in directories:
            raise ValueError("every parent directory must be in the manifest")
    if sum(entry.byte_length for entry in entries) > MAX_TOTAL_BYTES:
        raise ValueError("the complete workspace exceeds the byte bound")


class NativeFileEntry(WireModel):
    path: Annotated[str, Field(min_length=1, max_length=1024)]
    kind: Literal["file", "directory"]
    mode: Annotated[int, Field(ge=0, le=0o777)]
    byte_length: Annotated[int, Field(ge=0, le=MAX_FILE_BYTES)] = 0
    sha256: Digest | None = None

    _path = field_validator("path")(relative_path)

    @model_validator(mode="after")
    def file_material(self):
        if (self.kind == "file") != (self.sha256 is not None):
            raise ValueError("only a regular file has a content digest")
        if self.kind == "directory" and self.byte_length != 0:
            raise ValueError("directory entries do not contain file bytes")
        return self


class NativeFilesScope(WireModel):
    agent_id: Identifier
    request_id: Identifier
    source_session_id: Identifier
    source_agent_version: AgentVersion
    target_session_id: Identifier
    target_agent_version: AgentVersion

    @model_validator(mode="after")
    def distinct_sessions(self):
        if self.source_session_id == self.target_session_id:
            raise ValueError("workspace migration requires two distinct sessions")
        return self

    @property
    def operation_id(self):
        from .behavior_native_turn_wire import material_digest
        return material_digest({name: getattr(self, name) for name in NativeFilesScope.model_fields})


class NativeFilesManifest(NativeFilesScope):
    profile: Literal["fa-workspace-files-v1"] = FILES_PROFILE
    root: Literal[".fa-assistant-v12/desktop/files"] = WORKSPACE_ROOT
    root_mode: Annotated[int, Field(ge=0, le=0o777)]
    entries: Annotated[tuple[NativeFileEntry, ...], Field(max_length=MAX_FILE_ENTRIES)]

    def encoded(self):
        from .behavior_native_turn_wire import canonical_bytes
        return canonical_bytes(self)

    @property
    def sha256(self):
        from .behavior_native_turn_wire import material_digest
        return material_digest(self)

    @model_validator(mode="after")
    def bounded_tree(self):
        check_tree(self.entries)
        if len(self.encoded()) > MAX_MANIFEST_BYTES:
            raise ValueError("the complete manifest exceeds its byte bound")
        return self


class NativeFilesListing(WireModel):
    directory: Annotated[int, Field(ge=0, le=MAX_FILE_ENTRIES + 1)] = 0
    after: Annotated[str, Field(min_length=1, max_length=2048)] | None = None
    seen: Annotated[tuple[str, ...], Field(max_length=MAX_FILE_ENTRIES)] = ()
    complete: bool = False


class NativeFilesPage(WireModel):
    directory: Annotated[int, Field(ge=0, le=MAX_FILE_ENTRIES)]
    after: Annotated[str, Field(min_length=1, max_length=2048)] | None = None
    next_after: Annotated[str, Field(min_length=1, max_length=2048)] | None = None
    paths: Annotated[tuple[str, ...], Field(max_length=100)]


def listing_directories(manifest):
    return ("", *(entry.path for entry in manifest.entries if entry.kind == "directory"))


def listing_children(manifest, directory):
    return {entry.path for entry in manifest.entries
            if str(PurePosixPath(entry.path).parent) == (directory or ".")}


def advance_listing(manifest, current, page):
    directories = listing_directories(manifest)
    if current.complete or (page.directory, page.after) != (current.directory, current.after):
        raise ValueError("the original listing page changed")
    expected = listing_children(manifest, directories[current.directory])
    added = set(page.paths)
    if (len(added) != len(page.paths) or not added <= expected or added.intersection(current.seen)
            or page.next_after is not None and (not added or page.next_after == current.after)):
        raise ValueError("the original listing page is invalid or did not advance")
    seen = set(current.seen) | added
    if page.next_after is None:
        if seen != expected:
            raise ValueError("the original directory listing is incomplete")
        index = current.directory + 1
        return NativeFilesListing(directory=index, complete=index == len(directories))
    return NativeFilesListing(directory=current.directory, after=page.next_after, seen=tuple(sorted(seen)))


class NativeFilesProgress(WireModel):
    manifest: NativeFilesManifest
    next_index: Annotated[int, Field(ge=0, le=MAX_FILE_ENTRIES)] = 0
    sending: Identifier | None = None
    source_listing: NativeFilesListing = Field(default_factory=NativeFilesListing)
    stopped_listing: NativeFilesListing = Field(default_factory=NativeFilesListing)
    source_verified: bool = False
    stop_started: bool = False
    stopped_index: Annotated[int, Field(ge=0, le=MAX_FILE_ENTRIES)] = 0
    source_stopped_verified: bool = False
    publishing: bool = False
    published: bool = False

    @model_validator(mode="after")
    def ordered(self):
        count = len(self.manifest.entries)
        if self.next_index > count or self.next_index == count and self.sending is not None:
            raise ValueError("progress must identify an original unfinished file")
        directories = listing_directories(self.manifest)
        for listing in (self.source_listing, self.stopped_listing):
            if listing.complete:
                if listing.directory != len(directories) or listing.after is not None or listing.seen:
                    raise ValueError("a complete listing must cover every original directory")
            elif (listing.directory >= len(directories) or len(set(listing.seen)) != len(listing.seen)
                  or not set(listing.seen) <= listing_children(self.manifest, directories[listing.directory])):
                raise ValueError("listing progress must stay within the original manifest")
        if self.source_verified and (self.next_index != count or not self.source_listing.complete):
            raise ValueError("source verification follows complete destination verification")
        if self.stop_started and not self.source_verified:
            raise ValueError("source stop follows its complete verification")
        if self.stopped_index > count or self.stopped_index and not self.stop_started:
            raise ValueError("stopped verification must follow the original source check")
        if self.source_stopped_verified and (not self.stop_started or self.stopped_index != count
                                            or not self.stopped_listing.complete):
            raise ValueError("stopped verification follows the original source check")
        if self.publishing and not self.source_stopped_verified or self.published and not self.publishing:
            raise ValueError("publication follows confirmed files and source stop")
        return self


class NativeSessionFiles(NativeFilesScope):
    operation: Literal["session_files"] = "session_files"
    stage: Literal["read", "inspect", "begin", "confirm", "confirm_readback", "list_source", "list_stopped", "source_unavailable",
                   "verify_source", "stop", "source_stopped", "observe"]
    index: Annotated[int, Field(ge=0, lt=MAX_FILE_ENTRIES)] | None = None
    send_token: Identifier | None = None
    page: NativeFilesPage | None = None

    @model_validator(mode="after")
    def step_material(self):
        if (self.stage in {"begin", "confirm", "confirm_readback"} and self.index is None
                or self.index is not None and self.stage not in {"begin", "confirm", "confirm_readback", "source_stopped"}):
            raise ValueError("only a file step names an index")
        if (self.stage == "begin") != (self.send_token is not None):
            raise ValueError("only a first-send claim carries a unique token")
        if (self.stage in {"list_source", "list_stopped"}) != (self.page is not None):
            raise ValueError("only a listing step carries an original page")
        return self


class NativeFilesView(NativeFilesScope):
    progress: NativeFilesProgress | None = None
    send_allowed: bool = False


def staging_relative_path(manifest: NativeFilesManifest, index: int) -> str:
    if not 0 <= index < len(manifest.entries):
        raise ValueError("staging must name an original manifest entry")
    return f".fa-assistant-v12/desktop/.fa-transfers/{manifest.operation_id}/upload/{index:04d}"
