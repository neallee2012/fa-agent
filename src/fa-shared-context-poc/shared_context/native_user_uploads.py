"""Hosted side of files up to 10 MiB a human attaches to a native turn (ADR-0013).

The API wrote the exact bytes through the official Session Files API into this session's home, at a path made
only of their sha256. A new turn is accepted only when every referenced file is there with its length and digest;
the model then reads it in bounded parts, so a large file never enters the prompt or the saved state at once.
"""

import asyncio
import csv
import hashlib
import io
import itertools
import logging
import os
import posixpath
import threading
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from collections import OrderedDict
from pathlib import Path

from .behavior_assistant_wire import InstructionSubmit
from .behavior_user_uploads import (
    DOCX, MAX_UPLOAD_BYTES, PPTX, TEXT_MEDIA_TYPES, NativeUserUploadPart, UserUpload, upload_path)
from .errors import Problem

_LOG = logging.getLogger(__name__)
PART_BYTES = 32 * 1024
# One part's JSON result (every byte escaped as \uXXXX is 6 bytes, plus the envelope), and room left for the reply.
PART_RESERVE_BYTES = 512 * 1024
TEXT_LIMIT = MAX_UPLOAD_BYTES
# ADR-0013 §5: the text of the last four files extracted in this process, by the digest their checked bytes have;
# never on disk, where a shell could change it without a digest to catch it.
_TEXTS = OrderedDict()
_TEXTS_LOCK = threading.Lock()
# ADR-0013 §6: what one Office file may inflate to across the parts read; a cut is named where it falls.
OFFICE_READ_LIMIT = 64 * 1024 * 1024
# A workbook, presentation or rels part is parsed whole, so it is held to what a real one (a few KB) never reaches.
INDEX_PART_LIMIT = 4 * 1024 * 1024
# Real Office XML nests a few dozen deep; a string or a cell holds at most 32,767 formatted runs of about eight
# elements each. More is damage.
_XML_DEPTH, _KEPT_ELEMENTS = 1000, 300_000
# OOXML has no DTD, so refusing one means no entity is ever expanded, in every encoding expat reads.
_DOCTYPES = tuple("<!DOCTYPE".encode(encoding) for encoding in ("utf-8", "utf-16-le", "utf-16-be"))
_RELATIONSHIP_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


# Browser- and Word-printed PDFs often map CJK glyphs to the look-alike radicals (CJK Radicals Supplement and Kangxi
# Radicals); their compatibility forms are the ideographs a reader searches for.
_RADICALS = {code: unicodedata.normalize("NFKC", chr(code)) for code in range(0x2E80, 0x2FE0)
             if unicodedata.normalize("NFKC", chr(code)) != chr(code)}


class NoText(Exception):
    """A PDF with no text this reader can extract: scanned, encrypted or damaged."""


def turn_uploads(command) -> tuple[UserUpload, ...]:
    return command.uploads if isinstance(command, InstructionSubmit) else ()


def _set_aside(path: Path) -> None:
    """A partial or changed file leaves the digest path, so the next upload of the same bytes can write it."""
    bad = path.parent / "bad"
    bad.mkdir(exist_ok=True)
    n = 1
    while (bad / f"{path.name}.{n}").exists():
        n += 1
    path.rename(bad / f"{path.name}.{n}")


def load_upload(upload: UserUpload) -> bytes:
    """The exact uploaded bytes; blocking, so callers run it in a thread."""
    path = Path(os.environ["HOME"]) / upload_path(upload.sha256)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise Problem(409, "NATIVE_FILE_MISSING", "an attached file was not uploaded to this Agent") from None
    if len(data) != upload.byte_length or hashlib.sha256(data).hexdigest() != upload.sha256:
        _set_aside(path)
        raise Problem(409, "NATIVE_FILE_MISSING", "an attached file differs from its original reference; upload it again")
    return data


