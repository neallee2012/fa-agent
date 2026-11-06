"""ADR-0013: files up to 10 MiB a human attaches to a native turn (slices 1, 3, 4: text, PDF, Office)."""

import base64
import hashlib
import io
import sys
import zipfile
import zlib

import pytest

from shared_context import behavior_assistant_wire as aw
from shared_context.behavior_text_files import UserTextFile
from shared_context.behavior_user_uploads import MAX_UPLOAD_BYTES, UserUpload, upload_path
from assistant_sdk_fixture import sdk_assistant
from test_m3_auth import jwk_client, signing_key
from test_native_turn_api import native_api
from test_conversation_transfer_startup import synthetic_standing_v1
from test_native_turns import local_env
from test_run_copilot_handlers import harness


@pytest.fixture(autouse=True)
def fresh_texts(monkeypatch):
    """Each test starts with no remembered extracted text, so a fake extraction cannot leak into another test."""
    from collections import OrderedDict
    from shared_context import native_user_uploads
    monkeypatch.setattr(native_user_uploads, "_TEXTS", OrderedDict())


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def upload(name="data.csv", data=b"a,b\n1,2\n", media_type="text/csv"):
    return UserUpload(name=name, sha256=hashlib.sha256(data).hexdigest(), byte_length=len(data), media_type=media_type)


def pdf(*pages):
    """A real PDF whose pages each show one line of Helvetica text; an empty string is a page with no text."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                                NameObject("/Subtype"): NameObject("/Type1"),
                                                NameObject("/BaseFont"): NameObject("/Helvetica")}))
    for text in pages:
        page = writer.add_blank_page(612, 792)
        if text:
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
            page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def vector_pdf(pages):
    """A PDF whose pages are only a filled square, no font and no text layer: a print or scan the way pdfium sees it."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(612, 792)
        stream = DecodedStreamObject()
        stream.set_data(b"0 0 1 rg 100 100 300 300 re f")
        page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def png_pixels(encoded):
    """(width, height, channels, rows without filter bytes) of a PNG this module writes: one IDAT, filter 0."""
    import base64
    import struct
    import zlib
    data = base64.b64decode(encoded, validate=True)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, depth, color = struct.unpack(">IIBB", data[16:26])
    channels = {0: 1, 2: 3}[color]
    size = struct.unpack(">I", data[33:37])[0]
    assert depth == 8 and data[37:41] == b"IDAT"
    raw = zlib.decompress(data[41:41 + size])
    stride = width * channels + 1
    assert len(raw) == stride * height and all(raw[y * stride] == 0 for y in range(height))
    return width, height, channels, [raw[y * stride + 1:(y + 1) * stride] for y in range(height)]


def pdf_text(*pages):
    return "".join(f"--- 第 {n} 頁 ---\n{text}\n" for n, text in enumerate(pages, start=1))


def test_an_instruction_carries_only_upload_references_and_keeps_old_digests():
    plain = aw.InstructionSubmit(request_id="plain", content="Hello")
    assert "uploads" not in plain.model_dump(mode="json")
    attached = aw.InstructionSubmit(request_id="with-file", content="Sum column b", uploads=(upload(),))
    assert attached.model_dump(mode="json")["uploads"] == [upload().model_dump(mode="json")]
    # Names stay distinct across inline files and uploads, and one message names at most four.
    for bad in (dict(uploads=(upload(), upload())),
                dict(uploads=(upload(),), files=(UserTextFile(name="data.csv", text="x"),)),
                dict(uploads=tuple(upload(f"f{n}.csv", b"%d" % n) for n in range(5)))):
        with pytest.raises(ValueError):
            aw.InstructionSubmit(request_id="bad", content="x", **bad)


def test_an_upload_is_one_supported_text_type_within_ten_mib():
    for name, media_type in (("a.txt", "text/plain"), ("a.MD", "text/markdown"), ("a.json", "application/json"),
                             ("a.PDF", "application/pdf"), ("a.docx", DOCX), ("a.XLSX", XLSX), ("a.pptx", PPTX)):
        upload(name, media_type=media_type)
    for name, media_type in (("a.csv", "text/plain"), ("a.pdf", "text/plain"), ("noext", "text/plain"),
                             ("a.doc", DOCX), ("a.xlsx", DOCX)):
        with pytest.raises(ValueError):
            upload(name, media_type=media_type)
    UserUpload(name="big.txt", sha256="0" * 64, byte_length=MAX_UPLOAD_BYTES, media_type="text/plain")
    for size in (0, MAX_UPLOAD_BYTES + 1):
        with pytest.raises(ValueError):
            UserUpload(name="big.txt", sha256="0" * 64, byte_length=size, media_type="text/plain")
    assert MAX_UPLOAD_BYTES == 10 * 1024 * 1024
    assert upload_path("ab" * 32) == ".fa-attachments/" + "ab" * 32


def test_the_job_lane_refuses_uploads_instead_of_dropping_them(sdk_assistant):
    from shared_context.errors import Problem
    from test_work_policy_lifecycle import enabled
    _, control, human, _ = enabled(sdk_assistant)
    with pytest.raises(Problem) as refused:
        control.submit(human, aw.InstructionSubmit(request_id="legacy-upload", content="See", uploads=(upload(),)))
    assert refused.value.body["error"]["code"] == "WORK_POLICY_COMMAND"


def test_the_owner_uploads_raw_bytes_once_to_the_agent_session_at_their_digest_path(native_api, monkeypatch):
    from shared_context import behavior_session_files
    from shared_context.behavior_native_turn_api import PREFIX
    from shared_context.behavior_user_uploads import NativeUserUploadStored
    case, puts, written = native_api, [], {}

    def put_new(self, sid, path, data):
        puts.append((sid, path, data))
        reply = written.get("reply", len(data))
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(behavior_session_files.SessionFiles, "put_new", put_new)
    data = "名稱,數量\n".encode() + b"a,1\n" * 4096
    sha = hashlib.sha256(data).hexdigest()

    def send(body=data, digest=sha, **extra):
        headers = {**case.headers, "content-type": "application/octet-stream", "x-fa-content-sha256": digest, **extra}
        return case.client.put(PREFIX + "/files", headers=headers, content=body)

    stored = send()
    assert stored.status_code == 200, stored.text
    assert NativeUserUploadStored.model_validate_json(stored.content) == NativeUserUploadStored(
        agent_id=case.remote.target.agent_id, sha256=sha, byte_length=len(data))
    assert puts == [(case.remote.target.session_id, ".fa-attachments/" + sha, data)]
    # A different digest, an empty or oversized body: refused before any Session Files call.
    for response, status in ((send(digest="0" * 64), 422), (send(body=b""), 422),
                             (send(body=b"x" * (MAX_UPLOAD_BYTES + 1), digest="1" * 64), 413)):
        assert response.status_code == status and len(puts) == 1
    # The platform already holds this path: the same reference; Hosted checks the bytes before any turn uses them.
    written["reply"] = None
    assert send().status_code == 200
    # An unconfirmed or short write is reported as unknown: sending the same file again is safe.
    from shared_context.errors import Problem
    for reply in (len(data) - 1, Problem(503, "NATIVE_FILES_UNKNOWN", "OFFLINE lost ack")):
        written["reply"] = reply
        unknown = send()
        assert unknown.status_code == 503 and unknown.json()["error"]["code"] == "USER_FILE_UPLOAD_UNKNOWN"
    written["reply"] = Problem(403, "NATIVE_FILES_ACCESS_DENIED", "OFFLINE denied")
    assert send().json()["error"]["code"] == "NATIVE_FILES_ACCESS_DENIED"


