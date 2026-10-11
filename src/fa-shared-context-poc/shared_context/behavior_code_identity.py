"""Installed source identity shared by API and Hosted without API dependencies."""

import hashlib
import json
from pathlib import Path


def code_sha256():
    directory = Path(__file__).resolve().parent
    files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(directory.glob("*.py"))}
    raw = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
