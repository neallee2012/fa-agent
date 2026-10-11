"""Preview is local-only; serving trusted config never performs automatic login.

`serve-remote` is the same console behind Container Apps built-in Entra authentication (ADR-0008 §3).
"""
import argparse
import os
from pathlib import Path
import sys

import uvicorn

from .adapter import ConsoleConfig, SteadyConsoleConfig, ConsoleError, parse_config, require, sha, strict_json
from .bridge import create_app
from . import prewarm as typing_prewarm
from .standing import StandingConsoleConfig
from .pull_continuation import PullContinuationSidecar
from .instructions import InstructionConnection
from .agent_connections import AgentConnections


def _trusted_files(args):
    """Exact parent-reviewed bytes only; shared by local and remote serving."""
    raw = args.config.read_bytes()
    if len(raw) > 65536 or sha(raw) != args.config_sha256:
        raise ValueError("configuration bytes differ")
    config = parse_config(raw)
    continuation = None
    instructions = None
    agents = None
    if bool(args.pull_continuation) != bool(args.pull_continuation_sha256):
        raise ValueError("sidecar path and exact hash must be paired")
    if args.pull_continuation is not None:
        raw = args.pull_continuation.read_bytes()
        if len(raw) > 32768 or sha(raw) != args.pull_continuation_sha256:
            raise ValueError("sidecar hash differs")
        strict_json(raw)
        continuation = PullContinuationSidecar.model_validate_json(raw)
    if bool(args.instructions) != bool(args.instructions_sha256):
        raise ValueError("instruction connection path and exact hash must be paired")
    if args.instructions is not None:
        raw = args.instructions.read_bytes()
        if len(raw) > 4096 or sha(raw) != args.instructions_sha256:
            raise ValueError("instruction connection hash differs")
        strict_json(raw)
        instructions = InstructionConnection.model_validate_json(raw)
    if bool(args.agents) != bool(args.agents_sha256):
        raise ValueError("Agent connections path and exact hash must be paired")
    if args.agents is not None:
        raw = args.agents.read_bytes()
        if len(raw) > 65536 or sha(raw) != args.agents_sha256:
            raise ValueError("Agent connections hash differs")
        strict_json(raw)
        agents = AgentConnections.model_validate_json(raw)
    return config, continuation, instructions, agents


def _serve_remote(args):
    from azure.core.exceptions import AzureError
    from azure.identity import ManagedIdentityCredential
    from azure.storage.blob import BlobClient
    from .bridge import Journal
    from .remote import EasyAuthTokenProvider, LeasedArchive, RemoteBoundary

    archive = None
    try:
        boundary = RemoteBoundary(args.public_origin, args.owner_oid)
        config, continuation, instructions, agents = _trusted_files(args)
        require(config.human_oid == boundary.owner_oid, "REMOTE_CONFIGURATION_INVALID", 400)
        credential = ManagedIdentityCredential(client_id=args.managed_identity_client_id)
        archive = LeasedArchive(BlobClient.from_blob_url(args.state_blob_url, credential=credential), args.state_root)
        journals = [args.journal, *(() if agents is None else (entry.journal_path for entry in agents.agents))]
        require(all(archive.contains(path) for path in journals), "REMOTE_CONFIGURATION_INVALID", 400)
        archive.start()
        Journal.after_commit = archive.persist
        Journal.after_file = archive.persist_file
        app = create_app(config, args.journal, port=args.port, pull_continuation=continuation,
                         instruction_connection=instructions, token_provider=EasyAuthTokenProvider(), remote=boundary,
                         realtime_endpoint=os.environ.get("FA_REALTIME_ENDPOINT"), prewarm=typing_prewarm.configured(),
                         **({"agent_connections": agents} if agents is not None else {}))
    except (OSError, ValueError, ConsoleError, AzureError) as error:
        if archive is not None:
            archive.release()
        # Only the class and service codes, never messages that could echo URLs or headers.
        reason = " ".join(str(part) for part in (type(error).__name__, getattr(error, "code", None),
                          getattr(error, "status_code", None), getattr(error, "error_code", None)) if part)
        print(f"REMOTE_CONFIGURATION_REJECTED ({reason}): fixed config/hash/owner/state required.", file=sys.stderr)
        return 2
    print(f"FA remote console: {boundary.origin} — Container Apps sign-in; no automatic backend operation.")
    try:
        uvicorn.run(app, host="0.0.0.0", port=args.port, workers=1, access_log=False,
                    proxy_headers=False, server_header=False)
    finally:
        archive.release()
    return 0


