from __future__ import annotations

import hashlib
import itertools
import math
import re
import struct
from collections.abc import Iterable, Iterator
from pathlib import Path

from .models import MeshInspection, Placement, SourceModel


Triangle = tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]

_BINARY_RECORD = struct.Struct("<12fH")
_VERTEX_PATTERN = re.compile(
    rb"^\s*vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s*$"
)


class StlError(ValueError):
    pass


class PlatePackingError(ValueError):
    pass


def inspect_stl(path: str | Path) -> MeshInspection:
    source = Path(path).resolve()
    fmt = stl_format(source)
    bounds_min = [math.inf, math.inf, math.inf]
    bounds_max = [-math.inf, -math.inf, -math.inf]
    triangle_count = 0
    degenerate = 0
    signed_volume_six = 0.0
    for triangle in iter_triangles(source, fmt=fmt):
        triangle_count += 1
        for point in triangle:
            for axis in range(3):
                bounds_min[axis] = min(bounds_min[axis], point[axis])
                bounds_max[axis] = max(bounds_max[axis], point[axis])
        cross = _cross(_sub(triangle[1], triangle[0]), _sub(triangle[2], triangle[0]))
        if _dot(cross, cross) <= 1e-20:
            degenerate += 1
        signed_volume_six += _dot(triangle[0], _cross(triangle[1], triangle[2]))
    if triangle_count == 0:
        raise StlError(f"STL has no triangles: {source}")
    dimensions = tuple(bounds_max[i] - bounds_min[i] for i in range(3))
    largest_dimension = max(dimensions)
    return MeshInspection(
        path=str(source),
        format=fmt,
        triangle_count=triangle_count,
        degenerate_triangles=degenerate,
        bounds_min=tuple(bounds_min),  # type: ignore[arg-type]
        bounds_max=tuple(bounds_max),  # type: ignore[arg-type]
        dimensions=dimensions,  # type: ignore[arg-type]
        signed_volume_mm3=signed_volume_six / 6.0,
        units_ambiguous=largest_dimension <= 2.0 or largest_dimension >= 1000.0,
        sha256=_sha256(source),
    )


def stl_format(path: Path) -> str:
    size = path.stat().st_size
    if size >= 84:
        with path.open("rb") as handle:
            handle.seek(80)
            count_bytes = handle.read(4)
        if len(count_bytes) == 4:
            count = struct.unpack("<I", count_bytes)[0]
            if size == 84 + count * _BINARY_RECORD.size:
                return "binary-stl"
    with path.open("rb") as handle:
        prefix = handle.read(512).lstrip().lower()
    if prefix.startswith(b"solid"):
        return "ascii-stl"
    raise StlError(f"unrecognized STL encoding: {path}")


def iter_triangles(path: Path, *, fmt: str | None = None) -> Iterator[Triangle]:
    encoding = fmt or stl_format(path)
    if encoding == "binary-stl":
        yield from _iter_binary(path)
    elif encoding == "ascii-stl":
        yield from _iter_ascii(path)
    else:
        raise StlError(f"unsupported STL encoding: {encoding}")


