"""Legacy inline checkpoint reader/writer; new instruction state uses SessionFs.

Only the isolated profile's pre-SDK-filtered conversation and workspace metadata
are exported after graceful SDK shutdown. No environment, auth, tools, scripts,
symlinks or home directory are included. Compressed bytes are not a secret filter.
Keep these bytes and limits unchanged for existing checkpoints and legacy work.
"""

import hashlib
import json
import zlib
from pathlib import Path

from .behavior_assistant_wire import SdkArchive
from .errors import Problem
from .behavior_checkpoint_privacy import checkpoint_events, public_events

MAX_RAW = 128 * 1024
FILES = ("events.jsonl", "workspace.yaml")


def unpack(archive: SdkArchive):
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(archive.data, MAX_RAW + 1)
        if (len(raw) > MAX_RAW or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
                or hashlib.sha256(raw).hexdigest() != archive.sha256):
            raise ValueError()
        files = json.loads(raw)
        if not isinstance(files, dict) or set(files) != set(FILES) or any(not isinstance(v, str) for v in files.values()):
            raise ValueError()
        events = checkpoint_events(files["events.jsonl"])
        if not events or any(not isinstance(e, dict) or not isinstance(e.get("type"), str) for e in events):
            raise ValueError()
        starts = [e for e in events if e["type"] == "session.start"]
        if len(starts) != 1 or starts[0]["data"].get("sessionId", starts[0]["data"].get("session_id")) != archive.sdk_session_id:
            raise ValueError()
        return files
    except Exception:
        raise Problem(403, "ASSISTANT_CHECKPOINT_INVALID", "bounded exact SDK checkpoint is invalid") from None


def export(sdk_root: Path, session_id: str, *, public_only=False):
    directory = sdk_root / "config" / "session-state" / session_id
    files = {}
    for name in FILES:
        path = directory / name
        if path.is_symlink() or path.stat().st_size > MAX_RAW:
            raise Problem(403, "ASSISTANT_CHECKPOINT_INVALID", "checkpoint file is unsafe or exceeds its bound")
        files[name] = path.read_text(encoding="utf-8")
    if public_only:
        events,_=public_events(files["events.jsonl"])
        files["events.jsonl"]="".join(json.dumps(event,ensure_ascii=False)+"\n" for event in events)
        # This directory belongs solely to the stopped SDK for the current work.
        (directory/"events.jsonl").write_text(files["events.jsonl"],encoding="utf-8",newline="")
    raw = json.dumps(files, ensure_ascii=False, sort_keys=True).encode()
    if len(raw) > MAX_RAW:
        raise Problem(413, "ASSISTANT_CHECKPOINT_LIMIT", "complete checkpoint exceeds its bound")
    archive = SdkArchive(sdk_session_id=session_id, data=zlib.compress(raw), sha256=hashlib.sha256(raw).hexdigest())
    unpack(archive)
    return archive


def restore(archive: SdkArchive, sdk_root: Path):
    files = unpack(archive)
    directory = sdk_root / "config" / "session-state" / archive.sdk_session_id
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, text in files.items():
        path = directory / name
        # Never resume from unexpected/private RAM or an unverified local file.
        with path.open("x", encoding="utf-8", newline="") as output:
            output.write(text)
