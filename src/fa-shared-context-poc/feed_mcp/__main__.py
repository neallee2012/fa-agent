"""python -m feed_mcp serve | keygen

serve   needs FEED_MCP_PUBLIC_KEY (standard base64 Ed25519 public key) and listens on 0.0.0.0:8080.
keygen  prints a new key pair once: the private key goes only into the API's secret FA_FEED_MCP_SIGNING_KEY, the
        public key into this server's FEED_MCP_PUBLIC_KEY.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from shared_context.feed_signature import PUBLIC_KEY_ENV, SIGNING_KEY_ENV, generate_keypair, load_public_key


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m feed_mcp")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8080)
    commands.add_parser("keygen")
    args = parser.parse_args(argv)
    if args.command == "keygen":
        private, public = generate_keypair()
        print("# The private key is a secret: put it only into the API's secret, never into a file or chat.",
              file=sys.stderr)
        print(f"{SIGNING_KEY_ENV}={private}")
        print(f"{PUBLIC_KEY_ENV}={public}")
        return 0
    try:
        public_key = load_public_key(os.environ.get(PUBLIC_KEY_ENV, ""))
    except ValueError:
        print(f"{PUBLIC_KEY_ENV} must be the standard base64 Ed25519 public key", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx/httpcore log request URLs at INFO/DEBUG; the feed URL must never reach a log.
    for name in ("httpx", "httpcore", "mcp"):
        logging.getLogger(name).setLevel(logging.WARNING)
    import uvicorn

    from .server import create_app

    uvicorn.run(create_app(public_key), host="0.0.0.0", port=args.port, proxy_headers=False, server_header=False,
                log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
