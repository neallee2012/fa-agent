#!/usr/bin/env python3
"""Fail-closed check of a scrubbed export (or of one text, e.g. release notes) before it goes public.

Usage:
  python verify_export.py --out <export> --baseline <plain export> --mapping <mapping.json> [--gitleaks <bin>]
  python verify_export.py --text <in.txt> --text-out <out.txt> --mapping <mapping.json> [--gitleaks <bin>]

Exit 0 only when every check passes; otherwise 1. --text scrubs the input with the same mapping, verifies the
result the same way and writes --text-out only on success.
  0. (tree) export and baseline list the same files and no excluded path is present
  1. gitleaks ran hermetically (default rules, no .gitleaks.toml / .gitleaksignore in the tree, gitleaks:allow
     ignored, GITLEAKS_CONFIG* cleared), scanned a non-zero byte count and every finding is a known false
     positive: an exact (rule, file) pair in gitleaks_known_false_positives, no more often than its `count`
  2. no original mapped value (GUID, GUID prefix, rule find / verify_absent) is present
  3. every long hex / base64 token of the baseline is unchanged unless it embeds an original value or lies
     inside a span removed by a drops_tokens rule (private-repo URLs; the link text such a rule keeps is checked)
  4. the residual sweep: every GUID anywhere (tests/ included) is a mapping placeholder, in keep_guids or in
     synthetic_guid_sha256; /subscriptions/ IDs, Azure hostnames, emails, IPv4, Teams links, Teams thread /
     conversation IDs, GitHub numeric owner / repo IDs and azd generated resource names (SWEEPS: teams-link,
     teams-id, github-id, generated-name) are in
     residual_allow (review.json lists scrubbed account hosts by full, anchored name, so a replaced suffix does
     not let the rest of a real name through)
Output ends up in CI logs, so it names file:line and never a full value. [2] prints no character of an original
value at all: the rule ID, the GUID's placeholder or the position of a prefix / verify_absent string. [3] and
[4] print at most a 4-character prefix of the token or residual they flag.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrub_export import GUID_RE, Scrubber, decode_text, guid_sha256, is_excluded  # noqa: E402

SWEEPS = {
    "subscription-id": re.compile(r"/subscriptions/([^/\s\"'`)<>{}$]+)"),
    "azure-host": re.compile(r"(?i)\b[a-z0-9][a-z0-9.-]*\.(?:azurecontainerapps\.io|azurecr\.io|core\.windows\.net|"
                             r"services\.ai\.azure\.com|vault\.azure\.net|webpubsub\.azure\.com|openai\.azure\.com|"
                             r"cognitiveservices\.azure\.com|azurewebsites\.net|applicationinsights\.azure\.com)\b"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "ipv4": re.compile(r"(?<![0-9.])(?:\d{1,3}\.){3}\d{1,3}(?![0-9.]|\.\d)"),
    # Any Teams URL with a path (join, meeting, deep link, v2 web app, gov cloud) or an ms-teams scheme deep link; a bare domain in prose is fine.
    "teams-link": re.compile(r"(?i)\bteams\.cloud\.microsoft[^\s\"'`)<>]*|\bteams\.(?:microsoft\.(?:com|us)|live\.com)/[^\s\"'`)<>]+|\bmsteams:[^\s\"'`)<>]*"),
    # The thread suffix alone matches too, which catches most IDs split across literals; a suffix split itself
    # (as in a test fixture) is not caught.
    "teams-id": re.compile(r"(?i)(?<![0-9A-Za-z])19(?::|%3A|%253A)[A-Za-z0-9._-]{20,}|[A-Za-z0-9:%._-]*(?:@|%40)thread\.(?:tacv2|v2|skype)|[A-Za-z0-9:%._-]*@unq\.gbl\.spaces"),
    # GitHub's immutable OIDC subject names owner and repo as name@<numeric ID>; 5+ digits skip Bicep API versions.
    "github-id": re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9][A-Za-z0-9-]*@\d{5,}(?![0-9A-Za-z])"),
    # infra/azd/main.bicep names: a prefix plus a 13-character uniqueString token (with or without a digit).
    "generated-name": re.compile(r"(?<![A-Za-z0-9_-])(?:cog-|wps-|stbk|stcon|cr|st)[a-z0-9]{13}(?![A-Za-z0-9_])"),
}
HASHY = re.compile(r"(?<![0-9A-Za-z+/])(?:[0-9a-f]{32,}|[0-9A-F]{32,}|[A-Za-z0-9+/]{40,}={0,2})(?![0-9A-Za-z+/])")
SCANNED = re.compile(r"scanned ~(\d+) bytes")
GITLEAKS_FILES = frozenset({".gitleaks.toml", ".gitleaksignore"})


def redact(value: str) -> str:
    return value[:4] + "..."


def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def read_tree(root: Path) -> tuple[dict[str, str], list[str]]:
    """({relative path: text} for UTF-8 files, every relative path)."""
    texts, paths = {}, []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if not p.is_file() or ".git" in p.relative_to(root).parts:
            continue
        paths.append(rel)
        text = decode_text(p.read_bytes())
        if text is not None:
            texts[rel] = text
    return texts, paths


def run_gitleaks(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    """Process boundary; tests replace it."""
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)


def check_gitleaks(root: Path, known: list[dict], binary: str, min_bytes: int) -> bool:
    exe = shutil.which(binary) or (binary if Path(binary).is_file() else None)
    if not exe:
        print(f"[1] gitleaks: FAIL, not found: {binary}")
        return False
    # Nothing in the scanned tree or the environment may relax the scan: a .gitleaks.toml at the root would
    # replace the default rules, .gitleaksignore and gitleaks:allow comments would hide findings.
    planted = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.name in GITLEAKS_FILES)
    if planted:
        print(f"[1] gitleaks: FAIL, the tree holds gitleaks config: {', '.join(planted[:10])}")
        return False
    env = {k: v for k, v in os.environ.items() if k.upper() not in ("GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML")}
    with tempfile.TemporaryDirectory() as td:
        report = Path(td) / "gitleaks.json"
        no_ignore = Path(td) / "no-ignore"
        no_ignore.mkdir()
        try:
            proc = run_gitleaks([exe, "dir", str(root), "--redact", "--no-banner", "--report-format", "json",
                                 "--report-path", str(report), "--exit-code", "0", "--ignore-gitleaks-allow",
                                 "--gitleaks-ignore-path", str(no_ignore)], env)
        except OSError as exc:
            print(f"[1] gitleaks: FAIL, could not run ({type(exc).__name__})")
            return False
        log = (proc.stderr or "") + (proc.stdout or "")
        scanned = SCANNED.search(log)
        if proc.returncode != 0 or not scanned or int(scanned.group(1)) < max(min_bytes, 1) or not report.is_file():
            # A swallowed error once looked like "0 scanned, exit 0"; anything short of a real scan fails.
            print(f"[1] gitleaks: FAIL, exit {proc.returncode}, "
                  f"scanned {scanned.group(1) + ' bytes' if scanned else 'unknown'} (need {max(min_bytes, 1)}), "
                  f"report {report.is_file()}")
            for line in log.splitlines():
                if " FTL " in line or " ERR " in line:
                    print("    " + line[:200])
            return False
        try:
            findings = json.loads(report.read_text(encoding="utf-8") or "null")
        except ValueError:
            findings = None
    if not isinstance(findings, list):
        print("[1] gitleaks: FAIL, unreadable report")
        return False
    pairs = known_pairs(known)
    if pairs is None:
        return False
    found: dict[tuple[str, str], list[str]] = defaultdict(list)
    for f in findings:
        try:
            rel = Path(f["File"]).resolve().relative_to(root.resolve()).as_posix()
        except (KeyError, ValueError):
            rel = "?"
        found[(str(f.get("RuleID")), rel)].append(str(f.get("StartLine")))
    unknown, known_hits = [], []
    for (rule, rel), lines in sorted(found.items()):
        if (rule, rel) not in pairs:
            unknown += [f"{rule} {rel}:{ln}" for ln in lines]
        elif len(lines) > pairs[(rule, rel)]:
            # One more fake key in an already-listed file is still a new finding.
            unknown.append(f"{rule} {rel}: {len(lines)} findings, {pairs[(rule, rel)]} known (lines {', '.join(lines[:10])})")
        else:
            known_hits.append((rule, rel, len(lines)))
    print(f"[1] gitleaks: scanned {scanned.group(1)} bytes, {len(findings)} findings, {len(unknown)} unknown")
    for rule, rel, n in known_hits:
        print(f"    known FP {n:4d}  {rule} {rel}")
    for rule, rel in sorted(set(pairs) - set(found)):
        print(f"    unused known FP  {rule} {rel}")
    for u in unknown:
        print(f"    UNKNOWN {u}")
    return not unknown


def known_pairs(known: list[dict]) -> dict[tuple[str, str], int] | None:
    """{(gitleaks RuleID, exact path): max findings}; None when an entry is not an exact pair with a count."""
    pairs = {}
    for k in known:
        rule, rel, count = k.get("rule"), k.get("file"), k.get("count")
        # A missing count would allow any number of hits in that file, so a new real secret could hide there.
        if (not isinstance(rule, str) or not isinstance(rel, str) or not rule or not rel
                or any(c in rule + rel for c in "*?[]") or type(count) is not int or count < 1):
            print("[1] gitleaks: FAIL, a known false positive needs an exact rule, file and count >= 1, no wildcards: "
                  f"{k.get('rule')} {k.get('file')}")
            return None
        pairs[(rule, rel)] = count
    return pairs


def original_patterns(mapping: dict) -> list[tuple[str, re.Pattern]]:
    """(name, pattern) per original value. A name never holds any character of the original: GUIDs are named by
    their public placeholder, prefixes and verify_absent strings by position."""
    checks = []
    for g, placeholder in mapping["guids"].items():
        checks.append((f"guid -> {placeholder}",
                       re.compile(rf"(?<![0-9A-Za-z]){re.escape(g)}(?![0-9A-Za-z])", re.I)))
        checks.append((f"guid-nohyphen -> {placeholder}", re.compile(re.escape(g.replace("-", "")), re.I)))
    if mapping.get("guid_prefix_scrub"):
        prefixes = sorted({g[:8].lower() for g in mapping["guids"]} |
                          {p.lower() for p in mapping.get("truncated_prefixes", [])})
        for i, p in enumerate(prefixes, 1):
            checks.append((f"guid-prefix #{i}", re.compile(rf"(?<![0-9A-Za-z]){p}(?![0-9A-Za-z])", re.I)))
    for r in mapping["rules"]:
        if r["type"] == "literal":
            checks.append((f"rule {r['id']}", re.compile(re.escape(r["find"]), re.I if r.get("ignore_case") else 0)))
        for i, s in enumerate(r.get("verify_absent", []), 1):
            checks.append((f"rule {r['id']} absent #{i}",
                           re.compile(re.escape(s), re.I if r.get("verify_ignore_case") else 0)))
        if r.get("verify_absent_regex"):
            checks.append((f"rule {r['id']} absent-regex", re.compile(r["verify_absent_regex"])))
    return checks


def check_originals(files: dict[str, str], checks: list[tuple[str, re.Pattern]]) -> bool:
    present = []
    for rel, text in files.items():
        for name, rx in checks:
            for m in rx.finditer(text):
                present.append(f"{name} at {rel}:{line_of(text, m.start())}")
    print(f"[2] originals: {len(checks)} patterns, {len(present)} occurrences left")
    for p in present[:50]:
        print(f"    PRESENT {p}")
    return not present


def dropping_rules(mapping: dict) -> list[tuple[re.Pattern, tuple[int, ...]]]:
    """Rules marked drops_tokens remove spans (e.g. private-repo URLs with SHAs); tokens there may go. The groups
    the replacement keeps (backslash group references), such as a Markdown link's text, stay checked."""
    rules = []
    for r in mapping["rules"]:
        if r.get("drops_tokens") and r["type"] == "regex":
            rx = re.compile(r["find"], re.I if r.get("ignore_case") else 0)
            numbered = {int(g) for g in re.findall(r"\\(?:g<)?(\d+)", r["replace"])}
            named = {rx.groupindex[n] for n in re.findall(r"\\g<([A-Za-z_]\w*)>", r["replace"])}
            rules.append((rx, tuple(sorted(numbered | named))))
    return rules


