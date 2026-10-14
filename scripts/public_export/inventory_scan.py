#!/usr/bin/env python3
"""Gate 1: flag real Azure names in an export that verify_export.py cannot know (they are in no review.json rule).

Usage:
  python inventory_scan.py --export <scrubbed export> --azure-config-dir <dir> --subscription <id> [...]
                           [--save-inventory <file outside the repo>] [--no-graph]
  python inventory_scan.py --export <scrubbed export> --inventory <file outside the repo>

Read-only inventory, through the az CLI with the caller's isolated AZURE_CONFIG_DIR and explicit subscriptions:
  - resource group names and resource names (`az group list`, `az resource list`; child names such as
    account/project are split on "/"),
  - each subscription's display name, tenant display name and default domain (`az account show`),
  - the tenant's verified domain names (`az rest` to Microsoft Graph /organization; skipped with a warning when
    the CLI identity cannot read it, or with --no-graph).
The denylist lives in memory; --save-inventory / --inventory keep it in a file the caller names, which must be
outside this repo and outside the export, so it is never committed or exported.

Every name (4+ characters) and every fragment of 5+ alphanumeric characters (names split on - _ . / and spaces) is
searched case-insensitively in the export's text files as a whole token: the characters around a match must not
be ASCII letters or digits, so `-`, `.`, `/`, spaces, quotes and CJK text all end a token, but a fragment inside
a longer word does not match. Generic Azure and English words (STOPLIST) and the reviewed values in review.json
`inventory_allow` ([{value, why}]) are skipped; nothing else is. Every scan checks the whole export, so a name is
quiet only because a reviewer allowed it in review.json, never because an earlier release already published it.

Output names a hit by `inv-<8 hex of sha256(lowercase token)>`, its kind and file:line:col, never the value: the
reviewer reads the line in the export, then either adds a review.json rule (scrub it) or an inventory_allow entry.
Exit 0 with no hits, 1 with hits, 2 when the inventory cannot be read or a path is refused. An inventory that
would hide everything exits 2 too: a subscription that lists zero resource groups or zero resources (what an
identity without read access gets, with az exit 0), a saved --inventory that is not a non-empty list of
{kind, value} with both resource groups and resources, or a denylist left empty after the skips.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from verify_export import read_tree  # noqa: E402

MIN_NAME = 4
MIN_FRAGMENT = 5
SPLIT_RE = re.compile(r"[-_./\s]+")
ALNUM = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")  # "" (end of text) is not in it
GRAPH_ORG = "https://graph.microsoft.com/v1.0/organization?$select=displayName,verifiedDomains"

# Generic words that name no tenant: Azure service and region words, auto-created resource names and plain
# English. A real, distinctive name never belongs here; reviewed exceptions go to review.json inventory_allow.
STOPLIST = frozenset("""
    account accounts action actions agent agents alert alerts analytics anomalies apim application applications
    appservice assistant automation azure backup basic blob blobs cloud cluster cognitive cognitiveservices
    compute config connection container containers contoso dashboard database default defaultresourcegroup
    deploy deployment detection detector develop development diagnostics disk disks dns domain endpoint
    endpoints enterprise environment environments example failure failures files firewall foundry free gateway
    global group groups hosted hub identity identities insights interface keyvault logic logs loganalytics
    managed microsoft monitor network networks networkwatcher networkwatcherrg onmicrosoft openai operational
    preview private privatelink production professional project projects public queue queues registry
    resource resources sample sandbox search secret secrets server servers service services share shell smart
    snapshot staging standard storage subnet subnets subscription table tables tenant tests testing trial
    vault vaults virtual visual studio webpubsub website workspace workspaces
    eastus eastus2 westus westus2 westus3 centralus northcentralus southcentralus westcentralus canadacentral
    canadaeast brazilsouth northeurope westeurope uksouth ukwest francecentral germanywestcentral
    swedencentral switzerlandnorth norwayeast eastasia southeastasia japaneast japanwest koreacentral
    australiaeast australiasoutheast centralindia southindia westindia uaenorth southafricanorth
