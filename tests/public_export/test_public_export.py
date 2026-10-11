"""Offline contracts for scripts/public_export and the public-sync workflow; no network, no real gitleaks.

Fixture values are assembled at runtime so this file itself never trips the export's residual sweep.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "scripts" / "public_export"
sys.path.insert(0, str(TOOLS))

import discover  # noqa: E402
import scrub_export  # noqa: E402
import verify_export  # noqa: E402

TENANT_GUID = "3f2a9c41-7b6e-4d18-9a5c-2e8b71f04d63"
PLACEHOLDER = "00000000-0000-0000-0000-000000000010"
UNMAPPED_GUID = "8d4e1b72-c95a-4f03-b6e8-71a2d03c5f94"
HOST = "app.fixture-7c41" + ".westus3." + "azurecontainerapps.io"
HOST_PLACEHOLDER = "app.example-00000000" + ".westus3." + "azurecontainerapps.io"
EMAIL = "alice" + "@" + "corp-fixture.com"
EMAIL_PLACEHOLDER = "owner" + "@" + "contoso.com"
DIGEST = hashlib.sha256(b"fixture").hexdigest()
MAPPING = {
    "guids": {TENANT_GUID: PLACEHOLDER},
    "guid_prefix_scrub": True,
    "truncated_prefixes": [],
    "keep_guids": {},
    "rules": [
        {"id": "host", "category": "cae-domain", "type": "literal", "find": "fixture-7c41",
         "replace": "example-00000000", "verify_absent": ["7c41"]},
        {"id": "email", "category": "email", "type": "literal", "find": EMAIL, "replace": EMAIL_PLACEHOLDER},
    ],
    "gitleaks_known_false_positives": [],
    "residual_allow": [
        {"pattern": r"^azure-host:app\.example-00000000\.", "why": "placeholder"},
        {"pattern": r"^email:owner@contoso\.com$", "why": "placeholder"},
    ],
}
REVIEW = TOOLS / "review.json"
needs_review = pytest.mark.skipif(not REVIEW.is_file(), reason="review.json is not part of the public copy")
EXCLUDED = ["scripts/public_export/mapping.json", "scripts/public_export/review.json",
            ".github/workflows/public-sync.yml", ".github/workflows/deploy-staging.yml",
            ".github/workflows/validate-infrastructure.yml", ".claude/settings.json"]


def git(repo: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t"}
    subprocess.run(["git", "-C", str(repo), "-c", "core.autocrlf=false", *args], check=True, capture_output=True,
                   env=env)


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    write(repo, "docs/a.md", f"tenant: {TENANT_GUID}\nhost: {HOST}\ncontact: {EMAIL}\ndigest: {DIGEST}\n")
    for rel in EXCLUDED:
        write(repo, rel, "excluded\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    return repo


@pytest.fixture
def mapping_file(tmp_path: Path) -> Path:
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(MAPPING), encoding="utf-8")
    return path


def export(repo: Path, tmp_path: Path, mapping_file: Path) -> tuple[Path, Path]:
    base, out = tmp_path / "baseline", tmp_path / "out"
    assert scrub_export.main(["--src", str(repo), "--out", str(base), "--mapping", str(mapping_file), "--plain"]) == 0
    assert scrub_export.main(["--src", str(repo), "--out", str(out), "--mapping", str(mapping_file)]) == 0
    return base, out


@pytest.fixture
def fake_gitleaks(tmp_path: Path, monkeypatch):
    """Replaces the gitleaks process; `state` sets what the next run reports."""
    binary = tmp_path / "gitleaks-bin"
    binary.write_text("", encoding="utf-8")
    state = {"findings": [], "log": "INF scanned ~1000000000 bytes (1 GB) in 1s", "returncode": 0}
    # A config in the caller's environment must not reach gitleaks.
    monkeypatch.setenv("GITLEAKS_CONFIG", str(tmp_path / "relaxed.toml"))
    monkeypatch.setenv("GITLEAKS_CONFIG_TOML", "[allowlist]\npaths = ['.*']\n")

    def run(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
        assert cmd[0] == str(binary) and cmd[1] == "dir" and "--redact" in cmd
        assert "--ignore-gitleaks-allow" in cmd and "--config" not in cmd and "-c" not in cmd
        ignore = Path(cmd[cmd.index("--gitleaks-ignore-path") + 1])
        assert ignore.is_dir() and not any(ignore.iterdir()) and Path(cmd[2]) not in (ignore, *ignore.parents)
        assert not {k.upper() for k in env} & {"GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML"} and "PATH" in {
            k.upper() for k in env}
        state["runs"] = state.get("runs", 0) + 1
        report = Path(cmd[cmd.index("--report-path") + 1])
        root = Path(cmd[2])
        report.write_text(json.dumps([{**f, "File": str(root / f["File"])} for f in state["findings"]]),
                          encoding="utf-8")
        return subprocess.CompletedProcess(cmd, state["returncode"], stdout="", stderr=state["log"])

    monkeypatch.setattr(verify_export, "run_gitleaks", run)
    state["binary"] = str(binary)
    return state


def verify(out: Path, base: Path, mapping_file: Path, gitleaks: str) -> int:
    return verify_export.main(["--out", str(out), "--baseline", str(base), "--mapping", str(mapping_file),
                               "--gitleaks", gitleaks])


def test_scrub_replaces_mapped_values_and_keeps_hashes(repo, tmp_path, mapping_file):
    base, out = export(repo, tmp_path, mapping_file)
    text = (out / "docs/a.md").read_text(encoding="utf-8")
    assert text == f"tenant: {PLACEHOLDER}\nhost: {HOST_PLACEHOLDER}\ncontact: {EMAIL_PLACEHOLDER}\ndigest: {DIGEST}\n"
    assert (base / "docs/a.md").read_text(encoding="utf-8").startswith(f"tenant: {TENANT_GUID}\n")


def test_export_takes_head_only_and_skips_excluded_files(repo, tmp_path, mapping_file):
    write(repo, "untracked.md", "not committed\n")
    write(repo, "docs/a.md", "uncommitted edit\n")
    _, out = export(repo, tmp_path, mapping_file)
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == ["docs/a.md"]
    assert (out / "docs/a.md").read_text(encoding="utf-8").startswith(f"tenant: {PLACEHOLDER}\n")
    assert all(scrub_export.is_excluded(rel) for rel in EXCLUDED)


def test_export_refuses_a_non_empty_output(repo, tmp_path, mapping_file):
    write(tmp_path, "out/keep.txt", "user data\n")
    with pytest.raises(SystemExit):
        scrub_export.main(["--src", str(repo), "--out", str(tmp_path / "out"), "--mapping", str(mapping_file)])
    assert (tmp_path / "out/keep.txt").is_file()


def test_verify_passes_on_the_clean_export(repo, tmp_path, mapping_file, fake_gitleaks, capsys):
    base, out = export(repo, tmp_path, mapping_file)
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 0
    assert "RESULT: PASS" in capsys.readouterr().out


def test_verify_fails_on_an_unmapped_tenant_guid_without_printing_it(repo, tmp_path, mapping_file, fake_gitleaks,
                                                                    capsys):
    write(repo, "docs/b.md", f"tenantId: {UNMAPPED_GUID}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "leak")
    base, out = export(repo, tmp_path, mapping_file)
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1
    printed = capsys.readouterr().out
    assert "UNREVIEWED guid 8d4e... x1 e.g. docs/b.md:1" in printed
    assert UNMAPPED_GUID not in printed


def commit_test_file(repo: Path, text: str) -> None:
    write(repo, "tests/test_ids.py", text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "test ids")


def test_verify_fails_on_an_unknown_guid_in_tests_even_without_identity_words(repo, tmp_path, mapping_file,
                                                                              fake_gitleaks, capsys):
    commit_test_file(repo, f'VALUE = "{UNMAPPED_GUID}"\n')
    base, out = export(repo, tmp_path, mapping_file)
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1
    printed = capsys.readouterr().out
    assert "UNREVIEWED guid 8d4e... x1 e.g. tests/test_ids.py:1" in printed
    assert UNMAPPED_GUID not in printed


def test_verify_passes_a_listed_synthetic_guid_but_not_a_lookalike(repo, tmp_path, mapping_file, fake_gitleaks):
    commit_test_file(repo, f'VALUE = "{UNMAPPED_GUID}"\n')
    base, out = export(repo, tmp_path, mapping_file)
    listed = {**MAPPING, "synthetic_guid_sha256": [hashlib.sha256(UNMAPPED_GUID.encode()).hexdigest()]}
    mapping_file.write_text(json.dumps(listed), encoding="utf-8")
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 0
    assert UNMAPPED_GUID not in mapping_file.read_text(encoding="utf-8")
    write(out, "tests/test_ids.py", f'VALUE = "{UNMAPPED_GUID[:-1]}5"\n')
    write(base, "tests/test_ids.py", f'VALUE = "{UNMAPPED_GUID[:-1]}5"\n')
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1


def test_verify_passes_placeholders_and_keep_guids_in_tests(repo, tmp_path, mapping_file, fake_gitleaks):
    public_id = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
    commit_test_file(repo, f'MAPPED = "{TENANT_GUID}"\nPLACEHOLDER = "{PLACEHOLDER}"\nROLE = "{public_id}"\n')
    mapping_file.write_text(json.dumps({**MAPPING, "keep_guids": {public_id: "built-in role"}}), encoding="utf-8")
    base, out = export(repo, tmp_path, mapping_file)
    assert (out / "tests/test_ids.py").read_text(encoding="utf-8").startswith(f'MAPPED = "{PLACEHOLDER}"\n')
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 0


def test_verify_fails_on_a_leftover_original_value(repo, tmp_path, mapping_file, fake_gitleaks, capsys):
    base, out = export(repo, tmp_path, mapping_file)
    write(out, "docs/a.md", (out / "docs/a.md").read_text(encoding="utf-8") + f"again: {TENANT_GUID}\n")
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1
    printed = capsys.readouterr().out
    assert f"PRESENT guid -> {PLACEHOLDER} at docs/a.md:5" in printed
    assert TENANT_GUID not in printed
    # [2] names the placeholder only; the residual sweep [4] still prints its usual 4-character prefix.
    assert not any(TENANT_GUID[:4] in line for line in printed.splitlines() if "PRESENT" in line)


def test_verify_names_only_the_rule_when_a_verify_absent_value_is_left(repo, tmp_path, mapping_file, fake_gitleaks,
                                                                       capsys):
    base, out = export(repo, tmp_path, mapping_file)
    write(out, "docs/a.md", (out / "docs/a.md").read_text(encoding="utf-8") + "suffix 7c41 again\n")
    write(out, "docs/b.md", f"prefix {TENANT_GUID[:8]}\n")
    write(base, "docs/b.md", f"prefix {TENANT_GUID[:8]}\n")
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1
    printed = capsys.readouterr().out
    assert "PRESENT rule host absent #1 at docs/a.md:5" in printed
    assert "PRESENT guid-prefix #1 at docs/b.md:1" in printed
    present = "\n".join(line for line in printed.splitlines() if "PRESENT" in line)
    assert "7c41" not in present and TENANT_GUID[:4] not in present


def test_verify_fails_when_a_hash_changes(repo, tmp_path, mapping_file, fake_gitleaks, capsys):
    base, out = export(repo, tmp_path, mapping_file)
    path = out / "docs/a.md"
    path.write_text(path.read_text(encoding="utf-8").replace(DIGEST, "0" * 64), encoding="utf-8", newline="\n")
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1
    assert f"ALTERED docs/a.md:4 {DIGEST[:4]}... len 64" in capsys.readouterr().out


def test_verify_fails_on_an_excluded_or_extra_file(repo, tmp_path, mapping_file, fake_gitleaks):
    base, out = export(repo, tmp_path, mapping_file)
    write(out, ".github/workflows/any-new.yml", "on: push\n")
    write(base, ".github/workflows/any-new.yml", "on: push\n")
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1


@pytest.mark.parametrize("rel", [".gitleaks.toml", ".gitleaksignore", "src/pkg/.gitleaksignore"])
def test_verify_fails_when_the_export_holds_gitleaks_config(repo, tmp_path, mapping_file, fake_gitleaks, capsys, rel):
    base, out = export(repo, tmp_path, mapping_file)
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 0
    assert fake_gitleaks["runs"] == 1
    write(out, rel, "[allowlist]\npaths = ['.*']\n")
    write(base, rel, "[allowlist]\npaths = ['.*']\n")
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1
    assert f"[1] gitleaks: FAIL, the tree holds gitleaks config: {rel}" in capsys.readouterr().out
    assert fake_gitleaks["runs"] == 1  # refused before scanning


def test_verify_fails_when_gitleaks_is_missing(repo, tmp_path, mapping_file, capsys):
    base, out = export(repo, tmp_path, mapping_file)
    assert verify(out, base, mapping_file, str(tmp_path / "no-such-gitleaks")) == 1
    assert "[1] gitleaks: FAIL, not found" in capsys.readouterr().out


@pytest.mark.parametrize("log, returncode", [
    ("INF scanned ~0 bytes (0) in 2ms\nINF no leaks found", 0),
    ("INF no leaks found", 0),
    ("INF scanned ~10 bytes (10 bytes) in 2ms", 0),  # far less than the export
    ("FTL CreateFile: The system cannot find the file specified.", 1),
])
def test_verify_fails_when_gitleaks_does_not_really_scan(repo, tmp_path, mapping_file, fake_gitleaks, log, returncode):
    base, out = export(repo, tmp_path, mapping_file)
    fake_gitleaks.update(log=log, returncode=returncode)
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1


def test_verify_fails_on_an_unknown_gitleaks_finding(repo, tmp_path, mapping_file, fake_gitleaks, capsys):
    base, out = export(repo, tmp_path, mapping_file)
    fake_gitleaks["findings"] = [{"RuleID": "generic-api-key", "File": "docs/a.md", "StartLine": 4}]
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1
    assert "UNKNOWN generic-api-key docs/a.md:4" in capsys.readouterr().out
    known = {**MAPPING, "gitleaks_known_false_positives": [{"rule": "generic-api-key", "file": "docs/a.md", "count": 1, "why": "x"}]}
    mapping_file.write_text(json.dumps(known), encoding="utf-8")
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 0


def known_fp(mapping_file: Path, *entries: dict) -> None:
    mapping_file.write_text(json.dumps({**MAPPING, "gitleaks_known_false_positives": list(entries)}), encoding="utf-8")


def test_verify_fails_on_a_new_gitleaks_finding_in_an_unlisted_tests_file(repo, tmp_path, mapping_file,
                                                                         fake_gitleaks, capsys):
    base, out = export(repo, tmp_path, mapping_file)
    listed = {"rule": "jwt", "file": "tests/test_listed.py", "count": 1, "why": "fake JWT"}
    known_fp(mapping_file, listed)
    fake_gitleaks["findings"] = [{"RuleID": "jwt", "File": "tests/test_listed.py", "StartLine": 3}]
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 0
    fake_gitleaks["findings"].append({"RuleID": "jwt", "File": "tests/test_new.py", "StartLine": 7})
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1
    assert "UNKNOWN jwt tests/test_new.py:7" in capsys.readouterr().out
    # Same file, another rule, or one more finding than reviewed: still new.
    fake_gitleaks["findings"] = [{"RuleID": "generic-api-key", "File": "tests/test_listed.py", "StartLine": 4}]
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1
    fake_gitleaks["findings"] = [{"RuleID": "jwt", "File": "tests/test_listed.py", "StartLine": n} for n in (3, 9)]
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1
    assert "UNKNOWN jwt tests/test_listed.py: 2 findings, 1 known (lines 3, 9)" in capsys.readouterr().out


@pytest.mark.parametrize("entry", [{"rule": "*", "file": "tests/test_new.py"}, {"rule": "jwt", "file": "tests/*"},
                                   {"rule": "jwt", "file": "tests/test_?ew.py"}, {"rule": "jwt"}])
def test_verify_refuses_wildcard_known_false_positives(repo, tmp_path, mapping_file, fake_gitleaks, capsys, entry):
    base, out = export(repo, tmp_path, mapping_file)
    known_fp(mapping_file, {**entry, "why": "x"})
    fake_gitleaks["findings"] = [{"RuleID": "jwt", "File": "tests/test_new.py", "StartLine": 7}]
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1
    assert "no wildcards" in capsys.readouterr().out


@needs_review
def test_review_known_false_positives_are_exact_pairs_with_counts():
    for entry in json.loads(REVIEW.read_text(encoding="utf-8"))["gitleaks_known_false_positives"]:
        assert not set("*?[]") & set(entry["rule"] + entry["file"]) and isinstance(entry["count"], int), entry["file"]
        assert (ROOT / entry["file"]).is_file(), entry["file"]


def test_text_mode_scrubs_release_notes_and_writes_only_on_success(tmp_path, mapping_file, fake_gitleaks):
    src, dest = tmp_path / "notes.txt", tmp_path / "notes.md"
    src.write_text(f"第 1 版：請洽 {EMAIL}\n", encoding="utf-8")
    args = ["--text", str(src), "--text-out", str(dest), "--mapping", str(mapping_file),
            "--gitleaks", fake_gitleaks["binary"]]
    assert verify_export.main(args) == 0
    assert dest.read_text(encoding="utf-8") == f"第 1 版：請洽 {EMAIL_PLACEHOLDER}\n"
    dest.unlink()
    src.write_text(f"tenant {UNMAPPED_GUID}\n", encoding="utf-8")
    assert verify_export.main(args) == 1
    assert not dest.exists()


def review() -> dict:
    return json.loads(REVIEW.read_text(encoding="utf-8"))


def review_rule(rule_id: str) -> dict:
    return next(r for r in review()["rules"] if r["id"] == rule_id)


# Real names come from review.json (never exported), so this file names no real repo or resource.
PRIVATE_REPO = "https://github.com/" + (review_rule("this-repo-plain")["find"] if REVIEW.is_file() else "o/r")
MARKER = "（原私有 repo 連結）"
SHA = "6f9a637f" * 5


def repo_link_rules() -> list[dict]:
    return [r for r in review()["rules"] if r["id"].startswith("this-repo-")]




@needs_review
def test_private_repo_links_keep_their_text_and_bare_urls_become_a_marker():
    scrubber = scrub_export.Scrubber({**MAPPING, "rules": repo_link_rules()})
    text = (f"見 [#400]({PRIVATE_REPO}/issues/400#issuecomment-1) 與 [`{SHA[:7]}`]({PRIVATE_REPO}/commit/{SHA})。\n"
            f"[F02]: {PRIVATE_REPO}/blob/{SHA}/docs/a.md#L1-L3\n"
            f'"url": "{PRIVATE_REPO}/pull/361",\n'
            f"autolink <{PRIVATE_REPO}/actions/runs/37016099084> and bare {PRIVATE_REPO.upper()}/tree/main.\n"
            f"digest {DIGEST} and other repo https://github.com/github/copilot-sdk/blob/{SHA}/x.md\n")
    assert scrubber.scrub(text, "docs/x.md") == (
        f"見 #400 與 `{SHA[:7]}`。\n"
        f"[F02]: {MARKER}\n"
        f'"url": "{MARKER}",\n'
        f"autolink {MARKER} and bare {MARKER}.\n"
        f"digest {DIGEST} and other repo https://github.com/github/copilot-sdk/blob/{SHA}/x.md\n")
    assert scrubber.cat_counts == {"this-repo-link": 2, "this-repo-url": 4}


@needs_review
@pytest.mark.parametrize("before, after", [
    ("{u}/pull/770的說明", "{m}的說明"),                       # CJK right after the URL
    ("| {u}/issues/1|下一格 |", "| {m}|下一格 |"),              # table cell
    ("**{u}/pull/2**。", "**{m}**。"),                        # bold markers
    ("見（{u}/pull/3）與 ({u}/pull/4)。", "見（{m}）與 ({m})。"),  # full-width and ASCII parentheses
    (r'"{{\"url\": \"{u}/pull/5\"}}"', r'"{{\"url\": \"{m}\"}}"'),  # escaped quotes in embedded JSON
    ("{u}/blob/main/a.md#L1-L3?plain=1&x=%20.", "{m}."),       # the whole ASCII URL goes, the period stays
])
def test_bare_private_repo_urls_leave_the_following_text(before, after):
    scrubber = scrub_export.Scrubber({**MAPPING, "rules": repo_link_rules()})
    bare = PRIVATE_REPO.replace("https://", "")
    for url in (PRIVATE_REPO, bare):
        assert scrubber.scrub(before.format(u=url), "docs/x.md") == after.format(m=MARKER)


@needs_review
def test_verify_passes_rewritten_links_and_fails_on_a_surviving_private_repo_url(repo, tmp_path, mapping_file,
                                                                                 fake_gitleaks, capsys):
    mapping_file.write_text(json.dumps({**MAPPING, "rules": MAPPING["rules"] + repo_link_rules()}), encoding="utf-8")
    write(repo, "docs/links.md", f"[blob]({PRIVATE_REPO}/blob/{SHA}/src/fa-shared-context-poc/shared_context/m.py)\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "links")
    base, out = export(repo, tmp_path, mapping_file)
    assert (out / "docs/links.md").read_text(encoding="utf-8") == "blob\n"
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 0  # the SHA inside the removed URL may go
    capsys.readouterr()
    write(out, "docs/links.md", f"see {PRIVATE_REPO.replace('https://', '')}/pull/1\n")
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1
    assert "PRESENT rule this-repo-url absent-regex at docs/links.md:1" in capsys.readouterr().out


@needs_review
def test_verify_fails_when_a_hash_in_kept_link_text_changes(repo, tmp_path, mapping_file, fake_gitleaks, capsys):
    mapping_file.write_text(json.dumps({**MAPPING, "rules": MAPPING["rules"] + repo_link_rules()}), encoding="utf-8")
    write(repo, "docs/links.md", f"[`{SHA}`]({PRIVATE_REPO}/commit/{SHA})\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "commit link")
    base, out = export(repo, tmp_path, mapping_file)
    assert (out / "docs/links.md").read_text(encoding="utf-8") == f"`{SHA}`\n"
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 0
    capsys.readouterr()
    write(out, "docs/links.md", f"`{SHA[:-1]}0`\n")  # one hex digit of the kept link text changed
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1
    assert f"ALTERED docs/links.md:1 {SHA[:4]}... len 40" in capsys.readouterr().out


@needs_review
def test_progress_generator_repo_constant_is_emptied_in_the_public_copy():
    source = ROOT / "controller" / "progress_b1_b2.py"
    original = source.read_text(encoding="utf-8")
    assert f'\nREPO = "{PRIVATE_REPO}"\n' in original
    scrubbed = scrub_export.Scrubber({**MAPPING, "rules": repo_link_rules()}).scrub(original, "controller/x.py")
    assert '\nREPO = ""\n' in scrubbed and MARKER not in scrubbed
    namespace: dict = {"__name__": "public_progress"}
    exec(compile(scrubbed, "progress_b1_b2.py", "exec"), namespace)  # the generator as the public copy has it
    assert namespace["link"]("pr:#103（原接受）") == "#103（原接受）"
    assert namespace["link"]("commit:fe5ab6b") == "`fe5ab6b`"


@needs_review
def test_release_notes_lose_private_repo_links(tmp_path, mapping_file, fake_gitleaks):
    mapping_file.write_text(json.dumps({**MAPPING, "rules": MAPPING["rules"] + repo_link_rules()}), encoding="utf-8")
    src, dest = tmp_path / "notes.txt", tmp_path / "notes.md"
    src.write_text(f"本版包含 [#765]({PRIVATE_REPO}/pull/765)\n", encoding="utf-8")
    assert verify_export.main(["--text", str(src), "--text-out", str(dest), "--mapping", str(mapping_file),
                               "--gitleaks", fake_gitleaks["binary"]]) == 0
    assert dest.read_text(encoding="utf-8") == "本版包含 #765\n"


def test_discover_keeps_existing_placeholders(repo, tmp_path):
    write(repo, "docs/b.md", f"other: {UNMAPPED_GUID}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "new id")
    review = tmp_path / "review.json"
    review.write_text(json.dumps({"keep_guids": {}, "extra_scrub_guids": [], "role_context_regex": "roleDefinition",
                                  "rules": [], "gitleaks_known_false_positives": [], "residual_allow": []}),
                      encoding="utf-8")
    mapping = tmp_path / "mapping.json"
    mapping.write_text(json.dumps({"guids": {UNMAPPED_GUID: "00000000-0000-0000-0000-000000000010"}}), encoding="utf-8")
    assert discover.main(["--src", str(repo), "--review", str(review), "--mapping", str(mapping),
                          "--candidates", str(tmp_path / "candidates.txt")]) == 0
    assert json.loads(mapping.read_text(encoding="utf-8"))["guids"] == {
        UNMAPPED_GUID: "00000000-0000-0000-0000-000000000010",
        TENANT_GUID: "00000000-0000-0000-0000-000000000011",
    }


def test_discover_lists_test_guids_and_records_only_their_hashes_after_review(repo, tmp_path, capsys):
    commit_test_file(repo, f'VALUE = "{UNMAPPED_GUID}"\n')
    review, mapping = tmp_path / "review.json", tmp_path / "mapping.json"
    review.write_text(json.dumps({"keep_guids": {}, "extra_scrub_guids": [], "role_context_regex": "roleDefinition",
                                  "rules": [], "gitleaks_known_false_positives": [], "residual_allow": []}),
                      encoding="utf-8")
    args = ["--src", str(repo), "--review", str(review), "--mapping", str(mapping),
            "--candidates", str(tmp_path / "candidates.txt")]
    assert discover.main(args) == 0
    assert "UNLISTED 8d4e... first seen tests/test_ids.py:1" in capsys.readouterr().out
    assert json.loads(mapping.read_text(encoding="utf-8"))["synthetic_guid_sha256"] == []
    assert "NOT LISTED, verify fails" in (tmp_path / "candidates.txt").read_text(encoding="utf-8")
    assert discover.main(args + ["--accept-synthetic"]) == 0  # a random-looking GUID needs to be named
    assert json.loads(review.read_text(encoding="utf-8")).get("synthetic_guid_sha256", []) == []
    assert "UNLISTED 8d4e... first seen tests/test_ids.py:1" in capsys.readouterr().out
    assert discover.main(args + ["--accept-synthetic", UNMAPPED_GUID[:6]]) == 0
    digest = hashlib.sha256(UNMAPPED_GUID.encode()).hexdigest()
    assert json.loads(review.read_text(encoding="utf-8"))["synthetic_guid_sha256"] == [digest]
    assert json.loads(mapping.read_text(encoding="utf-8"))["synthetic_guid_sha256"] == [digest]
    assert UNMAPPED_GUID not in review.read_text(encoding="utf-8") + mapping.read_text(encoding="utf-8")
    printed = capsys.readouterr().out
    assert "ACCEPTED 8d4e... (named) first seen tests/test_ids.py:1" in printed
    assert "1 of 1 exported synthetic/test GUIDs listed" in printed


def test_discover_accepts_only_named_or_patterned_guids(repo, tmp_path, capsys):
    twin = UNMAPPED_GUID[:4] + "9a7b-0c1d-4e2f-8a3b-" + "5c6d7e8f9a0b"  # same first 4 characters
    patterned = "12345678-1234-4234-8234-" + "123456789abc"  # is_synthetic()
    commit_test_file(repo, f'A = "{UNMAPPED_GUID}"\nB = "{twin}"\nC = "{patterned}"\n')
    review, mapping = tmp_path / "review.json", tmp_path / "mapping.json"
    listed = hashlib.sha256(b"already-reviewed").hexdigest()
    review.write_text(json.dumps({"keep_guids": {}, "extra_scrub_guids": [], "role_context_regex": "roleDefinition",
                                  "rules": [], "gitleaks_known_false_positives": [], "residual_allow": [],
                                  "synthetic_guid_sha256": [listed]}), encoding="utf-8")
    args = ["--src", str(repo), "--review", str(review), "--mapping", str(mapping),
            "--candidates", str(tmp_path / "candidates.txt"), "--accept-synthetic"]
    before = review.read_text(encoding="utf-8")
    for bad in (UNMAPPED_GUID[:4], "8d4", "not-a-guid"):  # ambiguous, too short, not hex
        assert discover.main(args + [bad]) == 2
        assert review.read_text(encoding="utf-8") == before
    printed = capsys.readouterr().out
    assert "8d4e...: matches 2 unlisted GUIDs, need exactly 1" in printed and UNMAPPED_GUID not in printed
    assert discover.main(args) == 0
    sha = {g: hashlib.sha256(g.encode()).hexdigest() for g in (UNMAPPED_GUID, twin, patterned)}
    assert json.loads(review.read_text(encoding="utf-8"))["synthetic_guid_sha256"] == sorted([listed, sha[patterned]])
    assert discover.main(args + [twin.upper()]) == 0
    assert json.loads(review.read_text(encoding="utf-8"))["synthetic_guid_sha256"] == sorted(
        [listed, sha[patterned], sha[twin]])
    assert "UNLISTED 8d4e... first seen tests/test_ids.py:1" in capsys.readouterr().out


WORKFLOW = ROOT / ".github" / "workflows" / "public-sync.yml"


@pytest.mark.skipif(not WORKFLOW.is_file(), reason="the workflow is not part of the public copy")
def test_public_sync_workflow_contract():
    text = WORKFLOW.read_text(encoding="utf-8")
    on = text[text.index("\non:\n"):text.index("\npermissions:")]
    assert re.sub(r"\s+", " ", on).strip() == (
        "on: workflow_run: workflows: [deploy-staging] types: [completed] branches: [main]")
    assert "pull_request" not in text
    assert "github.event.workflow_run.conclusion == 'success' && vars.PUBLIC_REPO != ''" in text
    assert "\n    environment: public-sync\n" in text
    assert "\npermissions:\n  contents: read\n" in text and "write" not in text.split("jobs:")[0]
    assert "permissions:" not in text.split("jobs:")[1]
    assert "\n  group: public-sync\n  cancel-in-progress: false\n" in text
    checkout = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1"
    assert re.findall(r"uses: (\S+.*)", text) == [checkout]
    deploy = ROOT / ".github" / "workflows" / "deploy-staging.yml"
    if deploy.is_file():
        assert checkout in deploy.read_text(encoding="utf-8")
    assert "ref: ${{ github.event.workflow_run.head_sha }}" in text and "persist-credentials: false" in text
    assert "GITLEAKS_SHA256: 551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb" in text
    assert text.index("sha256sum -c") < text.index('"$RUNNER_TEMP/bin/gitleaks" version')
    assert "41898282+github-actions[bot]@users.noreply.github.com" in text
    assert 'commit -q --allow-empty -m "Release $VERSION"' in text
    # The token is only read from the environment by the credential helper or gh, never placed in a URL.
    assert "x-access-token:" not in text and "@github.com" not in text


def test_a_known_false_positive_needs_a_positive_count():
    entry = {"rule": "jwt", "file": "tests/a.py"}
    assert verify_export.known_pairs([entry]) is None
    assert verify_export.known_pairs([{**entry, "count": True}]) is None
    assert verify_export.known_pairs([{**entry, "count": 0}]) is None
    assert verify_export.known_pairs([{**entry, "count": 2}]) == {("jwt", "tests/a.py"): 2}


def test_link_text_kept_through_a_named_group_stays_checked():
    rules = verify_export.dropping_rules({"rules": [{
        "type": "regex", "drops_tokens": True, "find": r"\[(?P<text>[^\]]*)\]\(https://x/[^)]*\)",
        "replace": r"\g<text>"}]})
    text = "[`" + "a" * 40 + "`](https://x/" + "b" * 40 + ")"
    assert verify_export.removed_spans(text, rules) == [(0, 1), (43, len(text))]


TEAMS_HOST = "teams." + "microsoft.com"
TEAMS_ID_BODY = "a1B2c3D4e5F6g7H8i9J0"
HEX32 = "0123456789abcdef" * 2
THREAD = "@thread" + ".tacv2"
SYNTHETIC_TEAMS_ALLOW = [
    {"pattern": r"^teams-id:19:synthetic-personal-route$", "why": "synthetic"},
]


@pytest.mark.parametrize("text, kind", [
    (f"see https://{TEAMS_HOST}/l/channel/{TEAMS_ID_BODY}/General?groupId=x\n", "teams-link"),
    ("open https://teams." + "cloud.microsoft/l/message/abc\n", "teams-link"),
    (f"join https://{TEAMS_HOST}/meet/{TEAMS_ID_BODY}?p=Zz9Yy8\n", "teams-link"),
    (f"join https://{TEAMS_HOST}/v2/\n", "teams-link"),
    (f"join https://{TEAMS_HOST}/_#/conversations\n", "teams-link"),
    ("join https://teams." + f"live.com/meet/{TEAMS_ID_BODY}\n", "teams-link"),
    ("open msteams" + ":/l/chat/0/0?users=x\n", "teams-link"),
    ("open https://gov.teams." + "microsoft.us/l/meetup-join/x\n", "teams-link"),
    ("open https://teams." + "microsoft.us/l/x\n", "teams-link"),
    ("id = '19:" + TEAMS_ID_BODY + THREAD + "'\n", "teams-id"),
    ("id = '19%3A" + TEAMS_ID_BODY + "%40thread" + ".tacv2'\n", "teams-id"),
    ("id = '" + TEAMS_ID_BODY + "@thread" + ".skype'\n", "teams-id"),
    ("id = '" + TEAMS_ID_BODY + "%40thread" + ".v2'\n", "teams-id"),
    ("id = '19%253A" + TEAMS_ID_BODY + "'\n", "teams-id"),
    ("id = '" + TEAMS_ID_BODY + "@unq" + ".gbl.spaces'\n", "teams-id"),
    # the 19: part is too short to match and the hex body is kept as a hash; only the suffix is left to catch it
    ('id = "19:" + "' + HEX32 + '" + "@thread' + '.v2"\n', "teams-id"),
    ('id = f"19:{h}@thread' + '.v2"\n', "teams-id"),
])
def test_verify_fails_on_teams_links_and_ids_without_printing_them(repo, tmp_path, mapping_file, fake_gitleaks,
                                                                   capsys, text, kind):
    commit_test_file(repo, text)
    base, out = export(repo, tmp_path, mapping_file)
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1
    printed = capsys.readouterr().out
    assert f"UNREVIEWED {kind} " in printed and "tests/test_ids.py:1" in printed
    assert TEAMS_ID_BODY not in printed and HEX32 not in printed


def test_verify_passes_bare_teams_domain_prose_and_the_allowlisted_synthetic_id_but_not_a_lookalike(
        repo, tmp_path, mapping_file, fake_gitleaks):
    commit_test_file(repo, 'A = "19:synthetic-personal-route"\n# the ' + TEAMS_HOST + ' tenant, and teams.live.com too\n')
    base, out = export(repo, tmp_path, mapping_file)
    mapping_file.write_text(json.dumps({**MAPPING, "residual_allow": MAPPING["residual_allow"] + SYNTHETIC_TEAMS_ALLOW}),
                            encoding="utf-8")
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 0
    for d in (out, base):
        write(d, "tests/test_ids.py", 'A = "19:synthetic-personal-' + 'route2"\n')
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1


# Joined at runtime: a "{name}-...demo" + host suffix literal would itself trip the export's azure-host sweep.
AI_SUFFIX = ".services.ai.azure.com"


def use_review(mapping_file: Path, rules: bool = True) -> None:
    """The fixture mapping plus review.json's residual_allow and, unless rules=False, its rules."""
    r = review()
    mapping_file.write_text(json.dumps({**MAPPING, "rules": MAPPING["rules"] + (r["rules"] if rules else []),
                                        "residual_allow": MAPPING["residual_allow"] + r["residual_allow"]}),
                            encoding="utf-8")


