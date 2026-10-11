"""The missing file-type/mode checks and no-overwrite publish at the private HOME seam."""

import configparser
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
from time import monotonic
from urllib.parse import urlsplit

from .errors import Problem
from .native_files_wire import (
    MAX_FILE_BYTES, MAX_FILE_ENTRIES, MAX_TOTAL_BYTES, WORKSPACE_ROOT,
    NativeFileEntry, NativeFilesManifest, relative_path, staging_relative_path,
)
from .private_files import guard, read, write

_SENSITIVE = frozenset({
    ".azure", ".ssh", ".aws", ".gnupg", ".copilot", ".fa-assistant-v12", ".fa-transfers",
    "credentials", ".git-credentials", ".netrc", "cookies", "cookies.sqlite",
    "login data", "web data", "browser-profile", "browser_profile", "native-turns",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
})


def _safe_name(value):
    try:
        relative_path(value)
    except ValueError:
        raise Problem(409, "NATIVE_FILES_UNSAFE", "workspace paths must stay canonical and inside the fixed root") from None
    parts = PurePosixPath(value).parts
    lowered = tuple(part.casefold() for part in parts)
    if (any(part in _SENSITIVE or part.startswith(".env") or part.endswith((".pem", ".pfx", ".p12", ".key"))
            for part in lowered)
            or lowered[-1] in {"alternates", "http-alternates", "commondir", "gitdir"} and ".git" in lowered):
        raise Problem(409, "NATIVE_FILES_UNSAFE", "necessary credential or external workspace material cannot be migrated")


