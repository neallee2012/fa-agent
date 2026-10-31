"""Explicit CLI; schemas/sample are JSON, never a runtime readiness assertion.

Mount BEHAVIOR_SERVER_CONFIG and its current evidence catalog as trusted
read-only files. Recovery uses BEHAVIOR_RECOVERY_CONFIG and a separate identity.
The API image accepts serve/probe/recovery/controller/bootstrap. The Hosted image
uses hosted-bootstrap, then execs the existing constrained serve module.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path


def _hosted_profile(profile):
    if profile == "checksum-count-v1":
        return ".fa-behavior-checksum-v1", ["-m", "shared_context.behavior_hosted", "serve"]
    if profile == "assistant-v1.2" and os.environ.get("BEHAVIOR_ASSISTANT_PROFILE") == profile:
        return ".fa-assistant-v12", [
            "-c", "import importlib; importlib.import_module('behavior_assistant_main').main()",
        ]
    raise ValueError("unknown or disabled Hosted profile")


def hosted_bootstrap(profile="checksum-count-v1"):
    """Use the existing nonroot/MI identity bootstrap, without changing HOME."""
    import platform
    import stat
    from hosted_bootstrap import PLATFORM_HOME, RECEIPT_ENV, _account, _attributes, drop_privileges

    directory, entry = _hosted_profile(profile)
    if platform.system() != "Linux" or os.geteuid() != 0 or os.environ.get("HOME") != str(PLATFORM_HOME):
        raise ValueError("Hosted bootstrap requires the observed native HOME and initial UID0")
    home = PLATFORM_HOME
    if any(node.is_symlink() for node in (home, *home.parents)):
        raise ValueError("native HOME must not contain symlinks")
    uid, gid = _account()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    home_fd = os.open(home, flags)
    try:
        before = _attributes(os.fstat(home_fd))
        created = False
        try:
            os.mkdir(directory, mode=0o700, dir_fd=home_fd)
            created = True
        except FileExistsError:
            pass
        private_fd = os.open(directory, flags, dir_fd=home_fd)
        try:
            if created:
                os.fchown(private_fd, uid, gid)
                os.fchmod(private_fd, 0o700)
            info = os.fstat(private_fd)
            if not stat.S_ISDIR(info.st_mode) or _attributes(info) != {"uid": uid, "gid": gid, "mode": "0o700"}:
                raise ValueError("existing private directory ownership differs; no repair")
            if _attributes(os.fstat(home_fd)) != before:
                raise ValueError("native HOME metadata changed")
        finally:
            os.close(private_fd)
    finally:
        os.close(home_fd)
    receipt = {"home": str(home), "private_directory": str(home / directory),
               "created": created, "runtime_identity": drop_privileges(uid, gid)}
    os.environ[RECEIPT_ENV] = json.dumps(receipt, separators=(",", ":"))
    os.environ["USER"] = os.environ["LOGNAME"] = "app"
    os.execv(sys.executable, [sys.executable, *entry])


def main():
    # Hosted dependencies intentionally exclude the Table/JWT/server package.
    if sys.argv[1:] in (["hosted-bootstrap"], ["assistant-hosted-bootstrap"]):
        try:
            hosted_bootstrap("assistant-v1.2" if sys.argv[1] == "assistant-hosted-bootstrap" else "checksum-count-v1")
        except (ValueError, OSError, RuntimeError, KeyError):
            raise SystemExit("BEHAVIOR_HOSTED_BOOTSTRAP_FAILED") from None
        return
    from shared_context.behavior_server_config import (
        BootstrapAuthorization, NativeEvidenceBundle, RecoveryCatalog, RecoveryConfiguration,
        ProjectionInstallationEvidence, ServerConfiguration, SupervisorLimits, TokenizerConfiguration,
        load_configuration, read_document, synthetic_configuration,
    )
    from shared_context.memory_projection import ProjectionSetup
    from shared_context.behavior_server import bootstrap, build_server, run_controller
    from shared_context.errors import Problem

    parser = argparse.ArgumentParser(description="FA native behavior composition (no resource/RBAC provisioning)")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--evidence", type=Path, help="must equal the current catalog path pinned in trusted configuration")
    sub = parser.add_subparsers(dest="mode", required=True)
    for name in ("serve", "probe", "recovery"):
        serve = sub.add_parser(name)
        serve.add_argument("--host", default="0.0.0.0")
        serve.add_argument("--port", type=int, default=8080)
    schema = sub.add_parser("schema")
    schema.add_argument("kind", choices=("config", "evidence", "authorization", "recovery", "recovery-catalog",
                                        "projection", "projection-evidence", "tokenizer"),
                        default="config", nargs="?")
    sub.add_parser("sample-config")
    boot = sub.add_parser("bootstrap")
    boot.add_argument("--stage", choices=("records", "worker", "activate"), required=True)
    boot.add_argument("--authorization", type=Path, required=True)
    controller = sub.add_parser("controller")
    controller.add_argument("--preactivation", action="store_true")
    controller.add_argument("--ticks", type=int)
    controller.add_argument("--page-limit", type=int)
    controller.add_argument("--interval-seconds", type=float)
    controller.add_argument("--max-run-seconds", type=float)
    for command in sub.choices.values():
        command.add_argument("--config", type=Path, default=argparse.SUPPRESS)
        command.add_argument("--evidence", type=Path, default=argparse.SUPPRESS,
                             help="exact configured current catalog path; never an HTTP upload")
    args = parser.parse_args()
    try:
        if args.mode == "schema":
            models = {"config": ServerConfiguration, "evidence": NativeEvidenceBundle, "authorization": BootstrapAuthorization,
                      "recovery": RecoveryConfiguration, "recovery-catalog": RecoveryCatalog,
                      "projection": ProjectionSetup, "projection-evidence": ProjectionInstallationEvidence,
                      "tokenizer": TokenizerConfiguration}
            print(json.dumps(models[args.kind].model_json_schema(), indent=2))
            return
        if args.mode == "sample-config":
            print(synthetic_configuration().model_dump_json(indent=2))
            return
        if args.config is not None:
            key = "BEHAVIOR_RECOVERY_CONFIG" if args.mode == "recovery" else "BEHAVIOR_SERVER_CONFIG"
            if os.environ.get(key + "_JSON"):
                raise ValueError("explicit config path conflicts with JSON environment configuration")
            os.environ[key] = str(args.config.resolve())
        if args.evidence is not None:
            config = load_configuration(recovery=args.mode == "recovery")
            pinned = config.catalog.path if args.mode == "recovery" else config.evidence.path
            if args.evidence.resolve() != Path(pinned).resolve():
                raise ValueError("evidence must use the exact path bound into trusted configuration and its digest")
        if args.mode in {"serve", "probe", "recovery"}:
            import uvicorn
            target = {"serve": "create_production_app", "probe": "create_probe_app", "recovery": "create_recovery_app"}[args.mode]
            uvicorn.run("shared_context.behavior_server:" + target, factory=True,
                        host=args.host, port=args.port, access_log=False)
        elif args.mode == "bootstrap":
            config = load_configuration()
            approval = read_document(args.authorization, BootstrapAuthorization)
            with build_server(config) as composition:
                print(json.dumps(bootstrap(composition, approval, stage=args.stage), separators=(",", ":")))
        else:
            config = load_configuration()
            values = config.supervisor.model_dump()
            for field in values:
                value = getattr(args, field)
                if value is not None:
                    values[field] = value
            limits = SupervisorLimits(**values)
            stopped = False

            def stop(_signum, _frame):
                nonlocal stopped
                stopped = True

            prior = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                with build_server(config) as composition:
                    result = run_controller(composition, limits=limits, preactivation=args.preactivation,
                                            stop_requested=lambda: stopped)
                print(json.dumps(result, separators=(",", ":")))
            finally:
                for sig, handler in prior.items():
                    signal.signal(sig, handler)
    except (Problem, ValueError, OSError):
        # Raw provider/config payloads can contain secrets; CLI failures never echo them.
        raise SystemExit("BEHAVIOR_SERVER_CONFIGURATION_OR_DEPENDENCY_UNAVAILABLE") from None


if __name__ == "__main__":
    main()