@needs_review
def test_environment_names_owner_id_and_plain_repo_mentions_get_example_values():
    env, short, owner_id, plain, sha = (review_rule(i)["find"] for i in (
        "resource-stem-env", "resource-stem-short", "github-owner-id", "this-repo-plain", "private-commit-sha"))
    text = (f"rg-{env}-stage rg-{env}. project {env}-stage st{env}demo kv-{env.upper()}-de\n"
            f"https://cog-{short}-memory-demo{AI_SUFFIX}/api/projects/{env}-stage vm-{short}-controller\n"
            f"repo:{owner_id}/foundry-agents@0000000000:environment:staging\n"
            f"保存於 {plain}；見 [#1]({PRIVATE_REPO}/pull/1)\n"
            f"本機倉庫(`{plain}`),commit `{sha}`；main-{sha}採用\n")
    assert scrub_export.Scrubber({**MAPPING, "rules": review()["rules"]}).scrub(text, "docs/x.md") == (
        "rg-faexample-stage rg-faexample. project faexample-stage stfaexampledemo kv-faexample-de\n"
        "https://cog-fasample-memory-demo.services.ai.azure.com/api/projects/faexample-stage vm-fasample-controller\n"
        "repo:example-owner@0000000/foundry-agents@0000000000:environment:staging\n"
        "保存於 原私有 repo；見 #1\n"
        "本機倉庫(`原私有 repo`),commit `00000000`；main-00000000採用\n")


