#!/usr/bin/env python3
"""Print the git tree SHA the public commit will have for a private commit (the public-sync fingerprint).

Usage:
  python fingerprint.py --src <checkout> [--rev <commit>] [--mapping <mapping.json>]
  python fingerprint.py --dir <exported tree>

public-sync replaces the public clone's whole tree with scrub_export.py's export and commits it, so the public
commit's tree SHA is a function of the exported files alone: paths, bytes and the executable bit (the repo has
no .gitattributes, and the push step runs `git add -A` with git's defaults on Linux). --rev renders that export
in memory with scrub_export.render() and the mapping.json stored at that commit (as the workflow does: it runs
the tools of the deployed commit) and builds the tree with git plumbing in a throwaway repository; nothing is
written to --src. --dir hashes an export already on disk (on Windows the executable bit cannot be read, so use
--rev there).

Gate 1 records the printed line in the release PR; gate2_check.py compares it with the published commit's tree.
The output is the tree SHA only, never file contents.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrub_export import git, render  # noqa: E402

MAPPING_PATH = "scripts/public_export/mapping.json"
LINE_PREFIX = "public-sync fingerprint:"
LINE_RE = re.compile(r"public-sync fingerprint:\s*`?([0-9a-f]{40})`?", re.I)


def blob_sha(data: bytes) -> str:
    """git's object id of a blob (SHA-1 object format), the same as `git hash-object --no-filters`."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def git_mode(mode: str) -> str:
    """The mode `git add` records for an exported file: export() writes regular files and keeps only the
    executable bit, so a symlink or any other mode becomes a plain 100644 file."""
    return "100755" if mode == "100755" else "100644"


def tree_sha(entries: dict[str, tuple[str, bytes]]) -> str:
    """Tree SHA for {path: (mode, bytes)}, built with update-index + write-tree in a temporary repository."""
    if not entries:
        raise SystemExit("no files to fingerprint")
    index_info = b"".join(f"{git_mode(mode)} {blob_sha(data)}\t{rel}".encode("utf-8") + b"\0"
                          for rel, (mode, data) in sorted(entries.items()))
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "fingerprint.git"
        env = {**os.environ, "GIT_DIR": str(repo), "GIT_INDEX_FILE": str(Path(td) / "index"),
               "GIT_OPTIONAL_LOCKS": "0"}
        env.pop("GIT_WORK_TREE", None)

        def run(*args: str, stdin: bytes | None = None) -> str:
            return subprocess.run(["git", *args], check=True, capture_output=True, input=stdin,
                                  env=env).stdout.decode("ascii").strip()

        run("init", "-q", "--bare", str(repo))
        run("update-index", "-z", "--add", "--index-info", stdin=index_info)
        # Only the tree object is needed; the blob ids are computed above, so the blobs need not exist.
        return run("write-tree", "--missing-ok")


def dir_entries(root: Path) -> dict[str, tuple[str, bytes]]:
    """{path: (mode, bytes)} for the regular files under root, skipping any .git directory."""
    entries = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if ".git" in rel.parts or not p.is_file():
            continue
        mode = "100755" if os.name != "nt" and os.access(p, os.X_OK) else "100644"
        entries[rel.as_posix()] = (mode, p.read_bytes())
    return entries


def mapping_at(src: Path, rev: str) -> dict:
    return json.loads(git(src, "show", f"{rev}:{MAPPING_PATH}").decode("utf-8"))


def commit_fingerprint(src: Path, rev: str = "HEAD", mapping: dict | None = None) -> str:
    """The tree SHA public-sync will publish for `rev` of the checkout at src."""
    files, _, _ = render(src, mapping if mapping is not None else mapping_at(src, rev), plain=False, rev=rev)
    return tree_sha(files)


def tool_drift(src: Path, rev: str) -> list[str]:
    """Tool files whose bytes at `rev` differ from the ones running now (the fingerprint uses the running ones)."""
    here = Path(__file__).resolve().parent
    drift = []
    for name in ("scrub_export.py",):
        try:
            at_rev = git(src, "show", f"{rev}:scripts/public_export/{name}")
        except subprocess.CalledProcessError:
            drift.append(name)
            continue
        if at_rev.replace(b"\r\n", b"\n") != (here / name).read_bytes().replace(b"\r\n", b"\n"):
            drift.append(name)
    return drift


def parse_fingerprint(text: str) -> str | None:
    """The tree SHA from a `public-sync fingerprint: <sha>` line (e.g. in a release PR body), or None.
    More than one distinct value counts as none, so an ambiguous record is never picked silently."""
    found = {m.group(1).lower() for m in LINE_RE.finditer(text)}
    return found.pop() if len(found) == 1 else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", help="private checkout holding --rev")
    ap.add_argument("--rev", default="HEAD", help="private commit to fingerprint (default HEAD)")
    ap.add_argument("--mapping", help="mapping.json to use instead of the one stored at --rev")
    ap.add_argument("--dir", help="an export already on disk (scrub_export.py --out)")
    args = ap.parse_args(argv)
    if bool(args.src) == bool(args.dir):
        ap.error("use either --src [--rev] or --dir")
    if args.dir:
        sha = tree_sha(dir_entries(Path(args.dir).resolve()))
    else:
        src = Path(args.src).resolve()
        drift = tool_drift(src, args.rev)
        if drift:
            print(f"warning: {', '.join(drift)} differs from the copy at {args.rev}; the fingerprint uses the "
                  "running copy and may differ from what public-sync computes with that commit's tools",
                  file=sys.stderr)
        mapping = json.loads(Path(args.mapping).read_text(encoding="utf-8")) if args.mapping else None
        sha = commit_fingerprint(src, args.rev, mapping)
    print(f"{LINE_PREFIX} {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