def pack_shelf(
    *,
    root: Path,
    models: list[SourceModel],
    inspections: list[MeshInspection],
    plate_size_mm: tuple[float, float, float],
    clearance_mm: float,
    margin_mm: float,
    support_footprint_margin_mm: float = 0.0,
) -> list[Placement]:
    if len(models) != len(inspections):
        raise ValueError("model and inspection counts differ")
    usable_x = plate_size_mm[0] - 2 * margin_mm
    usable_y = plate_size_mm[1] - 2 * margin_mm
    items: list[dict[str, object]] = []
    for index, (model, inspection) in enumerate(
        zip(models, inspections, strict=True)
    ):
        if model.quantity != 1:
            raise PlatePackingError("quantity other than one is not implemented in the MVP")
        scale = 1.0
        if model.target_height_mm is not None:
            if inspection.dimensions[2] <= 0:
                raise PlatePackingError(f"model {model.id!r} has zero height")
            scale = model.target_height_mm / inspection.dimensions[2]
        width = inspection.dimensions[0] * scale
        depth = inspection.dimensions[1] * scale
        height = inspection.dimensions[2] * scale
        reserved_width = width + 2.0 * support_footprint_margin_mm
        reserved_depth = depth + 2.0 * support_footprint_margin_mm
        fits_flat = reserved_width <= usable_x and reserved_depth <= usable_y
        fits_rotated = reserved_depth <= usable_x and reserved_width <= usable_y
        if (not fits_flat and not fits_rotated) or height > plate_size_mm[2]:
            raise PlatePackingError(
                f"model {model.id!r} ({width:.2f} x {depth:.2f} x {height:.2f} mm; "
                f"support reserve {support_footprint_margin_mm:.2f} mm) does not fit the usable plate"
            )
        items.append(
            {
                "index": index,
                "model": model,
                "inspection": inspection,
                "source": str((root / model.source).resolve()),
                "scale": scale,
                "width": width,
                "depth": depth,
                "height": height,
            }
        )

    # The supported project sizes are small enough for a bounded exhaustive
    # search over input order and 0/90-degree bed rotations. This prevents a
    # feasible plate from being rejected merely because the manifest order is
    # poor while retaining deterministic shelf rows and safety margins.
    best: tuple[tuple[object, ...], list[Placement]] | None = None
    indices = tuple(range(len(items)))
    for order in itertools.permutations(indices):
        for rotations in itertools.product((0.0, 90.0), repeat=len(items)):
            x = margin_mm
            y = margin_mm
            row_depth = 0.0
            used_width = 0.0
            candidate: dict[int, Placement] = {}
            valid = True
            for index in order:
                item = items[index]
                model = item["model"]
                inspection = item["inspection"]
                assert isinstance(model, SourceModel)
                assert isinstance(inspection, MeshInspection)
                scale = float(item["scale"])
                width = float(item["width"])
                depth = float(item["depth"])
                height = float(item["height"])
                rotation = rotations[index]
                placed_width, placed_depth = (
                    (depth, width) if rotation == 90.0 else (width, depth)
                )
                reserved_width = placed_width + 2.0 * support_footprint_margin_mm
                reserved_depth = placed_depth + 2.0 * support_footprint_margin_mm
                if x + reserved_width > margin_mm + usable_x + 1e-9:
                    x = margin_mm
                    y += row_depth + clearance_mm
                    row_depth = 0.0
                if y + reserved_depth > margin_mm + usable_y + 1e-9:
                    valid = False
                    break
                rotated_min_x, rotated_min_y = _rotated_scaled_min_xy(
                    inspection, scale, rotation
                )
                translation = (
                    x + support_footprint_margin_mm - rotated_min_x,
                    y + support_footprint_margin_mm - rotated_min_y,
                    -inspection.bounds_min[2] * scale,
                )
                candidate[index] = Placement(
                    model_id=model.id,
                    source=str(item["source"]),
                    scale=scale,
                    translate=translation,
                    dimensions_mm=(placed_width, placed_depth, height),
                    rotate_z_deg=rotation,
                )
                used_width = max(used_width, x + reserved_width - margin_mm)
                x += reserved_width + clearance_mm
                row_depth = max(row_depth, reserved_depth)
            if not valid:
                continue
            used_depth = y + row_depth - margin_mm
            score: tuple[object, ...] = (
                round(used_depth, 9),
                round(used_width, 9),
                order,
                rotations,
            )
            placements = [candidate[index] for index in indices]
            if best is None or score < best[0]:
                best = (score, placements)
    if best is None:
        raise PlatePackingError(
            "models do not fit using deterministic rotation-aware shelf packing"
        )
    return best[1]