@needs_review
def test_scrubbed_stage_config_stays_valid_json_with_example_names():
    scrubber = scrub_export.Scrubber({**MAPPING, "rules": review()["rules"]})

    def scrubbed_json(rel: str) -> dict:
        return json.loads(scrubber.scrub((ROOT / rel).read_text(encoding="utf-8"), rel))

    stage = scrubbed_json("deploy/stage/stage.json")
    assert (stage["resource_group"], stage["foundry_resource_group"], stage["foundry_account"], stage["project"]) == (
        "rg-faexample-stage", "rg-faexample", "cog-fasample-memory-demo", "faexample-stage")
    assert stage["project_endpoint"] == (
        "https://cog-fasample-memory-demo.services.ai.azure.com/api/projects/faexample-stage")
    ci = scrubbed_json("infra/shared-context/stage-ci-identity.parameters.json")
    assert ci["parameters"]["federatedSubject"]["value"] == (
        "repo:example-owner@0000000/foundry-agents@0000000000:environment:staging:ref:refs/heads/main")


@needs_review
def test_tenant_name_abbreviations_and_case_variants_become_the_placeholder():
    name, abbr = (review_rule(i)["find"] for i in ("tenant-name", "tenant-name-abbrev"))
    text = (f"原{abbr}帳號、原{abbr.lower()}聊天室、{abbr.upper()}管理中心\n"
            f"{name} {name.upper()} {name.lower()}；租戶是 {name} admin\n")
    assert scrub_export.Scrubber({**MAPPING, "rules": review()["rules"]}).scrub(text, "docs/x.md") == (
        "原Contoso帳號、原Contoso聊天室、Contoso管理中心\n"
        "Contoso Contoso Contoso；租戶是 Contoso admin\n")


