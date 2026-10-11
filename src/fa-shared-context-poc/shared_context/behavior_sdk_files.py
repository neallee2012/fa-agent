"""Hosted-only implementation of the official SDK session filesystem."""
from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath
import hashlib
import json
import platform
import sys

from copilot import SessionFsFileInfo, SessionFsProvider
from copilot.rpc import SessionFSReaddirWithTypesEntry, SessionFSReaddirWithTypesEntryType

from .backup_models import MAX_BACKUP_BYTES
from .errors import Problem
from .behavior_sdk_state import (
    AUTO_PROFILE, SdkState, CheckpointProfile, CompactionExecution, DIAG_CODEC, DIAG_CODECS,
    RESPONSES_CODEC, RESPONSES_DIAG_CODEC, NativeCheckpointTurn, NativeCompactionExecution,
    checked_path, validate_provider_checkpoint,
)
from .behavior_checkpoint_privacy import checkpoint_events, checkpoint_files

STATE_PATH = "/session-state"
SESSION_FS = {"initial_working_directory": "/", "session_state_path": STATE_PATH, "conventions": "posix"}
TOOL_OUTPUT_READ_GUIDANCE = (
    "Use read_tool_output to read SDK-saved tool output under /session-state/temp (or list that directory), "
    "following next_offset for more text. These are this session's virtual files, not shell paths: "
    "do not use run_command to read them. Other files still require their existing tools and permissions.")
TOOL_OUTPUT_READ_REFUSAL = (
    "read_tool_output may only read this session's /session-state/temp and its contents. "
    "Use the existing authorized tools for other files; shell commands still follow the Team rules.")
# The SDK keeps every Hosted MCP screenshot's base64 in events.jsonl. Each running large result keeps room for one
# large viewport screenshot plus the rest of the turn; otherwise the write fails and the whole turn is lost.
LARGE_RESULT_RESERVE_BYTES = 3 * 1024 * 1024