def _capped(sections) -> bytes:
    """Each (label, text) after a `--- label ---` line, stopped before TEXT_LIMIT bytes with a marker saying where.

    A text of None is a section that failed to extract; NoText when no section has any text."""
    out, size, found = [], 0, False
    for label, text in sections:
        found = found or bool(text and text.strip())
        # A damaged font map can yield a lone surrogate; it becomes "?" instead of hiding every other section.
        chunk = f"--- {label} ---\n{f'（{label}無法抽取文字）' if text is None else text}\n".encode("utf-8", "replace")
        room = TEXT_LIMIT - 128 - size
        if len(chunk) > room:
            out.append(chunk[:room].decode("utf-8", "ignore").encode())
            out.append(f"\n--- {label}中途起沒有抽取：文字超過 10 MiB ---\n".encode())
            break
        out.append(chunk)
        size += len(chunk)
    if not found:
        raise NoText
    return b"".join(out)


def _pdf_text(data: bytes) -> bytes:
    """Each page's text layer after a page marker."""
    from pypdf import PdfReader
    failed = []
    # Untrusted bytes: a parser failure means no text, never a crash of the turn; only the error's type is logged.
    try:
        # pypdf itself opens a PDF encrypted with an empty password; one that needs a password has no readable text.
        pages = PdfReader(io.BytesIO(data)).pages
        count = len(pages)
    except Exception as error:
        _LOG.warning("PDF_TEXT_UNREADABLE %s", type(error).__name__)
        raise NoText from None

    def sections():
        for number in range(1, count + 1):
            try:
                text = (pages[number - 1].extract_text() or "").translate(_RADICALS)
            except Exception as error:  # one bad page does not hide the others
                failed.append(type(error).__name__)
                text = None
            yield f"第 {number} 頁", text

    try:
        return _capped(sections())
    finally:
        if failed:
            _LOG.warning("PDF_PAGES_UNREADABLE %d %s", len(failed), failed[0])


class _Office:
    """An Office file's zip, whose XML parts are read within one budget of uncompressed bytes (ADR-0013 §6)."""

    def __init__(self, data: bytes):
        self.zip = zipfile.ZipFile(io.BytesIO(data))
        self.left, self.cut = OFFICE_READ_LIMIT, False

    def read(self, name: str, limit: int | None = None) -> bytes:
        """A part within what is left of the budget; past `limit` it is damage instead of a cut."""
        cap = self.left if limit is None else min(self.left, limit)
        # zipfile inflates a member only up to its declared size, so a lying header cannot exceed this read.
        with self.zip.open(name) as member:
            data = member.read(cap + 1)
        if len(data) > cap:
            if cap == limit:
                raise ValueError("index part size")
            data, self.cut = data[:cap], True
        self.left -= len(data)
        if any(mark in data for mark in _DOCTYPES):
            raise ValueError("DTD")
        return data

    def events(self, name: str, keep: str | None = None):
        """(event, element) while one part is parsed; a part cut by the budget ends where it was cut.

        An element is dropped from the tree once read, so a huge part keeps a small tree; a `keep` element (a
        string, a cell) keeps its children until it ends."""
        stack, kept_at, kept = [], None, 0
        try:
            for event, element in ET.iterparse(io.BytesIO(self.read(name)), events=("start", "end")):
                if event == "start":
                    if len(stack) >= _XML_DEPTH:
                        raise ValueError("XML depth")
                    if kept_at is None and _tag(element) == keep:
                        kept_at, kept = len(stack), 0
                    if kept_at is not None:
                        kept += 1
                        if kept > _KEPT_ELEMENTS:
                            raise ValueError("XML elements")
                    stack.append(element)
                    yield event, element
                    continue
                stack.pop()
                yield event, element
                if kept_at is None or len(stack) == kept_at:
                    kept_at = None
                    if stack:
                        stack[-1].remove(element)
        except ET.ParseError:
            if not self.cut:
                raise

    def targets(self, part: str, path: str) -> list:
        """(element, part name) of each sheet or slide `part` lists at `path`, in the order it lists them."""
        folder, _, file = part.rpartition("/")
        rels = self.read(f"{folder}/_rels/{file}.rels", INDEX_PART_LIMIT)
        links = {link.get("Id"): link.get("Target") or "" for link in ET.fromstring(rels).iterfind("{*}Relationship")}
        found = []
        for element in ET.fromstring(self.read(part, INDEX_PART_LIMIT)).iterfind(path):
            target = links.get(element.get(_RELATIONSHIP_ID), "")
            found.append((element, target[1:] if target.startswith("/") else posixpath.normpath(f"{folder}/{target}")))
        return found


