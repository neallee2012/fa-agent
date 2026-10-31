"""OpenAI-compatible mock standing in for an on-prem model server (stdlib only, stock Ubuntu python3).

POST /v1/chat/completions and /v1/responses; header api-key must equal MOCK_API_KEY.
Streamed replies carry one get_time tool call and a usage record in each wire's real event format.
One JSON line per request goes to --log and stdout; request headers are never logged.
"""

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USAGE_CHAT = {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
USAGE_RESPONSES = {"input_tokens": 12, "output_tokens": 5, "total_tokens": 17}
CALL = {"type": "function_call", "id": "fc_mock", "call_id": "call_mock", "name": "get_time", "arguments": "{}"}


def chat_stream(model):
    base = {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": int(time.time()), "model": model}
    yield {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
        {"index": 0, "id": "call_mock", "type": "function", "function": {"name": "get_time", "arguments": "{}"}}]},
        "finish_reason": None}]}
    yield {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
    yield {**base, "choices": [], "usage": USAGE_CHAT}


def chat_json(model):
    return {"id": "chatcmpl-mock", "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                         "tool_calls": [{"id": "call_mock", "type": "function",
                                         "function": {"name": "get_time", "arguments": "{}"}}]}}],
            "usage": USAGE_CHAT}


def responses_json(model, status="completed"):
    return {"id": "resp_mock", "object": "response", "created_at": int(time.time()), "model": model, "status": status,
            "output": [CALL] if status == "completed" else [],
            "usage": USAGE_RESPONSES if status == "completed" else None}


def responses_stream(model):
    yield {"type": "response.created", "sequence_number": 0, "response": responses_json(model, "in_progress")}
    yield {"type": "response.output_item.added", "sequence_number": 1, "output_index": 0, "item": {**CALL, "arguments": ""}}
    yield {"type": "response.function_call_arguments.done", "sequence_number": 2, "output_index": 0,
           "item_id": CALL["id"], "arguments": "{}"}
    yield {"type": "response.output_item.done", "sequence_number": 3, "output_index": 0, "item": CALL}
    yield {"type": "response.completed", "sequence_number": 4, "response": responses_json(model)}


ROUTES = {"/v1/chat/completions": (chat_stream, chat_json), "/v1/responses": (responses_stream, responses_json)}


class Handler(BaseHTTPRequestHandler):
    log_path = None
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def _log(self, auth_ok, status):
        line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "src_ip": self.client_address[0],
                           "method": self.command, "path": self.path, "auth_ok": auth_ok, "status": status})
        print(line, flush=True)
        if self.log_path:
            with self.lock, open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._log(None, 404)
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        route = ROUTES.get(self.path)
        if route is None:
            self._log(None, 404)
            return self._send(404, {"error": {"message": "not found"}})
        key = os.environ.get("MOCK_API_KEY")
        auth_ok = bool(key) and self.headers.get("api-key") == key
        if not auth_ok:
            self._log(False, 401)
            return self._send(401, {"error": {"code": "401", "message": "invalid api-key"}})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            self._log(True, 400)
            return self._send(400, {"error": {"message": "invalid json"}})
        model = body.get("model", "mock")
        self._log(True, 200)
        if not body.get("stream"):
            return self._send(200, route[1](model))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for event in route[0](model):
            prefix = f"event: {event['type']}\n" if "type" in event else ""
            self.wfile.write(f"{prefix}data: {json.dumps(event)}\n\n".encode())
        if self.path == "/v1/chat/completions":
            self.wfile.write(b"data: [DONE]\n\n")
        self.close_connection = True


def serve(host, port, log_path=None):
    Handler.log_path = log_path
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--log")
    a = ap.parse_args(argv)
    if not os.environ.get("MOCK_API_KEY"):
        sys.exit("MOCK_API_KEY is not set")
    serve(a.host, a.port, a.log).serve_forever()


if __name__ == "__main__":
    main()
