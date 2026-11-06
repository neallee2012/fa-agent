"""pdfium on untrusted PDF bytes, alone in its own process under an address-space cap (ADR-0013 §5).

Run as `python -I -B native_pdf_render.py CAP_BYTES count` or `... CAP_BYTES render INDEX PNG_BYTES`, the PDF on
stdin. `count` prints the page count; `render` writes the page as the first PNG within PNG_BYTES, or nothing when
even the smallest render is larger. Any failure, the cap included, is a non-zero exit, except a soft mask too large
for the cap, which pdfium leaves out of the page. Imports nothing from this package: `-I` keeps the package off
sys.path and the child needs only pypdfium2 and the standard library.
"""

import struct
import sys
import zlib

RENDERS = ((1600, False), (1600, True), (1000, True), (768, True))  # (long side px, grayscale), in order
_GRAY_LEVELS = bytes(value // 16 * 17 for value in range(256))


def _scanlines(bitmap) -> tuple[int, int, int, bytes]:
    """An 8-bit grayscale or RGB pdfium bitmap's (width, height, channels, PNG scanlines) without stride padding."""
    channels = bitmap.n_channels
    assert channels in (1, 3), channels
    row = bitmap.width * channels
    buffer = memoryview(bitmap.buffer).cast("B")
    raw = b"".join(b"\0" + buffer[y * bitmap.stride:y * bitmap.stride + row] for y in range(bitmap.height))
    if channels == 1:  # 16 gray levels: scan noise no longer defeats unfiltered PNG compression, text stays legible
        raw = raw.translate(_GRAY_LEVELS)
    return bitmap.width, bitmap.height, channels, raw


def _png(width: int, height: int, channels: int, raw: bytes) -> bytes:
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", width, height, 8, 0 if channels == 1 else 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def main(cap: str, mode: str, *args: str) -> None:
    try:
        import resource
    except ImportError:  # Windows has no rlimit: the cap holds only on Linux, where Hosted runs
        pass
    else:  # set by the child itself: preexec_fn is unsafe in the threaded parent
        resource.setrlimit(resource.RLIMIT_AS, (int(cap), int(cap)))
    import pypdfium2
    document = pypdfium2.PdfDocument(sys.stdin.buffer.read())
    if mode == "count":
        sys.stdout.write(str(len(document)))
        return
    index, limit = map(int, args)
    page = document[index]
    long_side = max(page.get_size())
    for side, grayscale in RENDERS:
        # A gray bitmap, not pdfium's grayscale mode: that mode decodes each image whole (an A1 600 dpi color scan,
        # 908 MiB, came back blank under the cap); drawing into a gray bitmap reads images line by line, as color does.
        bitmap = page.render(scale=side / long_side, rev_byteorder=True,
                             force_bitmap_format=pypdfium2.raw.FPDFBitmap_Gray if grayscale else None)
        png = _png(*_scanlines(bitmap))
        if len(png) <= limit:
            sys.stdout.buffer.write(png)
            return


if __name__ == "__main__":
    main(*sys.argv[1:])