def _tag(element) -> str:
    return element.tag.rpartition("}")[2]


def _lines(events) -> str:
    """Each paragraph (docx w:p, pptx a:p) on its own line, with a run's tabs and the breaks inside it."""
    lines, line, outer, run, fallback, size = [], [], [], 0, 0, 0
    for event, element in events:
        tag = _tag(element)
        if event == "start":
            run += tag == "r"
            fallback += tag == "Fallback"
            if tag == "p" and not fallback:  # a text box's paragraph inside this one is a line of its own
                outer.append(line)
                line = []
            continue
        if tag == "r":
            run -= 1
        elif tag == "Fallback":  # the same text box again, for readers without DrawingML
            fallback -= 1
        elif fallback:
            pass
        elif tag == "t":
            line.append(element.text or "")
        elif tag == "tab" and run:  # outside a run, a tab is a tab stop
            line.append("\t")
        elif tag in ("br", "cr"):
            line.append("\n")
        elif tag == "p":
            lines.append("".join(line))
            line = outer.pop() if outer else []
            size += len(lines[-1]) + 1
        if size > TEXT_LIMIT:
            break
    return "\n".join(lines)


def _string(element) -> str:
    """A shared or inline string: its text or its rich-text runs, without a phonetic guide."""
    return "".join(part.text or "" for part in element.findall("{*}t") + element.findall("{*}r/{*}t"))


def _csv(cells: dict) -> str:
    out = io.StringIO()
    csv.writer(out, lineterminator="").writerow([cells.get(n, "") for n in range(max(cells) + 1)])
    return out.getvalue()


def _rows(events, strings, partial) -> str:
    """Each row as one CSV line, every cell at its column so a sparse row keeps its empty cells.

    A cell may refer past `strings` only when the string table was cut (`partial`); it is then empty."""
    lines, cells, column, size, held = [], {}, 0, 0, 0
    for event, element in events:
        tag = _tag(element)
        if event == "start":
            continue
        if tag == "c":
            kind, value = element.get("t"), element.findtext("{*}v")
            if kind == "inlineStr":
                value = _string(element.find("{*}is"))
            elif kind == "s":
                index = int(value)
                if not 0 <= index < len(strings) and not (partial and index >= 0):
                    raise IndexError("shared string index")
                value = strings[index] if index < len(strings) else ""
            column = _column(element.get("r")) if element.get("r") else column
            if column >= 16384:  # past Excel's last column (XFD)
                raise ValueError("cell column")
            cells[column] = value or ""
            column += 1
            # Held per cell until the row is written: one long shared string can fill every cell of a row.
            held += len(cells[column - 1]) + 1
        elif tag == "row":
            if cells:
                lines.append(_csv(cells))
                size += len(lines[-1]) + 1  # a sparse row's commas count too
            cells, column, held = {}, 0, 0
        if size + held > TEXT_LIMIT:
            break
    if cells:  # a row the limit or a cut stopped in
        lines.append(_csv(cells))
    # shortcut: values are as stored (a date is its serial number, no number format); apply styles if people ask.
    return "\n".join(lines)


def _column(reference: str) -> int:
    """The zero-based column of a cell reference such as "AB12"; past Excel's last column (XFD) is damage."""
    letters = reference.rstrip("0123456789").upper()
    if not 1 <= len(letters) <= 3:
        raise ValueError("cell column")
    number = 0
    for letter in letters:
        number = number * 26 + ord(letter) - 64
    if not 1 <= number <= 16384:
        raise ValueError("cell column")
    return number - 1


