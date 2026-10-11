"""Offline stdio MCP stand-in that reports the environment and cwd the runtime gave it; no network or model."""

import json
import os
import sys
import tempfile

from mcp.server.fastmcp import FastMCP

server = FastMCP("fake-env")


@server.tool(description="Report the process environment names, HOME, TMPDIR, DISPLAY, arguments and cwd.")
def report() -> str:
    return json.dumps({"names": sorted(os.environ), "HOME": os.environ.get("HOME"),
                       "TMPDIR": os.environ.get("TMPDIR"), "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME"),
                       "DISPLAY": os.environ.get("DISPLAY"), "argv": sys.argv[1:],
                       "tempdir": tempfile.gettempdir(), "cwd": os.getcwd()})


if __name__ == "__main__":
    server.run("stdio")
