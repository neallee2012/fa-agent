#!/usr/bin/env python3
"""deploy-staging's release gate: deploy only a commit that a merged stage release PR made (ADR-0008 §2).

Usage:
  python release_gate.py <associatedPullRequests GraphQL JSON> <deployed commit SHA> [<release PR number>]

A stage deploy needs Neal's approval, and merging the release PR is that approval. So the commit to deploy must be
the merge commit of exactly one merged release PR: it changed deploy/stage/release.json (ADR-0008) and its body,
as merged (not edited afterwards), holds gate 1's `public-sync fingerprint:` line (docs/public-sync.md). A manual
dispatch names that PR, and it must be the same one. Exit 0 when it holds; otherwise print one `::error::` annotation
and exit 1, before any Azure login. Who merged is not checked: this GitHub plan has no required reviewers for private
repos, so the gate proves "a release PR merged as this commit", not which person merged it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "public_export"))
from fingerprint import parse_fingerprint  # noqa: E402

RELEASE_FILE = "deploy/stage/release.json"


def refusal(data: dict, sha: str, named: str = "") -> str | None:
    """None when `sha` is the merge commit of one fingerprinted release PR (and the one `named`, if any)."""
    commit = ((data.get("data") or {}).get("repository") or {}).get("object") or {}
    nodes = (commit.get("associatedPullRequests") or {}).get("nodes") or []
    prs = [n for n in nodes if n.get("mergedAt") and (n.get("mergeCommit") or {}).get("oid") == sha]
    if not prs:
        return (f"{sha} is not the merge commit of any PR. A stage deploy is the merge of a release PR; to deploy "
                "newer main, open a new release PR")
    if len(prs) > 1:
        return f"{len(prs)} merged PRs have {sha} as their merge commit; which release it is is unclear"
    pr = prs[0]
    if named and str(pr["number"]) != named.strip().lstrip("#"):
        return f"release PR #{named} was named, but {sha} is the merge commit of #{pr['number']}"
    if pr.get("lastEditedAt") and pr["lastEditedAt"] > pr["mergedAt"]:
        return f"#{pr['number']} was edited after it was merged; its body as merged cannot be confirmed"
    files = pr.get("files") or {}
    paths = [n.get("path") for n in files.get("nodes") or []]
    if RELEASE_FILE not in paths:
        # Fail closed past the first page; a release PR changes only release.json.
        unread = files.get("totalCount", 0) > len(paths)
        return (f"#{pr['number']} is not a release PR: it did not change {RELEASE_FILE}"
                + (f" (only its first {len(paths)} of {files['totalCount']} files were read)" if unread else ""))
    if parse_fingerprint(pr.get("body") or "") is None:
        return (f"#{pr['number']} is not a release PR: its body has no single `public-sync fingerprint:` line "
                "(gate 1, docs/public-sync.md)")
    return None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (2, 3):
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    data = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    reason = refusal(data, args[1].strip().lower(), args[2] if len(args) == 3 else "")
    if reason:
        print(f"::error::release gate: {reason}; nothing was deployed")
        return 1
    print(f"release gate: {args[1]} is the merge commit of a fingerprinted release PR")
    return 0


if __name__ == "__main__":
    sys.exit(main())