def _office_text(media_type: str, data: bytes) -> bytes:
    """docx as its paragraphs, xlsx sheet by sheet as CSV rows, pptx slide by slide (ADR-0013 §5)."""
    failed = []

    def sections(office, parts, extract, keep=None):
        for label, name in parts:
            try:
                text = extract(office.events(name, keep))
            except Exception as error:  # one damaged sheet or slide does not hide the others
                failed.append(type(error).__name__)
                text = None
            if office.cut:
                yield label, f"{text or ''}\n--- {label}中途起沒有抽取：解壓後超過 64 MiB ---"
                return
            yield label, text

    # Untrusted bytes: a damaged or password-protected file (an OLE2 container, not a zip) has no text.
    try:
        office = _Office(data)
        if media_type == DOCX:
            return _capped(sections(office, [("文件內容", "word/document.xml")], _lines))
        if media_type == PPTX:
            slides = [(f"第 {n} 張投影片", name) for n, (_, name) in
                      enumerate(office.targets("ppt/presentation.xml", "{*}sldIdLst/{*}sldId"), start=1)]
            return _capped(sections(office, slides, _lines))
        sheets = [(f"工作表 {element.get('name')}", name) for element, name in
                  office.targets("xl/workbook.xml", "{*}sheets/{*}sheet")]
        strings, partial, note = [], False, []
        if "xl/sharedStrings.xml" in office.zip.namelist():
            # At most half the budget, so a string table larger than that still leaves the sheets some to read.
            rest = office.left - office.left // 2
            office.left -= rest
            for event, element in office.events("xl/sharedStrings.xml", "si"):
                if event == "end" and _tag(element) == "si":
                    strings.append(_string(element))
            partial, office.cut = office.cut, False
            office.left += rest
        if partial:
            note = [("共用字串", "共用字串解壓後超過 32 MiB，之後的字串沒有抽取，引用它們的儲存格是空的。")]
        return _capped(itertools.chain(
            note, sections(office, sheets, lambda events: _rows(events, strings, partial), "c")))
    except NoText:
        raise
    except Exception as error:
        _LOG.warning("OFFICE_TEXT_UNREADABLE %s", type(error).__name__)
        raise NoText from None
    finally:
        if failed:
            _LOG.warning("OFFICE_PARTS_UNREADABLE %d %s", len(failed), failed[0])


def readable(upload: UserUpload, data: bytes) -> bytes:
    """What the model reads from checked bytes: a text file as is, a PDF or Office file as its text; blocking."""
    if upload.media_type in TEXT_MEDIA_TYPES:
        return data
    key = upload.sha256, upload.media_type
    with _TEXTS_LOCK:
        text = _TEXTS.pop(key, None)
    if text is None:
        text = _pdf_text(data) if upload.media_type == "application/pdf" else _office_text(upload.media_type, data)
    with _TEXTS_LOCK:
        _TEXTS[key] = text
        while len(_TEXTS) > 4:
            _TEXTS.popitem(last=False)
    return text


async def check_user_uploads(command) -> None:
    """Before a new RID is accepted: every referenced file is in this session's home, whole and unchanged."""
    for upload in turn_uploads(command):
        await asyncio.to_thread(load_upload, upload)


def read_part(upload: UserUpload, data: bytes, offset: int) -> NativeUserUploadPart:
    """At most PART_BYTES from offset, both ends moved back to a UTF-8 character boundary."""
    if not 0 <= offset < len(data):
        raise ValueError("offset is outside the file")

    def boundary(index):
        # A UTF-8 character has at most 3 continuation bytes; without a character start that close, the bytes are
        # not UTF-8 and the index stays, so the next part starts exactly where this one ended.
        for start in range(index, max(index - 4, -1), -1):
            if start >= len(data) or data[start] & 0xC0 != 0x80:
                return start
        return index

    start = boundary(offset)
    end = len(data) if start + PART_BYTES >= len(data) else boundary(start + PART_BYTES)
    # shortcut: non-UTF-8 text (e.g. a Big5 CSV) reads with replacement characters; decode by charset when asked.
    return NativeUserUploadPart(
        name=upload.name, sha256=upload.sha256, byte_length=len(data), offset=start,
        next_offset=None if end == len(data) else end, text=data[start:end].decode("utf-8", errors="replace"))
