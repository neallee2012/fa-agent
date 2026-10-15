"""Offline contracts for the public-sync gate tools: fingerprint.py, gate2_check.py and inventory_scan.py.

No network, no Azure and no real gitleaks. Fixture values are assembled at runtime so this file never trips the
export's residual sweep.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "scripts" / "public_export"
sys.path.insert(0, str(TOOLS))

import fingerprint  # noqa: E402
import gate2_check  # noqa: E402
import inventory_scan  # noqa: E402
import prepush_check  # noqa: E402
import scrub_export  # noqa: E402

SECRET_NAME = "Zephyr" + "Quill"          # an "original" value the export must replace
PLACEHOLDER_NAME = "example" + "name"
MAPPING = {
    "guids": {},
    "guid_prefix_scrub": False,
    "truncated_prefixes": [],
    "keep_guids": {},
    "synthetic_guid_sha256": [],
    "rules": [{"id": "fixture-name", "category": "resource-name", "type": "literal", "find": SECRET_NAME,
               "replace": PLACEHOLDER_NAME, "ignore_case": True, "bounded": False}],
    "gitleaks_known_false_positives": [],
    "residual_allow": [],
}
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "core.autocrlf=false", *args], check=True,
                          capture_output=True, env=GIT_ENV).stdout.decode().strip()


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def private(tmp_path: Path) -> Path:
    repo = tmp_path / "private"
    repo.mkdir()
    git(repo, "init", "-q")
    write(repo, "README.md", f"# demo\nbuilt by {SECRET_NAME} team\n")
    write(repo, "docs/notes.md", "plain notes, nothing to replace\n")
    write(repo, "docs/中文.md", "非 ASCII 路徑\n")
    write(repo, fingerprint.MAPPING_PATH, json.dumps(MAPPING))
    write(repo, ".github/workflows/x.yml", "on: push\n")
    (repo / "bin.dat").write_bytes(b"\x00\x01binary")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    return repo


def publish(private: Path, public: Path, tag: str) -> str:
    """What public-sync's push step does: export, replace the clone's tree, git add -A, commit, tag."""
    export = public.parent / "export"
    scrub_export.export(private, export, MAPPING, plain=False)
    public.mkdir()
    git(public, "init", "-q")
    for p in export.rglob("*"):
        if p.is_file():
            dest = public / p.relative_to(export)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(p.read_bytes())
    git(public, "add", "-A")
    git(public, "commit", "-q", "-m", f"Release {tag[1:]}")
    git(public, "tag", tag)
    return git(public, "rev-parse", "HEAD^{tree}")


# --- fingerprint -------------------------------------------------------------------------------------------


def test_fingerprint_equals_the_tree_public_sync_commits(private: Path, tmp_path: Path):
    published = publish(private, tmp_path / "public", "v1")
    assert fingerprint.commit_fingerprint(private, "HEAD") == published
    assert fingerprint.tree_sha(fingerprint.dir_entries(tmp_path / "export")) == published
    assert fingerprint.tree_sha(fingerprint.dir_entries(tmp_path / "public")) == published  # .git skipped


def test_fingerprint_reads_the_mapping_and_files_of_the_given_commit(private: Path):
    first = fingerprint.commit_fingerprint(private, "HEAD")
    write(private, "docs/notes.md", "changed\n")
    git(private, "commit", "-qam", "change")
    second = fingerprint.commit_fingerprint(private, "HEAD")
    assert fingerprint.commit_fingerprint(private, "HEAD~1") == first and second != first
    # The worktree is not read: an uncommitted edit changes nothing.
    write(private, "docs/notes.md", "uncommitted\n")
    assert fingerprint.commit_fingerprint(private, "HEAD") == second


def test_fingerprint_keeps_only_the_executable_bit():
    data = {"a.sh": ("100755", b"echo\n")}
    assert fingerprint.git_mode("120000") == fingerprint.git_mode("100644") == "100644"
    assert fingerprint.tree_sha(data) != fingerprint.tree_sha({"a.sh": ("100644", b"echo\n")})
    assert fingerprint.blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"  # git's empty blob


def test_fingerprint_cli_prints_one_line(private: Path, capsys):
    assert fingerprint.main(["--src", str(private)]) == 0
    out = capsys.readouterr().out.strip()
    assert fingerprint.parse_fingerprint(out) == fingerprint.commit_fingerprint(private)
    assert out.startswith(fingerprint.LINE_PREFIX) and SECRET_NAME.lower() not in out.lower()


def test_parse_fingerprint_needs_exactly_one_value():
    a, b = "a" * 40, "b" * 40
    assert fingerprint.parse_fingerprint(f"intro\npublic-sync fingerprint: `{a}`\n") == a
    assert fingerprint.parse_fingerprint(f"Public-Sync Fingerprint: {a.upper()}") == a
    assert fingerprint.parse_fingerprint(f"public-sync fingerprint: {a}\npublic-sync fingerprint: {a}") == a
    assert fingerprint.parse_fingerprint(f"public-sync fingerprint: {a}\npublic-sync fingerprint: {b}") is None
    assert fingerprint.parse_fingerprint("no record here") is None


# --- gate 2 ------------------------------------------------------------------------------------------------


def gate2(private: Path, public: Path, tmp_path: Path, *extra: str) -> int:
    mapping = tmp_path / "mapping.json"
    mapping.write_text(json.dumps(MAPPING), encoding="utf-8")
    return gate2_check.main(["--tag", "v1", "--mapping", str(mapping), "--src", str(private),
                             "--public-dir", str(public), "--no-gitleaks", *extra])


def test_gate2_passes_when_the_public_tree_is_the_fingerprinted_one(private: Path, tmp_path: Path, capsys):
    expected = publish(private, tmp_path / "public", "v1")
    body = tmp_path / "body.md"
    body.write_text(f"release notes\n\npublic-sync fingerprint: {expected}\n", encoding="utf-8")
    assert gate2(private, tmp_path / "public", tmp_path, "--expected-file", str(body)) == 0
    out = capsys.readouterr().out
    assert "[A] fingerprint matches" in out and "gate 2: PASS" in out and "[1] gitleaks: SKIPPED" in out


def test_gate2_ignores_crlf_in_the_clone_worktree(private: Path, tmp_path: Path, capsys):
    """A Windows clone with core.autocrlf rewrites LF as CRLF in the worktree; gate 2 reads git objects."""
    public = tmp_path / "public"
    expected = publish(private, public, "v1")
    for p in public.rglob("*.md"):
        if ".git" not in p.parts:
            p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))
    assert gate2(private, public, tmp_path, "--expected", expected) == 0
    assert "gate 2: PASS" in capsys.readouterr().out


def test_gate2_flags_an_excluded_path_in_the_public_tree(private: Path, tmp_path: Path, capsys):
    public = tmp_path / "public"
    expected = publish(private, public, "v1")
    write(public, fingerprint.MAPPING_PATH, "{}\n")
    git(public, "add", "-A")
    git(public, "commit", "-qm", "add an excluded file")
    git(public, "tag", "-f", "v1")
    assert gate2(private, public, tmp_path, "--expected", expected) == 1
    assert f"UNEXPECTED {fingerprint.MAPPING_PATH}" in capsys.readouterr().out


def test_gate2_fails_on_a_fingerprint_mismatch(private: Path, tmp_path: Path, capsys):
    publish(private, tmp_path / "public", "v1")
    assert gate2(private, tmp_path / "public", tmp_path, "--expected", "c" * 40) == 1
    out = capsys.readouterr().out
    assert "[A] fingerprint mismatch" in out and "main changed after gate 1" in out


def test_gate2_fails_without_a_recorded_fingerprint(private: Path, tmp_path: Path, capsys):
    publish(private, tmp_path / "public", "v1")
    body = tmp_path / "body.md"
    body.write_text("a release PR body that forgot the line\n", encoding="utf-8")
    assert gate2(private, tmp_path / "public", tmp_path, "--expected-file", str(body)) == 1
    assert "left no usable fingerprint" in capsys.readouterr().out


def test_gate2_fails_when_the_public_copy_leaks_and_never_prints_the_value(private: Path, tmp_path: Path, capsys):
    public = tmp_path / "public"
    expected = publish(private, public, "v1")
    # Someone pushed a different tree under the tag: the original name is back in the README.
    write(public, "README.md", f"# demo\nbuilt by {SECRET_NAME} team\n")
    git(public, "commit", "-qam", "edit")
    git(public, "tag", "-f", "v1")
    assert gate2(private, public, tmp_path, "--expected", expected) == 1
    out = capsys.readouterr().out
    assert "[A] fingerprint mismatch" in out and "[B] the deployed commit exports to" in out
    assert "holds something this export did not produce" in out and "[C] verify_export failed" in out
    assert "rule fixture-name at README.md:2" in out
    assert SECRET_NAME.lower() not in out.lower()


def test_gate2_reports_an_unreadable_public_tag(private: Path, tmp_path: Path, capsys):
    (tmp_path / "empty").mkdir()
    git(tmp_path / "empty", "init", "-q")  # a clone without the tag or any commit
    assert gate2(private, tmp_path / "empty", tmp_path, "--expected", "a" * 40) == 2
    assert "cannot read v1 in the public repo" in capsys.readouterr().out


def test_gate2_fingerprints_the_deployed_commit_with_the_mapping_stored_at_rev(private: Path, tmp_path: Path,
                                                                                capsys):
    """--mapping comes from the current main and may hold a rule added after the release. [B] must rebuild the
    deployed commit with the mapping stored at --rev, as public-sync did; only [C] uses --mapping."""
    publish(private, tmp_path / "public", "v1")
    newer = {**MAPPING, "rules": MAPPING["rules"] + [
        {"id": "added-later", "category": "resource-name", "type": "literal", "find": "plain notes",
         "replace": "plain words", "ignore_case": True, "bounded": False}]}
    released = fingerprint.commit_fingerprint(private, "HEAD")
    assert fingerprint.commit_fingerprint(private, "HEAD", newer) != released  # the two mappings disagree
    mapping = tmp_path / "newer-mapping.json"
    mapping.write_text(json.dumps(newer), encoding="utf-8")
    rc = gate2_check.main(["--tag", "v1", "--mapping", str(mapping), "--src", str(private),
                           "--public-dir", str(tmp_path / "public"), "--no-gitleaks", "--expected", released])
    out = capsys.readouterr().out
    assert f"[B] the deployed commit exports to the public tree: {released}" in out
    assert "holds something this export did not produce" not in out and "[A] fingerprint matches" in out
    # The newer rule still applies to [C]: the value it replaces is in the public copy, so verify fails.
    assert rc == 1 and "[C] verify_export failed" in out


def test_gate2_never_falls_back_to_the_clone_head(private: Path, tmp_path: Path, capsys):
    """A clone that has the published commit but not the tag is unreadable, not "use HEAD"."""
    publish(private, tmp_path / "public", "v1")
    git(tmp_path / "public", "tag", "-d", "v1")
    expected = fingerprint.commit_fingerprint(private)
    assert gate2(private, tmp_path / "public", tmp_path, "--expected", expected) == 2
    out = capsys.readouterr().out
    assert "cannot read v1 in the public repo" in out and "gate 2: PASS" not in out


# --- inventory scan ----------------------------------------------------------------------------------------

STEM = "zephyr" + "quill"
INVENTORY = [
    {"kind": "resource-group", "value": f"rg-{STEM}-dev"},
    {"kind": "resource", "value": f"Ca-Fa-{STEM.capitalize()}Hub"},
    {"kind": "resource", "value": "storage"},             # stoplisted generic word
    {"kind": "resource", "value": "rg"},                  # too short
    {"kind": "resource", "value": "12345-westus3"},       # digits-only fragment and a region
    {"kind": "tenant-domain", "value": "ex" + "ample-tenant.onmicrosoft.com"},
]


def test_denylist_has_names_and_fragments_but_no_generic_words():
    deny, skipped = inventory_scan.build_denylist(INVENTORY, allow=set())
    assert deny[f"rg-{STEM}-dev"] == "resource-group"
    assert deny[STEM] == "resource-group-fragment"
    assert deny[f"{STEM}hub"] == "resource-fragment"
    assert "storage" not in deny and "westus3" not in deny and "rg" not in deny and "12345" not in deny
    assert "onmicrosoft" not in deny and "tenant" not in deny
    assert skipped["stoplist"] >= 3 and skipped["short"] >= 2
    deny, skipped = inventory_scan.build_denylist(INVENTORY, allow={STEM})
    assert STEM not in deny and skipped["allowlisted"] == 1


def test_scan_matches_whole_tokens_case_insensitively():
    deny, _ = inventory_scan.build_denylist(INVENTORY, allow=set())
    texts = {"docs/a.md": f"line one\nthe {STEM.upper()}-prod account\n",
             "docs/b.md": f"原{STEM.capitalize()}帳號\n",                     # CJK neighbours end a token
             "docs/c.md": "storage in westus3, nothing else\n",
             "docs/d.md": f"x{STEM}y and {STEM}2 and 9{STEM} are other words\n"}  # ASCII letters/digits do not
    hits = inventory_scan.scan(texts, deny)
    where = {(rel, line, col) for _, _, rel, line, col in hits}
    assert where == {("docs/a.md", 2, 5), ("docs/b.md", 1, 2)}
    assert {h[0] for h in hits} == {inventory_scan.token_id(STEM)}


def test_scan_reports_nested_and_overlapping_tokens():
    deny, _ = inventory_scan.build_denylist(INVENTORY, allow=set())
    hits = inventory_scan.scan({"a.md": f"rg-{STEM}-dev.\n"}, deny)
    assert {(h[0], h[4]) for h in hits} == {(inventory_scan.token_id(f"rg-{STEM}-dev"), 1),
                                           (inventory_scan.token_id(STEM), 4)}
    assert inventory_scan.scan({"a.md": "anything"}, {}) == []


def scan_cli(tmp_path: Path, files: dict[str, str], *extra: str) -> int:
    export = tmp_path / "export"
    for rel, text in files.items():
        write(export, rel, text)
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(INVENTORY), encoding="utf-8")
    review = tmp_path / "review.json"
    review.write_text(json.dumps({"inventory_allow": []}), encoding="utf-8")
    return inventory_scan.main(["--export", str(export), "--inventory", str(inventory), "--review", str(review),
                                *extra])


def test_scan_output_never_holds_the_matched_value(tmp_path: Path, capsys):
    assert scan_cli(tmp_path, {"docs/a.md": f"see rg-{STEM}-dev\n"}) == 1
    out = capsys.readouterr().out
    assert f"HIT {inventory_scan.token_id(f'rg-{STEM}-dev')} resource-group docs/a.md:1:5" in out
    assert STEM not in out.lower() and "RESULT: REVIEW" in out


def test_only_inventory_allow_quiets_a_token(tmp_path: Path, capsys):
    """There is no "already published" escape: a token is quiet only when review.json allows it."""
    assert scan_cli(tmp_path / "one", {"docs/a.md": f"{STEM} again\n"}) == 1
    with pytest.raises(SystemExit):
        scan_cli(tmp_path / "two", {"docs/a.md": f"{STEM} again\n"}, "--previous", str(tmp_path))
    export = tmp_path / "three" / "export"
    write(export, "docs/a.md", f"{STEM} again\n")
    inventory = tmp_path / "three" / "inventory.json"
    inventory.write_text(json.dumps(INVENTORY), encoding="utf-8")
    review = tmp_path / "three" / "review.json"
    review.write_text(json.dumps({"inventory_allow": [{"value": STEM.upper(), "why": "fixture"}]}), encoding="utf-8")
    capsys.readouterr()
    assert inventory_scan.main(["--export", str(export), "--inventory", str(inventory), "--review", str(review)]) == 0
    assert "RESULT: PASS" in capsys.readouterr().out


def test_inventory_files_inside_the_repo_are_refused(tmp_path: Path, capsys):
    export = tmp_path / "export"
    write(export, "a.md", "x\n")
    for flag in ("--inventory", "--save-inventory"):
        rc = inventory_scan.main(["--export", str(export), flag, str(ROOT / "inventory.json")])
        assert rc == 2
    assert inventory_scan.main(["--export", str(export), "--inventory", str(export / "inv.json")]) == 2
    assert "must be outside the repo and the export" in capsys.readouterr().out
    assert not (ROOT / "inventory.json").exists()


def test_collect_inventory_reads_with_the_given_config_dir(monkeypatch, capsys):
    calls = []
    answers = {
        "account": {"name": "Fixture Sub", "tenantDisplayName": "Fixture Org", "tenantDefaultDomain": "fx.example"},
        "group": [f"rg-{STEM}-dev"],
        "resource": [f"acct-{STEM}/proj-one", "plain"],
    }

    def fake_az(args, env):
        calls.append((args, env["AZURE_CONFIG_DIR"]))
        if args[0] == "rest":
            return 1, ""
        return 0, json.dumps(answers[args[0]])

    monkeypatch.setattr(inventory_scan, "run_az", fake_az)
    items = inventory_scan.collect_inventory(["sub-1"], "C:/cfg")
    values = {(i["kind"], i["value"]) for i in items}
    assert ("resource", f"acct-{STEM}") in values and ("resource", "proj-one") in values
    assert ("subscription", "Fixture Sub") in values and ("tenant-domain", "fx.example") in values
    assert all(cfg == "C:/cfg" for _, cfg in calls)
    verbs = {tuple(a[:2]) for a, _ in calls}
    assert verbs == {("account", "show"), ("group", "list"), ("resource", "list"), ("rest", "--method")}
    assert all("get" in a for a, _ in calls if a[0] == "rest")
    assert "cannot read the tenant's verified domains" in capsys.readouterr().out


def test_collect_inventory_stops_when_a_subscription_cannot_be_read(monkeypatch):
    monkeypatch.setattr(inventory_scan, "run_az", lambda args, env: (1, ""))
    with pytest.raises(SystemExit) as exc:
        inventory_scan.collect_inventory(["sub-1"], "C:/cfg", graph=False)
    assert exc.value.code == 2


@pytest.mark.parametrize("empty", ["group", "resource"])
def test_collect_inventory_stops_when_a_subscription_lists_nothing(monkeypatch, capsys, empty):
    """An identity without read access gets [] with exit 0; that must not look like a clean subscription."""
    answers = {"account": {"name": "Fixture Sub"}, "group": [f"rg-{STEM}"], "resource": [f"acct-{STEM}"]}
    answers[empty] = []
    monkeypatch.setattr(inventory_scan, "run_az", lambda args, env: (0, json.dumps(answers[args[0]])))
    with pytest.raises(SystemExit) as exc:
        inventory_scan.collect_inventory(["sub-1", "sub-2"], "C:/cfg", graph=False)
    assert exc.value.code == 2
    assert "subscription 1 of 2 returned" in capsys.readouterr().out


@pytest.mark.parametrize("inventory", [
    [], {}, None, "names",
    [{"kind": "resource", "value": "x"}, "not-a-dict"],
    [{"kind": "resource-group", "value": ""}, {"kind": "resource", "value": "y"}],
    [{"kind": "resource-group"}, {"kind": "resource", "value": "y"}],
    [{"kind": "resource", "value": f"acct-{STEM}"}],                       # no resource group
    [{"kind": "resource-group", "value": f"rg-{STEM}"}],                   # no resource
])
def test_a_malformed_saved_inventory_is_refused(tmp_path: Path, capsys, inventory):
    export = tmp_path / "export"
    write(export, "a.md", f"{STEM}\n")
    saved = tmp_path / "inventory.json"
    saved.write_text(json.dumps(inventory), encoding="utf-8")
    assert inventory_scan.main(["--export", str(export), "--inventory", str(saved)]) == 2
    out = capsys.readouterr().out
    assert "--inventory refused" in out and "RESULT" not in out


def test_an_unreadable_saved_inventory_is_refused(tmp_path: Path, capsys):
    export = tmp_path / "export"
    write(export, "a.md", "x\n")
    saved = tmp_path / "inventory.json"
    saved.write_text("{not json", encoding="utf-8")
    assert inventory_scan.main(["--export", str(export), "--inventory", str(saved)]) == 2
    assert inventory_scan.main(["--export", str(export), "--inventory", str(tmp_path / "missing.json")]) == 2
    assert "cannot read --inventory" in capsys.readouterr().out


def test_an_empty_denylist_stops_the_scan(tmp_path: Path, capsys):
    """Every name skipped (stoplist, too short) leaves nothing to check: that is not a pass."""
    export = tmp_path / "export"
    write(export, "a.md", "storage\n")
    saved = tmp_path / "inventory.json"
    saved.write_text(json.dumps([{"kind": "resource-group", "value": "rg"},
                                 {"kind": "resource", "value": "storage"}]), encoding="utf-8")
    review = tmp_path / "review.json"
    review.write_text(json.dumps({"inventory_allow": []}), encoding="utf-8")
    assert inventory_scan.main(["--export", str(export), "--inventory", str(saved), "--review", str(review)]) == 2
    out = capsys.readouterr().out
    assert "denylist is empty" in out and "PASS" not in out


WORKFLOW = ROOT / ".github" / "workflows" / "public-sync.yml"


@pytest.mark.skipif(not WORKFLOW.is_file(), reason="the workflow is not part of the public copy")
def test_workflow_runs_gate2_last_and_read_only():
    text = WORKFLOW.read_text(encoding="utf-8")
    top = text.split("jobs:")[0]
    assert "\n  contents: read\n  pull-requests: read" in top and "write" not in top
    assert "gate 2" in top.lower(), "the header comment names gate 2"
    step = text[text.index("- name: Gate 2"):]
    assert text.index("- name: Create the GitHub Release") < text.index("- name: Gate 2")
    assert "- name:" not in step[1:], "gate 2 is the last step"
    push = text[text.index("- name: Commit, tag and push"):text.index("- name: Create the GitHub Release")]
    assert "\n        id: push\n" in push
    # A failed Release creation after a successful push must not skip gate 2.
    assert "\n        if: always() && steps.push.outcome == 'success'\n" in step
    assert "gate2_check.py" in step and '--expected-file "$body"' in step
    assert '--baseline "$RUNNER_TEMP/baseline"' in step and '--gitleaks "$RUNNER_TEMP/bin/gitleaks"' in step
    assert "--no-gitleaks" not in step
    assert " push" not in step and "gh release" not in step and "--depth 1" in step
    assert "x-access-token:" not in step and "@github.com" not in step
    # The public repo is cloned unauthenticated; the write token never reaches this step.
    assert "PUBLIC_REPO_TOKEN" not in step and "credential.helper" not in step
    assert "2>/dev/null" not in step and "|| :" not in step
    # It compares the same release PR body the push step was checked against; it no longer reads it itself.
    assert 'body="$RUNNER_TEMP/release-pr-body.md"' in step and "gh api" not in step and "GH_TOKEN" not in step


def workflow_step(text: str, name: str) -> str:
    start = text.index(f"- name: {name}")
    nxt = text.find("\n      - name:", start)
    return text[start:] if nxt < 0 else text[start:nxt]


@pytest.mark.skipif(not WORKFLOW.is_file(), reason="the workflow is not part of the public copy")
def test_workflow_checks_gate_1_before_anything_is_pushed():
    """Finding 2 (2026-10-14): the fingerprint was first compared after the public push, so a manual deploy or main
    moving after gate 1 published unreviewed content. The release PR body is now read before the push step, and
    the push step compares the tree it is about to commit with it before `git commit`, `git tag` or `git push`."""
    text = WORKFLOW.read_text(encoding="utf-8")
    read = workflow_step(text, "Read gate 1's fingerprint from the release PR")
    push = workflow_step(text, "Commit, tag and push")
    assert text.index("- name: Read gate 1's fingerprint") < text.index("- name: Commit, tag and push")
    assert "if:" not in read.split("run: |")[0], "it runs on every publish"
    assert "GH_TOKEN: ${{ github.token }}" in read and "PUBLIC_REPO_TOKEN" not in read
    assert 'body="$RUNNER_TEMP/release-pr-body.md"' in read and "lastEditedAt" in read
    # API failures are reported, not swallowed.
    assert "2>/dev/null" not in read and "|| :" not in read and "::error::" in read and "::warning::" not in read
    check = push.index("prepush_check.py")
    assert push.index('tree="$(git write-tree)"') < check
    assert '--body "$RUNNER_TEMP/release-pr-body.md" --tree "$tree"' in push
    for later in ("commit -q", 'git tag "$tag"', " push -q"):
        assert check < push.index(later), later
    assert "|| true" not in push and "set +e" not in push


@pytest.mark.parametrize("body, tree, code, says", [
    ("notes\npublic-sync fingerprint: " + "a" * 40 + "\n", "a" * 40, 0, "matches"),
    ("public-sync fingerprint: `" + "A" * 40 + "`", "a" * 40, 0, "matches"),
    ("public-sync fingerprint: " + "a" * 40, "b" * 40, 1, "fingerprint mismatch"),
    ("", "a" * 40, 1, "no usable gate 1 fingerprint"),
    ("release notes without the line", "a" * 40, 1, "no usable gate 1 fingerprint"),
    ("public-sync fingerprint: " + "a" * 40 + "\npublic-sync fingerprint: " + "b" * 40, "a" * 40, 1,
     "no usable gate 1 fingerprint"),
])
def test_prepush_check_publishes_only_the_fingerprinted_tree(tmp_path: Path, capsys, body, tree, code, says):
    path = tmp_path / "release-pr-body.md"
    path.write_text(body, encoding="utf-8")
    assert prepush_check.main(["--body", str(path), "--tree", tree]) == code
    out = capsys.readouterr().out
    assert says in out and out.startswith("::error::" if code else "gate 1")
    if code:
        assert "nothing was pushed" in out and "by hand" not in out  # there is no manual bypass to point to


def test_prepush_check_fails_closed_on_the_tree_the_workflow_builds(private: Path, tmp_path: Path, capsys):
    """The same comparison against a real `git write-tree` of the export, as the push step runs it."""
    (tmp_path / "first").mkdir()
    (tmp_path / "moved").mkdir()
    tree = publish(private, tmp_path / "first" / "public", "v1")
    body = tmp_path / "body.md"
    body.write_text(f"public-sync fingerprint: {fingerprint.commit_fingerprint(private, 'HEAD')}\n", encoding="utf-8")
    assert prepush_check.main(["--body", str(body), "--tree", tree]) == 0
    write(private, "docs/notes.md", "changed after gate 1\n")
    git(private, "commit", "-qam", "after gate 1")
    moved = publish(private, tmp_path / "moved" / "public", "v1")
    assert moved != tree and prepush_check.main(["--body", str(body), "--tree", moved]) == 1
    assert "fingerprint mismatch" in capsys.readouterr().out


def pr_body_selector() -> str:
    step = WORKFLOW.read_text(encoding="utf-8").split("- name: Read gate 1's fingerprint")[1]
    code = step.split("<<'PY'\n", 1)[1].split("\n          PY\n", 1)[0]
    return "\n".join(line[10:] for line in code.splitlines())


@pytest.mark.skipif(not WORKFLOW.is_file(), reason="the workflow is not part of the public copy")
@pytest.mark.parametrize("nodes, body, annotation", [
    ([{"number": 7, "mergedAt": "2026-10-13T00:52:07Z", "lastEditedAt": None, "mergeCommit": {"oid": "c" * 40},
       "body": "public-sync fingerprint: " + "a" * 40}], "public-sync fingerprint: " + "a" * 40, ""),
    ([{"number": 7, "mergedAt": "2026-10-13T00:52:07Z", "lastEditedAt": "2026-10-13T00:50:00Z",
       "mergeCommit": {"oid": "c" * 40}, "body": "as merged"}], "as merged", ""),
    ([{"number": 7, "mergedAt": "2026-10-13T00:52:07Z", "lastEditedAt": "2026-10-13T01:00:00Z",
       "mergeCommit": {"oid": "c" * 40}, "body": "edited later"}], "", "::error::release PR: #7 was edited"),
    ([{"number": 8, "mergedAt": None, "lastEditedAt": None, "mergeCommit": None, "body": "open PR"}], "",
     "::error::release PR: no merged PR"),
    ([], "", "::error::release PR: no merged PR"),
    ([{"number": 7, "mergedAt": "2026-10-13T00:52:07Z", "lastEditedAt": None, "mergeCommit": {"oid": "d" * 40},
       "body": "x"},
      {"number": 9, "mergedAt": "2026-10-13T00:53:00Z", "lastEditedAt": None, "mergeCommit": {"oid": "e" * 40},
       "body": "y"}], "", "::error::release PR: 2 merged PRs"),
    ([{"number": 7, "mergedAt": "2026-10-13T00:52:07Z", "lastEditedAt": None, "mergeCommit": {"oid": "c" * 40},
       "body": "the merge commit's PR"},
      {"number": 9, "mergedAt": "2026-10-12T00:00:00Z", "lastEditedAt": None, "mergeCommit": {"oid": "e" * 40},
       "body": "an older PR that also holds the commit"}], "the merge commit's PR", ""),
])
def test_the_release_pr_body_is_read_as_merged(tmp_path: Path, nodes, body, annotation):
    answer = tmp_path / "release-pr.json"
    answer.write_text(json.dumps({"data": {"repository": {"object": {"associatedPullRequests": {"nodes": nodes}}}}}),
                      encoding="utf-8")
    out_body = tmp_path / "body.md"
    out_body.write_text("", encoding="utf-8")
    script = tmp_path / "selector.py"
    script.write_text(pr_body_selector(), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script), str(answer), str(out_body), "c" * 40],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, check=True)
    assert out_body.read_text(encoding="utf-8") == body
    assert proc.stdout.startswith(annotation) and (annotation or not proc.stdout)
