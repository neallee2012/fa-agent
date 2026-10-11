"""Original literal UTF-8 file input follows the same request and conversation as user text."""

import copy
import hashlib
import json

import pytest

from shared_context import m2_copilot
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_text_files import UserTextFile
from shared_context.behavior_attachments import TextAttachment
from shared_context.behavior_work_policy import WorkPreflightQuery
from test_behavior_assistant import assistant
from test_conversation_profile_selection import configured, select, template
from test_instruction_flow import instruction_models, runtime_path, sdk_io, check_history
from test_m3_auth import jwk_client, signing_key

BODY = "  USER_FILE_CANARY\r\n名稱,數值\n範例,42\n"


def configured_files(h):
    h, control, human = configured(h)
    response, _ = select(h, value=template(user_text_file_profile="fa-user-text-files-v1"))
    assert response.status_code == 200, response.text
    return h, control, human


def post(h, command, route="submit"):
    return h.client.post("/work-policy/v1/" + route, content=command.model_dump_json(), headers=h.headers())


def test_literal_file_is_an_explicit_versioned_input_and_empty_old_bytes_are_unchanged(assistant):
    h, control, _ = configured(assistant)
    original = InstructionSubmit(request_id="old-literal-free", content="Original plain text.")
    assert "files" not in original.model_dump(mode="json")
    before = h.store._guard()
    response = post(h, original.model_copy(update={"files": (UserTextFile(name="data.csv", text=BODY),)}))
    assert response.status_code == 403 and response.json()["error"]["code"] == "USER_TEXT_FILES_DISABLED"
    assert h.store._guard() == before and not control.records


def test_original_file_input_survives_reconcile_and_reconstruction_without_a_source_read(assistant, monkeypatch):
    h, control, _ = configured_files(assistant)
    file = UserTextFile(name="data.csv", text=BODY)
    for method in ("read_graph", "read_exact"):
        monkeypatch.setattr(h.sources, method, lambda *args, **kwargs: pytest.fail("Literal user input is not a server source read"))
    command = InstructionSubmit(request_id="literal-original", content="Inspect this literal input.", files=(file,))
    query = WorkPreflightQuery(read_id="literal-query", request_id=command.request_id,
        submit_body_sha256=hashlib.sha256(command.model_dump_json().encode()).hexdigest(),
        content=command.content, files=command.files)
    assert post(h, query, "preflight").status_code == 200
    result = post(h, command)
    assert result.status_code == 200, result.text
    binding = result.json()["binding_id"]
    assert control.records[binding].profile.instruction.files == (file,)
    before = h.store._guard()
    for route in ("submit", "submit-reconcile"):
        response = post(h, command, route)
        assert response.status_code == 200, response.text
        assert response.json()["binding_id"] == binding
    control.records.clear()
    control.refresh()
    assert control.records[binding].profile.instruction.files == (file,)
    assert h.store._guard() == before and not h.endpoint.requests
    changed = command.model_copy(update={"files": (UserTextFile(name="data.csv", text=BODY + "changed"),)})
    assert post(h, changed, "submit-reconcile").status_code == 409
    assert h.store._guard() == before


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


def test_maximum_combined_file_input_is_accepted_and_retained_by_the_real_store(assistant):
    h, control, _ = configured_files(assistant)
    prompt = "Read the file."
    overhead = len(json.dumps({"content": prompt, "files": [{"name": "input.txt", "text": ""}]},
                             ensure_ascii=False, separators=(",", ":")).encode())
    file = UserTextFile(name="input.txt", text="x" * (8192 - overhead))
    command = InstructionSubmit(request_id="full-file-input", content=prompt, files=(file,))
    accepted = post(h, command)
    assert accepted.status_code == 200, accepted.text
    record = control.records[accepted.json()["binding_id"]]
    assert record.profile.instruction.files == (file,)
    assert post(h, command, "submit-reconcile").status_code == 200


def test_empty_literal_file_remains_an_exact_empty_file_not_a_missing_source(assistant):
    h, _, _ = configured_files(assistant)
    command = InstructionSubmit(request_id="empty-literal", content="Inspect the empty file.",
                                files=(UserTextFile(name="empty.txt", text=""),))
    accepted = post(h, command)
    assert accepted.status_code == 200, accepted.text
    route = f"/work-policy/v1/conversations/{accepted.json()['conversation_id']}/turns/1/files/empty.txt"
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


def test_literal_and_referenced_files_cannot_reuse_a_name(assistant):
    reference = assistant.runtime.profiles[assistant.binding.binding_id].source
    with pytest.raises(ValueError):
        InstructionSubmit(request_id="ambiguous-file", content="Read files.",
            attachments=(TextAttachment(name="same.txt", source=reference),),
            files=(UserTextFile(name="same.txt", text="A distinct input"),))


