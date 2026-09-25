from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator

from ..errors import FormatError
from ..raster import LayerRaster, Run
from .aes import aes256_cbc_decrypt
from .ctb import (
    DEFAULT_XOR_KEY,
    ENCRYPT_IV,
    ENCRYPT_KEY,
    FORMAT_VERSION as CTB_VERSION,
    PAGE_SIZE,
    _xor_layer,
)
from .goo import DELIMITER, ENDING_STRING, HEADER_SIZE as GOO_HEADER_SIZE, MAGIC_TAG

_MAX_SECTION_BYTES = 16 * 1024 * 1024
_MAX_LAYER_COUNT = 1_000_000
_MAX_PIXELS_PER_LAYER = 100_000_000


@dataclass(frozen=True)
class NativeSliceInfo:
    format: str
    version: str
    resolution_x: int
    resolution_y: int
    layer_count: int
    layer_height_mm: float
    bottom_layers: int
    transition_layers: int
    file_size: int
    integrity_checks: tuple[str, ...]


@dataclass(frozen=True)
class DecodedLayer:
    index: int
    z_mm: float
    exposure_s: float
    encoded_bytes: int
    raster: LayerRaster


@dataclass(frozen=True)
class _CtbLayerRef:
    record_offset: int


@dataclass(frozen=True)
class NativeSlice:
    path: Path
    info: NativeSliceInfo
    _ctb_refs: tuple[_CtbLayerRef, ...] = ()
    _ctb_xor_seed: int = DEFAULT_XOR_KEY

    def iter_layers(self) -> Iterator[DecodedLayer]:
        if self.info.format == "ctb":
            yield from _iter_ctb_layers(self)
        elif self.info.format == "goo":
            yield from _iter_goo_layers(self)
        else:  # pragma: no cover - instances are created only by inspect_sliced_file
            raise FormatError(f"unsupported native reader format: {self.info.format}")

    def read_layer(self, index: int) -> DecodedLayer:
        """Decode one layer without materializing or retaining the full stack."""

        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("layer index must be an integer")
        if index < 0 or index >= self.info.layer_count:
            raise IndexError(
                f"layer index {index} is outside 0..{self.info.layer_count - 1}"
            )
        if self.info.format == "ctb":
            return _read_ctb_layer(self, index)
        for layer in self.iter_layers():
            if layer.index == index:
                return layer
        raise FormatError(f"sliced file ended before layer {index}")


def inspect_sliced_file(path: str | Path, fmt: str | None = None) -> NativeSlice:
    """Parse format metadata and return a streaming, bounded layer decoder."""

    source = Path(path)
    requested = (fmt or source.suffix).lower().lstrip(".")
    if requested == "ctb":
        return _inspect_ctb(source)
    if requested == "goo":
        return _inspect_goo(source)
    raise FormatError("native reader supports only CTB v5 and GOO v3")