@needs_review
def test_v18_storage_account_names_get_example_values():
    stem = review_rule("storage-v18-stem")["find"]
    scrubber = scrub_export.Scrubber({**MAPPING, "rules": review()["rules"]})
    assert scrubber.scrub(f"{stem}bk {stem.upper()}demo/workstate\n", "docs/x.md") == (
        "stfav18examplebk stfav18exampledemo/workstate\n")
    rel = "deploy/shared-context/environment.example.json"
    names = json.loads(scrubber.scrub((ROOT / rel).read_text(encoding="utf-8"), rel))["names"]
    assert (names["storage"], names["backup_storage"], names["registry"], names["foundry_account"]) == (
        "stfav18exampledemo", "stfav18examplebk", "crfaagentv18demo", "cog-fa-agent-v18-demo")


@needs_review
def test_v18_webpubsub_name_gets_the_example_value():
    name = review_rule("webpubsub-v18-name")["find"]
    scrubber = scrub_export.Scrubber({**MAPPING, "rules": review()["rules"]})
    assert scrubber.scrub(f'"webpubsub": "{name}", {name.upper()}\n', "docs/x.md") == (
        '"webpubsub": "wps-fa-v18-example", wps-fa-v18-example\n')
    rel = "deploy/shared-context/environment.example.json"
    names = json.loads(scrubber.scrub((ROOT / rel).read_text(encoding="utf-8"), rel))["names"]
    assert names["webpubsub"] == "wps-fa-v18-example"
    # The naming code derives the name from an f-string, not the literal, so the rule leaves the code intact.
    code = "deploy/shared-context/prepare_environment.py"
    derive = '"webpubsub": _name(source, "webpubsub", f"wps-fa-agent-{suffix}"),'
    assert derive in (ROOT / code).read_text(encoding="utf-8")
    assert derive in scrubber.scrub((ROOT / code).read_text(encoding="utf-8"), code)