class SdkSessionFiles(SessionFsProvider):
    """A per-turn SDK filesystem; only successful complete snapshots can be published."""
    def __init__(self, session_id, state=None, *, checkpoint_profile=None, model_provider=None):
        self.session_id = session_id
        if model_provider not in (None, "openai-responses"):
            raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "unknown SDK model provider")
        self.model_provider = model_provider
        validate_provider_checkpoint(model_provider, checkpoint_profile)
        self.checkpoint_profile = (None if checkpoint_profile is None else
                                   CheckpointProfile.model_validate_json(checkpoint_profile.model_dump_json()))
        if self.checkpoint_profile is not None:
            native_platform = {
                ("win32", "amd64"): "win32-x64",
                ("linux", "x86_64"): "linux-x64",
                ("linux", "amd64"): "linux-x64",
            }.get((sys.platform, platform.machine().lower()))
            if native_platform != self.checkpoint_profile.platform:
                raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "checkpoint profile does not match the supported native platform")
        self.restored = state is not None
        if state is not None:
            state = SdkState.model_validate_json(state.model_dump_json())
            if state.sdk_session_id != session_id:
                raise Problem(403, "ASSISTANT_CHECKPOINT_SCOPE", "SDK checkpoint belongs to another session")
            if state.model_provider != model_provider:
                raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "checkpoint model provider cannot change during resume")
            if state.codec in DIAG_CODECS and self.checkpoint_profile is None:
                raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "checkpoint consumer profile is disabled")
        self.previous = state
        self._workspace_stat_alias = None
        if state is not None:
            start = next(event for event in checkpoint_events(state.files["events.jsonl"])
                         if event["type"] == "session.start")
            context = start["data"].get("context")
            cwd = context.get("cwd") if isinstance(context, dict) else None
            if isinstance(cwd, str):
                normalized = cwd.replace("\\", "/")
                if ((PurePosixPath(cwd).is_absolute() or PureWindowsPath(cwd).is_absolute())
                        and "\x00" not in cwd and not ("/" in cwd and "\\" in cwd)
                        and not any(part in {".", ".."} for part in normalized.split("/"))
                        and normalized not in {"/", STATE_PATH} and not normalized.startswith(STATE_PATH + "/")):
                    self._workspace_stat_alias = normalized
        self.compaction = None
        self.checkpoint_error = None
        self._compacting = False
        self._compaction_events = 0
        self._compaction_model_request = None
        self._auto_open = False
        self._inherited_compaction_ids = set()
        if state is not None and (state.codec in DIAG_CODECS or self.automatic):
            from .behavior_sdk_compaction import events_from
            self._inherited_compaction_ids = {e["id"] for e in events_from(state.files)
                                               if e["type"].startswith("session.compaction")}
        self.files = {} if state is None else dict(state.files)
        self.directories = set(() if state is None else state.directories)
        for name in self.files:
            self._parents(name)
        self.failed = False
        self.frozen = False
        self._forbidden_values = set()
        # tool_call_id -> None while an approved large call runs, then its estimated bytes until they are written.
        self._unwritten_large = {}
        self.stamp = datetime.now(timezone.utc)

    def _path(self, value):
        if self.checkpoint_profile is not None and isinstance(value, str) and "\\" in value and "/" in value:
            self.failed = True
            raise PermissionError("SDK filesystem path uses mixed separators")
        normalized = value.replace("\\", "/") if isinstance(value, str) else ""
        if normalized in {"/", STATE_PATH, STATE_PATH + "/"}:
            return ""
        if not normalized.startswith(STATE_PATH + "/"):
            self.failed = True
            raise PermissionError("session filesystem request escaped its assigned root")
        try:
            return checked_path(normalized[len(STATE_PATH) + 1:])
        except ValueError:
            self.failed = True
            raise PermissionError("invalid session filesystem path") from None

    def _parents(self, path):
        self.directories.update(str(p) for p in PurePosixPath(path).parents if str(p) != ".")

    def can_read_tool_output(self, path):
        if not isinstance(path, str) or self.failed or self.frozen:
            return False
        if self.checkpoint_profile is not None and "\\" in path and "/" in path:
            return False
        normalized = path.replace("\\", "/")
        if not normalized.startswith(STATE_PATH + "/"):
            return False
        try:
            relative = checked_path(normalized[len(STATE_PATH) + 1:])
        except ValueError:
            return False
        return relative == "temp" or relative.startswith("temp/")

    def reserve_large_result(self, tool_call_id):
        """Reserve room for one more screenshot or browser result; the SDK files themselves are not changed.

        The SDK approves parallel calls before any of them completes, and writes results to events.jsonl later,
        sometimes only at the end of the turn. A running call keeps one result's reserve; a completed one counts
        its own size until it is written. Returns "reserved"; "busy" when only calls still running leave no room,
        so this call may fit after they finish; or "full" when written and completed results leave no room.
        """
        size = sum(len(content.encode("utf-8")) for content in self.files.values())
        running = sum(1 for value in self._unwritten_large.values() if value is None)
        completed = sum(value for value in self._unwritten_large.values() if value is not None)
        # "full" when the written state plus results that are certain to be written leaves no room; "busy" only
        # when calls still running may finish smaller.
        if size + completed + LARGE_RESULT_RESERVE_BYTES > MAX_BACKUP_BYTES:
            return "full"
        if size + completed + (running + 1) * LARGE_RESULT_RESERVE_BYTES > MAX_BACKUP_BYTES:
            return "busy"
        self._unwritten_large[tool_call_id] = None
        return "reserved"

    def large_result_completed(self, tool_call_id, estimated_bytes):
        if tool_call_id in self._unwritten_large:
            self._unwritten_large[tool_call_id] = estimated_bytes

    def reset_reservations(self):
        """A new turn: calls of an earlier turn are either written or will never complete."""
        self._unwritten_large.clear()

    def _release_written(self, previous, content):
        written = content[len(previous):] if content.startswith(previous) else content
        for line in written.splitlines():
            if "tool.execution_complete" not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            data = event.get("data")
            if event.get("type") == "tool.execution_complete" and isinstance(data, dict):
                self._unwritten_large.pop(data.get("toolCallId"), None)

    def _writable(self):
        if self.frozen or self.failed:
            self.failed = True
            raise PermissionError("SDK checkpoint filesystem is sealed or failed")

    @property
    def automatic(self):
        return self.checkpoint_profile is not None and self.checkpoint_profile.name == AUTO_PROFILE

    @property
    def automatic_open(self):
        """The runtime's own compaction started in this turn and has not completed."""
        return self._auto_open

    def continue_from(self, state):
        """A kept resident SDK continues on these same files; its published checkpoint is their new predecessor.

        The caller has already matched ``state.sha256`` to the release and the files to their released digest.
        """
        state = SdkState.model_validate_json(state.model_dump_json())
        if (state.sdk_session_id != self.session_id or state.model_provider != self.model_provider
                or state.codec in DIAG_CODECS or self.frozen or self.failed or self.compaction is not None
                or self.checkpoint_error is not None or self._auto_open):
            raise Problem(409, "SDK_STATE_SOURCE_UNVERIFIED", "a resident SDK continues only its own clean release")
        self.previous, self.restored = state, True
        if self.automatic:
            from .behavior_sdk_compaction import events_from
            self._inherited_compaction_ids = {e["id"] for e in events_from(state.files)
                                               if e["type"].startswith("session.compaction")}

    def snapshot(self, *, freeze=True):
        """``freeze=False`` only for a resident release, whose SDK keeps writing in the next turn."""
        if self.checkpoint_error is not None:
            raise Problem(409, self.checkpoint_error, "controlled checkpoint source is unavailable")
        if self.failed or not {"events.jsonl","workspace.yaml"} <= self.files.keys():
            raise Problem(409, "SDK_STATE_INCOMPLETE", "SDK filesystem reported an unresolved operation")
        try:
            projected = self.compaction is not None or self.previous is not None and self.previous.codec in DIAG_CODECS
            if projected:
                from .behavior_sdk_compaction import project_files, validate_execution, events_from
                if self.checkpoint_profile is None:
                    raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "checkpoint producer profile is disabled")
                files, ids = project_files(self.files, previous=self.previous,
                    forbidden_values=self._forbidden_values, directories=self.directories, model_provider=self.model_provider)
                codec = RESPONSES_DIAG_CODEC if self.model_provider == "openai-responses" else DIAG_CODEC
                state = SdkState(sdk_session_id=self.session_id, codec=codec, files=files,
                    directories=tuple(sorted(self.directories)), projected_event_ids=ids, model_provider=self.model_provider)
                if self.compaction is not None:
                    validate_execution(events_from(state.files), self.compaction)
            else:
                if self.checkpoint_profile is not None:
                    from .behavior_sdk_compaction import automatic_pairs, events_from, COMPACTION_EVENTS, unverified
                    events = events_from(self.files)
                    if self.automatic:
                        automatic_pairs(events)
                    elif any(e["type"] in COMPACTION_EVENTS for e in events):
                        unverified()
                state = SdkState(sdk_session_id=self.session_id, files=checkpoint_files(self.files,self._forbidden_values,
                    model_provider=self.model_provider), directories=tuple(sorted(self.directories)), model_provider=self.model_provider,
                    codec=RESPONSES_CODEC if self.model_provider == "openai-responses" else "session-fs-v1")
        except (ValueError, TypeError):
            raise Problem(409, "SDK_STATE_INCOMPLETE", "complete SDK filesystem state is unavailable") from None
        self.frozen = freeze
        return state

    async def compact(self, session, task, runtime_pid):
        from copilot.rpc import SessionHistoryCompactRequest, Trigger
        from .behavior_sdk_compaction import events_from, compaction_pair, unverified, validate_inherited
        native = isinstance(task, NativeCheckpointTurn)
        instruction = None if native else task.instruction
        profile = task.profile if native else None if instruction is None else instruction.checkpoint_profile
        if (profile != self.checkpoint_profile or self.automatic
                or self.checkpoint_profile is None or self.previous is None or self.previous.codec in DIAG_CODECS
                or self.compaction is not None or self.failed or self.frozen or session.session_id != self.session_id):
            unverified()
        if native and (task.previous is None or task.previous.lineage is None
                       or task.previous.sha256 != self.previous.sha256 or task.scope.sdk_session_id != self.session_id):
            unverified()
        observed = []
        unsubscribe = session.on(lambda e: observed.append(e.to_dict())
            if e.type.value.startswith("session.compaction") else None)
        try:
            before = events_from(self.files)
            validate_inherited(before, self.previous)
            if any(e["type"].startswith("session.compaction") for e in before):
                unverified()
            self._compacting = True
            result = await session.rpc.history.compact(SessionHistoryCompactRequest(trigger=Trigger.MANUAL))
            start, complete = compaction_pair(observed)
            data = complete["data"]
            if (result.success is not True or not result.summary_content or self._compaction_model_request is None
                    or (result.summary_content, result.messages_removed, result.tokens_removed)
                    != (data["summaryContent"], data["messagesRemoved"], data["tokensRemoved"])):
                unverified()
            observation = dict(
                sdk_session_id=self.session_id, base_state_sha256=self.previous.sha256, runtime_pid=runtime_pid,
                model_request_sha256=self._compaction_model_request,
                rpc_success=True, trigger="manual", start_event_id=start["id"], complete_event_id=complete["id"],
                checkpoint_number=data["checkpointNumber"], messages_removed=result.messages_removed,
                tokens_removed=result.tokens_removed,
                summary_sha256=hashlib.sha256(result.summary_content.encode("utf-8")).hexdigest())
            self.compaction = (NativeCompactionExecution(
                **observation, agent_id=task.scope.agent_id, request_id=task.request_id,
                conversation_id=task.scope.conversation_id, capture_digest=task.capture_digest,
            ) if native else CompactionExecution(
                **observation, job_id=task.job_id, conversation_id=instruction.conversation_id,
                profile_digest=task.profile_digest))
        except BaseException:
            self.checkpoint_error = self.checkpoint_error or "SDK_STATE_SOURCE_UNVERIFIED"
            raise
        finally:
            self._compacting = False
            unsubscribe()

    @property
    def compacting(self):
        return self._compacting

    def observe_model_request(self, request_sha256):
        if self._compacting:
            if self._compaction_model_request is not None:
                self.checkpoint_error = "SDK_STATE_SOURCE_UNVERIFIED"
                raise Problem(409, self.checkpoint_error, "multiple model requests cannot attest one supported compaction")
            self._compaction_model_request = request_sha256

    def observe_compaction(self, event):
        kind = event.get("type", "") if isinstance(event, dict) else event.type.value
        if not kind.startswith("session.compaction"):
            return
        if self.checkpoint_profile is None:
            self.checkpoint_error = "SDK_STATE_SOURCE_UNVERIFIED"
            return
        packet = event if isinstance(event, dict) else event.to_dict()
        if packet.get("id") in self._inherited_compaction_ids:
            return
        data = packet.get("data", {})
        if not isinstance(data, dict):
            self.checkpoint_error = "SDK_STATE_SOURCE_UNVERIFIED"
            return
        if self.automatic:
            # The runtime's own threshold compaction: strictly start then complete; verified again at snapshot.
            expected = "session.compaction_complete" if self._auto_open else "session.compaction_start"
            if kind != expected or not isinstance(data.get("trigger"), str) or data["trigger"] in ("", "manual"):
                self.checkpoint_error = "SDK_STATE_SOURCE_UNVERIFIED"
            if data.get("customInstructions") not in (None, ""):
                self.checkpoint_error = "SDK_STATE_PRIVATE_MATERIAL"
            self._auto_open = kind == "session.compaction_start"
            return
        self._compaction_events += 1
        expected = ("session.compaction_start", "session.compaction_complete")
        if (not self._compacting or self._compaction_events > 2
                or kind != expected[self._compaction_events - 1] or data.get("trigger") != "manual"):
            self.checkpoint_error = "SDK_STATE_SOURCE_UNVERIFIED"
        if data.get("customInstructions") not in (None, ""):
            self.checkpoint_error = "SDK_STATE_PRIVATE_MATERIAL"

    def forbid_value(self, value):
        if value:
            self._forbidden_values.add(value)

    async def read_file(self, path):
        name = self._path(path)
        if name not in self.files:
            raise FileNotFoundError("SDK file not found")
        return self.files[name]

    async def write_file(self, path, content, mode=None):
        self._writable()
        name = self._path(path)
        if not name or name in self.directories or not isinstance(content, str):
            self.failed = True
            raise ValueError("invalid SDK file write")
        try:
            size = sum(len(v.encode("utf-8")) for k, v in self.files.items() if k != name)
            total = size + len(content.encode("utf-8"))
        except UnicodeError:
            self.failed = True
            raise ValueError("SDK file is not valid UTF-8 text") from None
        if total > MAX_BACKUP_BYTES:
            self.failed = True
            raise ValueError("SDK filesystem exceeds the backup byte bound")
        self._parents(name)
        previous = self.files.get(name, "")
        self.files[name] = content
        if name == "events.jsonl" and self._unwritten_large:
            self._release_written(previous, content)
        self.stamp = datetime.now(timezone.utc)

    async def append_file(self, path, content, mode=None):
        name = self._path(path)
        await self.write_file(path, self.files.get(name, "") + content, mode)

    async def exists(self, path):
        name = self._path(path)
        return not name or name in self.files or name in self.directories

    async def stat(self, path):
        # Runtime 1.0.83 stats the saved cwd before applying resume's override.
        # This alias supplies virtual directory metadata, never host filesystem access.
        if (isinstance(path, str) and not ("/" in path and "\\" in path)
                and path.replace("\\", "/") == self._workspace_stat_alias):
            path = STATE_PATH
        name = self._path(path)
        if name and name not in self.files and name not in self.directories:
            raise FileNotFoundError("SDK entry not found")
        is_file = name in self.files
        return SessionFsFileInfo(is_file=is_file, is_directory=not is_file,
            size=len(self.files[name].encode()) if is_file else 0, mtime=self.stamp, birthtime=self.stamp)

    async def mkdir(self, path, recursive, mode=None):
        self._writable()
        name = self._path(path)
        if name in self.files:
            self.failed = True
            raise FileExistsError("SDK directory conflicts with a file")
        if name:
            parent = str(PurePosixPath(name).parent)
            if not recursive and parent != "." and parent not in self.directories:
                self.failed = True
                raise FileNotFoundError("SDK parent directory not found")
            self._parents(name)
            self.directories.add(name)

    async def readdir(self, path):
        name = self._path(path)
        if name and name not in self.directories:
            raise FileNotFoundError("SDK directory not found")
        prefix = name + "/" if name else ""
        return sorted({p[len(prefix):].split("/")[0] for p in (*self.files, *self.directories)
                       if p.startswith(prefix) and p != name})

    async def readdir_with_types(self, path):
        name = self._path(path)
        return [SessionFSReaddirWithTypesEntry(name=entry, type=(
            SessionFSReaddirWithTypesEntryType.FILE if (name + "/" if name else "") + entry in self.files
            else SessionFSReaddirWithTypesEntryType.DIRECTORY)) for entry in await self.readdir(path)]

    async def rm(self, path, recursive, force):
        self._writable()
        name = self._path(path)
        if not name:
            self.failed = True
            raise PermissionError("SDK cannot remove its checkpoint root")
        if name in self.files:
            del self.files[name]
            return
        if name not in self.directories:
            if not force:
                raise FileNotFoundError("SDK entry not found")
            return
        descendants = [p for p in (*self.files, *self.directories) if p.startswith(name + "/")]
        if descendants and not recursive:
            self.failed = True
            raise OSError("SDK directory is not empty")
        self.files = {p: text for p, text in self.files.items() if p not in descendants}
        self.directories.difference_update((name, *descendants))

    async def rename(self, src, dest):
        self._writable()
        old, new = self._path(src), self._path(dest)
        if not old or not new or new.startswith(old + "/"):
            self.failed = True
            raise PermissionError("invalid SDK entry rename")
        if old == new:
            return
        if old not in self.files and old not in self.directories:
            self.failed = True
            raise FileNotFoundError("SDK rename source not found")
        if new in self.directories or old in self.directories and new in self.files:
            self.failed = True
            raise FileExistsError("SDK rename destination conflicts")
        self._parents(new)
        if old in self.files:
            self.files[new] = self.files.pop(old)
        else:
            self.files = {(new + p[len(old):] if p.startswith(old + "/") else p): text
                          for p, text in self.files.items()}
            self.directories = {new + p[len(old):] if p == old or p.startswith(old + "/") else p
                                for p in self.directories}
        self.stamp = datetime.now(timezone.utc)