def _inspect_ctb(path: Path) -> NativeSlice:
    file_size = _file_size(path)
    with path.open("rb") as handle:
        header = _read_exact_at(handle, 0, 48, file_size, "CTB header")
        magic, settings_size, settings_offset = struct.unpack_from("<III", header, 0)
        version = struct.unpack_from("<I", header, 16)[0]
        signature_size, signature_offset = struct.unpack_from("<II", header, 20)
        if magic != 0x12FD0107:
            raise FormatError("invalid CTB magic")
        if version != CTB_VERSION:
            raise FormatError(f"native reader supports CTB v{CTB_VERSION}, found v{version}")
        if settings_size < 236 or settings_size > _MAX_SECTION_BYTES or settings_size % 16:
            raise FormatError("invalid CTB encrypted settings size")
        encrypted = _read_exact_at(
            handle, settings_offset, settings_size, file_size, "CTB settings"
        )
        settings = aes256_cbc_decrypt(encrypted, ENCRYPT_KEY, ENCRYPT_IV)
        layer_table_offset = struct.unpack_from("<I", settings, 8)[0]
        layer_height_mm = struct.unpack_from("<f", settings, 36)[0]
        bottom_layers = struct.unpack_from("<I", settings, 52)[0]
        resolution_x, resolution_y, layer_count = struct.unpack_from("<III", settings, 56)
        xor_seed = struct.unpack_from("<I", settings, 128)[0]
        transition_layers = struct.unpack_from("<I", settings, 188)[0]
        _validate_dimensions(resolution_x, resolution_y, layer_count, layer_height_mm)
        _validate_layer_groups(bottom_layers, transition_layers, layer_count)
        refs_payload = _read_exact_at(
            handle,
            layer_table_offset,
            layer_count * 16,
            file_size,
            "CTB layer reference table",
        )
        refs: list[_CtbLayerRef] = []
        previous = -1
        for index in range(layer_count):
            offset, page, record_size, reserved = struct.unpack_from(
                "<IIII", refs_payload, index * 16
            )
            absolute = page * PAGE_SIZE + offset
            if record_size != 0x58 or reserved != 0:
                raise FormatError(f"invalid CTB layer reference {index}")
            if absolute <= previous:
                raise FormatError("CTB layer references are not strictly increasing")
            _check_range(absolute, 0x58, file_size, f"CTB layer {index} record")
            refs.append(_CtbLayerRef(absolute))
            previous = absolute

        if signature_size != 32:
            raise FormatError("invalid CTB signature size")
        signature = _read_exact_at(
            handle, signature_offset, signature_size, file_size, "CTB signature"
        )
        marker = _read_exact_at(handle, signature_offset - 8, 8, file_size, "CTB signature marker")
        if marker != struct.pack("<II", 0x422052FA, 0):
            raise FormatError("invalid CTB signature marker")
        if _read_exact_at(handle, file_size - 4, 4, file_size, "CTB trailer") != struct.pack(
            "<I", 0x6D4232B3
        ):
            raise FormatError("invalid CTB trailer")
        checksum = struct.unpack_from("<Q", settings, 0)[0]
        expected_signature = hashlib.sha256(checksum.to_bytes(8, "little")).digest()
        if aes256_cbc_decrypt(signature, ENCRYPT_KEY, ENCRYPT_IV) != expected_signature:
            raise FormatError("CTB encrypted signature does not match the settings checksum")

    return NativeSlice(
        path=path,
        info=NativeSliceInfo(
            format="ctb",
            version=str(version),
            resolution_x=resolution_x,
            resolution_y=resolution_y,
            layer_count=layer_count,
            layer_height_mm=layer_height_mm,
            bottom_layers=bottom_layers,
            transition_layers=transition_layers,
            file_size=file_size,
            integrity_checks=(
                "bounded-sections",
                "layer-reference-order",
                "encrypted-signature",
                "trailer",
            ),
        ),
        _ctb_refs=tuple(refs),
        _ctb_xor_seed=xor_seed,
    )


def _iter_ctb_layers(source: NativeSlice) -> Iterator[DecodedLayer]:
    info = source.info
    pixel_count = info.resolution_x * info.resolution_y
    previous_z = -math.inf
    with source.path.open("rb") as handle:
        for index, reference in enumerate(source._ctb_refs):
            record = _read_exact_at(
                handle, reference.record_offset, 0x58, info.file_size, f"CTB layer {index} record"
            )
            record_size = struct.unpack_from("<I", record, 0)[0]
            z_mm, exposure_s = struct.unpack_from("<ff", record, 4)
            data_offset, data_page, data_size, checksum = struct.unpack_from("<IIII", record, 16)
            data_position = data_page * PAGE_SIZE + data_offset
            if record_size != 0x58:
                raise FormatError(f"invalid CTB layer {index} record size")
            if checksum != 0:
                raise FormatError(f"unsupported nonzero CTB layer checksum at layer {index}")
            if data_position != reference.record_offset + 0x58:
                raise FormatError(f"CTB layer {index} payload is not contiguous with its record")
            if data_size > pixel_count * 2 + 16:
                raise FormatError(f"CTB layer {index} payload exceeds the bounded RLE limit")
            _validate_layer_values(index, z_mm, exposure_s, previous_z)
            previous_z = z_mm
            encrypted = bytearray(
                _read_exact_at(
                    handle, data_position, data_size, info.file_size, f"CTB layer {index} payload"
                )
            )
            _xor_layer(encrypted, source._ctb_xor_seed, index)
            runs = _decode_ctb_runs(bytes(encrypted), pixel_count, index)
            yield DecodedLayer(
                index=index,
                z_mm=z_mm,
                exposure_s=exposure_s,
                encoded_bytes=data_size,
                raster=_raster_from_runs(info.resolution_x, info.resolution_y, runs),
            )


