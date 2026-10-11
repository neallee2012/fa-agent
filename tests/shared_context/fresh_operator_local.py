"""Re-runnable real module CLI with synthetic config and local Table SDK transport.

Requires the normal test dependencies and pinned TIKTOKEN_CACHE_DIR. All files
are synthetic and unsuitable for deployment. No product offline/auth bypass.
"""

import argparse
from contextlib import redirect_stdout
from datetime import timedelta
from io import StringIO
import json
from pathlib import Path
import runpy
import socket
import sys

import pytest

import shared_context.behavior_server as server
from shared_context.behavior_fresh_installation import FreshInstallationAuthorization
from test_behavior_assistant_server import AssistantHarness
from test_fresh_installation import FreshTableTransport, fresh_inputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    root = args.output_directory.resolve()
    root.mkdir(parents=True, exist_ok=False)

    def blocked(*args, **kwargs):
        raise AssertionError("offline CLI must not open a socket")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, "connect", blocked)
        patch.setattr("test_behavior_server.WireTransport", FreshTableTransport)
        h = AssistantHarness(root, None)
        real_build = server.build_server
        patch.setattr(server, "build_server", lambda config: real_build(config, **h.options))
        config_file, input_file = root / "synthetic-config.json", root / "synthetic-inputs.json"
        plan_file, approval_file = root / "synthetic-plan.json", root / "synthetic-approval.json"
        config_file.write_text(h.config.model_dump_json(), encoding="utf-8")
        input_file.write_text(fresh_inputs(h.config).model_dump_json(), encoding="utf-8")

        def cli(arguments):
            patch.setattr(sys, "argv", ["shared_context.behavior_fresh_operator", *arguments])
            output = StringIO()
            with redirect_stdout(output):
                try:
                    runpy.run_module("shared_context.behavior_fresh_operator", run_name="__main__")
                except SystemExit as exit_:
                    if exit_.code != 0:
                        raise
            raw = output.getvalue()
            assert len(raw.splitlines()) == 1
            print(raw, end="")
            return json.loads(raw)

        try:
            plan = cli(["plan", "--config", str(config_file), "--inputs", str(input_file),
                        "--request-id", "synthetic-original-rid", "--installation-id", "synthetic-native-install",
                        "--output", str(plan_file)])
            assert not h.table.backing.rows
            approval = FreshInstallationAuthorization(
                decision_id="synthetic-operator-file", authorizing_human_oid=h.config.identity.human_oid,
                configuration_sha256=h.config.sha256, plan_sha256=plan["plan_sha256"],
                not_before=h.time, expires_at=h.time + timedelta(minutes=5),
            )
            approval_file.write_text(approval.model_dump_json(), encoding="utf-8")
            arguments = ["--config", str(config_file), "--plan", str(plan_file), "--authorization", str(approval_file)]
            applied = cli(["apply", *arguments])
            assert cli(["reconcile", *arguments]) == applied
            assert cli(["apply", *arguments]) == applied
            assert sum(request.method == "POST" for request in h.table.requests) == 1
            assert not h.calls and not h.source.requests and not h.backup.requests
            print('{"offline_cli":"passed","table_batches":1,"actual_environment":"NOT_RUN"}')
        finally:
            h.composition.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