def removed_spans(text: str, dropping: list[tuple[re.Pattern, tuple[int, ...]]]) -> list[tuple[int, int]]:
    """The character ranges the dropping rules delete: each match minus the groups its replacement keeps."""
    spans = []
    for rx, kept in dropping:
        for m in rx.finditer(text):
            start = m.start()
            for s, e in sorted(m.span(g) for g in kept if m.start(g) >= 0):
                spans.append((start, s))
                start = e
            spans.append((start, m.end()))
    return [(s, e) for s, e in spans if s < e]


def check_hashes(files: dict[str, str], baseline: dict[str, str], checks: list[tuple[str, re.Pattern]],
                 dropping: list[tuple[re.Pattern, tuple[int, ...]]] = ()) -> bool:
    altered = []
    for rel, btext in baseline.items():
        otext = files.get(rel)
        if otext == btext:
            continue
        spans = removed_spans(btext, dropping)
        for m in HASHY.finditer(btext):
            tok = m.group(0)
            if any(s <= m.start() and m.end() <= e for s, e in spans):
                continue
            if (otext is None or tok not in otext) and not any(rx.search(tok) for _, rx in checks):
                altered.append(f"{rel}:{line_of(btext, m.start())} {redact(tok)} len {len(tok)}")
    print(f"[3] hash safety: {len(altered)} hex/base64 tokens altered")
    for a in altered[:50]:
        print(f"    ALTERED {a}")
    return not altered