@needs_review
@pytest.mark.parametrize("rule_id, case", [
    ("tenant-name", str.upper), ("tenant-name", str.lower),
    ("tenant-name-abbrev", str.upper), ("tenant-name-abbrev", str.lower),
    ("tenant-name-abbrev", lambda s: s + "x"),  # a longer form the bounded scrub leaves still fails [2]
])
def test_verify_fails_when_a_tenant_name_variant_comes_back(repo, tmp_path, mapping_file, fake_gitleaks, capsys,
                                                            rule_id, case):
    value = case(review_rule(rule_id)["find"])
    base, out = export(repo, tmp_path, mapping_file)
    use_review(mapping_file)
    for d in (out, base):
        write(d, "docs/leak.md", f"原{value}帳號\n")
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1
    printed = capsys.readouterr().out
    assert f"PRESENT rule {rule_id} at docs/leak.md:1" in printed
    assert value.lower() not in printed.lower()


@needs_review
@pytest.mark.parametrize("rule_id, template", [
    ("resource-stem-env", "resource_group: rg-{}-stage\n"),
    ("resource-stem-short", "endpoint: https://cog-{}-memory-demo" + AI_SUFFIX + "\n"),
    ("this-repo-plain", "保存於 {}\n"),
    ("github-owner-id", "sub: repo:{}/foundry-agents@0000000000:environment:staging\n"),
    ("private-commit-sha", "commit `{}`\n"),
    ("tenant-name-abbrev", "原{}帳號\n"),
    ("storage-v18-stem", '"backup_storage": "{}bk"\n'),
    ("webpubsub-v18-name", '"webpubsub": "{}"\n'),
])
def test_verify_fails_when_a_real_name_comes_back(repo, tmp_path, mapping_file, fake_gitleaks, capsys, rule_id,
                                                  template):
    value = review_rule(rule_id)["find"]
    base, out = export(repo, tmp_path, mapping_file)
    use_review(mapping_file)
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 0
    capsys.readouterr()
    for d in (out, base):
        write(d, "docs/leak.md", template.format(value))
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1
    printed = capsys.readouterr().out
    assert f"PRESENT rule {rule_id} at docs/leak.md:1" in printed
    assert value not in printed


