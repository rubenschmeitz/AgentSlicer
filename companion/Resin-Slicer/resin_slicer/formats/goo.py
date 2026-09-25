from __future__ import annotations

import tempfile
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from ..config import PrintConfig
from ..errors import FormatError
from ..material import estimate_material_mm3
from ..raster import LayerRaster, Run
from ..workspace import work_subdirectory
from .binary import ByteWriter, FileWriter, atomic_binary_writer, sized_ascii
from .timing import estimate_print_time_seconds, layer_exposure_seconds

MAGIC_TAG = bytes([0x07, 0x00, 0x00, 0x00, 0x44, 0x4C, 0x50, 0x00])
DELIMITER = bytes([0x0D, 0x0A])
ENDING_STRING = bytes([0x00, 0x00, 0x00, 0x07, 0x00, 0x00, 0x00, 0x44, 0x4C, 0x50, 0x00])
HEADER_SIZE = 0x2FB95


@dataclass(frozen=True)
class FormatStats:
    material_mm3: float
    encoded_bytes: int = 0
    max_layer_bytes: int = 0
    storage_mode: str = "disk-spooled-atomic"


@dataclass(frozen=True)
class GooLayer:
    z_mm: float
    exposure_s: float
    rest_before_lift_s: float
    rest_after_lift_s: float
    after_retract_s: float
    lift_distance_mm: float
    lift_speed_mm_min: float
    lift_distance2_mm: float
    lift_speed2_mm_min: float
    retract_distance_mm: float
    retract_speed_mm_min: float
    retract_distance2_mm: float
    retract_speed2_mm_min: float
    light_pwm: int
    data: bytes


@dataclass(frozen=True)
class _SpooledGooLayer:
    layer: GooLayer
    payload_offset: int
    payload_size: int


def write_goo(
    path: Path,
    config: PrintConfig,
    layer_count: int,
    layers: object,
) -> FormatStats:
    material = 0.0
    encoded_bytes = 0
    max_layer_bytes = 0
    encoded_layers: list[_SpooledGooLayer] = []
    with tempfile.TemporaryFile(
        mode="w+b", dir=work_subdirectory("format-spool")
    ) as spool:
        expected_index = 0
        for layer_index, raster in layers:  # type: ignore[assignment]
            if layer_index != expected_index:
                raise FormatError(
                    "layer iterator must yield consecutive indices starting at 0; "
                    f"expected {expected_index}, received {layer_index}"
                )
            material += estimate_material_mm3(raster, config)
            layer = _encode_layer(layer_index, raster, config)
            payload_offset = spool.tell()
            spool.write(layer.data)
            payload_size = len(layer.data)
            encoded_layers.append(
                _SpooledGooLayer(
                    replace(layer, data=b""),
                    payload_offset,
                    payload_size,
                )
            )
            encoded_bytes += payload_size
            max_layer_bytes = max(max_layer_bytes, payload_size)
            expected_index += 1
        if expected_index != layer_count:
            raise FormatError(
                f"layer iterator yielded {expected_index} layers; expected {layer_count}"
            )

        with atomic_binary_writer(path) as writer:
            _write_header(writer, config, layer_count, material)
            for layer in encoded_layers:
                _write_spooled_layer(writer, layer, spool)
            writer.write_bytes(ENDING_STRING)
    return FormatStats(material, encoded_bytes, max_layer_bytes)


def encode_rle(runs: object) -> bytes:
    data = bytearray()
    last_value = 0

    def add_run(length: int, value: int) -> None:
        nonlocal last_value
        if length <= 0:
            return
        value &= 0xFF
        diff = value - last_value
        if value == 0x00:
            chunk_type = 0b00
        elif value == 0xFF:
            chunk_type = 0b11
        elif data and abs(diff) <= 15:
            if length > 255:
                add_run(255, value)
                add_run(length - 255, value)
                return
            byte_0 = (0b10 << 6) | ((1 if diff < 0 else 0) << 5) | ((1 if length != 1 else 0) << 4) | abs(diff)
            data.append(byte_0)
            if length != 1:
                data.append(length)
            last_value = value
            return
        else:
            chunk_type = 0b01

        if length <= 0xF:
            size_code = 0b00
        elif length <= 0xFFF:
            size_code = 0b01
        elif length <= 0xFFFFF:
            size_code = 0b10
        elif length <= 0xFFFFFFF:
            size_code = 0b11
        else:
            add_run(0xFFFFFFF, value)
            add_run(length - 0xFFFFFFF, value)
            return

        data.append((chunk_type << 6) | (size_code << 4) | (length & 0x0F))
        if chunk_type == 0b01:
            data.append(value)
        if size_code == 1:
            data.append((length >> 4) & 0xFF)
        elif size_code == 2:
            data.extend([(length >> 12) & 0xFF, (length >> 4) & 0xFF])
        elif size_code == 3:
            data.extend([(length >> 20) & 0xFF, (length >> 12) & 0xFF, (length >> 4) & 0xFF])
        last_value = value

    for run in runs:  # type: ignore[assignment]
        add_run(run.length, run.value)
    return bytes(data)


