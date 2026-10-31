"""Model-gateway probe (scripts/model-gateway-probe) against its local mock; no Azure login, token or deployment."""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE_DIR = ROOT / "scripts" / "model-gateway-probe"
sys.path.insert(0, str(PROBE_DIR))

import mock_openai  # noqa: E402
import probe  # noqa: E402

KEY = "local-test-key"


@pytest.fixture(scope="module")
def mock(tmp_path_factory, monkeypatch_module):
    log = tmp_path_factory.mktemp("mock") / "mock.jsonl"
    monkeypatch_module.setenv("MOCK_API_KEY", KEY)
    server = mock_openai.serve("127.0.0.1", 0, str(log))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1", log
    server.shutdown()


@pytest.fixture(scope="module")
def monkeypatch_module():
    with pytest.MonkeyPatch.context() as patch:
        yield patch


def target(url, wire, auth):
    return {"id": "T", "url": url, "wire": wire, "model": "gpt-5-mini", "auth": auth}


def no_token(audience):
    raise AssertionError("no token expected")


def unsigned_jwt(claims):
    encode = lambda part: base64.urlsafe_b64encode(json.dumps(part).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'none'})}.{encode(claims)}.sig-secret-part"


KEY_AUTH = {"type": "key", "header": "api-key", "env": "MOCK_API_KEY"}


@pytest.mark.parametrize("wire,path", [("chat", "/chat/completions"), ("responses", "/responses")])
def test_stream_reports_tool_call_and_usage(mock, wire, path):
    base, log = mock
    result = probe.run_target(target(base + path, wire, KEY_AUTH), no_token)
    assert result["status"] == 200, result
    assert result["tool_call"] and result["usage"]
    assert result["dns"] == ["127.0.0.1"] and result["local_ip"] == "127.0.0.1"
    assert result["error"] is None and result["body_snippet"] is None and result["token_claims"] is None
    last = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert last == {**last, "src_ip": "127.0.0.1", "method": "POST", "path": "/v1" + path, "auth_ok": True, "status": 200}


def test_wrong_key_is_401_with_snippet(mock):
    base, log = mock
    result = probe.run_target(target(base + "/chat/completions", "chat", KEY_AUTH), no_token,
                              env={"MOCK_API_KEY": "wrong"})
    assert result["status"] == 401 and "invalid api-key" in result["body_snippet"]
    assert not result["tool_call"] and not result["usage"]
    assert json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["auth_ok"] is False


def test_unknown_host_is_captured_not_raised():
    result = probe.run_target(target("https://no-such-host.invalid/v1/chat/completions", "chat", {"type": "none"}),
                              no_token)
    assert result["dns"].startswith("error") and result["error"] and result["status"] is None


def test_mi_target_reports_claims_never_the_token(mock):
    base, _ = mock
    claims = {"aud": "https://cognitiveservices.azure.com", "appid": "app", "azp": None, "oid": "oid", "tid": "tid",
              "exp": 1, "extra": "dropped"}
    token = unsigned_jwt(claims)
    assert probe.decode_claims(token) == {k: v for k, v in claims.items() if k != "extra"}
    auth = {"type": "mi", "audience": "https://cognitiveservices.azure.com"}
    result = probe.run_target(target(base + "/chat/completions", "chat", auth), lambda audience: token)
    assert result["status"] == 401 and result["token_claims"]["appid"] == "app"
    dumped = json.dumps(result)
    assert token not in dumped and token.split(".")[1] not in dumped


def test_unknown_route_is_404(mock):
    base, _ = mock
    result = probe.run_target(target(base + "/embeddings", "chat", KEY_AUTH), no_token)
    assert result["status"] == 404


def test_addon_bicep_builds(tmp_path):
    compiler = os.environ.get("BICEP_CLI") or shutil.which("bicep")
    az = shutil.which("az")
    if not (compiler or az):
        pytest.skip("no Bicep compiler or az CLI")
    template = ROOT / "infra" / "model-gateway-probe" / "addon.bicep"
    output = tmp_path / "addon.json"
    # --outfile, not --stdout: az on Windows writes stdout in the console code page, which breaks UTF-8 module text.
    command = ([compiler, "build", str(template), "--outfile", str(output)] if compiler
               else [az, "bicep", "build", "--file", str(template), "--outfile", str(output)])
    process = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=600)
    assert process.returncode == 0, process.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["resources"]