def guid_reason(guid: str, mapping: dict) -> str | None:
    """Why a GUID may be public, or None. Only these three sources count; residual_allow never covers GUIDs."""
    if guid in {p.lower() for p in mapping["guids"].values()}:
        return "GUID: mapping placeholder"
    if guid in {g.lower() for g in mapping.get("keep_guids", {})}:
        return "GUID: keep_guids (public IDs)"
    if guid_sha256(guid) in set(mapping.get("synthetic_guid_sha256", [])):
        return "GUID: reviewed synthetic (synthetic_guid_sha256)"
    return None


def check_residual(files: dict[str, str], mapping: dict) -> bool:
    allow = [(re.compile(a["pattern"]), a["why"]) for a in mapping.get("residual_allow", [])]
    hits: dict[tuple[str, str], list[str]] = defaultdict(list)
    for rel, text in files.items():
        for ln, line in enumerate(text.splitlines(), 1):
            for m in GUID_RE.finditer(line):  # every path, tests/ included
                hits[("guid", m.group(0).lower())].append(f"{rel}:{ln}")
            for kind, rx in SWEEPS.items():
                for m in rx.finditer(line):
                    hits[(kind, m.group(1) if kind == "subscription-id" else m.group(0))].append(f"{rel}:{ln}")
    unreviewed, allowed = [], Counter()
    for (kind, val), where in sorted(hits.items()):
        if kind == "guid":
            why = guid_reason(val, mapping)
        else:
            why = next((w for rx, w in allow if rx.search(f"{kind}:{val}")), None)
        if why:
            allowed[why] += len(where)
        else:
            unreviewed.append(f"{kind} {redact(val)} x{len(where)} e.g. {where[0]}")
    print(f"[4] residual sweep: {sum(len(w) for w in hits.values())} hits, {len(unreviewed)} distinct unreviewed")
    for why, n in allowed.most_common():
        print(f"    allowed {n:5d}  {why}")
    for u in unreviewed:
        print(f"    UNREVIEWED {u}")
    return not unreviewed