def _remote_bundle(args):
    import json
    from .remote import remote_bundle

    try:
        raw = args.agents.read_bytes()
        if len(raw) > 65536 or sha(raw) != args.agents_sha256:
            raise ValueError("Agent connections hash differs")
        strict_json(raw)
        files = remote_bundle(AgentConnections.model_validate_json(raw), args.client_id)
        args.out.mkdir(parents=True, exist_ok=False)
        for name, value in files.items():
            (args.out / name).write_bytes(value)
        # Deployment parameter values; the files stay in the operator's private folder, never in Git.
        (args.out / "configFiles.json").write_text(json.dumps(
            {name: value.decode("utf-8") for name, value in files.items()}, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError, ConsoleError):
        print("REMOTE_BUNDLE_REJECTED: exact local manifest/hash, remote client ID and a new output folder required.",
              file=sys.stderr)
        return 2
    print(json.dumps({"consoleConfigSha256": sha(files["console.json"]),
                      "instructionsSha256": sha(files["instructions.json"]),
                      "agentsSha256": sha(files["agents.json"])}, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    preview = modes.add_parser("preview", help="No config, credential, backend request or test data.")
    serve = modes.add_parser("serve", help="Mount only a parent-reviewed fixed configuration.")
    remote = modes.add_parser("serve-remote", help="Behind Container Apps Entra sign-in; one owner, leased Blob journal.")
    for command in (preview, serve):
        command.add_argument("--port", type=int, default=8765)
    remote.add_argument("--port", type=int, default=8080)
    for command in (serve, remote):
        command.add_argument("--config", required=True, type=Path)
        command.add_argument("--config-sha256", required=True)
        command.add_argument("--journal", required=True, type=Path)
        command.add_argument("--pull-continuation", type=Path)
        command.add_argument("--pull-continuation-sha256")
        command.add_argument("--instructions", type=Path)
        command.add_argument("--instructions-sha256")
        command.add_argument("--agents", type=Path)
        command.add_argument("--agents-sha256")
    remote.add_argument("--state-root", required=True, type=Path)
    remote.add_argument("--state-blob-url", required=True)
    remote.add_argument("--public-origin", required=True)
    remote.add_argument("--owner-oid", required=True)
    remote.add_argument("--managed-identity-client-id", required=True)
    bundle = modes.add_parser("remote-bundle", help="Offline: rewrite a reviewed local Agent manifest for serve-remote.")
    bundle.add_argument("--agents", required=True, type=Path)
    bundle.add_argument("--agents-sha256", required=True)
    bundle.add_argument("--client-id", required=True)
    bundle.add_argument("--out", required=True, type=Path)
    schema = modes.add_parser("schema", help="Export schema only; no real scope or dates.")
    schema.add_argument("--configuration", choices=(
        "legacy", "steady", "standing", "pull-continuation", "instructions", "agents"), default="legacy")
    args = parser.parse_args(argv)
    if args.mode == "schema":
        import json
        model = {"legacy": ConsoleConfig, "steady": SteadyConsoleConfig, "standing": StandingConsoleConfig,
                 "pull-continuation": PullContinuationSidecar, "instructions": InstructionConnection,
                 "agents": AgentConnections}[args.configuration]
        print(json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2))
        return 0
    if args.mode == "serve-remote":
        return _serve_remote(args)
    if args.mode == "remote-bundle":
        return _remote_bundle(args)
    try:
        config = None
        continuation = None
        instructions = None
        agents = None
        if args.mode == "serve":
            config, continuation, instructions, agents = _trusted_files(args)
        app = create_app(config, args.journal if args.mode == "serve" else None, port=args.port,
                         pull_continuation=continuation, instruction_connection=instructions,
                         realtime_endpoint=os.environ.get("FA_REALTIME_ENDPOINT") if args.mode == "serve" else None,
                         prewarm=args.mode == "serve" and typing_prewarm.configured(),
                         **({"agent_connections": agents} if agents is not None else {}))
    except (OSError, ValueError, ConsoleError):
        print("LOCAL_CONFIGURATION_REJECTED: fixed config/hash/path/port required.", file=sys.stderr)
        return 2
    print(f"FA local console: http://127.0.0.1:{args.port} — no automatic backend operation.")
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, access_log=False,
                proxy_headers=False, server_header=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