def _safe_content(path, data):
    if re.search(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----", data):
        raise Problem(409, "NATIVE_FILES_UNSAFE", "private key material cannot be migrated")
    parts = tuple(part.casefold() for part in PurePosixPath(path).parts)
    if ".git" in parts and parts[-1] == "config" or parts[-1] in {".gitconfig", ".gitmodules"}:
        try:
            config = configparser.RawConfigParser(strict=True)
            config.read_string(data.decode("utf-8"))
            for section in config.sections():
                if section.lower().startswith(("include", "credential")):
                    raise ValueError()
                for key, value in config.items(section):
                    if (key.lower() in {"extraheader", "sshcommand", "cookiefile", "sslkey", "worktree", "hookspath",
                                       "password", "token", "access_token", "authorization"}
                            or key.lower() in {"url", "pushurl"} and urlsplit(value).username is not None):
                        raise ValueError()
        except (ValueError, configparser.Error, UnicodeError):
            raise Problem(409, "NATIVE_FILES_UNSAFE", "Git configuration contains unsupported credential or external material") from None


class NativeWorkspaceFiles:
    def __init__(self, home: Path):
        self.home = home.absolute()
        self.root = self.home / WORKSPACE_ROOT

    def _stat(self, path, kind):
        self._guard(path)
        value = path.lstat()
        if type(value.st_mode) is not int or type(value.st_nlink) is not int:
            raise Problem(409, "NATIVE_FILES_UNSAFE", "source file types and modes must be known")
        valid = stat.S_ISDIR(value.st_mode) if kind == "directory" else stat.S_ISREG(value.st_mode)
        if (not valid or kind == "file" and value.st_nlink != 1
                or value.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
                or getattr(value, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise Problem(409, "NATIVE_FILES_UNSAFE", "workspace links, hardlinks, special files or unknown modes are unsupported")
        return value

    def _guard(self, path):
        try:
            guard(self.home, path)
        except Problem:
            raise Problem(409, "NATIVE_FILES_UNSAFE", "workspace paths must not traverse a link or leave private HOME") from None

    def _bytes(self, path, *, limit=MAX_FILE_BYTES):
        before = self._stat(path, "file")
        if before.st_size > limit:
            raise Problem(413, "NATIVE_FILES_LIMIT", "one workspace file exceeds its byte bound")
        try:
            data = read(self.home, path, limit, single_link=True)
        except Problem:
            raise Problem(409, "NATIVE_FILES_UNSAFE", "the original file could not be read as one bounded regular file") from None
        after = self._stat(path, "file")
        fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, name) != getattr(after, name) for name in fields) or len(data) != after.st_size:
            raise Problem(409, "NATIVE_FILES_SOURCE_CHANGED", "workspace content changed during the original read")
        return data, stat.S_IMODE(before.st_mode)

    def _entries(self, root):
        root_mode = stat.S_IMODE(self._stat(root, "directory").st_mode)
        entries, todo, total = [], [root], 0
        deadline = monotonic() + 10
        while todo:
            directory = todo.pop()
            before = self._stat(directory, "directory")
            with os.scandir(directory) as children:
                for child in children:
                    if len(entries) >= MAX_FILE_ENTRIES or monotonic() > deadline:
                        raise Problem(413, "NATIVE_FILES_LIMIT", "workspace enumeration exceeds its entry or time bound")
                    path = directory / child.name
                    relative = path.relative_to(root).as_posix()
                    _safe_name(relative)
                    value = path.lstat()
                    if stat.S_ISDIR(value.st_mode):
                        mode = stat.S_IMODE(self._stat(path, "directory").st_mode)
                        entry = NativeFileEntry(path=relative, kind="directory", mode=mode)
                        todo.append(path)
                    else:
                        if PurePosixPath(relative).name == ".git":
                            raise Problem(409, "NATIVE_FILES_UNSAFE", "external Git worktree pointers are unsupported")
                        data, mode = self._bytes(path)
                        _safe_content(relative, data)
                        total += len(data)
                        if total > MAX_TOTAL_BYTES:
                            raise Problem(413, "NATIVE_FILES_LIMIT", "the complete workspace exceeds its byte bound")
                        entry = NativeFileEntry(path=relative, kind="file", mode=mode,
                                                byte_length=len(data), sha256=hashlib.sha256(data).hexdigest())
                    entries.append(entry)
            after = self._stat(directory, "directory")
            if (before.st_ino, before.st_mtime_ns, before.st_mode) != (after.st_ino, after.st_mtime_ns, after.st_mode):
                raise Problem(409, "NATIVE_FILES_SOURCE_CHANGED", "the source directory changed during enumeration")
        return root_mode, tuple(sorted(entries, key=lambda entry: entry.path))

    def snapshot(self, **scope):
        try:
            root_mode, entries = self._entries(self.root)
            manifest = NativeFilesManifest(**scope, root_mode=root_mode, entries=entries)
        except ValueError:
            raise Problem(413, "NATIVE_FILES_LIMIT", "the complete workspace manifest exceeds its bound") from None
        except OSError:
            raise Problem(503, "NATIVE_FILES_UNAVAILABLE", "the original workspace is missing or cannot be read") from None
        return manifest

    def verify(self, manifest):
        try:
            mode, entries = self._entries(self.root)
        except OSError:
            raise Problem(503, "NATIVE_FILES_UNAVAILABLE", "the original workspace cannot be verified") from None
        if (mode, entries) != (manifest.root_mode, manifest.entries):
            raise Problem(409, "NATIVE_FILES_SOURCE_CHANGED", "the original workspace no longer matches its fixed manifest")

    def _transfer(self, manifest):
        return self.home / ".fa-assistant-v12" / "desktop" / ".fa-transfers" / manifest.operation_id

    def staging_path(self, manifest, index):
        return self.home / staging_relative_path(manifest, index)

    def _mkdir(self, path, mode=0o700):
        self._guard(path)
        # `mkdir(parents=True)` creates parents with the default mode; the shell refuses a non-private `desktop/`.
        missing, parent = [], path
        while not parent.exists() and parent.parent != parent:
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            directory.mkdir(mode=mode, exist_ok=True)
        self._stat(path, "directory")

    def initialize(self):
        try:
            self._mkdir(self.root)
        except OSError:
            raise Problem(503, "NATIVE_FILES_UNAVAILABLE", "the new workspace directory could not be initialized") from None

    def prepare(self, manifest):
        try:
            self._guard(self.root)
            if self.root.exists() and any(self.root.iterdir()):
                try:
                    self.verify(manifest)
                except Problem:
                    raise Problem(409, "NATIVE_FILES_CONFLICT", "existing destination workspace content must not be overwritten") from None
            base = self._transfer(manifest)
            self._mkdir(base / "upload")
            self._mkdir(base / "tree")
        except OSError:
            raise Problem(503, "NATIVE_FILES_UNAVAILABLE", "the operation staging directory cannot be prepared") from None

    def confirm(self, manifest, index):
        entry = manifest.entries[index]
        _safe_name(entry.path)
        base = self._transfer(manifest) / "tree"
        path = base / entry.path
        try:
            self._mkdir(path if entry.kind == "directory" else path.parent)
            if entry.kind == "directory":
                return
            data, _ = self._bytes(self.staging_path(manifest, index))
            if len(data) != entry.byte_length or hashlib.sha256(data).hexdigest() != entry.sha256:
                raise Problem(409, "NATIVE_FILES_CONFLICT", "the original staging file does not match its manifest")
            _safe_content(entry.path, data)
            try:
                write(self.home, path, data)
            except FileExistsError:
                previous, _ = self._bytes(path)
                if previous != data:
                    raise Problem(409, "NATIVE_FILES_CONFLICT", "a different staged workspace file must not be overwritten") from None
            os.chmod(path, entry.mode)
        except OSError:
            raise Problem(503, "NATIVE_FILES_UNKNOWN", "the original staging file has not been confirmed; do not upload again") from None

    def published(self, manifest):
        self._guard(self.root)
        if not self.root.exists():
            return False
        self.verify(manifest)
        return True

    def publish(self, manifest):
        base = self._transfer(manifest) / "tree"
        try:
            self._guard(self.root)
            if self.root.exists():
                self._stat(self.root, "directory")
                if not base.exists() or any(self.root.iterdir()):
                    if self.published(manifest):
                        return
            for entry in reversed(manifest.entries):
                if entry.kind == "directory":
                    path = base / entry.path
                    self._stat(path, "directory")
                    os.chmod(path, entry.mode)
            os.chmod(base, manifest.root_mode)
            mode, entries = self._entries(base)
            if (mode, entries) != (manifest.root_mode, manifest.entries):
                raise Problem(409, "NATIVE_FILES_CONFLICT", "the complete staged workspace differs from its original manifest")
            # Uploaded inodes never become live files: a late PUT stays in upload/, not this tree.
            os.rename(base, self.root)
            self.verify(manifest)
        except OSError:
            raise Problem(503, "NATIVE_FILES_UNKNOWN", "workspace publication is unconfirmed; read the original operation") from None
