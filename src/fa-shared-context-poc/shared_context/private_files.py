from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .errors import Problem


def guard(home: Path, path: Path) -> None:
    if not path.is_relative_to(home):
        raise Problem(422, "INVALID_ENVIRONMENT_PATH", "private state must stay under platform HOME")
    for node in (path, *path.parents):
        if node.is_symlink() or getattr(node, "is_junction", lambda: False)():
            raise Problem(409, "UNSAFE_ENVIRONMENT_PATH", "symlinks and junctions are not accepted for private state")
        if node == home:
            break


def read(home: Path, path: Path, limit: int = 8192, *, single_link: bool = False) -> bytes:
    guard(home, path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or single_link and metadata.st_nlink != 1:
            raise Problem(409, "INVALID_ENVIRONMENT_STATE", "private state must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
    finally:
        os.close(descriptor)
    if len(data) > limit:
        raise Problem(413, "ENVIRONMENT_STATE_TOO_LARGE", "private fixture exceeds its bounded size")
    return data


def write(home: Path, path: Path, data: bytes) -> None:
    guard(home, path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def load_json(data: bytes) -> dict:
    try:
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("expected object")
        return value
    except (ValueError, UnicodeDecodeError) as exc:
        raise Problem(409, "INVALID_ENVIRONMENT_STATE", "private fixture contains invalid JSON") from exc