def decode_rle(data: bytes) -> list[Run]:
    out: list[Run] = []
    color = 0
    offset = 0
    while offset < len(data):
        head = data[offset]
        length = 0
        chunk_type = head >> 6
        chunk_length_size = (head >> 4) & 0x03
        if chunk_type == 0b00:
            color = 0
        elif chunk_type == 0b01:
            offset += 1
            color = data[offset]
        elif chunk_type == 0b10:
            diff_type = (head >> 4) & 0x03
            diff_value = head & 0x0F
            if diff_type & 0b01:
                offset += 1
                length = data[offset]
            else:
                length = 1
            if diff_type & 0b10:
                color -= diff_value
            else:
                color += diff_value
            color &= 0xFF
        elif chunk_type == 0b11:
            color = 0xFF

        if chunk_type != 0b10:
            base = head & 0x0F
            if chunk_length_size == 0:
                length = base
            elif chunk_length_size == 1:
                length = base + (data[offset + 1] << 4)
                offset += 1
            elif chunk_length_size == 2:
                length = base + (data[offset + 1] << 12) + (data[offset + 2] << 4)
                offset += 2
            else:
                length = base + (data[offset + 1] << 20) + (data[offset + 2] << 12) + (data[offset + 3] << 4)
                offset += 3
        offset += 1
        out.append(Run(length, color))
    return out


def _encode_layer(layer_index: int, raster: LayerRaster, config: PrintConfig) -> GooLayer:
    bottom = layer_index < config.bottom_layers
    exposure = layer_exposure_seconds(config, layer_index)
    pwm = config.bottom_light_pwm if bottom else config.light_pwm
    return GooLayer(
        z_mm=config.layer_height_mm * (layer_index + 1),
        exposure_s=exposure,
        rest_before_lift_s=config.rest_before_lift_s,
        rest_after_lift_s=config.rest_after_lift_s,
        after_retract_s=config.wait_after_retract_s,
        lift_distance_mm=config.lift_distance_for(bottom=bottom),
        lift_speed_mm_min=config.lift_speed_for(bottom=bottom),
        lift_distance2_mm=config.lift_distance2_mm,
        lift_speed2_mm_min=config.lift_speed2_mm_min,
        retract_distance_mm=config.retract_distance_for(bottom=bottom),
        retract_speed_mm_min=config.retract_speed_for(bottom=bottom),
        retract_distance2_mm=config.retract_distance2_mm,
        retract_speed2_mm_min=config.retract_speed2_mm_min,
        light_pwm=pwm,
        data=encode_rle(raster.runs()),
    )