def verify(root: Path, files: dict[str, str], baseline: dict[str, str], mapping: dict, gitleaks: str) -> bool:
    checks = original_patterns(mapping)
    # A partial scan fails too: gitleaks must report at least half the text bytes checked here.
    min_bytes = sum(len(t.encode("utf-8")) for t in files.values()) // 2
    results = [check_gitleaks(root, mapping["gitleaks_known_false_positives"], gitleaks, min_bytes),
               check_originals(files, checks),
               check_hashes(files, baseline, checks, dropping_rules(mapping)),
               check_residual(files, mapping)]
    return all(results)


def verify_tree(out: Path, base: Path, mapping: dict, gitleaks: str) -> bool:
    if not out.is_dir() or not base.is_dir():
        print("FAIL: --out and --baseline must be existing directories")
        return False
    files, out_paths = read_tree(out)
    baseline, base_paths = read_tree(base)
    excluded = [p for p in out_paths if is_excluded(p)]
    mismatch = sorted(set(out_paths) ^ set(base_paths))
    print(f"[0] file set: {len(out_paths)} files, {len(mismatch)} differ from baseline, {len(excluded)} excluded present")
    for p in (mismatch + excluded)[:50]:
        print(f"    UNEXPECTED {p}")
    ok = bool(out_paths) and not mismatch and not excluded
    return verify(out, files, baseline, mapping, gitleaks) and ok


def verify_text(src: Path, dest: Path, mapping: dict, gitleaks: str) -> bool:
    original = src.read_text(encoding="utf-8")
    scrubbed = Scrubber(mapping).scrub(original, "release-notes")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "release-notes.md").write_text(scrubbed, encoding="utf-8", newline="\n")
        ok = bool(scrubbed.strip()) and verify(root, {"release-notes.md": scrubbed},
                                               {"release-notes.md": original}, mapping, gitleaks)
    if ok:
        dest.write_text(scrubbed, encoding="utf-8", newline="\n")
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mapping", required=True)
    ap.add_argument("--gitleaks", default="gitleaks", help="gitleaks binary (name on PATH or path)")
    ap.add_argument("--out", help="scrubbed export to verify")
    ap.add_argument("--baseline", help="plain export of the same commit (scrub_export.py --plain)")
    ap.add_argument("--text", help="text to scrub and verify")
    ap.add_argument("--text-out", help="where the scrubbed text is written when it passes")
    args = ap.parse_args(argv)
    mapping = json.loads(Path(args.mapping).read_text(encoding="utf-8"))
    if args.text and args.text_out and not args.out:
        ok = verify_text(Path(args.text), Path(args.text_out), mapping, args.gitleaks)
    elif args.out and args.baseline and not args.text:
        ok = verify_tree(Path(args.out).resolve(), Path(args.baseline).resolve(), mapping, args.gitleaks)
    else:
        ap.error("use either --out with --baseline, or --text with --text-out")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
