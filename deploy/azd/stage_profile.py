"""A stage.json-shaped profile of an azd environment, for running the stage CI steps against it (ADR-0010
#stage-private-1026, blockers 3, 4 and 10).

profile.build() already writes the stage profile's shape. This adds the three things a stage profile has and azd
never writes, each only when its azd environment value is set:
- deploy_client_id (FA_DEPLOY_CLIENT_ID): the GitHub OIDC identity; only `ci_deploy.py meta` needs it, so a laptop
  run uses `check` and leaves it out;
- the feed reader (FA_FEED_APP and FA_FEED_PUBLIC_KEY, set together) and the API secret holding its signing key
  (FA_FEED_SIGNING_SECRET, default the stage name fa-feed-mcp-signing-key). ci_deploy checks their format.

The runtime directory is copied, never regenerated: `ci_deploy.py api` stops unless the deployed
BEHAVIOR_SERVER_CONFIG_JSON equals <profile dir>/runtime/config.json, and azd deployed its own .azure/<env>/fa/runtime.

  python deploy/azd/stage_profile.py [--out DIR]   (the selected azd environment; default .azure/<env>/fa/stage)
writes DIR/stage.json and DIR/runtime/, the layout of deploy/stage/, so ci_deploy.py --config DIR/stage.json finds the
runtime where the stage workflow does.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fa_azd  # noqa: E402
import profile as fa_profile  # noqa: E402

SIGNING_SECRET = "fa-feed-mcp-signing-key"  # the stage app's secret name (src README "Feed reader MCP")


def build(values: Mapping[str, str], *, base: Mapping[str, Any] | None = None) -> dict[str, Any]:
    profile = fa_profile.build(values, base=base)
    client = (values.get("FA_DEPLOY_CLIENT_ID") or "").strip()
    if client:
        profile["deploy_client_id"] = client
    app, key = ((values.get(name) or "").strip() for name in ("FA_FEED_APP", "FA_FEED_PUBLIC_KEY"))
    if bool(app) != bool(key):
        raise SystemExit("FA_FEED_APP and FA_FEED_PUBLIC_KEY are set together: the feed reader app and its public key")
    if app:
        profile["feed_app"] = app
        profile["feed_environment"] = {"FEED_MCP_PUBLIC_KEY": key}
        secret = (values.get("FA_FEED_SIGNING_SECRET") or SIGNING_SECRET).strip()
        profile["api_secret_environment"] = {"FA_FEED_MCP_SIGNING_KEY": secret}
    return profile


def write(out: Path, values: Mapping[str, str], runtime: Path) -> Path:
    """DIR/stage.json and a byte copy of the deployed runtime; the profile is regenerated each run."""
    if not (runtime / "config.json").is_file():
        raise SystemExit(f"{runtime / 'config.json'} does not exist; the first `azd deploy` writes it")
    profile = build(values)
    # Replace, not merge: a file left by an earlier run must not survive next to the deployed runtime.
    staged = out / "runtime.new"
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(runtime, staged)
    if (out / "runtime").exists():
        shutil.rmtree(out / "runtime")
    staged.rename(out / "runtime")
    path = out / "stage.json"
    fa_profile.write(path, profile)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, help="output directory; default .azure/<env>/fa/stage (git-ignored)")
    args = parser.parse_args(argv)
    values = fa_azd.azd_values()
    name = (values.get("AZURE_ENV_NAME") or "").strip()
    if not name:
        raise SystemExit("no azd environment is selected (AZURE_ENV_NAME is empty); run `azd env select <name>`")
    work = fa_azd.ROOT / ".azure" / name / "fa"
    print(write(args.out or work / "stage", values, work / "runtime"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