def test_file_reaches_sdk_as_data_and_metadata_does_not_return_body(assistant, monkeypatch, tmp_path):
    h, control, _ = configured_files(assistant)
    calls = sdk_io(monkeypatch)
    factory, sent = m2_copilot.CopilotClient.side_effect, []
    def capture(**connection):
        client = factory(**connection)
        opened = client.create_session.side_effect
        async def session(*args, **options):
            value = await opened(*args, **options)
            send = value.send.side_effect
            async def sending(prompt, **kwargs):
                sent.append((prompt, copy.deepcopy(kwargs.get("attachments"))))
                return await send(prompt, **kwargs)
            value.send.side_effect = sending
            return value
        client.create_session.side_effect = session
        client.resume_session.side_effect = session
        return client
    m2_copilot.CopilotClient.side_effect = capture
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "File input accepted."}, {"content": "Original file conversation retained."}])
    file = UserTextFile(name="data.csv", text=BODY)
    command = InstructionSubmit(request_id="file-sdk-first", content="  Original instruction.\r\n", files=(file,))
    first = post(h, command).json()
    assert h.runtime.controller.step(first["binding_id"]).state == "succeeded"
    metadata = h.client.get(f"/work-policy/v1/conversations/{first['conversation_id']}/turns/1/context", headers=h.headers())
    assert metadata.status_code == 200, metadata.text
    assert metadata.json()["user_files"] == [{
        "name": "data.csv", "sha256": hashlib.sha256(BODY.encode()).hexdigest(), "utf8_bytes": len(BODY.encode()),
    }]
    assert "USER_FILE_CANARY" not in metadata.text
    h.executor.root = tmp_path / "file-cold-runtime"
    second = post(h, InstructionSubmit(request_id="file-sdk-followup", content="Continue.",
                                       conversation_id=first["conversation_id"])).json()
    assert h.runtime.controller.step(second["binding_id"]).state == "succeeded"
    assert sent[0] == (command.content + "\n\n" + m2_copilot._TEXT_FILES_HEADER + "\n"
                       + m2_copilot._text_file_block(file.name, file.text), None)
    assert sent[1] == ("Continue.", None)
    assert calls[0]["prompt"] == sent[0][0] and calls[1]["resume"]
    assert calls[0]["sdk_session_id"] == calls[1]["sdk_session_id"]
    assert control.records[first["binding_id"]].profile.instruction.files == (file,)
    check_history(h)


def test_original_file_download_requires_current_owner_read_and_retains_exact_bytes(assistant):
    h, control, human = configured_files(assistant)
    file = UserTextFile(name="data.csv", text=BODY)
    accepted = post(h, InstructionSubmit(request_id="download-original-file", content="Read the file.", files=(file,))).json()
    route = f"/work-policy/v1/conversations/{accepted['conversation_id']}/turns/1/files/data.csv"
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


def test_file_origin_cannot_be_attributed_to_different_literal_or_referenced_input(assistant):
    from shared_context.behavior_assistant_wire import Task
    from shared_context.behavior_turn_origin import instruction_turn_origin
    from shared_context.errors import Problem
    h, control, _ = configured_files(assistant)
    file = UserTextFile(name="input.txt", text=BODY)
    response = post(h, InstructionSubmit(request_id="file-origin", content="Original input.", files=(file,))).json()
    record = control.records[response["binding_id"]]
    task = Task.model_validate_json(h.runtime.job_input(record.binding))
    assert instruction_turn_origin(task, record.binding, record).source_kind == "human"
    altered = task.model_copy(update={"instruction": task.instruction.model_copy(update={
        "files": (UserTextFile(name="input.txt", text="Different bytes"),),
    })})
    with pytest.raises(Problem) as denied:
        instruction_turn_origin(altered, record.binding, record)
    assert denied.value.body["error"]["code"] == "ASSISTANT_ORIGIN_DENIED"


def test_new_file_profile_does_not_reconfigure_existing_conversations(assistant, monkeypatch):
    h, control, _ = configured(assistant)
    sdk_io(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Legacy."}, {"content": "File profile."}, {"content": "Captured file profile."}])
    old = post(h, InstructionSubmit(request_id="before-file-profile", content="Original conversation.")).json()
    assert h.runtime.controller.step(old["binding_id"]).state == "succeeded"
    selected, _ = select(h, value=template(user_text_file_profile="fa-user-text-files-v1"))
    assert selected.status_code == 200
    file = UserTextFile(name="input.txt", text=BODY)
    old_followup = post(h, InstructionSubmit(request_id="old-cannot-borrow-file-profile", content="Read file.",
                                            conversation_id=old["conversation_id"], files=(file,)))
    assert old_followup.status_code == 403 and old_followup.json()["error"]["code"] == "USER_TEXT_FILES_DISABLED"
    first = post(h, InstructionSubmit(request_id="new-file-profile", content="Read file.", files=(file,))).json()
    assert h.runtime.controller.step(first["binding_id"]).state == "succeeded"
    changed, _ = select(h, request_id="disable-new-file-profile",
                        previous=selected.json()["result"]["selection_id"], value=template())
    assert changed.status_code == 200
    followup = post(h, InstructionSubmit(request_id="keep-captured-file-profile", content="Read this original file again.",
                                        conversation_id=first["conversation_id"], files=(file,))).json()
    assert h.runtime.controller.step(followup["binding_id"]).state == "succeeded"
    assert control.records[old["binding_id"]].profile.user_text_file_profile is None
    assert control.records[followup["binding_id"]].profile.user_text_file_profile == "fa-user-text-files-v1"
    check_history(h)


@pytest.mark.parametrize("case", ["combined-limit", "duplicate-name"])
def test_direct_preflight_rejects_invalid_file_input_as_a_client_error(assistant, case):
    h, control, _ = configured_files(assistant)
    query = {"read_id": "invalid-file-read", "request_id": "invalid-file-input",
             "submit_body_sha256": "a" * 64, "content": "Read the files."}
    if case == "combined-limit":
        query["files"] = [{"name": "one.txt", "text": "x" * 4500}, {"name": "two.txt", "text": "y" * 4500}]
    else:
        query["files"] = [{"name": "same.txt", "text": BODY}]
        query["attachments"] = [TextAttachment(name="same.txt", source=control.policy.profile.source).model_dump(mode="json")]
    before = h.store._guard()
    response = h.client.post("/work-policy/v1/preflight", json=query, headers=h.headers())
    assert response.status_code == 422, response.text
    assert h.store._guard() == before and not control.records and not h.endpoint.requests
