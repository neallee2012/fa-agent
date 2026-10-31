"""Model-gateway probe: one streamed tool-call request per target, reported as a JSON result.

Stdlib only so the same file runs inside the hosted agent and on the stock Ubuntu mock VM (T3).
Targets come from a file or PROBE_TARGETS, never from a request; tokens are reduced to selected claims.
"""

import argparse
import base64
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30
PROMPT = "Call the get_time tool now. Do not answer in text."
CLAIMS = ("aud", "appid", "azp", "oid", "tid", "exp")
PROXY_ENV = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")
IMDS = "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource="


def _chat_tool_call(event):
    return any((c.get("delta") or {}).get("tool_calls") for c in event.get("choices") or [])


def _responses_tool_call(event):
    return (event.get("type") in ("response.output_item.added", "response.output_item.done")
            and (event.get("item") or {}).get("type") == "function_call")


def _responses_usage(event):
    return event.get("type") == "response.completed" and bool((event.get("response") or {}).get("usage"))


WIRES = {
    "chat": {
        "body": lambda model: {
            "model": model, "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": PROMPT}],
            "tools": [{"type": "function", "function": {
                "name": "get_time", "description": "Current UTC time", "parameters": {"type": "object", "properties": {}}}}],
        },
        "tool_call": _chat_tool_call,
        "usage": lambda event: event.get("usage") is not None,
    },
    "responses": {
        "body": lambda model: {
            "model": model, "stream": True, "input": PROMPT,
            "tools": [{"type": "function", "name": "get_time", "description": "Current UTC time",
                       "parameters": {"type": "object", "properties": {}}}],
        },
        "tool_call": _responses_tool_call,
        "usage": _responses_usage,
    },
}

AUTH = {
    "mi": lambda auth, get_token, env: {"Authorization": "Bearer " + get_token(auth["audience"])},
    "key": lambda auth, get_token, env: {auth["header"]: env.get(auth["env"], "")},
    "none": lambda auth, get_token, env: {},
}


def decode_claims(jwt):
    """Selected payload claims of a JWT, without verifying the signature."""
    payload = jwt.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return {k: claims.get(k) for k in CLAIMS}


def _local_ip(ip, port):
    # A UDP connect only asks the kernel for a route; no packet is sent.
    with socket.socket(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((ip, port))
        return s.getsockname()[0]


def run_target(target, get_token, env=None):
    env = os.environ if env is None else env
    url = urllib.parse.urlsplit(target["url"])
    result = {"id": target.get("id"), "host": url.hostname, "dns": None, "local_ip": None, "status": None,
              "latency_ms": None, "token_claims": None, "tool_call": False, "usage": False, "error": None,
              "body_snippet": None, "proxy_env": [k for k in PROXY_ENV if env.get(k)]}
    port = url.port or (443 if url.scheme == "https" else 80)
    try:
        result["dns"] = sorted({a[4][0] for a in socket.getaddrinfo(url.hostname, port, proto=socket.IPPROTO_TCP)})
        result["local_ip"] = _local_ip(result["dns"][0], port)
    except OSError as e:
        result["dns"] = f"error: {e}"
    wire = WIRES[target["wire"]]
    started = time.monotonic()
    try:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        headers.update(AUTH[target["auth"]["type"]](target["auth"], get_token, env))
        if target["auth"]["type"] == "mi":
            result["token_claims"] = decode_claims(headers["Authorization"][len("Bearer "):])
        request = urllib.request.Request(target["url"], data=json.dumps(wire["body"](target["model"])).encode(),
                                         headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            result["status"] = response.status
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                event = json.loads(data)
                result["tool_call"] = result["tool_call"] or wire["tool_call"](event)
                result["usage"] = result["usage"] or wire["usage"](event)
    except urllib.error.HTTPError as e:
        result["status"] = e.code
        result["body_snippet"] = e.read()[:300].decode("utf-8", "replace")
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"[:300]
    result["latency_ms"] = round((time.monotonic() - started) * 1000)
    return result


def imds_token(audience):
    request = urllib.request.Request(IMDS + urllib.parse.quote(audience, safe=""), headers={"Metadata": "true"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.load(response)["access_token"]


def _no_token(audience):
    raise RuntimeError("mi target needs --imds")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--targets", required=True, help="JSON list of targets")
    ap.add_argument("--id", help="run only this target id")
    ap.add_argument("--imds", action="store_true", help="get mi tokens from Azure IMDS (on a VM)")
    a = ap.parse_args(argv)
    with open(a.targets, encoding="utf-8") as f:
        targets = [t for t in json.load(f) if a.id in (None, t["id"])]
    if not targets:
        sys.exit(f"no target {a.id!r}")
    print(json.dumps([run_target(t, imds_token if a.imds else _no_token) for t in targets], indent=2))


if __name__ == "__main__":
    main()
