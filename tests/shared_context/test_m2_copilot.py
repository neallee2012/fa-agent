"""Offline acceptance: real SDK/runtime, synthetic OpenAI HTTP responses only."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.metadata
import json
import os
import random
import shutil
import signal
import ssl
import subprocess
import struct
import sys
import threading
import time
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from shared_context.m2_copilot import ToolOutput, ToolSpec, run_copilot


ROOT = Path(__file__).resolve().parents[2]
PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg+M/w"
    "HwAEAQH/cetH5QAAAABJRU5ErkJggg=="
)
SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string", "maxLength": 40}},
    "required": ["label"],
    "additionalProperties": False,
}


@pytest.fixture
def runtime_path():
    supplied = os.environ.get("M2_TEST_RUNTIME")
    path = Path(supplied) if supplied else (
        ROOT / ".azure" / "m2-runtime-1.0.83" / "prebuilds" / "win32-x64"
        / "copilot-runtime.exe"
    )
    if not path.is_file():
        pytest.fail("Provision pinned runtime 1.0.83 and set M2_TEST_RUNTIME; no auto-download.")
    return path


@pytest.fixture
def workspace():
    path = ROOT / ".azure" / f"m2-offline-{uuid.uuid4().hex}"
    (path / "work").mkdir(parents=True)
    yield path
    shutil.rmtree(path)


class ScriptedEndpoint:
    """Transport seam: no sockets, no model calls; never record Authorization."""

    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []
        self.request_sizes = []
        self.token_digests = []
        self.token_count = 0
        self.expected_url = "https://context.invalid/model/v1/chat/completions"
        self.expected_model = "gpt-4.1-mini"
        self.unbounded = False

    def token(self):
        self.token_count += 1
        return f"synthetic-offline-{self.token_count}"

    async def send(self, request):
        assert str(request.url) == self.expected_url
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body["model"] == self.expected_model
        if not self.unbounded:
            assert body.get("max_tokens", body.get("max_completion_tokens")) == 1024
        self.requests.append(body)
        self.request_sizes.append(len(request.content))
        self.token_digests.append(hashlib.sha256(
            request.headers["authorization"].encode()
        ).hexdigest())
        reply = next(self.replies)
        if callable(reply):
            reply = reply(body)
        base = {
            "id": f"chatcmpl-offline-{len(self.requests)}",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gpt-4.1-mini",
        }
        delta = {"role": "assistant", **reply}
        reason = "tool_calls" if "tool_calls" in reply else "stop"
        chunks = [
            {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
            {**base, "choices": [], "usage": {
                "prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
            }},
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
            "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            + "data: [DONE]\n\n"
        ).encode())

    @property
    def transport(self):
        return httpx.MockTransport(self.send)


@pytest.fixture
def local_tls_endpoint(workspace):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "M2 offline TLS fixture")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]), False)
        .sign(key, hashes.SHA256())
    )
    ca_file, key_file = workspace / "tls-ca.pem", workspace / "tls-key.pem"
    ca_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    endpoint = ScriptedEndpoint([{"content": "TRUSTED_LOCAL_TLS"}])

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/model/v1/chat/completions"
            body = self.rfile.read(int(self.headers["content-length"]))
            response = asyncio.run(endpoint.send(httpx.Request(
                "POST", endpoint.expected_url, content=body,
                headers={"authorization": self.headers["authorization"]},
            )))
            self.send_response(response.status_code)
            self.send_header("content-type", response.headers["content-type"])
            self.send_header("content-length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ca_file, key_file)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    url = f"https://127.0.0.1:{server.server_port}"
    endpoint.expected_url = url + "/model/v1/chat/completions"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield url, ca_file, endpoint
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def tool_call(name, arguments, call_id="call-screenshot"):
    return {"tool_calls": [{
        "index": 0, "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }]}


def test_real_sdk_tool_image_and_rotating_tokens(workspace, runtime_path, monkeypatch):
    assert importlib.metadata.version("github-copilot-sdk") == "1.0.13"
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN"):
        monkeypatch.setenv(key, "poisoned-host-auth")
    for key in ("HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.setenv(key, "http://127.0.0.1:1")
    monkeypatch.setenv("COPILOT_HOME", str(workspace / "untrusted-host-home"))
    endpoint = ScriptedEndpoint([
        tool_call("capture_screen", {"label": "synthetic"}),
        {"content": "OFFLINE_DONE"},
    ])
    calls, events = [], []

    async def capture(invocation):
        calls.append(invocation)
        return ToolOutput("Synthetic screenshot", png_base64=PNG)

    result = asyncio.run(run_copilot(
        state_dir=workspace / "state", working_directory=workspace / "work",
        sdk_session_id="offline-image", resume=False, prompt="Capture the synthetic screen.",
        tools=[ToolSpec("capture_screen", "Capture a synthetic screenshot", SCHEMA, capture)],
        api_url="https://context.invalid", token_provider=endpoint.token,
        on_event=events.append, timeout_seconds=35, runtime_path=runtime_path,
        http_transport=endpoint.transport,
    ))

    assert calls == [{
        "session_id": "offline-image", "tool_call_id": "call-screenshot",
        "tool_name": "capture_screen", "arguments": {"label": "synthetic"},
    }]
    assert result["sdk_session_id"] == "offline-image"
    assert result["final_message"] == "OFFLINE_DONE"
    assert len(endpoint.requests) == 2
    assert endpoint.token_count == 2
    assert len(set(endpoint.token_digests)) == 2
    versions = [e["data"]["copilot_version"] for e in result["history"] if e["type"] == "session.start"]
    # Native hostless Windows runtime reports 0.0.0; provenance is the pinned
    # preprovisioned artifact, not a fabricated correction of this SDK field.
    assert versions and isinstance(versions[0], str)
    messages = endpoint.requests[1]["messages"]
    images = [
        part for message in messages if isinstance(message.get("content"), list)
        for part in message["content"] if part["type"] == "image_url"
    ]
    assert images and images[0]["image_url"]["url"] == f"data:image/png;base64,{PNG}"
    png, offset = base64.b64decode(PNG), 8
    while offset < len(png):
        size = struct.unpack(">I", png[offset:offset + 4])[0]
        chunk = png[offset + 4:offset + 8 + size]
        crc = struct.unpack(">I", png[offset + 8 + size:offset + 12 + size])[0]
        assert zlib.crc32(chunk) == crc
        offset += 12 + size
    assert any(m.get("tool_call_id") == "call-screenshot" for m in messages)
    assert any(e["type"] == "tool.execution_complete"
               and e["data"]["tool_call_id"] == "call-screenshot" for e in events)
    assert any(e["type"] == "m2.permission" and e["data"]["approved"]
               and e["data"]["tool_name"] == "capture_screen"
               and e["data"]["tool_call_id"] == "call-screenshot" for e in events)
    assert any(e["type"] == "assistant.usage"
               and e["data"]["input_tokens"] == 11 for e in events)
    assert any(e["type"] == "session.idle" for e in events)
    assert "success" not in result  # idle is not the parent's finish_task decision
    assert "synthetic-offline-" not in json.dumps(result)
    assert not (workspace / "untrusted-host-home").exists()
    assert all(b"synthetic-offline-" not in path.read_bytes()
               for path in (workspace / "state").rglob("*") if path.is_file())


@pytest.mark.parametrize("size", [(1, 1), (1280, 800)])
def test_model_context_keeps_latest_png_and_all_text_and_tool_receipts(
    workspace, runtime_path, size,
):
    width, height = size
    labels = ("first", "second", "third")

    def png_chunk(kind, content):
        return (struct.pack(">I", len(content)) + kind + content
                + struct.pack(">I", zlib.crc32(kind + content)))

    screenshots = {}
    for seed, label in enumerate(labels):
        pixels = random.Random(seed).randbytes(width * height * 3)
        rows = b"".join(
            b"\x00" + pixels[start:start + width * 3]
            for start in range(0, len(pixels), width * 3)
        )
        png = (
            b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + png_chunk(b"IDAT", zlib.compress(rows))
            + png_chunk(b"IEND", b"")
        )
        screenshots[label] = base64.b64encode(png).decode("ascii")
    if width > 1:
        assert sum(map(len, screenshots.values())) > 4 * 1024 * 1024

    endpoint = ScriptedEndpoint([
        {"content": f"Inspecting {label}", **tool_call(
            "capture_screen", {"label": label}, f"call-{label}",
        )}
        for label in labels
    ] + [{"content": "LATEST_DESKTOP_ONLY"}])

    async def capture(invocation):
        label = invocation["arguments"]["label"]
        return ToolOutput(f"Capture receipt {label}", png_base64=screenshots[label])

    result = asyncio.run(run_copilot(
        state_dir=workspace / "state", working_directory=workspace / "work",
        sdk_session_id="offline-latest-image", resume=False,
        prompt="Keep every capture receipt while inspecting the current desktop.",
        tools=[ToolSpec("capture_screen", "Capture synthetic desktop", SCHEMA, capture)],
        api_url="https://context.invalid", token_provider=endpoint.token,
        on_event=lambda e: None, timeout_seconds=60, runtime_path=runtime_path,
        http_transport=endpoint.transport,
    ))
    assert result["final_message"] == "LATEST_DESKTOP_ONLY"
    assert len(endpoint.requests) == 4
    assert max(endpoint.request_sizes) <= 4 * 1024 * 1024
    for index, body in enumerate(endpoint.requests[1:]):
        messages = body["messages"]
        parts = [part for message in messages if isinstance(message.get("content"), list)
                 for part in message["content"]]
        image_urls = [part["image_url"]["url"] for part in parts if part["type"] == "image_url"]
        assert len(image_urls) == 1
        actual_hash = hashlib.sha256(image_urls[0].split(",", 1)[1].encode()).hexdigest()
        expected_hash = hashlib.sha256(screenshots[labels[index]].encode()).hexdigest()
        assert actual_hash == expected_hash
        omitted = [part for part in parts if part["type"] == "text"
                   and "Earlier inline PNG omitted from model context" in part["text"]]
        assert len(omitted) == index
        conversation = json.dumps(messages)
        assert "Keep every capture receipt" in conversation
        old_pixels_absent = all(screenshots[label] not in conversation for label in labels[:index])
        assert old_pixels_absent
        for label in labels[:index + 1]:
            assert f"Inspecting {label}" in conversation
            assert f"Capture receipt {label}" in conversation
            assert any(message.get("tool_call_id") == f"call-{label}" for message in messages)
            assert any(call["id"] == f"call-{label}" for message in messages
                       for call in message.get("tool_calls", []))
    history = json.dumps(result["history"])
    assert all(f"Capture receipt {label}" in history for label in labels)
    original_images_persisted = all(screenshots[label] in history for label in labels)
    assert original_images_persisted
    assert "Earlier inline PNG omitted from model context" not in history


@pytest.mark.parametrize("ca_setting", ["SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", None])
def test_platform_ca_contract_verifies_tls_without_using_host_proxy(
    workspace, runtime_path, local_tls_endpoint, monkeypatch, ca_setting,
):
    url, ca_file, endpoint = local_tls_endpoint
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        monkeypatch.delenv(key, raising=False)
    for key in ("HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.setenv(key, "http://127.0.0.1:1")
    if ca_setting:
        monkeypatch.setenv(ca_setting, str(ca_file))
    if ca_setting == "SSL_CERT_FILE":
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(workspace / "lower-priority-missing.pem"))
    coroutine = run_copilot(
        state_dir=workspace / "state", working_directory=workspace / "work",
        sdk_session_id="offline-tls", resume=False, prompt="Verify the local TLS endpoint.",
        tools=[], api_url=url, token_provider=endpoint.token,
        on_event=lambda e: None, timeout_seconds=20, runtime_path=runtime_path,
    )
    if ca_setting:
        result = asyncio.run(coroutine)
        assert result["final_message"] == "TRUSTED_LOCAL_TLS"
        assert len(endpoint.requests) == 1
    else:
        with pytest.raises(RuntimeError, match="Copilot turn failed"):
            asyncio.run(coroutine)
        assert not endpoint.requests


def test_stop_new_client_resume_retains_conversation(workspace, runtime_path):
    async def scenario():
        first = ScriptedEndpoint([{"content": "RECEIPT_AMBER_27"}])
        common = dict(
            state_dir=workspace / "state", working_directory=workspace / "work",
            sdk_session_id="offline-history", tools=[], api_url="https://context.invalid",
            on_event=lambda event: None, timeout_seconds=35, runtime_path=runtime_path,
        )
        before = await run_copilot(
            **common, resume=False, prompt="Remember the amber receipt.",
            token_provider=first.token, http_transport=first.transport,
        )

        def continuation(body):
            messages = body["messages"]
            assert any(m["role"] == "user" and "Remember the amber receipt." in str(m["content"])
                       for m in messages)
            assert any(m["role"] == "assistant" and "RECEIPT_AMBER_27" in str(m["content"])
                       for m in messages)
            return {"content": "CONTINUED_AMBER_27"}

        second = ScriptedEndpoint([continuation])
        after = await run_copilot(
            **common, resume=True, prompt="Continue using the saved receipt.",
            token_provider=second.token, http_transport=second.transport,
        )
        assert after["sdk_session_id"] == before["sdk_session_id"]
        assert after["final_message"] == "CONTINUED_AMBER_27"
        before_messages = {e["id"] for e in before["history"] if e["type"] == "assistant.message"}
        after_messages = {e["id"] for e in after["history"] if e["type"] == "assistant.message"}
        assert before_messages and before_messages < after_messages

    asyncio.run(scenario())


def test_builtin_unknown_and_invalid_custom_calls_have_no_effect(workspace, runtime_path):
    endpoint = ScriptedEndpoint([
        tool_call("powershell", {"command": "exit 99"}, "call-builtin"),
        tool_call("unknown_external", {}, "call-unknown"),
        tool_call("capture_screen", {"label": 123}, "call-invalid"),
        {"content": "CALLS_REJECTED"},
    ])
    effects, events = [], []

    async def capture(invocation):
        effects.append(invocation)
        return ToolOutput("This must not run")

    result = asyncio.run(run_copilot(
        state_dir=workspace / "state", working_directory=workspace / "work",
        sdk_session_id="offline-denial", resume=False, prompt="Use only registered valid tools.",
        tools=[ToolSpec("capture_screen", "Synthetic capture", SCHEMA, capture)],
        api_url="https://context.invalid", token_provider=endpoint.token,
        on_event=events.append, timeout_seconds=35, runtime_path=runtime_path,
        http_transport=endpoint.transport,
    ))
    assert not effects
    assert result["final_message"] == "CALLS_REJECTED"
    declared = [tool["function"]["name"] for tool in endpoint.requests[0]["tools"]]
    assert declared == ["capture_screen"]
    assert not any(e["type"] == "m2.permission" and e["data"]["approved"] for e in events)
    failed = {e["data"]["tool_call_id"] for e in events
              if e["type"] == "tool.execution_complete" and not e["data"]["success"]}
    assert failed == {"call-builtin", "call-unknown", "call-invalid"}


def test_timeout_explicitly_aborts_pending_tool(workspace, runtime_path):
    endpoint = ScriptedEndpoint([tool_call("checkpoint_and_wait", {"label": "saved"}, "call-pending")])
    events = []

    async def wait(invocation):
        await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        asyncio.run(run_copilot(
            state_dir=workspace / "state", working_directory=workspace / "work",
            sdk_session_id="offline-timeout", resume=False, prompt="Checkpoint and wait.",
            tools=[ToolSpec("checkpoint_and_wait", "Wait after checkpoint", SCHEMA, wait)],
            api_url="https://context.invalid", token_provider=endpoint.token,
            on_event=events.append, timeout_seconds=6, runtime_path=runtime_path,
            http_transport=endpoint.transport,
        ))
    assert any(e["type"] == "tool.execution_start"
               and e["data"]["tool_call_id"] == "call-pending" for e in events)
    assert any(e["type"] == "m2.aborted" for e in events)


def owned_children(worker):
    if os.name == "nt":
        command = (
            f"Get-CimInstance Win32_Process -Filter 'ParentProcessId={worker.pid}'"
            " | Select-Object -ExpandProperty ProcessId"
        )
        children = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-Command", command],
            text=True, timeout=10,
        ).split()
    else:
        children_file = Path(f"/proc/{worker.pid}/task/{worker.pid}/children")
        children = children_file.read_text().split() if children_file.exists() else []
    return [int(pid) for pid in children]


def test_native_stop_cold_resume_does_not_resend_orphan_tool(workspace, runtime_path):
    worker = subprocess.Popen([
        sys.executable, str(Path(__file__).with_name("test_m2_copilot_worker.py")),
        str(workspace), str(runtime_path),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    runtime_pids = []
    try:
        deadline = time.monotonic() + 30
        receipt = workspace / "checkpoint.json"
        while not receipt.is_file() and worker.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert receipt.is_file(), "Owned worker did not reach its checkpoint in 30 seconds"
        saved = json.loads(receipt.read_text(encoding="utf-8"))
        assert saved["tool_call_id"] == "call-orphan"
        # Observe only this test's child tree, never kill by process name.
        runtime_pids = owned_children(worker)
        assert len(runtime_pids) == 1, "Expected exactly one owned Copilot runtime"
        os.kill(runtime_pids[0], signal.SIGTERM)
        runtime_pids.clear()
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=10)
    finally:
        for pid in set(runtime_pids + owned_children(worker)):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=10)

    effects = []

    async def pending(invocation):
        effects.append(invocation)
        return ToolOutput("An orphan must never be resubmitted")

    def continuation(body):
        conversation = json.dumps(body["messages"])
        assert "call-orphan" in conversation
        assert "checkpoint_and_wait" in conversation
        assert "interrupted" in conversation.lower()
        return {"content": "RESUMED_WITH_RECEIPT"}

    endpoint = ScriptedEndpoint([continuation])
    result = asyncio.run(run_copilot(
        state_dir=workspace / "state", working_directory=workspace / "work",
        sdk_session_id="offline-orphan", resume=True,
        prompt="Continue from checkpoint receipt call-orphan; do not repeat its external effect.",
        tools=[ToolSpec("checkpoint_and_wait", "Wait after checkpoint", SCHEMA, pending)],
        api_url="https://context.invalid", token_provider=endpoint.token, on_event=lambda e: None,
        timeout_seconds=35, runtime_path=runtime_path, http_transport=endpoint.transport,
    ))
    assert not effects
    assert result["final_message"] == "RESUMED_WITH_RECEIPT"
    assert any(e["type"] == "assistant.message"
               and any(t["tool_call_id"] == "call-orphan"
                       for t in (e["data"].get("tool_requests") or []))
               for e in result["history"])


def test_successful_terminal_tool_stops_without_another_provider_request(workspace, runtime_path):
    endpoint = ScriptedEndpoint([tool_call("finish_task", {"label": "receipt"}, "call-finish")])
    receipts = []

    async def finish(invocation):
        receipts.append(invocation["tool_call_id"])
        return ToolOutput("Confirmed finish receipt")

    result = asyncio.run(run_copilot(
        state_dir=workspace / "state", working_directory=workspace / "work",
        sdk_session_id="offline-terminal", resume=False, prompt="Finish using the required receipt.",
        tools=[ToolSpec("finish_task", "Verify and finish", SCHEMA, finish, is_terminal=True)],
        api_url="https://context.invalid", token_provider=endpoint.token, on_event=lambda e: None,
        timeout_seconds=35, runtime_path=runtime_path, http_transport=endpoint.transport,
    ))
    assert receipts == ["call-finish"]
    assert len(endpoint.requests) == 1
    assert any(e["type"] == "tool.execution_complete" and e["data"]["success"]
               and e["data"]["tool_call_id"] == "call-finish" for e in result["events"])


def test_model_redirect_never_reaches_another_destination(workspace, runtime_path):
    destinations = []

    async def redirect(request):
        destinations.append(str(request.url))
        return httpx.Response(
            307, headers={"location": "https://api.github.com/not-an-approved-model"},
        )

    with pytest.raises(RuntimeError, match="Copilot turn failed"):
        asyncio.run(run_copilot(
            state_dir=workspace / "state", working_directory=workspace / "work",
            sdk_session_id="offline-redirect", resume=False, prompt="Do not follow redirects.",
            tools=[], api_url="https://context.invalid",
            token_provider=lambda: "synthetic-redirect-token", on_event=lambda e: None,
            timeout_seconds=20, runtime_path=runtime_path,
            http_transport=httpx.MockTransport(redirect),
        ))
    assert destinations
    assert set(destinations) == {"https://context.invalid/model/v1/chat/completions"}


def test_remote_schema_references_are_rejected_before_starting_runtime(workspace, runtime_path):
    async def forbidden(invocation):
        pytest.fail("Remote schema tool must never run")

    endpoint = ScriptedEndpoint([])
    with pytest.raises(ValueError, match="local definitions"):
        asyncio.run(run_copilot(
            state_dir=workspace / "state", working_directory=workspace / "work",
            sdk_session_id="offline-schema", resume=False, prompt="No schema networking.",
            tools=[ToolSpec("unsafe_schema", "Unsupported reference", {
                "type": "object", "$ref": "https://example.invalid/schema.json",
            }, forbidden)],
            api_url="https://context.invalid", token_provider=endpoint.token,
            on_event=lambda e: None, runtime_path=runtime_path, http_transport=endpoint.transport,
        ))
    assert not endpoint.requests
    assert not (workspace / "state").exists()


@pytest.mark.parametrize("failure", ["transport", "http", "stream", "invalid-response"])
def test_failed_model_transport_is_not_blindly_retried(workspace, runtime_path, failure):
    attempts, events = [], []

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"id":"partial-offline-response",'
            raise httpx.ReadError("Synthetic offline stream interruption")

    async def fail(request):
        attempts.append(str(request.url))
        if failure == "transport":
            raise httpx.ConnectError("Synthetic offline gateway failure", request=request)
        if failure == "http":
            return httpx.Response(503, json={"error": {"message": "Synthetic upstream failure"}})
        if failure == "stream":
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=BrokenStream())
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b"not JSON")

    with pytest.raises(RuntimeError, match="Copilot turn failed"):
        asyncio.run(run_copilot(
            state_dir=workspace / "state", working_directory=workspace / "work",
            sdk_session_id="offline-no-retry", resume=False, prompt="Do not retry a failed model POST.",
            tools=[], api_url="https://context.invalid",
            token_provider=lambda: "synthetic-no-retry-token", on_event=events.append,
            timeout_seconds=20, runtime_path=runtime_path,
            http_transport=httpx.MockTransport(fail),
        ))
    assert len(attempts) == 1
    failures = [event["data"] for event in events if event["type"] == "m2.provider_failed"]
    assert len(failures) == 1
    assert failures[0]["phase"] == {
        "transport": "transport", "http": "http_status",
        "stream": "stream", "invalid-response": "replayed_request",
    }[failure]
    if failure == "http":
        assert failures[0]["status_code"] == 503
    assert "synthetic-no-retry-token" not in json.dumps(events)


def test_namespaced_file_tools_reach_custom_handlers(workspace, runtime_path):
    read_name, write_name = "fa_read_file", "fa_write_file"
    read_schema = {
        "type": "object", "properties": {"name": {"type": "string", "enum": ["input.txt"]}},
        "required": ["name"], "additionalProperties": False,
    }
    write_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "enum": ["draft.txt", "result.txt"]},
            "text": {"type": "string", "minLength": 1, "maxLength": 8192},
        },
        "required": ["name", "text"], "additionalProperties": False,
    }
    calls, events = [], []

    async def read_file(invocation):
        calls.append(invocation)
        return ToolOutput("Synthetic input from the custom handler")

    async def write_file(invocation):
        calls.append(invocation)
        return ToolOutput(f'Custom write receipt: {invocation["arguments"]["name"]}')

    endpoint = ScriptedEndpoint([
        tool_call(read_name, {"name": "input.txt"}, "call-fa-read"),
        tool_call(write_name, {"name": "draft.txt", "text": "draft body"}, "call-fa-draft"),
        tool_call(write_name, {"name": "result.txt", "text": "result body"}, "call-fa-result"),
        {"content": "FILE_TOOLS_DONE"},
    ])
    result = asyncio.run(run_copilot(
        state_dir=workspace / "state", working_directory=workspace / "work",
        sdk_session_id="offline-file-tools", resume=False, prompt="Use the custom file tools.",
        tools=[
            ToolSpec(read_name, "Read the named synthetic input", read_schema, read_file),
            ToolSpec(write_name, "Write a named synthetic output", write_schema, write_file),
        ],
        api_url="https://context.invalid", token_provider=endpoint.token, on_event=events.append,
        timeout_seconds=35, runtime_path=runtime_path, http_transport=endpoint.transport,
    ))
    errors = [event["data"]["error"]["message"] for event in events
              if event["type"] == "tool.execution_complete" and not event["data"]["success"]]
    assert errors == []
    assert calls == [
        {"session_id": "offline-file-tools", "tool_call_id": "call-fa-read",
         "tool_name": "fa_read_file", "arguments": {"name": "input.txt"}},
        {"session_id": "offline-file-tools", "tool_call_id": "call-fa-draft",
         "tool_name": "fa_write_file", "arguments": {"name": "draft.txt", "text": "draft body"}},
        {"session_id": "offline-file-tools", "tool_call_id": "call-fa-result",
         "tool_name": "fa_write_file", "arguments": {"name": "result.txt", "text": "result body"}},
    ]
    approved = [(event["data"]["tool_name"], event["data"]["tool_call_id"]) for event in events
                if event["type"] == "m2.permission" and event["data"]["approved"]]
    assert approved == [
        ("fa_read_file", "call-fa-read"),
        ("fa_write_file", "call-fa-draft"),
        ("fa_write_file", "call-fa-result"),
    ]
    assert result["final_message"] == "FILE_TOOLS_DONE"
    assert len(endpoint.requests) == 4


def test_public_tool_failure_guides_correction_without_leaking_exceptions(
    workspace, runtime_path, caplog,
):
    from shared_context.m2_copilot import ToolFailure

    known_token = "synthetic-known-tool-credential"
    private_detail = "PRIVATE_UNEXPECTED_EXCEPTION_DETAIL"
    diagnostic = "DESKTOP_ACTION_INVALID: a type action accepts text, not x/y; remove the coordinates."
    calls, events = [], []

    async def act(invocation):
        label = invocation["arguments"]["label"]
        if label == "needs-correction":
            raise ToolFailure(diagnostic + " " + known_token + " " + "safe detail " * 200)
        if label == "unexpected":
            raise RuntimeError(private_detail + " " + known_token)
        calls.append(invocation)
        return ToolOutput("CORRECTED_ACTION_RECEIPT")

    endpoint = ScriptedEndpoint([
        tool_call("desktop_act", {"label": "needs-correction"}, "call-public-failure"),
        tool_call("desktop_act", {"label": "corrected"}, "call-corrected"),
        tool_call("desktop_act", {"label": "unexpected"}, "call-unexpected"),
        {"content": "DIAGNOSTICS_DONE"},
    ])
    result = asyncio.run(run_copilot(
        state_dir=workspace / "state", working_directory=workspace / "work",
        sdk_session_id="offline-tool-failure", resume=False, prompt="Correct invalid tool calls.",
        tools=[ToolSpec("desktop_act", "Synthetic desktop action", SCHEMA, act)],
        api_url="https://context.invalid", token_provider=lambda: known_token,
        on_event=events.append, timeout_seconds=35, runtime_path=runtime_path,
        http_transport=endpoint.transport,
    ))
    public_reply = next(message["content"] for message in endpoint.requests[1]["messages"]
                        if message.get("tool_call_id") == "call-public-failure")
    assert diagnostic in public_reply
    assert "[REDACTED]" in public_reply
    assert "Custom tool failed" not in public_reply
    unexpected_reply = next(message["content"] for message in endpoint.requests[3]["messages"]
                            if message.get("tool_call_id") == "call-unexpected")
    assert "Custom tool failed" in unexpected_reply
    assert calls == [{
        "session_id": "offline-tool-failure", "tool_call_id": "call-corrected",
        "tool_name": "desktop_act", "arguments": {"label": "corrected"},
    }]
    failures = [event["data"] for event in events if event["type"] == "m2.tool_failure"]
    assert [(failure["tool_name"], failure["tool_call_id"]) for failure in failures] == [
        ("desktop_act", "call-public-failure"), ("desktop_act", "call-unexpected"),
    ]
    assert failures[0]["error"].startswith(diagnostic)
    assert len(failures[0]["error"]) <= 1024
    assert failures[1]["error"] == "Custom tool failed"
    assert result["final_message"] == "DIAGNOSTICS_DONE"
    evidence = json.dumps(result) + caplog.text
    assert known_token not in evidence
    assert private_detail not in evidence
    assert all(known_token.encode() not in path.read_bytes()
               and private_detail.encode() not in path.read_bytes()
               for path in (workspace / "state").rglob("*") if path.is_file())


def test_schema_failure_feedback_reports_constraints_without_submitted_values(
    workspace, runtime_path,
):
    submitted_value = "PRIVATE_SUBMITTED_VALUE_" * 3
    calls, events = [], []

    async def act(invocation):
        calls.append(invocation)
        return ToolOutput("SCHEMA_CORRECTED")

    endpoint = ScriptedEndpoint([
        tool_call("desktop_act", {"label": submitted_value}, "call-schema-invalid"),
        tool_call("desktop_act", {"label": "corrected"}, "call-schema-corrected"),
        {"content": "SCHEMA_DIAGNOSTICS_DONE"},
    ])
    result = asyncio.run(run_copilot(
        state_dir=workspace / "state", working_directory=workspace / "work",
        sdk_session_id="offline-schema-feedback", resume=False, prompt="Correct the tool schema error.",
        tools=[ToolSpec("desktop_act", "Synthetic desktop action", SCHEMA, act)],
        api_url="https://context.invalid", token_provider=endpoint.token, on_event=events.append,
        timeout_seconds=35, runtime_path=runtime_path, http_transport=endpoint.transport,
    ))
    feedback = next(message["content"] for message in endpoint.requests[1]["messages"]
                    if message.get("tool_call_id") == "call-schema-invalid")
    assert "Invalid tool arguments" in feedback
    assert "#/properties/label/maxLength" in feedback
    assert submitted_value not in feedback
    failures = [event["data"] for event in events if event["type"] == "m2.tool_failure"]
    assert len(failures) == 1
    assert failures[0]["tool_call_id"] == "call-schema-invalid"
    assert submitted_value not in json.dumps(failures)
    assert len(failures[0]["error"]) <= 1024
    assert calls == [{
        "session_id": "offline-schema-feedback", "tool_call_id": "call-schema-corrected",
        "tool_name": "desktop_act", "arguments": {"label": "corrected"},
    }]
    assert result["final_message"] == "SCHEMA_DIAGNOSTICS_DONE"