""".split())


def token_id(token: str) -> str:
    return "inv-" + hashlib.sha256(token.lower().encode("utf-8")).hexdigest()[:8]


def run_az(args: list[str], env: dict[str, str]) -> tuple[int, str]:
    """Process boundary; tests replace it. Only read commands are ever passed in."""
    exe = shutil.which("az")
    if not exe:
        return 127, ""
    proc = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    return proc.returncode, proc.stdout


def collect_inventory(subscriptions: list[str], config_dir: str, graph: bool = True) -> list[dict]:
    """[{kind, value}] read with az under config_dir. Raises SystemExit(2) when a subscription cannot be read."""
    env = {**os.environ, "AZURE_CONFIG_DIR": config_dir, "AZURE_CORE_ONLY_SHOW_ERRORS": "true",
           "AZURE_CORE_COLLECT_TELEMETRY": "false"}
    items: list[dict] = []

    def az_json(args: list[str]) -> object:
        rc, out = run_az([*args, "-o", "json"], env)
        if rc != 0:
            return None
        try:
            return json.loads(out or "null")
        except ValueError:
            return None

    for i, sub in enumerate(subscriptions, 1):
        account = az_json(["account", "show", "--subscription", sub])
        groups = az_json(["group", "list", "--subscription", sub, "--query", "[].name"])
        resources = az_json(["resource", "list", "--subscription", sub, "--query", "[].name"])
        if not isinstance(account, dict) or not isinstance(groups, list) or not isinstance(resources, list):
            print(f"cannot read subscription {i} of {len(subscriptions)} (az failed or no access); "
                  "check that --azure-config-dir is logged in")
            raise SystemExit(2)
        if not groups or not resources:
            # An identity that cannot list resources gets [] with rc 0; an empty list must not pass as clean.
            print(f"subscription {i} of {len(subscriptions)} returned {len(groups)} resource groups and "
                  f"{len(resources)} resources; an identity without read access looks like this, so the scan stops")
            raise SystemExit(2)
        for key, kind in (("name", "subscription"), ("tenantDisplayName", "tenant-name"),
                          ("tenantDefaultDomain", "tenant-domain")):
            if isinstance(account.get(key), str) and account[key]:
                items.append({"kind": kind, "value": account[key]})
        items += [{"kind": "resource-group", "value": g} for g in groups if isinstance(g, str)]
        for r in resources:
            if isinstance(r, str):
                items += [{"kind": "resource", "value": part} for part in r.split("/") if part]
    if graph:
        org = az_json(["rest", "--method", "get", "--url", GRAPH_ORG])
        orgs = org.get("value") if isinstance(org, dict) else None
        if not isinstance(orgs, list):
            print("warning: cannot read the tenant's verified domains (Graph /organization); they are not scanned")
        else:
            for o in orgs:
                if isinstance(o.get("displayName"), str) and o["displayName"]:
                    items.append({"kind": "tenant-name", "value": o["displayName"]})
                for d in o.get("verifiedDomains") or []:
                    if isinstance(d, dict) and isinstance(d.get("name"), str):
                        items.append({"kind": "tenant-domain", "value": d["name"]})
    return items


def check_inventory(inventory: object) -> str | None:
    """Why a saved inventory cannot be used, or None. It must have the shape collect_inventory() returns: a
    non-empty list of {kind, value} strings, with at least one resource group and one resource."""
    if not isinstance(inventory, list) or not inventory:
        return "the inventory must be a non-empty JSON list"
    for n, item in enumerate(inventory, 1):
        if (not isinstance(item, dict) or not isinstance(item.get("kind"), str)
                or not isinstance(item.get("value"), str) or not item["value"].strip()):
            return f"item {n} is not {{kind: <string>, value: <non-empty string>}}"
    kinds = {item["kind"] for item in inventory}
    missing = [k for k in ("resource-group", "resource") if k not in kinds]
    if missing:
        return f"no {' or '.join(missing)} names"
    return None


def build_denylist(inventory: list[dict], allow: set[str]) -> tuple[dict[str, str], dict[str, int]]:
    """({lowercase token: kind}, skip counts). Names are whole values; fragments come from splitting names."""
    deny: dict[str, str] = {}
    skipped = {"stoplist": 0, "allowlisted": 0, "short": 0}

    def add(token: str, kind: str, minimum: int) -> None:
        if len(token) < minimum or token.isdigit():
            skipped["short"] += 1
        elif token in STOPLIST:
            skipped["stoplist"] += 1
        elif token in allow:
            skipped["allowlisted"] += 1
        else:
            deny.setdefault(token, kind)

    for item in inventory:
        value = str(item.get("value", "")).strip().lower()
        kind = str(item.get("kind", "name"))
        if not value:
            continue
        add(value, kind, MIN_NAME)
        parts = [p for p in SPLIT_RE.split(value) if p]
        if len(parts) > 1:
            for part in parts:
                if part.isalnum() and part != value:
                    add(part, f"{kind}-fragment", MIN_FRAGMENT)
    return deny, skipped


def token_starts(tokens: list[str]) -> re.Pattern[str]:
    """Zero-width matches at each position where some token starts as a whole token: no ASCII letter or digit
    right before or right after it. Zero width, so overlapping and nested tokens are all found."""
    alternation = "|".join(re.escape(t) for t in sorted(tokens, key=lambda t: (-len(t), t)))
    return re.compile(rf"(?<![a-z0-9])(?=(?:{alternation})(?![a-z0-9]))")


def scan(texts: dict[str, str], deny: dict[str, str]) -> list[tuple[str, str, str, int, int]]:
    """[(id, kind, path, line, col)] for every case-insensitive whole-token occurrence of a denylist token.
    Tokens that start at the same place, or inside another match, are reported too: `rg-x-dev` and `x` both."""
    if not deny:
        return []
    starts = token_starts(list(deny))
    lengths = sorted({len(t) for t in deny})
    hits = set()
    for rel, text in texts.items():
        lower = text.lower()
        for m in starts.finditer(lower):
            start = m.start()
            for n in lengths:
                token = lower[start:start + n]
                if token in deny and lower[start + n:start + n + 1] not in ALNUM:
                    line = lower.count("\n", 0, start) + 1
                    col = start - (lower.rfind("\n", 0, start) + 1) + 1
                    hits.add((token_id(token), deny[token], rel, line, col))
    return sorted(hits, key=lambda h: (h[2], h[3], h[4], h[0]))


def inside(path: Path, root: Path) -> bool:
    path, root = path.resolve(), root.resolve()
    return path == root or root in path.parents


def load_allow(review: Path) -> set[str]:
    if not review.is_file():
        return set()
    data = json.loads(review.read_text(encoding="utf-8"))
    return {str(a["value"]).strip().lower() for a in data.get("inventory_allow", []) if a.get("value")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--export", required=True, help="scrubbed export to scan (scrub_export.py --out)")
    ap.add_argument("--subscription", action="append", default=[], help="subscription ID or name; repeatable")
    ap.add_argument("--azure-config-dir", default=os.environ.get("AZURE_CONFIG_DIR"),
                    help="isolated az login directory (default: $AZURE_CONFIG_DIR)")
    ap.add_argument("--no-graph", action="store_true", help="skip the tenant's verified domains")
    ap.add_argument("--inventory", help="read a saved inventory instead of calling az (outside the repo)")
    ap.add_argument("--save-inventory", help="also write the inventory here (outside the repo and the export)")
    ap.add_argument("--review", default=str(HERE / "review.json"), help="review.json with inventory_allow")
    args = ap.parse_args(argv)
    export = Path(args.export).resolve()
    if not export.is_dir():
        ap.error("--export must be an existing directory")
    for flag, value in (("--inventory", args.inventory), ("--save-inventory", args.save_inventory)):
        if value and (inside(Path(value), REPO_ROOT) or inside(Path(value), export)):
            print(f"{flag} must be outside the repo and the export (it holds real names, which must never be "
                  "committed or exported)")
            return 2
    if args.inventory:
        try:
            inventory = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"cannot read --inventory ({type(exc).__name__})")
            return 2
        problem = check_inventory(inventory)
        if problem:
            print(f"--inventory refused: {problem}")
            return 2
    else:
        if not args.subscription or not args.azure_config_dir:
            ap.error("give --subscription (one or more) and --azure-config-dir, or --inventory")
        inventory = collect_inventory(args.subscription, args.azure_config_dir, graph=not args.no_graph)
    if args.save_inventory:
        Path(args.save_inventory).write_text(json.dumps(inventory, ensure_ascii=False, indent=1), encoding="utf-8")

    deny, skipped = build_denylist(inventory, load_allow(Path(args.review)))
    if not deny:
        print(f"denylist is empty ({len(inventory)} names, all skipped): nothing would be checked, so the scan stops")
        return 2
    texts, _ = read_tree(export)
    hits = scan(texts, deny)
    kinds: dict[str, int] = defaultdict(int)
    for item in inventory:
        kinds[str(item.get("kind"))] += 1
    print(f"inventory: {len(inventory)} names ({', '.join(f'{k} {n}' for k, n in sorted(kinds.items()))})")
    print(f"denylist: {len(deny)} tokens; skipped {skipped['stoplist']} stoplist, "
          f"{skipped['allowlisted']} inventory_allow, {skipped['short']} too short")
    print(f"scanned {len(texts)} text files: {len(hits)} hits, {len({h[0] for h in hits})} distinct tokens")
    for tid, kind, rel, line, col in hits[:500]:
        print(f"    HIT {tid} {kind} {rel}:{line}:{col}")
    if len(hits) > 500:
        print(f"    ... {len(hits) - 500} more")
    print("RESULT:", "PASS" if not hits else "REVIEW (read each HIT's line: add a review.json rule to replace the "
                                              "name, or an inventory_allow entry to publish it)")
    return 0 if not hits else 1


if __name__ == "__main__":
    sys.exit(main())