@needs_review
@pytest.mark.parametrize("real_owner", [True, False])
def test_verify_fails_when_the_oidc_subject_names_an_owner_other_than_the_placeholder(
        repo, tmp_path, mapping_file, fake_gitleaks, capsys, real_owner):
    # A zeroed ID is not enough: the subject must not pair a real owner with the private repo name.
    owner = review_rule("github-owner-id")["find"].split("@")[0] if real_owner else "octo-owner"
    base, out = export(repo, tmp_path, mapping_file)
    use_review(mapping_file)
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 0
    capsys.readouterr()
    subject = f"sub: repo:{owner}@" + "0000000/foundry-agents@" + "0000000000:environment:staging\n"
    for d in (out, base):
        write(d, "docs/leak.md", subject)
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1
    printed = capsys.readouterr().out
    assert f"UNREVIEWED github-id {owner[:4]}... x1 e.g. docs/leak.md:1" in printed and owner not in printed
    assert ("PRESENT rule github-owner-id absent-regex at docs/leak.md:1" in printed) is real_owner


@needs_review
def test_azure_host_allowlist_passes_only_reviewed_hosts_by_full_name(repo, tmp_path, mapping_file, fake_gitleaks,
                                                                      capsys):
    short = review_rule("resource-stem-short")["find"]
    commit_test_file(repo, f'A = "cog-fasample-memory-demo{AI_SUFFIX}"\n')
    base, out = export(repo, tmp_path, mapping_file)
    use_review(mapping_file, rules=False)  # residual sweep only: no stem rule to catch the real name in [2]
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 0
    capsys.readouterr()
    # The real endpoint after only the suffix scrub passed the old "...demo." pattern; so would any other prefix.
    for host in (f"cog-{short}-memory-demo", "cog-notreviewed-memory-demo", "cog-fasample-memory-demo2"):
        for d in (out, base):
            write(d, "tests/test_ids.py", f'A = "{host}{AI_SUFFIX}"\n')
        assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1
        printed = capsys.readouterr().out
        assert "UNREVIEWED azure-host cog-... x1 e.g. tests/test_ids.py:1" in printed and host not in printed