def _write_header(
    writer: ByteWriter | FileWriter,
    config: PrintConfig,
    layer_count: int,
    material_mm3: float,
) -> None:
    writer.write_bytes(sized_ascii("V3.0", 4))
    writer.write_bytes(MAGIC_TAG)
    writer.write_bytes(sized_ascii("resin-slicer", 32))
    writer.write_bytes(sized_ascii("0.1.0", 24))
    writer.write_bytes(sized_ascii(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 24))
    writer.write_bytes(sized_ascii(config.machine_name, 32))
    writer.write_bytes(sized_ascii("Default", 32))
    writer.write_bytes(sized_ascii(config.resin_name, 32))
    writer.write_u16_be(0)
    writer.write_u16_be(0)
    writer.write_u16_be(0)
    _write_rgb565_preview(writer, 116, 116)
    writer.write_bytes(DELIMITER)
    _write_rgb565_preview(writer, 290, 290)
    writer.write_bytes(DELIMITER)
    writer.write_u32_be(layer_count)
    writer.write_u16_be(config.resolution_x)
    writer.write_u16_be(config.resolution_y)
    writer.write_bool(False)
    writer.write_bool(False)
    writer.write_f32_be(config.size_x_mm)
    writer.write_f32_be(config.size_y_mm)
    writer.write_f32_be(config.size_z_mm)
    writer.write_f32_be(config.layer_height_mm)
    writer.write_f32_be(config.exposure_time_s)
    writer.write_u8(1)
    writer.write_f32_be(0.0)  # bottom layer off time
    writer.write_f32_be(config.rest_before_lift_s)
    writer.write_f32_be(config.rest_after_lift_s)
    writer.write_f32_be(config.wait_after_retract_s)
    writer.write_f32_be(config.rest_before_lift_s)
    writer.write_f32_be(config.rest_after_lift_s)
    writer.write_f32_be(config.wait_after_retract_s)
    writer.write_f32_be(config.bottom_exposure_time_s)
    writer.write_u32_be(config.bottom_layers)
    # GOO stores bottom and normal first-stage motion before their second-stage
    # values. Keep the global fields useful to readers even though advance mode
    # below tells the printer to use every serialized layer definition.
    writer.write_f32_be(config.lift_distance_for(bottom=True))
    writer.write_f32_be(config.lift_speed_for(bottom=True))
    writer.write_f32_be(config.lift_distance_for(bottom=False))
    writer.write_f32_be(config.lift_speed_for(bottom=False))
    writer.write_f32_be(config.retract_distance_for(bottom=True))
    writer.write_f32_be(config.retract_speed_for(bottom=True))
    writer.write_f32_be(config.retract_distance_for(bottom=False))
    writer.write_f32_be(config.retract_speed_for(bottom=False))
    writer.write_f32_be(config.lift_distance2_mm)
    writer.write_f32_be(config.lift_speed2_mm_min)
    writer.write_f32_be(config.lift_distance2_mm)
    writer.write_f32_be(config.lift_speed2_mm_min)
    writer.write_f32_be(config.retract_distance2_mm)
    writer.write_f32_be(config.retract_speed2_mm_min)
    writer.write_f32_be(config.retract_distance2_mm)
    writer.write_f32_be(config.retract_speed2_mm_min)
    writer.write_u16_be(config.bottom_light_pwm)
    writer.write_u16_be(config.light_pwm)
    writer.write_bool(True)  # advance mode: use the serialized layer definitions
    writer.write_u32_be(estimate_print_time_seconds(config, layer_count))
    writer.write_f32_be(material_mm3)
    writer.write_f32_be(material_mm3 / 1000.0 * config.resin_density_g_ml)
    writer.write_f32_be(0.0)
    writer.write_bytes(sized_ascii("$", 8))
    writer.write_u32_be(HEADER_SIZE)
    writer.write_bool(True)
    writer.write_u16_be(config.transition_layers)


def _write_layer(writer: ByteWriter | FileWriter, layer: GooLayer) -> None:
    writer.write_u16_be(0)
    writer.write_f32_be(200.0)
    writer.write_f32_be(layer.z_mm)
    writer.write_f32_be(layer.exposure_s)
    writer.write_f32_be(0.0)  # layer off time
    writer.write_f32_be(layer.rest_before_lift_s)
    writer.write_f32_be(layer.rest_after_lift_s)
    writer.write_f32_be(layer.after_retract_s)
    writer.write_f32_be(layer.lift_distance_mm)
    writer.write_f32_be(layer.lift_speed_mm_min)
    writer.write_f32_be(layer.lift_distance2_mm)
    writer.write_f32_be(layer.lift_speed2_mm_min)
    writer.write_f32_be(layer.retract_distance_mm)
    writer.write_f32_be(layer.retract_speed_mm_min)
    writer.write_f32_be(layer.retract_distance2_mm)
    writer.write_f32_be(layer.retract_speed2_mm_min)
    writer.write_u16_be(layer.light_pwm)
    writer.write_bytes(DELIMITER)
    writer.write_u32_be(len(layer.data) + 2)
    writer.write_u8(0x55)
    writer.write_bytes(layer.data)
    writer.write_u8(_checksum(layer.data))
    writer.write_bytes(DELIMITER)


def _write_spooled_layer(
    writer: FileWriter,
    spooled: _SpooledGooLayer,
    spool: BinaryIO,
) -> None:
    spool.seek(spooled.payload_offset)
    data = spool.read(spooled.payload_size)
    if len(data) != spooled.payload_size:
        raise FormatError("temporary GOO layer spool ended unexpectedly")
    _write_layer(writer, replace(spooled.layer, data=data))


def _write_rgb565_preview(
    writer: ByteWriter | FileWriter,
    width: int,
    height: int,
) -> None:
    writer.write_bytes(b"\0\0" * width * height)


def _checksum(data: bytes) -> int:
    total = 0
    for byte in data:
        total = (total + byte) & 0xFF
    return (~total) & 0xFF
