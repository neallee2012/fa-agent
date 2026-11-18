"""從 test_assistant_images.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

import struct
import zlib


def png(color=0):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + bytes([color, 0, 0]) * 2 for _ in range(2))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _read_bytes(monkeypatch, call):
    from shared_context import behavior_store

    total = [0]
    original = behavior_store.BehaviorStore._read

    def counted(self, key, model, fetched=behavior_store._NOT_FETCHED):
        view = self._read_view()
        fresh = view is not None and not view.verifying and key not in view.values
        value = original(self, key, model, fetched)
        if fresh:
            total[0] += len(key.encode()) + (0 if value is None else len(behavior_store._json(value[0])))
        return value

    with monkeypatch.context() as patch:
        patch.setattr(behavior_store.BehaviorStore, "_read", counted)
        response = call()
    return response, total[0]