@needs_review
@pytest.mark.parametrize("subject, ok", [
    ("repo:octo-owner@" + "4815162/octo-repo@" + "2342108:environment:staging", False),
    ("repo:octo-owner@" + "0000000/octo-repo@" + "0000000000:environment:staging", False),  # zeros alone are not enough
    ("repo:example-owner@" + "0000000/foundry-agents@" + "0000000000:environment:staging", True),
    ("Microsoft.Storage/storageAccounts@2023-05-01", True),  # Bicep API versions are not IDs
])
def test_verify_flags_github_numeric_owner_and_repo_ids(repo, tmp_path, mapping_file, fake_gitleaks, capsys,
                                                       subject, ok):
    commit_test_file(repo, f'SUB = "{subject}"\n')
    base, out = export(repo, tmp_path, mapping_file)
    use_review(mapping_file, rules=False)  # the production github-id allow pattern, not a copy of it
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == (0 if ok else 1)
    printed = capsys.readouterr().out
    if not ok:
        assert printed.count("UNREVIEWED github-id octo... x1 e.g. tests/test_ids.py:1") == 2
        assert "4815162" not in printed and "2342108" not in printed


FAKE_SUFFIX = "ab2cd3ef4gh5i"  # a 13-character uniqueString-like suffix with digits; prefixes are joined at runtime
LETTER_SUFFIX = "qwertyuiopasd"  # one without digits, which the fallback rule leaves


