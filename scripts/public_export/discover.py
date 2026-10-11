#!/usr/bin/env python3
"""Find GUIDs at a checkout's HEAD and rebuild mapping.json from the hand-reviewed review.json.

Usage:  python discover.py --src <checkout> [--review review.json] [--mapping mapping.json] [--candidates candidates.txt]
                           [--accept-synthetic [GUID_OR_PREFIX ...]]

review.json: keep_guids {guid: reason}, extra_scrub_guids [guid], truncated_prefixes {values: [...]},
role_context_regex, synthetic_guid_sha256 [sha256], rules, gitleaks_known_false_positives, residual_allow (the
last four are copied as-is). Writes mapping.json (see scrub_export.py) and candidates.txt (every GUID with its
decision and up to four contexts, for review; git-ignored, never exported).

GUIDs are scrubbed when they occur outside tests/ and are neither kept nor synthetic; test-only GUIDs are
scrubbed only when listed in extra_scrub_guids. A GUID already in mapping.json keeps its placeholder and stays
mapped even after it leaves the repo, so placeholders do not shift between releases and verify keeps checking it.
Every other GUID left in the scrubbed export must be in synthetic_guid_sha256 or verify fails; discover lists the
unlisted ones (4-character prefix). After reading their context in candidates.txt, --accept-synthetic adds to that
list only what it is told: each GUID named on the command line (in full, or by a prefix of 4+ characters that
matches exactly one unlisted GUID) plus the unlisted GUIDs is_synthetic() recognises. Anything else stays unlisted.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import OrderedDict, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from scrub_export import GUID_RE, Scrubber, decode_text, guid_sha256, head_files, is_synthetic  # noqa: E402

FIRST_PLACEHOLDER = 0x10  # ...000000000000 to ...000000000003 are already used by tests


def placeholder(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012x}"


def select_accepted(pending: list[str], named: list[str]) -> tuple[dict[str, str], list[str]]:
    """({unlisted GUID: why}, errors) for --accept-synthetic: each named value must be a full GUID or a prefix of
    4+ characters matching exactly one unlisted GUID; unlisted GUIDs is_synthetic() recognises come along."""
    accepted, errors = {}, []
    for value in named:
        v = value.strip().lower()
        hits = [g for g in pending if g.startswith(v)] if re.fullmatch(r"[0-9a-f-]{4,36}", v) else None
        if hits is None:
            errors.append(f"{v[:4]}...: not a GUID or a prefix of at least 4 characters")
        elif len(hits) != 1:
            errors.append(f"{v[:4]}...: matches {len(hits)} unlisted GUIDs, need exactly 1")
        else:
            accepted[hits[0]] = "named"
    for g in pending:
        if is_synthetic(g):
            accepted.setdefault(g, "synthetic pattern")
    return accepted, errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", required=True)
    ap.add_argument("--review", default=str(HERE / "review.json"))
    ap.add_argument("--mapping", default=str(HERE / "mapping.json"))
    ap.add_argument("--candidates", default=str(HERE / "candidates.txt"))
    ap.add_argument("--accept-synthetic", nargs="*", metavar="GUID_OR_PREFIX",
                    help="after reviewing candidates.txt: add to review.json synthetic_guid_sha256 the sha256 of the "
                         "unlisted GUIDs named here (full or a 4+ character prefix matching exactly one) and of those "
                         "is_synthetic() recognises")
    args = ap.parse_args(argv)
    review = json.loads(Path(args.review).read_text(encoding="utf-8"))
    mapping_path = Path(args.mapping)
    previous = json.loads(mapping_path.read_text(encoding="utf-8"))["guids"] if mapping_path.is_file() else {}
    keep = {k.lower(): v for k, v in review["keep_guids"].items()}
    extra = {k.lower() for k in review.get("extra_scrub_guids", [])}
    role_ctx = re.compile(review["role_context_regex"], re.I)

    occurrences: dict[str, list[tuple[str, int, str]]] = OrderedDict()
    texts: dict[str, str] = {}
    for rel, (_mode, data) in head_files(Path(args.src)).items():
        text = decode_text(data)
        if text is None:
            continue
        texts[rel] = text
        for ln, line in enumerate(text.splitlines(), 1):
            for m in GUID_RE.finditer(line):
                occurrences.setdefault(m.group(0).lower(), []).append((rel, ln, line.strip()))

    decisions: dict[str, str] = {}
    for guid, occ in occurrences.items():
        if guid in keep:
            decisions[guid] = "keep: " + keep[guid]
        elif guid in extra:
            decisions[guid] = "scrub: listed in review.json extra_scrub_guids"
        elif guid in previous:
            decisions[guid] = "scrub: already in mapping.json"
        elif is_synthetic(guid):
            decisions[guid] = "keep: synthetic/placeholder pattern"
        elif all(o[0].startswith("tests/") for o in occ):
            decisions[guid] = "keep: test-only value (not scrubbed unless listed in extra_scrub_guids)"
        elif any(role_ctx.search(o[2]) for o in occ):
            decisions[guid] = "REVIEW: role-definition context but not in keep_guids -> scrubbed until confirmed"
        else:
            decisions[guid] = "scrub"

    guid_map = OrderedDict((g.lower(), p) for g, p in previous.items() if g.lower() not in keep)
    used = {int(p.rsplit("-", 1)[1], 16) for p in guid_map.values()}
    n = FIRST_PLACEHOLDER
    new = []
    for g, d in decisions.items():
        if d.startswith(("scrub", "REVIEW")) and g not in guid_map:
            while n in used:
                n += 1
            guid_map[g], n = placeholder(n), n + 1
            new.append(g)

    mapping = OrderedDict()
    mapping["_comment"] = ("Generated by discover.py from review.json; holds original values and is never exported. "
                           "Edit review.json, then rerun discover.py.")
    mapping["guids"] = guid_map
    mapping["guid_prefix_scrub"] = True
    mapping["truncated_prefixes"] = review.get("truncated_prefixes", {}).get("values", [])
    mapping["keep_guids"] = keep
    mapping["rules"] = review["rules"]
    mapping["gitleaks_known_false_positives"] = review["gitleaks_known_false_positives"]
    mapping["residual_allow"] = review["residual_allow"]

    # GUIDs the scrubbed export would still hold besides placeholders and keep_guids: synthetic / test values
    # (and any a prefix scrub produces). verify passes them only once their sha256 is in review.json.
    placeholders = set(guid_map.values())
    scrubber = Scrubber(mapping)
    public: dict[str, tuple[str, int]] = OrderedDict()
    for rel, text in texts.items():
        for ln, line in enumerate(scrubber.scrub(text, rel).splitlines(), 1):
            for m in GUID_RE.finditer(line):
                g = m.group(0).lower()
                if g not in placeholders and g not in keep:
                    public.setdefault(g, (rel, ln))
    reviewed = set(review.get("synthetic_guid_sha256", []))
    pending = [g for g in public if guid_sha256(g) not in reviewed]
    if args.accept_synthetic is not None:
        accepted, errors = select_accepted(pending, args.accept_synthetic)
        if errors:
            for e in errors:
                print(f"--accept-synthetic: {e}")
            print("nothing written")
            return 2
        for g, why in accepted.items():
            print(f"    ACCEPTED {g[:4]}... ({why}) first seen {public[g][0]}:{public[g][1]}")
        reviewed |= {guid_sha256(g) for g in accepted}
        review["synthetic_guid_sha256"] = sorted(reviewed)
        Path(args.review).write_text(json.dumps(review, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
                                     newline="\n")
        pending = [g for g in pending if g not in accepted]
    mapping["synthetic_guid_sha256"] = sorted(reviewed)
    mapping_path.write_text(json.dumps(mapping, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")

    def allowlist(g: str) -> str:
        if g not in public:
            return ""
        return "  [synthetic allowlist: " + ("listed" if g not in pending else "NOT LISTED, verify fails") + "]"

    with Path(args.candidates).open("w", encoding="utf-8", newline="\n") as fh:
        for guid, occ in occurrences.items():
            fh.write(f"{guid}  ->  {guid_map.get(guid, 'KEEP')}  [{decisions[guid]}]{allowlist(guid)}\n")
            fh.write(f"    {len(occ)} occurrences in {len({o[0] for o in occ})} files\n")
            for rel, ln, line in occ[:4]:
                fh.write(f"    {rel}:{ln}: {line[:200]}\n")
        for guid, (rel, ln) in public.items():
            if guid not in occurrences:
                fh.write(f"{guid}  only after scrubbing, first at {rel}:{ln}{allowlist(guid)}\n")

    counts = defaultdict(int)
    for d in decisions.values():
        counts[d.split(":")[0]] += 1
    print(f"{len(occurrences)} distinct GUIDs; " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"mapped GUIDs: {len(guid_map)} ({len(new)} new)")
    for g in new:
        print(f"    NEW {g[:4]}... {decisions[g].split(':')[0]} first seen {occurrences[g][0][0]}:{occurrences[g][0][1]}")
    print(f"synthetic allowlist: {len(public) - len(pending)} of {len(public)} exported synthetic/test GUIDs listed"
          + ("" if not pending else "; review candidates.txt, then rerun with --accept-synthetic <GUID or prefix>"))
    for g in pending:
        print(f"    UNLISTED {g[:4]}... first seen {public[g][0]}:{public[g][1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
