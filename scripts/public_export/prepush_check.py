#!/usr/bin/env python3
"""Before public-sync pushes: the tree about to be published must be the one gate 1 fingerprinted.

Usage:
  python prepush_check.py --body <release PR body as merged> --tree <tree SHA of the public commit to be made>

public-sync runs this after `git add -A` in the public clone and before it commits, tags or pushes anything. Exit 0
only when the body holds exactly one `public-sync fingerprint: <tree SHA>` line and it equals --tree; otherwise it
prints one `::error::` annotation saying why and exits 1, so nothing is published. An empty body file means the
release PR could not be used (a manual deploy with no release PR, a PR edited after the merge, several merged PRs or
a GitHub API failure; the step that read it says which). Prints tree SHAs only.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fingerprint import parse_fingerprint  # noqa: E402

SHA_RE = re.compile(r"[0-9a-f]{40}")


def refusal(body: str, tree: str) -> str | None:
    """None when `tree` is the tree gate 1 recorded in `body`; otherwise why it must not be published."""
    expected = parse_fingerprint(body)
    if expected is None:
        return ("no usable gate 1 fingerprint (the release PR as merged needs exactly one `public-sync fingerprint: "
                "<tree SHA>` line), so nothing was pushed. Run gate 1 on this commit and publish it through a release "
                "PR that records the fingerprint")
    if expected != tree:
        return (f"fingerprint mismatch: gate 1 recorded {expected}, this export is {tree} (main changed after gate 1, "
                "or a deploy without that release PR), so nothing was pushed. Run gate 1 again on the deployed commit "
                "and publish it through a new release")
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--body", required=True, help="the release PR body as merged (empty when unusable)")
    ap.add_argument("--tree", required=True, help="tree SHA of the public commit about to be made (git write-tree)")
    args = ap.parse_args(argv)
    tree = args.tree.strip().lower()
    if not SHA_RE.fullmatch(tree):
        ap.error("--tree must be a 40-hex tree SHA")
    reason = refusal(Path(args.body).read_text(encoding="utf-8", errors="replace"), tree)
    if reason:
        print(f"::error::public-sync: {reason}")
        return 1
    print(f"gate 1 fingerprint matches the export: {tree}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
