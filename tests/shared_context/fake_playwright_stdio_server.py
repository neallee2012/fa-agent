"""Offline stdio stand-in for the preinstalled Playwright MCP (same tool names); no browser or network."""

from mcp.server.fastmcp import FastMCP, Image

server = FastMCP("fake-playwright")


@server.tool(description="Navigate to a URL.")
def browser_navigate(url: str) -> str:
    if url.endswith("/launch-crash"):
        # Shaped like a Playwright launch failure: a long launch argv, then the browser's own process lines.
        argv = " ".join(f"--synthetic-flag-{n}" for n in range(40))
        raise RuntimeError("async initializeServer: Target page, context or browser has been closed\nBrowser logs:\n\n"
                           f"<launching> /opt/fa-tools/chrome {argv}\n<launched> pid=4242\n"
                           "[pid=4242][err] [0930/035710:FATAL:synthetic_zygote.cc(1)] SYNTHETIC-CHROME-FATAL\n"
                           "[pid=4242] <process did exit: exitCode=null, signal=SIGTRAP>\n")
    if ".invalid" in url:
        # Shaped like a Playwright navigation failure and longer than the 200-character log excerpt.
        raise RuntimeError(f"net::ERR_NAME_NOT_RESOLVED at {url}\nCall log:\n" + f"  - navigating to \"{url}\"\n" * 8)
    return "navigated:" + url


@server.tool(description="Page snapshot.")
def browser_snapshot() -> str:
    return "- heading \"FA offline page\" [ref=e1]"


@server.tool(description="Take a screenshot.")
def browser_take_screenshot(fullPage: bool = False, filename: str | None = None):
    import os
    import random
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    if filename is not None and ("/" in filename or "\\" in filename):
        # The real Playwright MCP refuses to save outside its output folder.
        raise RuntimeError(f"File access denied: {filename} is outside allowed roots")
    log = os.environ.get("FAKE_PLAYWRIGHT_CALL_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as calls:
            calls.write(f"screenshot fullPage={fullPage}\n")
    if filename is not None:
        # Like @playwright/mcp 0.0.82: with a filename the screenshot is only saved, no image is returned.
        return f"- [Screenshot of viewport](./{filename})"
    side = int(os.environ.get("FAKE_PLAYWRIGHT_SCREENSHOT_SIDE", "0"))
    if side:
        # Distinct incompressible pixels, so each result keeps its full size in the SDK history.
        noise = random.Random(len(open(log, encoding="utf-8").readlines()) if log else side)
        raw = b"".join(b"\x00" + noise.randbytes(side * 3) for _ in range(side))
        return Image(data=b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""), format="png")
    return Image(data=b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(b"\x00\x10\x20\x30")) + chunk(b"IEND", b""), format="png")


@server.tool(description="Click.")
def browser_click(target: str) -> str:
    return "clicked:" + target


@server.tool(description="Type text.")
def browser_type(target: str, text: str) -> str:
    return "typed"


@server.tool(description="Fill form.")
def browser_fill_form(fields: list[dict]) -> str:
    return "filled"


@server.tool(description="Select option.")
def browser_select_option(target: str, values: list[str]) -> str:
    return "selected"


@server.tool(description="Wait for.")
def browser_wait_for(time: float = 0) -> str:
    return "waited"


@server.tool(description="Go back.")
def browser_navigate_back() -> str:
    return "back"


if __name__ == "__main__":
    server.run("stdio")