def write_combined_binary_stl(
    output: str | Path,
    placements: list[Placement],
) -> dict[str, int]:
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    triangle_count = 0
    removed_degenerate = 0
    with target.open("wb+") as handle:
        header = b"Agent-Slicer combined plate"
        handle.write(header.ljust(80, b"\0"))
        handle.write(struct.pack("<I", 0))
        for placement in placements:
            source = Path(placement.source)
            for triangle in iter_triangles(source):
                transformed_points = []
                for point in triangle:
                    scaled_x = point[0] * placement.scale
                    scaled_y = point[1] * placement.scale
                    rotated_x, rotated_y = _rotate_xy(
                        scaled_x, scaled_y, placement.rotate_z_deg
                    )
                    transformed_points.append(
                        (
                            _float32(rotated_x + placement.translate[0]),
                            _float32(rotated_y + placement.translate[1]),
                            _float32(
                                point[2] * placement.scale + placement.translate[2]
                            ),
                        )
                    )
                transformed = tuple(transformed_points)
                normal = _cross(
                    _sub(transformed[1], transformed[0]),
                    _sub(transformed[2], transformed[0]),
                )
                length = math.sqrt(_dot(normal, normal))
                if length <= 1e-10:
                    removed_degenerate += 1
                    continue
                normal = tuple(value / length for value in normal)
                values = (*normal, *transformed[0], *transformed[1], *transformed[2], 0)
                handle.write(_BINARY_RECORD.pack(*values))
                triangle_count += 1
        handle.seek(80)
        handle.write(struct.pack("<I", triangle_count))
    return {
        "triangle_count": triangle_count,
        "removed_degenerate_triangles": removed_degenerate,
    }


def _rotate_xy(x: float, y: float, rotation_deg: float) -> tuple[float, float]:
    normalized = int(round(rotation_deg)) % 360
    if normalized == 0:
        return x, y
    if normalized == 90:
        return -y, x
    if normalized == 180:
        return -x, -y
    if normalized == 270:
        return y, -x
    angle = math.radians(rotation_deg)
    return (
        x * math.cos(angle) - y * math.sin(angle),
        x * math.sin(angle) + y * math.cos(angle),
    )


def _rotated_scaled_min_xy(
    inspection: MeshInspection, scale: float, rotation_deg: float
) -> tuple[float, float]:
    corners = (
        (inspection.bounds_min[0] * scale, inspection.bounds_min[1] * scale),
        (inspection.bounds_min[0] * scale, inspection.bounds_max[1] * scale),
        (inspection.bounds_max[0] * scale, inspection.bounds_min[1] * scale),
        (inspection.bounds_max[0] * scale, inspection.bounds_max[1] * scale),
    )
    rotated = [_rotate_xy(x, y, rotation_deg) for x, y in corners]
    return min(point[0] for point in rotated), min(point[1] for point in rotated)


def _iter_binary(path: Path, chunk_records: int = 65_536) -> Iterator[Triangle]:
    with path.open("rb") as handle:
        handle.seek(80)
        count = struct.unpack("<I", handle.read(4))[0]
        remaining = count
        while remaining:
            request = min(remaining, chunk_records)
            data = handle.read(request * _BINARY_RECORD.size)
            if len(data) != request * _BINARY_RECORD.size:
                raise StlError(f"truncated binary STL: {path}")
            for values in struct.iter_unpack(_BINARY_RECORD.format, data):
                yield (
                    (values[3], values[4], values[5]),
                    (values[6], values[7], values[8]),
                    (values[9], values[10], values[11]),
                )
            remaining -= request


def _iter_ascii(path: Path) -> Iterator[Triangle]:
    vertices: list[tuple[float, float, float]] = []
    with path.open("rb") as handle:
        for line in handle:
            match = _VERTEX_PATTERN.match(line)
            if not match:
                continue
            vertices.append(tuple(float(group) for group in match.groups()))  # type: ignore[arg-type]
            if len(vertices) == 3:
                yield (vertices[0], vertices[1], vertices[2])
                vertices.clear()
    if vertices:
        raise StlError(f"incomplete triangle in ASCII STL: {path}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sub(a: Iterable[float], b: Iterable[float]) -> tuple[float, float, float]:
    aa = tuple(a)
    bb = tuple(b)
    return aa[0] - bb[0], aa[1] - bb[1], aa[2] - bb[2]


def _cross(a: Iterable[float], b: Iterable[float]) -> tuple[float, float, float]:
    aa = tuple(a)
    bb = tuple(b)
    return (
        aa[1] * bb[2] - aa[2] * bb[1],
        aa[2] * bb[0] - aa[0] * bb[2],
        aa[0] * bb[1] - aa[1] * bb[0],
    )


def _dot(a: Iterable[float], b: Iterable[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _float32(value: float) -> float:
    """Round exactly as binary STL storage will round before geometry checks."""
    return struct.unpack("<f", struct.pack("<f", value))[0]