@pytest.mark.parametrize("kind", ["csv", "pdf", "pdf-pages"])
def test_a_native_turn_checks_its_uploaded_file_and_the_model_reads_it_in_parts(local_env, monkeypatch, tmp_path, kind):
    import json
    from types import SimpleNamespace
    from starlette.testclient import TestClient
    from shared_context import native_sdk
    from shared_context.behavior_hosted import create_host
    from shared_context.behavior_native_turn_wire import NativeSubmit
    from shared_context.native_turns import NativeTurnRuntime
    from shared_context.sdk_pin import RUNTIME_VERSION
    from test_native_capabilities import selected_capture
    from test_native_turns import Harness, IDENTITY
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    if kind == "csv":
        name, data = "data.csv", ("名稱,數量\n" + "甲,1\n" * 9000).encode()
        reference, expected = upload(name, data), data.decode()
    else:
        # A PDF is read as its text layer, in the same parts, also by a model that reads images.
        pages = tuple(f"Page {n} " + "x" * 1990 for n in range(1, 21))
        name, data = "data.pdf", pdf(*pages)
        reference, expected = upload(name, data, "application/pdf"), pdf_text(*pages)
    seen, texts, step = [], [], [None]
    from shared_context import native_user_uploads
    extract = native_user_uploads._pdf_text

    def scanned_or_text(pdf_bytes):
        if step[0] == "scan":
            raise native_user_uploads.NoText
        return extract(pdf_bytes)

    monkeypatch.setattr(native_user_uploads, "_pdf_text", scanned_or_text)

    async def sdk(**options):
        seen.append(options.get("user_uploads"))
        # The default v3 profile declares no file reader; a conversation that takes files still has one every turn,
        # and one that takes none has none.
        readers = [tool for tool in options["tools"] if tool.name == "read_user_file"]
        # Only a model that reads images is shown a PDF's pages, and its session admits images from the start.
        assert options.get("pdf_page_images", False) is (kind == "pdf-pages" and bool(readers))
        if not readers:
            texts.append("no reader")
        elif options.get("user_uploads") is None:
            # A later turn reads the earlier turn's file without attaching it again; once changed or gone, the model
            # is told to ask the user to attach it again.
            call = {"tool_name": "read_user_file", "tool_call_id": "later", "arguments": {"name": name}}
            if step[0] == "read":
                output = await readers[0].handler(call)
                part = json.loads(options["behavior_boundary"].tool_result("read_user_file", output.text))
                texts.append(part["text"][:5])
                # An offset past the end is a correctable hint, not a refusal.
                call["arguments"]["offset"] = part["byte_length"]
                with pytest.raises(native_sdk.ToolFailure) as refused:
                    await readers[0].handler(call)
                shown = options["behavior_boundary"].public_tool_failure("read_user_file", refused.value.public_code)
                assert "below its byte_length" in shown and "Authority refused" not in shown
            elif step[0] == "scan" and kind == "pdf-pages":
                # A PDF with no usable text is shown to a model that reads images one page image at a time.
                output = await readers[0].handler(call)
                page = json.loads(options["behavior_boundary"].tool_result("read_user_file", output.text))
                assert (page["page"], page["page_count"], page["next_offset"]) == (1, 20, 1)
                assert output.png_base64 and "pages 11-20 were not read" in page["text"]
                texts.append("scan")
            else:
                code, guidance = (("USER_FILE_NO_TEXT", "cannot be read as text") if step[0] == "scan"
                                  else ("USER_FILE_GONE", "附件已不在，請重新附加"))
                with pytest.raises(native_sdk.ToolFailure) as refused:
                    await readers[0].handler(call)
                # What the model is shown: the fixed guidance, not a refusal that hides why (seen on dev 2026-11-04).
                shown = options["behavior_boundary"].public_tool_failure("read_user_file", refused.value.public_code)
                assert refused.value.public_code == code and guidance in shown and "Authority refused" not in shown
                texts.append(step[0])
        else:
            (tool,) = readers
            parts, offset = [], None
            while True:
                arguments = {"name": name, **({} if offset is None else {"offset": offset})}
                output = await tool.handler({"tool_name": "read_user_file", "tool_call_id": f"r{len(parts)}",
                                             "arguments": arguments})
                # Through the same result check every custom tool result passes before the model sees it.
                part = json.loads(options["behavior_boundary"].tool_result("read_user_file", output.text))
                # A shell rewrite during the turn does not change what the model reads: the bytes were checked once.
                stored.write_bytes(b"x" * len(data))
                assert len(part["text"].encode()) <= 32 * 1024 and part["byte_length"] == len(expected.encode())
                parts.append(part["text"])
                offset = part.get("next_offset")
                if offset is None:
                    break
            texts.append("".join(parts))
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": options["sdk_session_id"]}}) + "\n",
            "workspace.yaml": "id: " + options["sdk_session_id"] + "\n",
        })
        return {"final_message": "Read.", "sdk_stop": {"pid": 1, "exit_code": 0, "graceful": True,
                                                        "runtime_version": RUNTIME_VERSION}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native-turns", runtime_path=tmp_path / "OFFLINE-not-executed",
        api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE-token")
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    capture = selected_capture()
    if kind == "pdf-pages":
        seeing = capture.model_dump(mode="json")
        seeing["model"]["supports_image_input"] = True
        capture = type(capture).model_validate_json(json.dumps(seeing))
    stored = home / upload_path(reference.sha256)

    def submit(rid):
        return NativeSubmit(command=aw.InstructionSubmit(request_id=rid, content="Sum it", uploads=(reference,)),
                            capture=capture)

    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        h = Harness(runtime, SimpleNamespace(controls={}), client)
        missing = h.invoke(submit("rid-missing"))
        assert missing.status_code == 409 and missing.json()["error"]["code"] == "NATIVE_FILE_MISSING"
        # A partial write or a changed file is moved aside, so the next upload can write the same path.
        stored.parent.mkdir(parents=True)
        for n, bad in enumerate((data[:100], b"y" * len(data)), start=1):
            stored.write_bytes(bad)
            refused = h.invoke(submit(f"rid-bad-{n}"))
            assert refused.status_code == 409 and refused.json()["error"]["code"] == "NATIVE_FILE_MISSING"
            assert not stored.exists() and (stored.parent / "bad" / f"{reference.sha256}.{n}").read_bytes() == bad
        stored.write_bytes(data)
        # A conversation whose template takes no files refuses them, whatever the profile.
        closed = capture.model_dump(mode="json")
        closed["template"]["settings"]["user_text_file_profile"] = None
        closed = type(capture).model_validate_json(json.dumps(closed))
        with pytest.raises(ValueError, match="file profile"):
            NativeSubmit(command=submit("rid-closed").command, capture=closed)
        plain = NativeSubmit(command=aw.InstructionSubmit(request_id="rid-plain", content="Hi"), capture=closed)
        assert h.invoke(plain).status_code == 202
        h.wait("rid-plain", "completed")
        assert h.invoke(submit("rid-file")).status_code == 202
        cid = h.wait("rid-file", "completed").conversation_id
        stored.write_bytes(data)
        # A PDF with no extractable text (a scan) is reported as such, never guessed. A file a shell changed is
        # refused even while its text is still remembered: the bytes are checked before any remembered text is used.
        steps = ("scan",) * kind.startswith("pdf") + ("read", "changed", "gone")
        for step[0] in steps:
            if step[0] == "scan":
                native_user_uploads._TEXTS.clear()
            elif step[0] == "changed":
                stored.write_bytes(b"y" * len(data))
            elif step[0] == "gone":
                stored.unlink(missing_ok=True)
            rid = "rid-" + step[0]
            later = NativeSubmit(command=aw.InstructionSubmit(request_id=rid, content="And the total?",
                                                              conversation_id=cid), capture=capture)
            assert h.invoke(later).status_code == 202
            h.wait(rid, "completed")
    followups = [expected[:5] if done == "read" else done for done in steps]
    assert seen == [None, (reference,)] + [None] * len(steps) and texts == ["no reader", expected, *followups]


def test_the_message_names_large_files_and_each_part_read_reserves_its_own_state_room(harness):
    from unittest.mock import call
    from shared_context import m2_copilot
    from shared_context.native_user_uploads import PART_RESERVE_BYTES
    from test_run_copilot_handlers import SID, assert_rejected, custom_request, permission
    from test_run_copilot_lifecycle import session_files

    async def read(_packet):
        return m2_copilot.ToolOutput("part")

    tools = [m2_copilot.ToolSpec("read_user_file", "Fixture", {"type": "object"}, read)]
    request = custom_request("read_user_file", args={"name": "data.csv"})
    files = session_files(session_id=SID)
    for room, approved in (("reserved", True), ("busy", False), ("full", False)):
        files.reserve_large_result.return_value = room
        decision, _ = harness.run(permission(request), tools=tools, session_files=files, user_uploads=(upload(),),
                                    capability_session=None)
        assert files.reserve_large_result.call_args == call("call-1", PART_RESERVE_BYTES)
        if not approved:
            assert_rejected(decision, m2_copilot.PARALLEL_FEEDBACK if room == "busy" else m2_copilot.FILE_STATE_FULL_FEEDBACK)
    prompt = harness.session.send_and_wait.call_args.args[0]
    assert prompt.startswith("Fixture\n\n") and "- data.csv (text/csv, 8 bytes)" in prompt and "read_user_file" in prompt


def test_a_part_reserve_is_its_own_size_not_a_screenshot_reserve():
    import asyncio
    from shared_context.backup_models import MAX_BACKUP_BYTES
    from shared_context.behavior_sdk_files import SdkSessionFiles
    from shared_context.native_user_uploads import PART_RESERVE_BYTES
    files = SdkSessionFiles("part-session")
    asyncio.run(files.write_file("/session-state/events.jsonl", "x" * (MAX_BACKUP_BYTES - 3 * PART_RESERVE_BYTES // 2)))
    assert files.reserve_large_result("shot") == "full"
    assert files.reserve_large_result("part-1", PART_RESERVE_BYTES) == "reserved"
    assert files.reserve_large_result("part-2", PART_RESERVE_BYTES) == "busy"
    files.large_result_completed("part-1", 1000)
    assert files.reserve_large_result("part-2", PART_RESERVE_BYTES) == "reserved"


def test_bytes_that_are_not_utf8_still_move_forward_part_by_part():
    from shared_context.native_user_uploads import PART_BYTES, read_part
    data = b"a" * 10 + bytes([0xA4]) * (PART_BYTES + 8192) + b"tail"
    file = upload("odd.txt", data, "text/plain")
    offsets, offset = [], 0
    while offset is not None:
        offsets.append(offset)
        part = read_part(file, data, offset)
        assert part.offset == offset, "parts never overlap"
        offset = part.next_offset
        assert offset is None or offset > offsets[-1]
    assert len(offsets) == 2


def test_a_part_ends_on_a_character_boundary_and_names_the_next_offset():
    from shared_context.native_user_uploads import PART_BYTES, read_part
    data = "甲".encode() * (PART_BYTES // 3 + 5)
    file = upload("a.txt", data, "text/plain")
    first = read_part(file, data, 0)
    assert first.offset == 0 and first.next_offset % 3 == 0 and len(first.text.encode()) <= PART_BYTES
    # An offset inside a character starts at that character.
    second = read_part(file, data, first.next_offset + 1)
    assert second.offset == first.next_offset and second.next_offset is None
    assert first.text + second.text == data.decode()
    assert read_part(file, data, 1).offset == 0, "inside the first character starts at the file's start"
    with pytest.raises(ValueError):
        read_part(file, data, len(data))


def test_a_pdf_is_read_as_its_text_layer_page_by_page():
    from shared_context.native_user_uploads import readable
    data = pdf("Hello page one", "", "Third page")
    assert readable(upload("a.pdf", data, "application/pdf"), data).decode() == pdf_text("Hello page one", "", "Third page")
    # A text type is read exactly as uploaded.
    assert readable(upload(), b"a,b\n1,2\n") == b"a,b\n1,2\n"


def test_a_pdf_with_no_readable_text_is_refused_not_guessed():
    from shared_context.native_user_uploads import NoText, readable
    for data in (pdf("", ""), b"%PDF-1.4 broken", b"not a pdf at all"):
        with pytest.raises(NoText):
            readable(upload("scan.pdf", data, "application/pdf"), data)


def test_a_pdf_without_a_text_layer_is_read_one_bounded_page_image_at_a_time():
    from shared_context.native_user_uploads import NoText, PAGE_PNG_BYTES, pdf_pages, read_page, readable
    data = vector_pdf(3)
    file = upload("print.pdf", data, "application/pdf")
    with pytest.raises(NoText):
        readable(file, data)
    pages = pdf_pages(data)
    assert pages.count == 3
    offset, numbers = 0, []
    while offset is not None:
        part, png = read_page(file, pages, offset)
        assert len(png) * 3 // 4 <= PAGE_PNG_BYTES and "no text layer" in part.text
        width, height, channels, rows = png_pixels(png)
        # A US Letter page (612 x 792 pt) at a 1600 px long side, rounded up, in color; the square is blue.
        assert (width, height, channels) == (1237, 1600, 3)
        assert rows[height - 500][600 * 3:600 * 3 + 3] == b"\x00\x00\xff"
        assert rows[10][:3] == b"\xff\xff\xff"
        numbers.append(part.page)
        offset = part.next_offset
    assert numbers == [1, 2, 3] and "were not read" not in part.text


def test_at_most_ten_pages_are_shown_and_the_model_is_told_which_were_not():
    from shared_context.native_user_uploads import pdf_pages, read_page
    data = vector_pdf(12)
    file, pages = upload("long.pdf", data, "application/pdf"), pdf_pages(data)
    last, png = read_page(file, pages, 9)
    assert (last.page, last.page_count, last.next_offset) == (10, 12, None) and png
    assert "tell the user pages 11-12 were not read" in last.text
    with pytest.raises(ValueError):
        read_page(file, pages, 10)


def test_a_page_too_large_to_show_is_named_instead_of_sent(monkeypatch):
    from shared_context import native_user_uploads
    monkeypatch.setattr(native_user_uploads, "PAGE_PNG_BYTES", 10)
    data = vector_pdf(1)
    part, png = native_user_uploads.read_page(upload("big.pdf", data, "application/pdf"),
                                              native_user_uploads.pdf_pages(data), 0)
    assert png is None and "tell the user page 1 was not read" in part.text


def test_a_page_too_large_in_color_falls_back_to_grayscale_then_to_a_shorter_side(monkeypatch):
    from shared_context import native_user_uploads
    data = vector_pdf(1)
    file, pages = upload("print.pdf", data, "application/pdf"), native_user_uploads.pdf_pages(data)
    color = len(base64.b64decode(native_user_uploads.read_page(file, pages, 0)[1]))
    monkeypatch.setattr(native_user_uploads, "PAGE_PNG_BYTES", color - 1)
    width, height, channels, rows = png_pixels(native_user_uploads.read_page(file, pages, 0)[1])
    # Same 1600 px long side, one channel; the blue square is a dark gray, the margin stays white.
    assert (width, height, channels) == (1237, 1600, 1)
    assert 0 < rows[height - 500][600] < 128 and rows[10][0] == 255
    gray = len(base64.b64decode(native_user_uploads.read_page(file, pages, 0)[1]))
    monkeypatch.setattr(native_user_uploads, "PAGE_PNG_BYTES", gray - 1)
    width, height, channels, rows = png_pixels(native_user_uploads.read_page(file, pages, 0)[1])
    # 612 x 792 pt at a 1000 px long side, rounded up.
    assert (width, height, channels) == (773, 1000, 1) and 0 < rows[height - 400][300] < 128


def stand_in_renderer(monkeypatch, tmp_path, source):
    """Points the module at a renderer child that runs `source` instead of pdfium."""
    from shared_context import native_user_uploads
    script = tmp_path / "renderer.py"
    script.write_text(source)
    monkeypatch.setattr(native_user_uploads, "_RENDERER", script)


@pytest.mark.parametrize("source, logged", [("raise MemoryError", "exit=1 MemoryError"), (
    "import os, signal\nos.kill(os.getpid(), getattr(signal, 'SIGKILL', signal.SIGTERM))", "PDF_RENDER_FAILED exit=")])
def test_a_page_whose_renderer_fails_or_is_killed_is_named_not_shown(monkeypatch, tmp_path, caplog, source, logged):
    from shared_context import native_user_uploads
    data = vector_pdf(1)
    pages = native_user_uploads.pdf_pages(data)
    stand_in_renderer(monkeypatch, tmp_path, source)
    part, png = native_user_uploads.read_page(upload("bomb.pdf", data, "application/pdf"), pages, 0)
    assert png is None and "tell the user page 1 was not read" in part.text
    assert "PDF_RENDER_FAILED exit=" in caplog.text and logged in caplog.text


def test_a_page_the_renderer_does_not_finish_in_time_is_named_not_shown(monkeypatch, tmp_path, caplog):
    import time
    from shared_context import native_user_uploads
    data = vector_pdf(1)
    pages = native_user_uploads.pdf_pages(data)
    stand_in_renderer(monkeypatch, tmp_path, "import time\ntime.sleep(60)")
    monkeypatch.setattr(native_user_uploads, "PAGE_SECONDS", 1)
    started = time.monotonic()
    part, png = native_user_uploads.read_page(upload("slow.pdf", data, "application/pdf"), pages, 0)
    assert png is None and "tell the user page 1 was not read" in part.text
    assert time.monotonic() - started < 20 and "PDF_RENDER_FAILED TimeoutExpired" in caplog.text


@pytest.mark.parametrize("source", ["raise MemoryError", "print('three')",
                                    "import sys\nfor _ in range(64): sys.stdout.write('7' * 1048576)"])
def test_a_pdf_whose_page_count_fails_in_the_renderer_has_no_pages(monkeypatch, tmp_path, source):
    from shared_context.native_user_uploads import NoText, pdf_pages
    stand_in_renderer(monkeypatch, tmp_path, source)
    with pytest.raises(NoText):
        pdf_pages(vector_pdf(1))


PNG_START = b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("written", ["b'GIF89a' + b'0' * 100", "PNG_START + b'0' * (PAGE_PNG_BYTES - 7)",
                                     "PNG_START + b'0' * (64 << 20)"])
def test_a_page_the_renderer_returns_too_large_or_not_as_a_png_is_named_not_shown(monkeypatch, tmp_path, caplog,
                                                                                  written):
    from shared_context import native_user_uploads
    data = vector_pdf(1)
    pages = native_user_uploads.pdf_pages(data)
    stand_in_renderer(monkeypatch, tmp_path, f"import sys\nPNG_START, PAGE_PNG_BYTES = {PNG_START!r}, "
                                             f"{native_user_uploads.PAGE_PNG_BYTES}\nsys.stdout.buffer.write({written})")
    if sys.platform.startswith("linux"):
        import resource
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    part, png = native_user_uploads.read_page(upload("bomb.pdf", data, "application/pdf"), pages, 0)
    assert png is None and "tell the user page 1 was not read" in part.text and "PDF_RENDER_FAILED" in caplog.text
    if sys.platform.startswith("linux"):
        assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before < 16 * 1024, "KiB the parent grew"


def test_a_renderer_that_writes_past_its_limit_and_hangs_is_killed_at_once(monkeypatch, tmp_path, caplog):
    import time
    from shared_context import native_user_uploads
    data = vector_pdf(1)
    pages = native_user_uploads.pdf_pages(data)
    stand_in_renderer(monkeypatch, tmp_path, f"import sys, time\nsys.stdout.buffer.write({PNG_START!r} + b'0' * "
                                             f"{native_user_uploads.PAGE_PNG_BYTES})\nsys.stdout.flush()\ntime.sleep(60)")
    started = time.monotonic()
    part, png = native_user_uploads.read_page(upload("bomb.pdf", data, "application/pdf"), pages, 0)
    assert png is None and "tell the user page 1 was not read" in part.text
    # Reading to the end, or waiting without killing it, lasts until the PAGE_SECONDS (30 s) timer.
    assert time.monotonic() - started < 10 and "PDF_RENDER_FAILED output over" in caplog.text


def test_a_failing_renderer_is_logged_by_the_end_of_its_last_stderr_line_only(monkeypatch, tmp_path, caplog):
    from shared_context import native_user_uploads
    data = vector_pdf(1)
    pages = native_user_uploads.pdf_pages(data)
    stand_in_renderer(monkeypatch, tmp_path, "import sys\nfor _ in range(65536): sys.stderr.write('x' * 1023 + '\\n')\n"
                                             "sys.stderr.write('last ' + 'y' * 500)\nsys.exit(3)")
    if sys.platform.startswith("linux"):
        import resource
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    part, png = native_user_uploads.read_page(upload("bomb.pdf", data, "application/pdf"), pages, 0)
    assert png is None and "tell the user page 1 was not read" in part.text
    assert "PDF_RENDER_FAILED exit=3 last " + "y" * 195 in caplog.text
    assert "y" * 196 not in caplog.text and "x" * 10 not in caplog.text
    if sys.platform.startswith("linux"):  # 64 MiB went to stderr; only its last 4 KiB is kept
        assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before < 16 * 1024, "KiB the parent grew"


def test_a_renderer_that_exits_before_reading_the_pdf_is_logged_by_its_own_error(monkeypatch, tmp_path, caplog):
    from shared_context import native_user_uploads
    stand_in_renderer(monkeypatch, tmp_path, "import sys\nsys.stderr.write('MemoryError')\nsys.exit(1)")
    pages = native_user_uploads.PdfPages(bytes(8 << 20), 1)  # far more than a pipe holds: writing it fails
    part, png = native_user_uploads.read_page(upload("bomb.pdf", pages.data, "application/pdf"), pages, 0)
    assert png is None and "tell the user page 1 was not read" in part.text
    assert "PDF_RENDER_FAILED exit=1 MemoryError" in caplog.text and "BrokenPipe" not in caplog.text


def test_a_renderer_is_killed_when_its_helper_threads_cannot_start(monkeypatch, tmp_path):
    import threading
    import time
    import types
    from shared_context import native_user_uploads
    data = vector_pdf(1)
    pages = native_user_uploads.pdf_pages(data)
    stand_in_renderer(monkeypatch, tmp_path, "import time\ntime.sleep(60)")

    class Unstartable(threading.Thread):
        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(native_user_uploads, "threading", types.SimpleNamespace(Timer=threading.Timer,
                                                                                Thread=Unstartable))
    started = time.monotonic()
    with pytest.raises(RuntimeError):
        native_user_uploads.read_page(upload("a.pdf", data, "application/pdf"), pages, 0)
    assert time.monotonic() - started < 10, "the child was left running until the PAGE_SECONDS timer"


def test_the_renderer_is_isolated_and_gets_the_pdf_and_page_but_not_the_environment(monkeypatch, tmp_path):
    import json
    from shared_context import native_user_uploads
    data = vector_pdf(2)
    pages = native_user_uploads.pdf_pages(data)
    monkeypatch.setenv("FA_TEST_SECRET", "x")
    stand_in_renderer(monkeypatch, tmp_path, f"import json, os, sys\nsys.stdout.buffer.write({PNG_START!r} + "
                                             "json.dumps([sys.argv[1:], len(sys.stdin.buffer.read()), "
                                             "sorted(os.environ), sys.flags.isolated]).encode())")
    seen = json.loads(base64.b64decode(native_user_uploads.read_page(upload("a.pdf", data, "application/pdf"),
                                                                      pages, 1)[1])[len(PNG_START):])
    assert seen[0] == [str(native_user_uploads.PAGE_ADDRESS_SPACE), "render", "1",
                       str(native_user_uploads.PAGE_PNG_BYTES)]
    assert seen[1] == len(data) and set(seen[2]) <= {"SYSTEMROOT", "LC_CTYPE"}, seen[2]
    assert seen[3] == 1, "-I keeps sys.path, PYTHON* variables and user site out of the child"


def flate_gray(side):
    """A `side`² 8-bit gray Flate image of zeros, compressed in pieces so the test never holds it decoded."""
    packer, row = zlib.compressobj(9), b"\0" * side
    body = b"".join(packer.compress(row) for _ in range(side)) + packer.flush()
    return (b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray /BitsPerComponent 8 "
            b"/Filter /FlateDecode /Length %d >>\nstream\n" % (side, side, len(body)) + body + b"\nendstream")


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="RLIMIT_AS exists only on Linux (Hosted); elsewhere the renderer runs without a cap")
def test_the_renderer_cap_drops_an_image_too_large_and_fails_a_page_whose_images_add_up_too_large():
    import resource
    from shared_context import native_user_uploads
    box, draw = b"/MediaBox [0 0 612 792]", b"<< /Length 30 >>\nstream\nq 612 0 0 792 0 0 cm /Im0 Do Q\nendstream"
    catalog = b"<< /Type /Catalog /Pages 2 0 R >>"
    # A 1 x 1 image whose 30000² soft mask pdfium decodes whole (900 MB), as a plain page and under a page tree
    # node with /Type /Page and /Kids, which pypdf and pdfium count differently.
    plain = tree_pdf([catalog, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                      b"<< /Type /Page /Parent 2 0 R %s /Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>"
                      % box, draw, tree_gray(1, b"/SMask 6 0 R"), flate_gray(30000)])
    tree = tree_pdf([catalog, b"<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
                     b"<< /Type /Page /Kids [4 0 R] /Count 1 /Parent 2 0 R %s >>" % box,
                     b"<< /Type /Page /Parent 3 0 R %s /Resources << /XObject << /Im0 6 0 R >> >> /Contents 8 0 R >>"
                     % box, b"<< /Type /Page /Parent 2 0 R %s >>" % box, tree_gray(1, b"/SMask 7 0 R"),
                     flate_gray(30000), draw])
    # 40 different 7000² images (49 MB each, 1.9 GB together) drawn on one page.
    ops = b" ".join(b"q 612 0 0 792 0 0 cm /Im%d Do Q" % n for n in range(40))
    many = tree_pdf([catalog, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                     b"<< /Type /Page /Parent 2 0 R %s /Resources << /XObject << %s >> >> /Contents 4 0 R >>"
                     % (box, b" ".join(b"/Im%d %d 0 R" % (n, n + 5) for n in range(40))),
                     b"<< /Length %d >>\nstream\n%s\nendstream" % (len(ops), ops), *[flate_gray(7000)] * 40])
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    for data in (plain, tree):
        pages = native_user_uploads.pdf_pages(data)
        part, png = native_user_uploads.read_page(upload("bomb.pdf", data, "application/pdf"), pages, 0)
        # Over the cap pdfium leaves the mask out and still draws the page: the gray pixel fills it instead of
        # being masked away to white. The model is not told; ADR-0013 §5 names this.
        width, height, channels, rows = png_pixels(png)
        assert rows[height // 2][width // 2 * channels] == 0x80 and "was not read" not in part.text
    part, png = native_user_uploads.read_page(upload("bomb.pdf", many, "application/pdf"),
                                              native_user_uploads.pdf_pages(many), 0)
    assert png is None and "tell the user page 1 was not read" in part.text
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before < 100 * 1024, "KiB the parent grew"


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="RLIMIT_AS exists only on Linux (Hosted); elsewhere the renderer runs without a cap")
def test_a_color_scan_larger_than_the_cap_decoded_is_still_drawn_when_it_falls_back_to_gray():
    import random
    from shared_context import native_user_uploads
    # 13000 x 16000 RGB (595 MiB decoded) of random 32 px blocks, so its 1600 px color PNG is over 300 KiB and the
    # page is drawn again in gray. pdfium's grayscale mode decodes the image whole and, past the cap, leaves it out:
    # a blank page the model would read as empty. The PNG Up predictor keeps the file small.
    width, height, rng, packer, body = 13000, 16000, random.Random(7), zlib.compressobj(6), []
    same_row = b"\2" + bytes(width * 3)
    for y in range(height):
        if y % 32:
            body.append(packer.compress(same_row))
        else:
            colors = rng.randbytes(width // 32 * 3 + 3)
            row = b"".join(colors[i:i + 3] * 32 for i in range(0, len(colors), 3))[:width * 3]
            body.append(packer.compress(b"\0" + row))
    body = b"".join(body) + packer.flush()
    image = (b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB /BitsPerComponent 8 "
             b"/Filter /FlateDecode /DecodeParms << /Predictor 15 /Colors 3 /Columns %d >> /Length %d >>\nstream\n"
             % (width, height, width, len(body)) + body + b"\nendstream")
    data = tree_pdf([b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                     b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 754] /Resources << /XObject << /Im0 5 0 R >> >>"
                     b" /Contents 4 0 R >>", b"<< /Length 30 >>\nstream\nq 612 0 0 754 0 0 cm /Im0 Do Q\nendstream",
                     image])
    part, png = native_user_uploads.read_page(upload("a1.pdf", data, "application/pdf"),
                                              native_user_uploads.pdf_pages(data), 0)
    assert png and "was not read" not in part.text
    width, height, channels, rows = png_pixels(png)
    assert channels == 1, "the color PNG was small enough: this no longer reaches the gray fallback"
    assert sum(value >= 250 for row in rows for value in row) < width * height // 10, "the page came back blank"


def scan_pdf(width, height, pixels):
    """A one-page PDF that is only a full-page 8-bit gray image, the way a scanner writes one."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject
    writer = PdfWriter()
    page = writer.add_blank_page(612, 792)
    image = DecodedStreamObject()
    image.set_data(pixels)
    image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                  NameObject("/Width"): NumberObject(width), NameObject("/Height"): NumberObject(height),
                  NameObject("/ColorSpace"): NameObject("/DeviceGray"), NameObject("/BitsPerComponent"): NumberObject(8)})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject(
        {NameObject("/Im0"): writer._add_object(image)})})
    contents = DecodedStreamObject()
    contents.set_data(b"q 612 0 0 792 0 0 cm /Im0 Do Q")
    page[NameObject("/Contents")] = writer._add_object(contents)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_a_noisy_scanned_page_is_still_shown_within_the_size_cap():
    import random
    from shared_context import native_user_uploads
    width, height = 1275, 1650  # US Letter at 150 dpi
    paper = bytes(min(255, 223 + value % 25) for value in range(256))  # 223-247: white paper with sensor noise
    ink = bytes(18 + value % 25 for value in range(256))
    noise = random.Random(1).randbytes(width * height)
    band = width * 12
    pixels = b"".join(noise[at:at + band].translate(ink if at // band % 3 == 0 else paper)
                      for at in range(0, len(noise), band))
    data = scan_pdf(width, height, pixels)
    part, png = native_user_uploads.read_page(upload("scan.pdf", data, "application/pdf"),
                                              native_user_uploads.pdf_pages(data), 0)
    assert png is not None and len(base64.b64decode(png)) <= native_user_uploads.PAGE_PNG_BYTES
    # Gray at the full 1600 px long side, not shrunk until the noise fits.
    assert png_pixels(png)[1:3] == (1600, 1) and "was not read" not in part.text


def test_pages_sharing_one_resource_dict_of_scans_are_each_shown():
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject
    from shared_context import native_user_uploads
    writer = PdfWriter()
    images = DictionaryObject()
    for number in range(10):  # 300 dpi A4 scans, 8.7 MP each, 87 MP together
        image = DecodedStreamObject()
        image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                      NameObject("/Width"): NumberObject(2480), NameObject("/Height"): NumberObject(3508),
                      NameObject("/ColorSpace"): NameObject("/DeviceGray"),
                      NameObject("/BitsPerComponent"): NumberObject(8)})
        images[NameObject(f"/Im{number}")] = writer._add_object(image)
    resources = writer._add_object(DictionaryObject({NameObject("/XObject"): images}))
    for number in range(10):  # each page draws only its own scan
        page = writer.add_blank_page(595, 842)
        page[NameObject("/Resources")] = resources
        contents = DecodedStreamObject()
        contents.set_data(b"q 595 0 0 842 0 0 cm /Im%d Do Q" % number)
        page[NameObject("/Contents")] = writer._add_object(contents)
    for number, page in enumerate(writer.pages):  # a link to the next page reaches that page, not drawn here
        link = DictionaryObject({NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Link"),
                                 NameObject("/Rect"): ArrayObject([NumberObject(0)] * 4),
                                 NameObject("/P"): page.indirect_reference,
                                 NameObject("/Dest"): ArrayObject([writer.pages[(number + 1) % 10].indirect_reference,
                                                                   NameObject("/Fit")])})
        page[NameObject("/Annots")] = ArrayObject([writer._add_object(link)])
    out = io.BytesIO()
    writer.write(out)
    data = out.getvalue()
    pages = native_user_uploads.pdf_pages(data)
    for index in (0, 9):
        assert native_user_uploads.read_page(upload("scans.pdf", data, "application/pdf"), pages, index)[1]


def tree_pdf(objects):
    """A PDF of `objects` (numbered from 1, the catalog first) with a plain xref table, page tree as written."""
    out, offsets = bytearray(b"%PDF-1.7\n"), []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1) + b"".join(b"%010d 00000 n \n" % offset
                                                                             for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def tree_gray(side, extra=b""):
    return (b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray /BitsPerComponent 8 "
            b"/Length 1 %s >>\nstream\n\x80\nendstream" % (side, side, extra))


def test_pages_inheriting_resources_from_a_nested_page_tree_node_are_each_shown():
    from shared_context import native_user_uploads
    box = b"/MediaBox [0 0 612 792]"
    data = tree_pdf([b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 2 >>",
                     b"<< /Type /Pages /Parent 2 0 R /Kids [4 0 R 5 0 R] /Count 2 /Resources 7 0 R >>",
                     b"<< /Type /Page /Parent 3 0 R %s /Contents 6 0 R >>" % box,
                     b"<< /Type /Page /Parent 3 0 R %s /Contents 6 0 R >>" % box,
                     b"<< /Length 30 >>\nstream\nq 612 0 0 792 0 0 cm /Im0 Do Q\nendstream",
                     b"<< /XObject << /Im0 8 0 R >> >>", tree_gray(1)])
    pages = native_user_uploads.pdf_pages(data)
    assert pages.count == 2
    for index in (0, 1):
        assert native_user_uploads.read_page(upload("nested.pdf", data, "application/pdf"), pages, index)[1]


def test_a_damaged_or_locked_pdf_has_no_pages_to_show():
    from pypdf import PdfReader, PdfWriter
    from shared_context.native_user_uploads import NoText, pdf_pages
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(vector_pdf(1))))
    writer.encrypt(user_password="user", owner_password="owner")
    locked = io.BytesIO()
    writer.write(locked)
    for data in (b"%PDF-1.4 broken", b"not a pdf at all", locked.getvalue()):
        with pytest.raises(NoText):
            pdf_pages(data)


def test_a_pdf_page_reaches_the_model_as_an_image_but_is_never_a_screenshot_to_deliver(harness):
    from shared_context import m2_copilot
    from test_run_copilot_handlers import PNG, invocation
    images = []

    async def read(_packet):
        return m2_copilot.ToolOutput("page", png_base64=PNG)

    async def scenario(options):
        tool = next(item for item in options["tools"] if item.name == "read_user_file")
        result = await tool.handler(invocation("read_user_file", {}))
        # Told apart by the image, not the call id: a delegated child's event may carry another id.
        for call, data in (("child-page", PNG), ("shot-1", "shot-1")):
            options["on_event"]({"type": "tool.execution_complete", "data": {
                "success": True, "tool_call_id": call,
                "result": {"binary_results_for_llm": [{"mime_type": "image/png", "data": data}]}}})
        return result, options.get("model_capabilities")

    tools = [m2_copilot.ToolSpec("read_user_file", "Fixture", {"type": "object"}, read)]
    (result, capabilities), _ = harness.run(scenario, tools=tools, capability_session=None, pdf_page_images=True,
                                            on_tool_image=images.append)
    assert result.result_type != "failure" and capabilities.supports.vision is True
    assert [(item.mime_type, item.data) for item in result.binary_results_for_llm] == [("image/png", PNG)]
    # The page's image is not the latest screenshot; another call's image still is.
    assert images == ["shot-1"]
    (result, capabilities), _ = harness.run(scenario, tools=tools, capability_session=None,
                                            on_tool_image=images.append)
    assert result.result_type == "failure" and capabilities is None


def test_pdf_text_keeps_what_fits_its_limit_and_says_where_it_stopped(monkeypatch):
    from shared_context import native_user_uploads
    monkeypatch.setattr(native_user_uploads, "TEXT_LIMIT", 256)
    marker = "--- 第 2 頁中途起沒有抽取：文字超過 10 MiB ---\n"
    data = pdf("A" * 60, "B" * 60, "C" * 60)
    text = native_user_uploads.readable(upload("long.pdf", data, "application/pdf"), data).decode()
    assert text.startswith(pdf_text("A" * 60) + "--- 第 2 頁 ---\nBBB") and text.endswith("B\n" + marker) and "C" not in text
    assert len(text.encode()) <= 256
    # One page larger than the whole limit is still read as far as it fits, not reported as having no text.
    data = pdf("D" * 400)
    text = native_user_uploads.readable(upload("one.pdf", data, "application/pdf"), data).decode()
    assert text.startswith("--- 第 1 頁 ---\nDDD") and text.endswith(marker.replace("2", "1", 1))


def test_a_broken_character_in_a_pdf_does_not_hide_the_rest_of_its_text(monkeypatch):
    # pypdf decodes a damaged ToUnicode map with surrogatepass, so a lone surrogate can reach the text.
    from pypdf import PageObject
    from shared_context.native_user_uploads import readable
    monkeypatch.setattr(PageObject, "extract_text", lambda self, *a, **k: "page \ud800 two")
    data = pdf("x")
    assert readable(upload("odd.pdf", data, "application/pdf"), data).decode() == pdf_text("page ? two")


def test_a_pdf_is_extracted_once_for_each_of_the_last_four_checked_digests(monkeypatch):
    from shared_context import native_user_uploads
    calls = []
    extract = native_user_uploads._pdf_text
    monkeypatch.setattr(native_user_uploads, "_pdf_text", lambda data: calls.append(data) or extract(data))
    files = [pdf(f"File {n}") for n in range(5)]

    def read(data):
        return native_user_uploads.readable(upload("a.pdf", data, "application/pdf"), data)

    assert read(files[0]) == read(files[0]) and len(calls) == 1
    for data in files[1:]:
        read(data)
    assert len(calls) == 5
    read(files[1])
    assert len(calls) == 5, "still one of the last four"
    read(files[0])
    assert len(calls) == 6, "the oldest was dropped"


def test_pdf_text_uses_the_ideographs_a_browser_printed_pdf_maps_to_look_alike_radicals(monkeypatch):
    # Chromium and Word PDFs often map CJK glyphs to Kangxi radicals (U+2F00-U+2FDF), so 「第二頁」 would not match.
    from pypdf import PageObject
    from shared_context import native_user_uploads
    monkeypatch.setattr(PageObject, "extract_text", lambda self, *a, **k: "第⼆⾴：⼀季營收 12%")
    data = pdf("x")
    text = native_user_uploads.readable(upload("print.pdf", data, "application/pdf"), data).decode()
    assert text == pdf_text("第二頁：一季營收 12%")


def test_a_page_that_fails_to_extract_is_marked_and_the_other_pages_are_still_read(monkeypatch):
    from pypdf import PageObject
    from shared_context.native_user_uploads import readable
    extract = PageObject.extract_text

    def flaky(self, *args, **kwargs):
        text = extract(self, *args, **kwargs)
        if text == "Two":
            raise KeyError("/Widths")
        return text

    monkeypatch.setattr(PageObject, "extract_text", flaky)
    data = pdf("One", "Two", "Three")
    assert readable(upload("mixed.pdf", data, "application/pdf"), data).decode() == pdf_text(
        "One", "（第 2 頁無法抽取文字）", "Three")


def test_an_encrypted_pdf_opens_only_when_it_needs_no_password_to_view():
    from pypdf import PdfReader, PdfWriter
    from shared_context.native_user_uploads import NoText, readable

    def encrypted(user_password):
        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf("Secret page"))))
        writer.encrypt(user_password=user_password, owner_password="owner")
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()

    open_to_view = encrypted("")
    assert readable(upload("a.pdf", open_to_view, "application/pdf"), open_to_view).decode() == pdf_text("Secret page")
    locked = encrypted("user")
    with pytest.raises(NoText):
        readable(upload("b.pdf", locked, "application/pdf"), locked)


def office(parts):
    """A zip of the given parts, the container every docx, xlsx and pptx is."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in parts.items():
            archive.writestr(name, body)
    return out.getvalue()


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
MC = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
RELS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'
SML = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
PML = ('xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
       'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"')


def docx(body, head=""):
    return office({"word/document.xml": f"{head}<w:document {W} {MC}><w:body>{body}</w:body></w:document>"})


def rels(targets):
    return f"<Relationships {RELS}>" + "".join(
        f'<Relationship Id="{rid}" Type="x" Target="{target}"/>' for rid, target in targets.items()) + "</Relationships>"


def xlsx(sheets, strings=()):
    """sheets: (name, target, rows XML) in workbook order; a target is relative to xl/ unless it starts with /."""
    parts = {
        "xl/workbook.xml": f"<workbook {SML} {R}><sheets>" + "".join(
            f'<sheet name="{name}" sheetId="{n}" r:id="rId{n}"/>' for n, (name, _, _) in enumerate(sheets, 1))
        + "</sheets></workbook>",
        "xl/_rels/workbook.xml.rels": rels({f"rId{n}": target for n, (_, target, _) in enumerate(sheets, 1)}),
    }
    for _, target, rows in sheets:
        parts[target[1:] if target.startswith("/") else "xl/" + target] = (
            f"<worksheet {SML}><sheetData>{rows}</sheetData></worksheet>")
    if strings:
        parts["xl/sharedStrings.xml"] = f"<sst {SML}>" + "".join(f"<si>{s}</si>" for s in strings) + "</sst>"
    return office(parts)


def pptx(slides):
    """slides: (part file, paragraphs XML) in presentation order."""
    parts = {
        "ppt/presentation.xml": f"<p:presentation {PML} {R}><p:sldIdLst>" + "".join(
            f'<p:sldId id="{255 + n}" r:id="rId{n}"/>' for n in range(1, len(slides) + 1))
        + "</p:sldIdLst></p:presentation>",
        "ppt/_rels/presentation.xml.rels": rels({f"rId{n}": f"slides/{file}" for n, (file, _) in enumerate(slides, 1)}),
    }
    for file, body in slides:
        parts[f"ppt/slides/{file}"] = (f"<p:sld {PML}><p:cSld><p:spTree><p:sp><p:txBody>{body}</p:txBody></p:sp>"
                                       "</p:spTree></p:cSld></p:sld>")
    return office(parts)


def read_office(name, data, media_type):
    from shared_context.native_user_uploads import readable
    return readable(upload(name, data, media_type), data).decode()


def test_a_docx_is_read_as_its_paragraphs_with_tables_tabs_and_breaks():
    data = docx(
        # A tab stop in the paragraph's properties is layout, not text; a tab inside a run is text.
        '<w:p><w:pPr><w:tabs><w:tab w:val="left" w:pos="720"/></w:tabs></w:pPr>'
        "<w:r><w:t>名稱</w:t><w:tab/><w:t>數量</w:t></w:r></w:p>"
        # A tracked deletion is not the document's text.
        '<w:p><w:r><w:t xml:space="preserve">第一行 </w:t><w:br/><w:t>第二行</w:t></w:r>'
        "<w:del><w:r><w:delText>刪掉的</w:delText></w:r></w:del></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>格子甲</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>格子乙</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        # Word stores a text box twice (DrawingML and a VML fallback); it is read once.
        '<w:p><w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><w:txbxContent>'
        "<w:p><w:r><w:t>框內</w:t></w:r></w:p></w:txbxContent></w:drawing></mc:Choice>"
        "<mc:Fallback><w:pict><w:txbxContent><w:p><w:r><w:t>框內</w:t></w:r></w:p></w:txbxContent></w:pict>"
        "</mc:Fallback></mc:AlternateContent></w:r><w:r><w:t>框後</w:t></w:r></w:p>")
    assert read_office("a.docx", data, DOCX) == (
        "--- 文件內容 ---\n名稱\t數量\n第一行 \n第二行\n格子甲\n格子乙\n框內\n框後\n")


def test_an_xlsx_is_read_sheet_by_sheet_in_workbook_order_as_csv_rows():
    data = xlsx([
        # The workbook's order, not the part's file name, is the order a person sees.
        ("摘要", "/xl/worksheets/sheet2.xml",
         '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="inlineStr"><is><t>x, y</t></is></c></row>'
         '<row r="3"><c r="A3"><v>12.5</v></c><c r="B3" t="b"><v>1</v></c><c r="D3" t="s"><v>1</v></c></row>'),
        ("資料", "worksheets/sheet1.xml", '<row r="1"><c r="B1" t="s"><v>1</v></c></row><row r="2"/>'),
    ], strings=[
        # Rich text is its runs joined; a phonetic guide is not part of the value.
        '<r><t>Hel</t></r><r><t>lo</t></r><rPh sb="0" eb="1"><t>ハロー</t></rPh>', "<t>名稱</t>"])
    assert read_office("a.xlsx", data, XLSX) == (
        '--- 工作表 摘要 ---\nHello,,"x, y"\n12.5,1,,名稱\n--- 工作表 資料 ---\n,名稱\n')


def test_a_pptx_is_read_slide_by_slide_in_presentation_order():
    data = pptx([
        ("slide2.xml", "<a:p><a:r><a:t>先出現</a:t></a:r></a:p>"
                       "<a:p><a:r><a:t>上一行</a:t></a:r><a:br/><a:r><a:t>下一行</a:t></a:r></a:p>"),
        ("slide1.xml", "<a:p><a:r><a:t>後出現</a:t></a:r></a:p>"),
    ])
    assert read_office("a.pptx", data, PPTX) == (
        "--- 第 1 張投影片 ---\n先出現\n上一行\n下一行\n--- 第 2 張投影片 ---\n後出現\n")


def test_an_office_file_without_readable_text_is_refused_not_guessed():
    from shared_context.native_user_uploads import NoText, readable
    cases = [
        ("a.docx", b"not a zip", DOCX),
        # A password-protected Office file is an OLE2 container, not a zip.
        ("b.docx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 504, DOCX),
        ("c.docx", office({"word/other.xml": "<x/>"}), DOCX),
        ("d.docx", docx("<w:p/><w:p><w:r><w:t> </w:t></w:r></w:p>"), DOCX),
        ("e.xlsx", office({"xl/workbook.xml": "<workbook/>"}), XLSX),
        ("f.pptx", pptx([("slide1.xml", "<a:p/>")]), PPTX),
    ]
    for name, data, media_type in cases:
        with pytest.raises(NoText):
            readable(upload(name, data, media_type), data)


def test_office_xml_with_a_dtd_is_damage_so_no_entity_is_ever_expanded():
    from shared_context.native_user_uploads import NoText, readable
    external = docx("<w:p><w:r><w:t>&x;</w:t></w:r></w:p>",
                    head='<!DOCTYPE w:document [<!ENTITY x SYSTEM "file:///etc/hostname">]>')
    laughs = '<!ENTITY a "aaaaaaaaaa">' + "".join(
        f'<!ENTITY {chr(98 + n)} "{("&" + chr(97 + n) + ";") * 10}">' for n in range(9))
    blowup = docx("<w:p><w:r><w:t>&j;</w:t></w:r></w:p>", head=f"<!DOCTYPE w:document [{laughs}]>")
    # Expat stops only a large amplification; a modest one repeated many times would inflate past the budget.
    modest = docx("<w:p><w:r><w:t>" + "&x;" * 1000 + "</w:t></w:r></w:p>",
                  head='<!DOCTYPE w:document [<!ENTITY x "' + "x" * 60 + '">]>')
    utf16 = office({"word/document.xml": ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE w:document [<!ENTITY x "y">]>'
                                          f"<w:document {W}><w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body>"
                                          "</w:document>").encode("utf-16")})
    for data in (external, blowup, modest, utf16):
        with pytest.raises(NoText):
            readable(upload("x.docx", data, DOCX), data)


def test_office_xml_too_deep_or_with_an_oversized_index_part_is_damage(monkeypatch):
    from shared_context import native_user_uploads
    from shared_context.native_user_uploads import NoText, readable
    deep = docx("<w:sdt>" * 2000 + "<w:p><w:r><w:t>深</w:t></w:r></w:p>" + "</w:sdt>" * 2000)
    # A workbook, presentation or rels part is a few KB in a real file; it is parsed whole, so it is kept small.
    monkeypatch.setattr(native_user_uploads, "INDEX_PART_LIMIT", 2048)
    padded = office({
        "xl/workbook.xml": (f'<workbook {SML} {R}><sheets><sheet name="一" sheetId="1" r:id="rId1"/></sheets>'
                            + "<x/>" * 1000 + "</workbook>"),
        "xl/_rels/workbook.xml.rels": rels({"rId1": "worksheets/sheet1.xml"}),
        "xl/worksheets/sheet1.xml": f'<worksheet {SML}><sheetData><row r="1"><c r="A1"><v>1</v></c></row></sheetData></worksheet>',
    })
    for name, data, media_type in (("deep.docx", deep, DOCX), ("padded.xlsx", padded, XLSX)):
        with pytest.raises(NoText):
            readable(upload(name, data, media_type), data)


def test_parsed_office_xml_does_not_stay_in_memory(monkeypatch):
    import tracemalloc
    from shared_context import native_user_uploads
    monkeypatch.setattr(native_user_uploads, "TEXT_LIMIT", 4096)
    # Elements outside any string are dropped once read, and a row that repeats one long string stops at the limit.
    data = xlsx([("一", "worksheets/sheet1.xml", '<row r="1">' + '<c t="s"><v>0</v></c>' * 16000 + "</row>")],
                strings=["<t>" + "長" * 2000 + "</t></si>" + "<x/>" * 200_000 + "<si><t>尾</t>"])
    tracemalloc.start()
    try:
        text = read_office("wide.xlsx", data, XLSX)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert text.endswith("中途起沒有抽取：文字超過 10 MiB ---\n") and peak < 32 * 1024 * 1024


def test_a_string_colored_letter_by_letter_is_still_read():
    # Excel allows 32,767 characters in a cell, each its own formatted run of about eight elements.
    run = '<r><rPr><b/><i/><sz val="11"/><color rgb="FFFF0000"/><rFont val="Arial"/></rPr><t>字</t></r>'
    data = xlsx([("一", "worksheets/sheet1.xml", '<row r="1"><c r="A1" t="s"><v>0</v></c></row>')],
                strings=[run * 32_767])
    assert read_office("colored.xlsx", data, XLSX) == "--- 工作表 一 ---\n" + "字" * 32_767 + "\n"


def test_rows_under_the_text_limit_are_all_kept_without_a_marker(monkeypatch):
    from shared_context import native_user_uploads
    monkeypatch.setattr(native_user_uploads, "TEXT_LIMIT", 4096)
    data = xlsx([("一", "worksheets/sheet1.xml", "".join(
        f'<row r="{n}"><c r="A{n}" t="inlineStr"><is><t>abcdef</t></is></c></row>' for n in range(1, 401)))])
    # About 2.8 KB of rows: well under the limit, so every row is there and nothing claims a cut.
    assert read_office("rows.xlsx", data, XLSX) == "--- 工作表 一 ---\n" + "abcdef\n" * 400


def test_a_large_shared_string_table_leaves_half_the_budget_to_the_sheets(monkeypatch):
    from shared_context import native_user_uploads
    monkeypatch.setattr(native_user_uploads, "OFFICE_READ_LIMIT", 8192)
    data = xlsx([("一", "worksheets/sheet1.xml", '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>499</v></c>'
                                                 '<c r="C1"><v>7</v></c></row>')],
                strings=[f"<t>s{n}</t>" for n in range(500)])
    # The strings past half the budget are not read; a cell that refers to one is empty, and that is said.
    assert read_office("strings.xlsx", data, XLSX) == (
        "--- 共用字串 ---\n共用字串解壓後超過 32 MiB，之後的字串沒有抽取，引用它們的儲存格是空的。\n"
        "--- 工作表 一 ---\ns0,,7\n")


def test_a_damaged_sheet_is_marked_and_the_other_sheets_are_still_read():
    data = xlsx([
        ("壞", "worksheets/sheet1.xml", '<row r="1"><c r="A1" t="s"><v>7</v></c></row>'),
        ("寬", "worksheets/sheet2.xml", '<row r="1"><c r="ZZZZZZ1"><v>1</v></c></row>'),
        ("長", "worksheets/sheet3.xml", f'<row r="1"><c r="{"A" * 100_000}1"><v>1</v></c></row>'),
        ("無號", "worksheets/sheet4.xml", "<row>" + "<c><v>1</v></c>" * 16385 + "</row>"),
        ("好", "worksheets/sheet5.xml", '<row r="1"><c r="A1"><v>ok</v></c></row>'),
    ])
    # A shared-string index past the table and a column past Excel's last (XFD), named or counted, are damage.
    assert read_office("a.xlsx", data, XLSX) == "".join(
        f"--- 工作表 {name} ---\n（工作表 {name}無法抽取文字）\n" for name in ("壞", "寬", "長", "無號")
    ) + "--- 工作表 好 ---\nok\n"


def test_a_text_box_inside_a_paragraph_is_its_own_line():
    data = docx('<w:p><w:r><w:t>前</w:t></w:r><w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing>'
                "<w:txbxContent><w:p><w:r><w:t>框內</w:t></w:r></w:p></w:txbxContent></w:drawing></mc:Choice>"
                "</mc:AlternateContent></w:r><w:r><w:t>後</w:t></w:r></w:p>")
    assert read_office("box.docx", data, DOCX) == "--- 文件內容 ---\n框內\n前後\n"


def test_an_office_file_reads_at_most_its_budget_of_uncompressed_bytes(monkeypatch):
    import struct
    from shared_context import native_user_uploads
    from shared_context.native_user_uploads import NoText, readable
    monkeypatch.setattr(native_user_uploads, "OFFICE_READ_LIMIT", 4096)
    rows = "".join(f'<row r="{n}"><c r="A{n}"><v>{n}</v></c></row>' for n in range(1, 400))
    data = xlsx([("一", "worksheets/sheet1.xml", rows), ("二", "worksheets/sheet2.xml", rows)])
    text = read_office("big.xlsx", data, XLSX)
    # What fit is kept, the cut is named, and nothing after it is read.
    assert text.startswith("--- 工作表 一 ---\n1\n2\n3\n")
    assert text.endswith("\n--- 工作表 一中途起沒有抽取：解壓後超過 64 MiB ---\n") and "工作表 二" not in text
    # A zip that declares a smaller size than its member really inflates to stops at the declared size.
    lying = bytearray(docx("<w:p><w:r><w:t>" + "A" * 100_000 + "</w:t></w:r></w:p>"))
    struct.pack_into("<I", lying, lying.find(b"PK\x03\x04") + 22, 1000)
    struct.pack_into("<I", lying, lying.find(b"PK\x01\x02") + 24, 1000)
    with pytest.raises(NoText):
        readable(upload("lie.docx", bytes(lying), DOCX), bytes(lying))


def test_office_text_keeps_what_fits_its_text_limit(monkeypatch):
    from shared_context import native_user_uploads
    monkeypatch.setattr(native_user_uploads, "TEXT_LIMIT", 256)
    data = docx("".join(f"<w:p><w:r><w:t>第 {n} 段</w:t></w:r></w:p>" for n in range(1, 200)))
    text = read_office("long.docx", data, DOCX)
    assert text.startswith("--- 文件內容 ---\n第 1 段\n第 2 段\n")
    assert text.endswith("\n--- 文件內容中途起沒有抽取：文字超過 10 MiB ---\n") and len(text.encode()) <= 256


def test_the_same_bytes_named_as_another_type_are_read_as_that_type():
    from shared_context.native_user_uploads import NoText, readable
    data = docx("<w:p><w:r><w:t>Word</w:t></w:r></w:p>")
    assert read_office("a.docx", data, DOCX) == "--- 文件內容 ---\nWord\n"
    with pytest.raises(NoText):
        readable(upload("a.pdf", data, "application/pdf"), data)
