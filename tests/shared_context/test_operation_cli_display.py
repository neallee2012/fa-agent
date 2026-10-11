"""Readable terminal JSON must preserve exact data without terminal-control effects."""

from contextlib import contextmanager, redirect_stdout
import io
import json

import httpx
import pytest

from shared_context.behavior_operations import OperationDescription
from shared_context.behavior_runtime import material
from shared_context.behavior_store import InlineMaterial
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_operation_approval_cli import approval_environment
from test_operation_rejection import rejection_case
from test_operation_rejection_cli import arguments
from test_skill_guidance_authority import approval_command, operation_path, protected_rows


@pytest.mark.parametrize("encoding", ["utf-8", "ascii", "cp1252"])
def test_approval_terminal_json_is_readable_exact_and_control_safe(
        rejection_case, monkeypatch, tmp_path, encoding):
    c = rejection_case
    cli, _, requests = approval_environment(c, monkeypatch)
    text = '請核對原始內容：臺灣資料 📄 "原文"\\路徑\n\u001b[31m\u009b31m\u202e逆向\u2066隔離\u2028下一行\u00a0\u200b'
    original_material = InlineMaterial.model_validate_json(json.dumps(c.preview["content"]))
    description = OperationDescription.model_validate_json(original_material.data).model_copy(
        update={"content": text.encode("utf-8")})
    prepared = material(description.model_dump(mode="json"))
    original_client = cli.httpx.Client

    def client(**options):
        http = original_client(**options)

        @contextmanager
        def preview_only(method, path, **kwargs):
            assert method == "GET" and path == operation_path(c.turn)
            requests.append((method, path))
            yield httpx.Response(200, json={
                **c.preview, "content": prepared.model_dump(mode="json"), "description_digest": prepared.sha256,
            })

        http.stream = preview_only
        return http

    monkeypatch.setattr(cli.httpx, "Client", client)
    monkeypatch.setattr("builtins.input", lambda _prompt: "cancel")
    command = approval_command(c.turn, c.prepared).model_copy(update={"description_digest": prepared.sha256})
    original = tmp_path / "unicode-approval.json"
    original.write_text(command.model_dump_json(), encoding="utf-8")
    before = protected_rows(c.turn)
    buffer = io.BytesIO()
    output = io.TextIOWrapper(buffer, encoding=encoding, write_through=True)
    with redirect_stdout(output):
        assert cli.main(arguments(c, "approve", original)) == 1
        output.flush()
    rendered = buffer.getvalue().decode(encoding)
    preview, _ = json.JSONDecoder().raw_decode(rendered)
    assert preview["content_view"]["text"] == text
    assert preview["description_digest"] == prepared.sha256
    assert preview["description"] == description.model_dump(mode="json")
    if encoding == "utf-8":
        assert "請核對原始內容" in rendered and "📄" in rendered
    else:
        assert "\\u8acb" in rendered and rendered.isascii()
    assert all(character not in rendered for character in ("\u001b", "\u009b", "\u202e", "\u2066", "\u2028", "\u00a0", "\u200b"))
    assert protected_rows(c.turn) == before and not c.effects
    assert requests == [("GET", operation_path(c.turn))]
    assert not original.with_name(original.name + ".attempt.json").exists()
