"""deploy-staging's release gate (deploy/stage/release_gate.py) with GraphQL replies built in memory; no GitHub call."""

import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
BODY = "release 30\n\npublic-sync fingerprint: " + "b" * 40 + "\n"


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_file_location("release_gate", ROOT / "deploy" / "stage" / "release_gate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_gate"] = module
    spec.loader.exec_module(module)
    return module


def reply(*prs):
    return {"data": {"repository": {"object": {"associatedPullRequests": {"nodes": list(prs)}}}}}


def pr(number=900, merge=SHA, body=BODY, merged="2026-10-16T01:00:00Z", edited=None,
       files=("deploy/stage/release.json",), total=None):
    return {"number": number, "mergedAt": merged, "lastEditedAt": edited, "mergeCommit": {"oid": merge}, "body": body,
            "files": {"totalCount": len(files) if total is None else total, "nodes": [{"path": p} for p in files]}}


def test_the_merge_of_a_fingerprinted_release_pr_passes(gate):
    assert gate.refusal(reply(pr()), SHA) is None
    assert gate.refusal(reply(pr()), SHA, "900") is None
    assert gate.refusal(reply(pr()), SHA, "#900") is None
    assert gate.refusal(reply(pr(edited="2026-10-16T00:59:00Z")), SHA) is None  # edited before the merge


@pytest.mark.parametrize("data, named, reason", [
    (reply(), "", "not the merge commit of any PR"),                         # a push that no PR merged
    (reply(pr(merge="c" * 40)), "", "not the merge commit of any PR"),       # newer main: an older PR's commit
    (reply(pr(merged=None)), "", "not the merge commit of any PR"),          # open, not merged
    (reply(pr(), pr(number=901)), "", "2 merged PRs"),
    (reply(pr()), "899", "#899 was named"),                                  # a dispatch naming another PR
    (reply(pr(edited="2026-10-16T02:00:00Z")), "", "edited after it was merged"),
    (reply(pr(body="docs: fix a typo")), "", "not a release PR"),            # an ordinary PR touching release.json
    (reply(pr(body=BODY + "public-sync fingerprint: " + "d" * 40)), "", "not a release PR"),  # two fingerprints
    # A docs PR that quotes a release fingerprint is not a release PR: it did not change release.json.
    (reply(pr(files=("docs/progress.md",))), "", "did not change deploy/stage/release.json"),
    (reply(pr(files=())), "", "did not change deploy/stage/release.json"),
    (reply(pr(files=("docs/progress.md",), total=150)), "", "only its first 1 of 150 files were read"),
    ({"data": {"repository": {"object": None}}}, "", "not the merge commit of any PR"),
])
def test_anything_else_is_refused(gate, data, named, reason):
    assert reason in gate.refusal(data, SHA, named)


def test_main_annotates_the_refusal_and_fails(gate, tmp_path, capsys):
    path = tmp_path / "reply.json"
    path.write_text(json.dumps(reply(pr(body=""))), encoding="utf-8")
    assert gate.main([str(path), SHA, "900"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("::error::release gate: #900 is not a release PR") and "nothing was deployed" in out
    path.write_text(json.dumps(reply(pr())), encoding="utf-8")
    assert gate.main([str(path), SHA.upper()]) == 0


def test_the_workflow_runs_the_gate_before_the_azure_login():
    workflow = ROOT / ".github" / "workflows" / "deploy-staging.yml"
    if not workflow.exists():
        pytest.skip("no .github/workflows in this copy")
    text = workflow.read_text(encoding="utf-8")
    deploy = text[text.index("\n  deploy:\n"):]
    assert "needs: [v1-wire-snapshot, release-gate]" in deploy and "release_gate.py" not in deploy
    gate_job = text[text.index("\n  release-gate:\n"):text.index("\n  deploy:\n")]
    assert "id-token" not in gate_job and "pull-requests: read" in gate_job
    assert 'release_gate.py "$RUNNER_TEMP/release-pr.json" "$GITHUB_SHA"' in gate_job
    assert "files(first:100){totalCount nodes{path}}" in gate_job
    assert "release_pr:" in text and "required: true" in text