@needs_review
def test_azd_generated_names_get_example_tokens():
    t1, t2 = (review_rule(i)["find"] for i in ("azd-token-1", "azd-token-2"))
    text = " ".join([f"cog-{t1}", f"cog-{t2}", f"cr{t2}", f"stbk{t2}", f"stcon{t2}.table.core.windows.net", "cog-" + FAKE_SUFFIX,
                     "st" + FAKE_SUFFIX, "crcontosodemo01", "storageservices", "rg-fazd1012", "fazd1012"])
    assert scrub_export.Scrubber({**MAPPING, "rules": review()["rules"]}).scrub(text, "docs/x.md") == (
        "cog-example000001 cog-example000002 crexample000002 stbkexample000002 stconexample000002.table.core.windows.net "
        "cog-example000000 stexample000000 crcontosodemo01 storageservices rg-fazd1012 fazd1012")


@needs_review
def test_verify_passes_scrubbed_azd_names_and_fails_on_a_token_the_rules_leave(repo, tmp_path, mapping_file,
                                                                              fake_gitleaks, capsys):
    commit_test_file(repo, "A = 'cog-" + FAKE_SUFFIX + "'\nB = 'wps-" + FAKE_SUFFIX + ".webpubsub.azure.com'\n")
    use_review(mapping_file)
    base, out = export(repo, tmp_path, mapping_file)
    assert (out / "tests/test_ids.py").read_text(encoding="utf-8") == (
        "A = 'cog-example000000'\nB = 'wps-example000000.webpubsub.azure.com'\n")
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 0
    capsys.readouterr()
    for d in (out, base):
        write(d, "tests/test_ids.py", "A = 'cog-" + LETTER_SUFFIX + "'\n")
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == 1
    printed = capsys.readouterr().out
    assert "UNREVIEWED generated-name cog-... x1 e.g. tests/test_ids.py:1" in printed and LETTER_SUFFIX not in printed


@pytest.mark.parametrize("name, ok", [
    ("cog-" + LETTER_SUFFIX, False), ("cr" + FAKE_SUFFIX, False), ("stcon" + FAKE_SUFFIX, False),
    ("cog-example000000", True), ("st" + FAKE_SUFFIX + "x", True), ("rg-fazd1012", True),
])
def test_verify_sweeps_azd_generated_names(repo, tmp_path, mapping_file, fake_gitleaks, capsys, name, ok):
    commit_test_file(repo, f"NAME = '{name}'\n")
    base, out = export(repo, tmp_path, mapping_file)
    placeholders = {"pattern": r"^generated-name:(cog-|wps-|stbk|stcon|cr|st)example\d{6}$", "why": "placeholder"}
    mapping_file.write_text(json.dumps({**MAPPING, "residual_allow": MAPPING["residual_allow"] + [placeholders]}),
                            encoding="utf-8")
    assert verify(out, base, mapping_file, fake_gitleaks["binary"]) == (0 if ok else 1)
    if not ok:
        assert f"UNREVIEWED generated-name {name[:4]}... x1 e.g. tests/test_ids.py:1" in capsys.readouterr().out
