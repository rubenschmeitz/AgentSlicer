from __future__ import annotations

import os
import struct
import tempfile
from contextlib import contextmanager
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, Iterator


class ByteWriter:
    """Small binary serializer with patch/reserve support."""

    def __init__(self) -> None:
        self.data = bytearray()

    def pos(self) -> int:
        return len(self.data)

    def reserve(self, size: int) -> int:
        if size < 0:
            raise ValueError("reserve size cannot be negative")
        offset = len(self.data)
        self.data.extend(b"\0" * size)
        return offset

    def write_at(self, offset: int, payload: bytes) -> None:
        end = offset + len(payload)
        if offset < 0 or end > len(self.data):
            raise IndexError("patch is outside writer bounds")
        self.data[offset:end] = payload

    def patch_with(self, offset: int, callback: Callable[["ByteWriter"], None]) -> None:
        patch = ByteWriter()
        callback(patch)
        self.write_at(offset, bytes(patch.data))

    def view_mut(self, offset: int, size: int) -> memoryview:
        return memoryview(self.data)[offset : offset + size]

    def write_bytes(self, payload: bytes | bytearray | memoryview) -> None:
        self.data.extend(payload)

    def write_u8(self, value: int) -> None:
        self.data.append(value & 0xFF)

    def write_bool(self, value: bool) -> None:
        self.write_u8(1 if value else 0)

    def write_u16_be(self, value: int) -> None:
        self.data.extend(struct.pack(">H", value & 0xFFFF))

    def write_u16_le(self, value: int) -> None:
        self.data.extend(struct.pack("<H", value & 0xFFFF))

    def write_u32_be(self, value: int) -> None:
        self.data.extend(struct.pack(">I", value & 0xFFFFFFFF))

    def write_u32_le(self, value: int) -> None:
        self.data.extend(struct.pack("<I", value & 0xFFFFFFFF))

    def write_u64_le(self, value: int) -> None:
        self.data.extend(struct.pack("<Q", value & 0xFFFFFFFFFFFFFFFF))

    def write_f32_be(self, value: float) -> None:
        self.data.extend(struct.pack(">f", float(value)))

    def write_f32_le(self, value: float) -> None:
        self.data.extend(struct.pack("<f", float(value)))


class FileWriter:
    """Seekable binary serializer that does not retain the output in RAM."""

    def __init__(self, handle: BinaryIO) -> None:
        self._handle = handle
        self._handle.seek(0, os.SEEK_END)
        self._size = self._handle.tell()

    def pos(self) -> int:
        return self._handle.tell()

    def reserve(self, size: int) -> int:
        if size < 0:
            raise ValueError("reserve size cannot be negative")
        offset = self.pos()
        remaining = size
        zeroes = b"\0" * min(1024 * 1024, max(1, size))
        while remaining:
            chunk = min(remaining, len(zeroes))
            self.write_bytes(zeroes[:chunk])
            remaining -= chunk
        return offset

    def write_at(self, offset: int, payload: bytes) -> None:
        end = offset + len(payload)
        if offset < 0 or end > self._size:
            raise IndexError("patch is outside writer bounds")
        cursor = self.pos()
        self._handle.seek(offset)
        self._handle.write(payload)
        self._handle.seek(cursor)

    def patch_with(self, offset: int, callback: Callable[[ByteWriter], None]) -> None:
        patch = ByteWriter()
        callback(patch)
        self.write_at(offset, bytes(patch.data))

    def write_bytes(self, payload: bytes | bytearray | memoryview) -> None:
        self._handle.write(payload)
        self._size = max(self._size, self.pos())

    def write_u8(self, value: int) -> None:
        self.write_bytes(bytes((value & 0xFF,)))

    def write_bool(self, value: bool) -> None:
        self.write_u8(1 if value else 0)

    def write_u16_be(self, value: int) -> None:
        self.write_bytes(struct.pack(">H", value & 0xFFFF))

    def write_u16_le(self, value: int) -> None:
        self.write_bytes(struct.pack("<H", value & 0xFFFF))

    def write_u32_be(self, value: int) -> None:
        self.write_bytes(struct.pack(">I", value & 0xFFFFFFFF))

    def write_u32_le(self, value: int) -> None:
        self.write_bytes(struct.pack("<I", value & 0xFFFFFFFF))

    def write_u64_le(self, value: int) -> None:
        self.write_bytes(struct.pack("<Q", value & 0xFFFFFFFFFFFFFFFF))

    def write_f32_be(self, value: float) -> None:
        self.write_bytes(struct.pack(">f", float(value)))

    def write_f32_le(self, value: float) -> None:
        self.write_bytes(struct.pack("<f", float(value)))


@contextmanager
def atomic_binary_writer(path: Path) -> Iterator[FileWriter]:
    """Publish a complete binary file atomically, preserving any prior file.

    The temporary file lives beside the destination so the final replace is on
    the same filesystem. A failed render or serialization therefore cannot
    leave a truncated printer file at the requested path.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w+b",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        yield FileWriter(handle)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, path)
    except BaseException:
        handle.close()
        temporary.unlink(missing_ok=True)
        raise


def sized_ascii(text: str, size: int) -> bytes:
    raw = text.encode("ascii", errors="replace")[:size]
    return raw + b"\0" * (size - len(raw))


def align(value: int, boundary: int) -> int:
    return ((value + boundary - 1) // boundary) * boundary
