"""Owned offline process for the native-stop test; not a fake SDK."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"))

from shared_context.m2_copilot import ToolSpec, run_copilot
from test_m2_copilot import SCHEMA, ScriptedEndpoint, tool_call


async def main(workspace, runtime):
    endpoint = ScriptedEndpoint([
        tool_call("checkpoint_and_wait", {"label": "durable"}, "call-orphan"),
    ])

    async def checkpoint(invocation):
        (workspace / "checkpoint.json").write_text(json.dumps(invocation), encoding="utf-8")
        await asyncio.Event().wait()

    await run_copilot(
        state_dir=workspace / "state", working_directory=workspace / "work",
        sdk_session_id="offline-orphan", resume=False, prompt="Checkpoint once and wait.",
        tools=[ToolSpec("checkpoint_and_wait", "Wait after checkpoint", SCHEMA, checkpoint)],
        api_url="https://context.invalid", token_provider=endpoint.token, on_event=lambda e: None,
        timeout_seconds=40, runtime_path=runtime, http_transport=endpoint.transport,
    )


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), Path(sys.argv[2])))