def _read_ctb_layer(source: NativeSlice, index: int) -> DecodedLayer:
    info = source.info
    reference = source._ctb_refs[index]
    pixel_count = info.resolution_x * info.resolution_y
    with source.path.open("rb") as handle:
        record = _read_exact_at(
            handle,
            reference.record_offset,
            0x58,
            info.file_size,
            f"CTB layer {index} record",
        )
        record_size = struct.unpack_from("<I", record, 0)[0]
        z_mm, exposure_s = struct.unpack_from("<ff", record, 4)
        data_offset, data_page, data_size, checksum = struct.unpack_from(
            "<IIII", record, 16
        )
        data_position = data_page * PAGE_SIZE + data_offset
        if record_size != 0x58:
            raise FormatError(f"invalid CTB layer {index} record size")
        if checksum != 0:
            raise FormatError(
                f"unsupported nonzero CTB layer checksum at layer {index}"
            )
        if data_position != reference.record_offset + 0x58:
            raise FormatError(
                f"CTB layer {index} payload is not contiguous with its record"
            )
        if data_size > pixel_count * 2 + 16:
            raise FormatError(
                f"CTB layer {index} payload exceeds the bounded RLE limit"
            )
        _validate_layer_values(index, z_mm, exposure_s, -math.inf)
        encrypted = bytearray(
            _read_exact_at(
                handle,
                data_position,
                data_size,
                info.file_size,
                f"CTB layer {index} payload",
            )
        )
    _xor_layer(encrypted, source._ctb_xor_seed, index)
    runs = _decode_ctb_runs(bytes(encrypted), pixel_count, index)
    return DecodedLayer(
        index=index,
        z_mm=z_mm,
        exposure_s=exposure_s,
        encoded_bytes=data_size,
        raster=_raster_from_runs(info.resolution_x, info.resolution_y, runs),
    )


def _inspect_goo(path: Path) -> NativeSlice:
    file_size = _file_size(path)
    if file_size < GOO_HEADER_SIZE + len(ENDING_STRING):
        raise FormatError("GOO file is shorter than its fixed header")
    with path.open("rb") as handle:
        prefix = _read_exact_at(handle, 0, 12, file_size, "GOO header")
        if prefix[:4] != b"V3.0" or prefix[4:] != MAGIC_TAG:
            raise FormatError("native reader supports only GOO V3.0")
        core_offset = GOO_HEADER_SIZE - 167
        core = _read_exact_at(handle, core_offset, 32, file_size, "GOO print settings")
        layer_count = struct.unpack_from(">I", core, 0)[0]
        resolution_x, resolution_y = struct.unpack_from(">HH", core, 4)
        layer_height_mm = struct.unpack_from(">f", core, 22)[0]
        bottom_layers = struct.unpack_from(">I", _read_exact_at(handle, core_offset + 63, 4, file_size, "GOO bottom layers"))[0]
        transition_layers = struct.unpack_from(
            ">H", _read_exact_at(handle, GOO_HEADER_SIZE - 2, 2, file_size, "GOO transition layers")
        )[0]
        declared_header_size = struct.unpack_from(
            ">I", _read_exact_at(handle, GOO_HEADER_SIZE - 7, 4, file_size, "GOO header size")
        )[0]
        if declared_header_size != GOO_HEADER_SIZE:
            raise FormatError("GOO declared header size does not match V3.0")
        _validate_dimensions(resolution_x, resolution_y, layer_count, layer_height_mm)
        _validate_layer_groups(bottom_layers, transition_layers, layer_count)

    return NativeSlice(
        path=path,
        info=NativeSliceInfo(
            format="goo",
            version="3.0",
            resolution_x=resolution_x,
            resolution_y=resolution_y,
            layer_count=layer_count,
            layer_height_mm=layer_height_mm,
            bottom_layers=bottom_layers,
            transition_layers=transition_layers,
            file_size=file_size,
            integrity_checks=("fixed-header", "per-layer-checksum", "record-delimiters", "trailer"),
        ),
    )


