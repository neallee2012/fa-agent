"""Original literal UTF-8 file input follows the same request and conversation as user text."""

import hashlib
import json

import pytest

from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_text_files import UserTextFile
from shared_context.behavior_attachments import TextAttachment
from legacy_submit_rows_helpers import load_store
from assistant_api_helpers import assistant_api
from conversation_profile_selection_helpers import configured, select, template
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key

BODY = "  USER_FILE_CANARY\r\n名稱,數值\n範例,42\n"


def configured_files(h):
    h, control, human = configured(h)
    response, _ = select(h, value=template(user_text_file_profile="fa-user-text-files-v1"))
    assert response.status_code == 200, response.text
    return h, control, human


# The conversations the retiring submit opened for "empty-literal" (empty.txt) and "download-original-file"
# (data.csv, BODY): the whole stored table, then a restart's refresh installs them.
STORED = {
    "empty-literal": "5480c89cc088212a3e9199de5b9267a3855a288de0bd045239f605184b3f6885",
    "download-original-file": "6ed6c222e33119e9e517d7de7501265e5a5b0ea9d4ef5626ee62fca99b63ee2b",
}


def load_stored_files(h, control):
    load_store(h.transport.backing, "text-file-turns")
    control.refresh()


def post(h, command, route="submit"):
    return h.client.post("/work-policy/v1/" + route, content=command.model_dump_json(), headers=h.headers())


def test_complete_user_input_bound_includes_filenames_and_text_without_truncation():
    prompt = "Read the file."
    overhead = len(json.dumps({"content": prompt, "files": [{"name": "input.txt", "text": ""}]},
                             ensure_ascii=False, separators=(",", ":")).encode())
    at_limit = UserTextFile(name="input.txt", text="x" * (8192 - overhead))
    command = InstructionSubmit(request_id="at-input-limit", content=prompt, files=(at_limit,))
    assert command.files[0].text == at_limit.text
    with pytest.raises(ValueError):
        InstructionSubmit(request_id="over-input-limit", content=prompt,
                          files=(UserTextFile(name="input.txt", text=at_limit.text + "x"),))


def test_empty_literal_file_remains_an_exact_empty_file_not_a_missing_source(assistant_api):
    h, control, _ = configured_files(assistant_api)
    load_stored_files(h, control)
    route = f"/work-policy/v1/conversations/{STORED['empty-literal']}/turns/1/files/empty.txt"
    response = h.client.get(route, headers=h.headers("read-only"))
    assert response.status_code == 200 and response.content == b""
    assert response.headers["x-content-sha256"] == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("name", ["../file.txt", "C:\\file.txt", "/file.txt", "https://example.invalid/file.txt"])
def test_user_file_name_is_not_a_path_or_url(name):
    with pytest.raises(ValueError):
        UserTextFile(name=name, text=BODY)


@pytest.mark.parametrize("value", ["binary\x00data", "\ud800"])
def test_non_text_payload_is_rejected(value):
    with pytest.raises(ValueError):
        UserTextFile(name="data.txt", text=value)


def test_literal_and_referenced_files_cannot_reuse_a_name(assistant_api):
    reference = assistant_api.runtime.profiles[assistant_api.binding.binding_id].source
    with pytest.raises(ValueError):
        InstructionSubmit(request_id="ambiguous-file", content="Read files.",
            attachments=(TextAttachment(name="same.txt", source=reference),),
            files=(UserTextFile(name="same.txt", text="A distinct input"),))


def test_original_file_download_requires_current_owner_read_and_retains_exact_bytes(assistant_api):
    h, control, human = configured_files(assistant_api)
    load_stored_files(h, control)
    route = f"/work-policy/v1/conversations/{STORED['download-original-file']}/turns/1/files/data.csv"
    before = h.store._guard()
    response = h.client.get(route, headers=h.headers("read-only"))
    assert response.status_code == 200 and response.content == BODY.encode("utf-8")
    assert response.headers["x-content-sha256"] == hashlib.sha256(BODY.encode()).hexdigest()
    assert response.headers["content-disposition"] == 'attachment; filename="data.csv"'
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert h.client.get(route.replace("data.csv", "other.csv"), headers=h.headers()).status_code == 404
    assert h.client.get(route, headers=h.headers("agent")).status_code == 403
    assert h.store._guard() == before
    control.revoke(human, "withdraw-file-read-scope")
    assert h.client.get(route, headers=h.headers("read-only")).status_code == 403
