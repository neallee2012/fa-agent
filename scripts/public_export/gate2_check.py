#!/usr/bin/env python3
"""Gate 2 after public-sync: the published tree must be the one gate 1 fingerprinted, and must pass verify.

Usage:
  python gate2_check.py --tag v<version> --mapping <mapping.json> --src <private checkout> [--rev <commit>]
                        (--expected <tree SHA> | --expected-file <release PR body>)
                        [--public-repo <owner/name> | --public-dir <clone>] [--baseline <plain export>]
                        (--gitleaks <bin> | --no-gitleaks)

Checks, all of them run even after one fails:
  A. the public tag's tree SHA equals the fingerprint gate 1 recorded (`public-sync fingerprint: <sha>` in the
     release PR body, or --expected);
  B. it also equals the fingerprint of the deployed private commit (--src/--rev, with the mapping.json stored
     at --rev, as public-sync used it), which tells "main moved after gate 1" apart from "the public repo
     holds something else";
  C. verify_export.py's checks [0]-[4] on the public tag's files, against a plain export of --rev (or
     --baseline), with --mapping: it may be newer than --rev's, so rules added later apply too. gitleaks is
     required; --no-gitleaks is for a local rerun only (public-sync always passes --gitleaks), and the report
     must then say that [1] did not run.
A and B compare git tree SHAs, and C reads the files from git objects, not from the clone's worktree, so a
clone made with core.autocrlf (Windows) cannot produce false differences.
Exit 0 only when all pass; 1 otherwise, 2 when the public tag cannot be read: a clone without the tag fails,
its HEAD is never used instead. The messages name tree SHAs, check numbers and file:line only; verify_export
prints no original value.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_export  # noqa: E402
from fingerprint import commit_fingerprint, parse_fingerprint  # noqa: E402
from scrub_export import head_files, render  # noqa: E402

SHA_RE = re.compile(r"[0-9a-f]{40}")
REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+")
TAG_RE = re.compile(r"v[0-9A-Za-z][0-9A-Za-z._-]{0,63}")


def run_git(*args: str) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    return subprocess.run(["git", "-c", "core.autocrlf=false", *args], check=True, capture_output=True,
                          env=env).stdout.decode("utf-8").strip()


def clone_public(repo: str, tag: str, dest: Path) -> None:
    """Shallow, unauthenticated clone of the public tag (process boundary; tests replace it)."""
    run_git("clone", "-q", "--depth", "1", "--branch", tag, "--no-tags", f"https://github.com/{repo}.git", str(dest))


def public_tree(clone: Path, tag: str) -> str:
    """The tree of the tag itself; raises CalledProcessError when the clone does not have it."""
    return run_git("-C", str(clone), "rev-parse", "--verify", "--end-of-options", f"refs/tags/{tag}^{{tree}}")


def published_files(clone: Path, tree: str, dest: Path) -> None:
    """Write the published tree's blobs, byte for byte, to dest. Read from git objects rather than the clone's
    worktree: a Windows clone with core.autocrlf turns LF into CRLF there and makes verify compare other bytes.
    Nothing is excluded, so an excluded path that reached the public tree still fails verify [0]."""
    for rel, (_, data) in head_files(clone, tree, exclude=False).items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def write_baseline(src: Path, rev: str, mapping: dict, dest: Path) -> None:
    files, _, _ = render(src, mapping, plain=True, rev=rev)
    for rel, (_, data) in files.items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def expected_value(args: argparse.Namespace) -> str | None:
    if args.expected:
        return args.expected.strip().lower()
    text = Path(args.expected_file).read_text(encoding="utf-8", errors="replace")
    return parse_fingerprint(text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tag", required=True, help="public tag, v<release.json version>")
    ap.add_argument("--mapping", required=True)
    ap.add_argument("--src", required=True, help="private checkout holding the deployed commit")
    ap.add_argument("--rev", default="HEAD", help="deployed private commit (default HEAD of --src)")
    exp = ap.add_mutually_exclusive_group(required=True)
    exp.add_argument("--expected", help="fingerprint gate 1 recorded (40-hex tree SHA)")
    exp.add_argument("--expected-file", help="text holding a `public-sync fingerprint: <sha>` line")
    pub = ap.add_mutually_exclusive_group(required=True)
    pub.add_argument("--public-repo", help="owner/name; the tag is cloned shallow and unauthenticated")
    pub.add_argument("--public-dir", help="an existing clone with the tag checked out")
    ap.add_argument("--baseline", help="plain export of --rev (default: built here)")
    leaks = ap.add_mutually_exclusive_group(required=True)
    leaks.add_argument("--gitleaks", help="gitleaks binary for verify check [1]")
    leaks.add_argument("--no-gitleaks", action="store_true",
                       help="local rerun only: skip check [1] and say so in the report (public-sync never does)")
    args = ap.parse_args(argv)
    if not TAG_RE.fullmatch(args.tag):
        ap.error("--tag must look like v<version>")
    if args.public_repo and not REPO_RE.fullmatch(args.public_repo):
        ap.error("--public-repo must be owner/name")

    src = Path(args.src).resolve()
    mapping = json.loads(Path(args.mapping).read_text(encoding="utf-8"))
    expected = expected_value(args)
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        clone = Path(args.public_dir).resolve() if args.public_dir else tmp / "public"
        try:
            if args.public_repo:
                clone_public(args.public_repo, args.tag, clone)
            actual = public_tree(clone, args.tag)
        except (subprocess.CalledProcessError, OSError):
            print(f"gate 2: cannot read {args.tag} in the public repo (clone failed or the tag is missing); "
                  "check that the tag was pushed, then rerun")
            return 2
        print(f"public {args.tag} tree: {actual}")

        # A. gate 1's record
        if not expected or not SHA_RE.fullmatch(expected):
            failures.append("[A] gate 1 left no usable fingerprint (the release PR needs one `public-sync "
                            "fingerprint: <tree SHA>` line), so nothing to compare: review the public commit by hand")
        elif expected != actual:
            failures.append(f"[A] fingerprint mismatch: gate 1 recorded {expected}, the public tree is {actual}; "
                            "what was published is not what gate 1 reviewed")
        else:
            print(f"[A] fingerprint matches: {expected}")

        # B. the deployed commit
        # The mapping stored at --rev is what public-sync used; --mapping (maybe newer) only drives verify.
        deployed = commit_fingerprint(src, args.rev)
        if deployed != actual:
            failures.append(f"[B] the deployed commit exports to {deployed}, not the public {actual}: the public "
                            "repo holds something this export did not produce")
        else:
            print(f"[B] the deployed commit exports to the public tree: {deployed}")
            if expected and SHA_RE.fullmatch(expected) and expected != actual:
                failures.append("[B] main changed after gate 1 and that was deployed: review by hand what was "
                                "added or changed after gate 1")

        # C. verify on the published files
        files_dir = tmp / "public-files"
        published_files(clone, actual, files_dir)
        if args.baseline:
            baseline = Path(args.baseline).resolve()
        else:
            baseline = tmp / "baseline"
            write_baseline(src, args.rev, mapping, baseline)
        if args.no_gitleaks:
            print("note: --no-gitleaks skips gitleaks (local rerun only; the report must say [1] did not run)")
        ok = verify_export.verify_tree(files_dir, baseline, mapping, None if args.no_gitleaks else args.gitleaks)
        if not ok:
            failures.append("[C] verify_export failed on the public files (see [0]-[4] above, file:line only)")
        else:
            print("[C] verify_export passes on the public files")

    if failures:
        print("gate 2: FAIL. Stop further releases and follow docs/public-sync.md 「第二道發現洩漏」.")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("gate 2: PASS, the public tree is the one gate 1 reviewed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