def _iter_goo_layers(source: NativeSlice) -> Iterator[DecodedLayer]:
    info = source.info
    pixel_count = info.resolution_x * info.resolution_y
    previous_z = -math.inf
    cursor = GOO_HEADER_SIZE
    with source.path.open("rb") as handle:
        for index in range(info.layer_count):
            fixed = _read_exact_at(handle, cursor, 70, info.file_size, f"GOO layer {index} record")
            if fixed[:2] != b"\0\0" or fixed[64:66] != DELIMITER:
                raise FormatError(f"invalid GOO layer {index} record delimiter")
            z_mm, exposure_s = struct.unpack_from(">ff", fixed, 6)
            record_data_size = struct.unpack_from(">I", fixed, 66)[0]
            if record_data_size < 2:
                raise FormatError(f"invalid GOO layer {index} payload size")
            payload_size = record_data_size - 2
            if payload_size > pixel_count * 2 + 16:
                raise FormatError(f"GOO layer {index} payload exceeds the bounded RLE limit")
            record_tail = _read_exact_at(
                handle,
                cursor + 70,
                record_data_size + 2,
                info.file_size,
                f"GOO layer {index} payload",
            )
            if record_tail[0] != 0x55 or record_tail[-2:] != DELIMITER:
                raise FormatError(f"invalid GOO layer {index} payload marker")
            payload = record_tail[1 : 1 + payload_size]
            checksum = record_tail[1 + payload_size]
            if checksum != _goo_checksum(payload):
                raise FormatError(f"GOO layer {index} checksum mismatch")
            _validate_layer_values(index, z_mm, exposure_s, previous_z)
            previous_z = z_mm
            runs = _decode_goo_runs(payload, pixel_count, index)
            yield DecodedLayer(
                index=index,
                z_mm=z_mm,
                exposure_s=exposure_s,
                encoded_bytes=payload_size,
                raster=_raster_from_runs(info.resolution_x, info.resolution_y, runs),
            )
            cursor += 70 + record_data_size + 2
        ending = _read_exact_at(handle, cursor, len(ENDING_STRING), info.file_size, "GOO trailer")
        if ending != ENDING_STRING or cursor + len(ENDING_STRING) != info.file_size:
            raise FormatError("invalid GOO trailer or unexpected trailing bytes")


def _decode_ctb_runs(data: bytes, expected_pixels: int, layer_index: int) -> Iterator[Run]:
    offset = 0
    total = 0
    try:
        while offset < len(data):
            value = data[offset]
            length = 1
            if value & 0x80:
                value &= 0x7F
                offset += 1
                marker = data[offset]
                if marker & 0x80 == 0:
                    length = marker
                elif marker & 0xC0 == 0x80:
                    length = ((marker & 0x3F) << 8) + data[offset + 1]
                    offset += 1
                elif marker & 0xE0 == 0xC0:
                    length = ((marker & 0x1F) << 16) + (data[offset + 1] << 8) + data[offset + 2]
                    offset += 2
                elif marker & 0xF0 == 0xE0:
                    length = ((marker & 0x0F) << 24) + (data[offset + 1] << 16) + (data[offset + 2] << 8) + data[offset + 3]
                    offset += 3
                else:
                    raise FormatError(f"invalid CTB RLE marker in layer {layer_index}")
            offset += 1
            if length <= 0 or total + length > expected_pixels:
                raise FormatError(f"invalid CTB RLE length in layer {layer_index}")
            if value:
                value = ((value << 1) | 1) & 0xFF
            yield Run(length, value)
            total += length
    except IndexError as error:
        raise FormatError(f"truncated CTB RLE payload in layer {layer_index}") from error
    if total != expected_pixels:
        raise FormatError(
            f"CTB layer {layer_index} decodes to {total} pixels; expected {expected_pixels}"
        )


