"""Build-time non-root SDK protocol check; no model or Hosted persistence proof."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from copilot import CopilotClient, CopilotRequestHandler, RuntimeConnection


class NoNetwork(CopilotRequestHandler):
    async def send_request(self, request, ctx):
        raise RuntimeError("The runtime build probe must not send network requests.")

    async def open_websocket(self, ctx):
        raise RuntimeError("The runtime build probe must not open WebSockets.")


async def main():
    if os.geteuid() != 10001:
        raise RuntimeError("Run the SDK build probe as UID10001.")
    with TemporaryDirectory(prefix="fa-m2-runtime-probe-") as temporary:
        client = CopilotClient(
            connection=RuntimeConnection.for_stdio(
                path="/opt/copilot/1.0.83/prebuilds/linux-x64/copilot-runtime",
            ),
            working_directory=temporary, base_directory=str(Path(temporary) / "sdk"),
            env={"HOME": temporary, "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
                 "COPILOT_SKIP_CLI_DOWNLOAD": "1"},
            use_logged_in_user=False, mode="empty", request_handler=NoNetwork(),
        )
        try:
            await asyncio.wait_for(client.start(), timeout=30)
            print("M2_RUNTIME_PROTOCOL_READY uid=10001; no model turn requested", flush=True)
        finally:
            await client.stop()


if __name__ == "__main__":
    asyncio.run(main())
