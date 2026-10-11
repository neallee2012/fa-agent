"""Offline stdio MCP server for the pinned-runtime capability test; no network or model."""

from mcp.server.fastmcp import FastMCP

server = FastMCP("fake-capability")


@server.tool(description="Echo the given text.")
def echo(text: str) -> str:
    return "echo:" + text


@server.tool(description="Not in the organization allowlist.")
def hidden() -> str:
    return "hidden-called"


if __name__ == "__main__":
    server.run("stdio")