def _decode_goo_runs(data: bytes, expected_pixels: int, layer_index: int) -> Iterator[Run]:
    color = 0
    offset = 0
    total = 0
    try:
        while offset < len(data):
            head = data[offset]
            length = 0
            chunk_type = head >> 6
            length_size = (head >> 4) & 0x03
            if chunk_type == 0:
                color = 0
            elif chunk_type == 1:
                offset += 1
                color = data[offset]
            elif chunk_type == 2:
                diff_type = (head >> 4) & 0x03
                if diff_type & 1:
                    offset += 1
                    length = data[offset]
                else:
                    length = 1
                color = (color - (head & 0x0F) if diff_type & 2 else color + (head & 0x0F)) & 0xFF
            else:
                color = 0xFF
            if chunk_type != 2:
                length = head & 0x0F
                for byte_index in range(length_size):
                    length += data[offset + 1 + byte_index] << (4 + 8 * (length_size - 1 - byte_index))
                offset += length_size
            offset += 1
            if length <= 0 or total + length > expected_pixels:
                raise FormatError(f"invalid GOO RLE length in layer {layer_index}")
            yield Run(length, color)
            total += length
    except IndexError as error:
        raise FormatError(f"truncated GOO RLE payload in layer {layer_index}") from error
    if total != expected_pixels:
        raise FormatError(
            f"GOO layer {layer_index} decodes to {total} pixels; expected {expected_pixels}"
        )


def _raster_from_runs(width: int, height: int, runs: Iterable[Run]) -> LayerRaster:
    raster = LayerRaster(width, height)
    cursor = 0
    for run in runs:
        end = cursor + run.length
        if run.value:
            span_cursor = cursor
            while span_cursor < end:
                y, x0 = divmod(span_cursor, width)
                row_end = min(end, (y + 1) * width)
                raster.set_span(y, x0, row_end - y * width - 1)
                span_cursor = row_end
        cursor = end
    if cursor != width * height:  # defensive: strict decoders already enforce this
        raise FormatError("decoded raster size does not match its resolution")
    return raster


def _validate_dimensions(width: int, height: int, layers: int, layer_height: float) -> None:
    if width <= 0 or height <= 0 or width * height > _MAX_PIXELS_PER_LAYER:
        raise FormatError("invalid or unsafe sliced-file resolution")
    if layers <= 0 or layers > _MAX_LAYER_COUNT:
        raise FormatError("invalid or unsafe sliced-file layer count")
    if not math.isfinite(layer_height) or layer_height <= 0:
        raise FormatError("invalid sliced-file layer height")


def _validate_layer_groups(bottom_layers: int, transition_layers: int, layers: int) -> None:
    # Very short calibration objects can legitimately end before the configured
    # bottom/transition schedule completes, so do not require the groups to fit
    # inside this particular file.
    if bottom_layers > _MAX_LAYER_COUNT or transition_layers > _MAX_LAYER_COUNT:
        raise FormatError("bottom or transition layer count exceeds the safe limit")


def _validate_layer_values(index: int, z_mm: float, exposure_s: float, previous_z: float) -> None:
    if not math.isfinite(z_mm) or z_mm <= previous_z:
        raise FormatError(f"layer {index} has a non-increasing or non-finite Z position")
    if not math.isfinite(exposure_s) or exposure_s < 0:
        raise FormatError(f"layer {index} has an invalid exposure")


def _goo_checksum(data: bytes) -> int:
    return (~(sum(data) & 0xFF)) & 0xFF


def _file_size(path: Path) -> int:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise FormatError(f"cannot inspect sliced file: {path}") from error
    if size <= 0:
        raise FormatError("sliced file is empty")
    return size


def _check_range(offset: int, size: int, file_size: int, label: str) -> None:
    if offset < 0 or size < 0 or offset > file_size or size > file_size - offset:
        raise FormatError(f"{label} is outside file bounds")


def _read_exact_at(
    handle: BinaryIO, offset: int, size: int, file_size: int, label: str
) -> bytes:
    _check_range(offset, size, file_size, label)
    handle.seek(offset)
    payload = handle.read(size)
    if len(payload) != size:
        raise FormatError(f"could not read complete {label}")
    return payload
