from __future__ import annotations

from array import array
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from math import ceil, floor

from .config import PrintConfig
from .errors import ConfigError, MeshError
from .mesh import Mesh, Point3, Triangle
from .raster import LayerRaster

Segment = tuple[tuple[float, float], tuple[float, float]]


@dataclass(slots=True)
class _ScanEdge:
    end_row: int
    x: float
    x_step: float
    winding: int
    order: int


@dataclass(frozen=True)
class PreparedMesh:
    mesh: Mesh
    layer_count: int
    height_mm: float
    layer_triangles: "LayerTriangleIndex"


@dataclass(frozen=True)
class LayerTriangleIndex:
    """Compact interval index for random and sequential layer rendering.

    A dense index repeats a tall triangle in every intersected layer. This
    block index stores it once per group of layers, then applies its exact
    first/last interval when a layer is rendered. Memory therefore drops by up
    to ``block_size`` while parallel and out-of-order preview access still work.
    """

    blocks: tuple[array, ...]
    first_layers: array
    last_layers: array
    block_size: int
    layer_count: int
    dense_reference_count: int

    @property
    def reference_count(self) -> int:
        return sum(len(block) for block in self.blocks)

    def indices_for_layer(self, layer_index: int) -> Iterable[int]:
        if layer_index < 0 or layer_index >= self.layer_count:
            return ()
        block = self.blocks[layer_index // self.block_size]
        if self.block_size == 1:
            return iter(block)  # Exact bounded index: no repeated interval tests.
        first_layers = self.first_layers
        last_layers = self.last_layers
        return (
            int(index)
            for index in block
            if first_layers[int(index)] <= layer_index <= last_layers[int(index)]
        )


def prepare_mesh(
    mesh: Mesh,
    config: PrintConfig,
    z_offset_mm: float = 0.0,
    xy_offset_mm: tuple[float, float] = (0.0, 0.0),
    preserve_coordinates: bool = False,
) -> PreparedMesh:
    bounds = mesh.bounds()
    if bounds.width <= 0 or bounds.depth <= 0 or bounds.height <= 0:
        raise MeshError("mesh bounds are degenerate")
    if preserve_coordinates:
        min_x = bounds.min_x + xy_offset_mm[0]
        max_x = bounds.max_x + xy_offset_mm[0]
        min_y = bounds.min_y + xy_offset_mm[1]
        max_y = bounds.max_y + xy_offset_mm[1]
        if min_x < 0 or min_y < 0 or max_x > config.size_x_mm or max_y > config.size_y_mm:
            raise ConfigError(
                f"mesh footprint spans {min_x:.2f},{min_y:.2f} to {max_x:.2f},{max_y:.2f}mm, "
                f"outside build area {config.size_x_mm:.2f}x{config.size_y_mm:.2f}mm"
            )
    elif bounds.width > config.size_x_mm or bounds.depth > config.size_y_mm:
        raise ConfigError(
            f"mesh footprint {bounds.width:.2f}x{bounds.depth:.2f}mm exceeds "
            f"build area {config.size_x_mm:.2f}x{config.size_y_mm:.2f}mm"
        )
    if z_offset_mm < 0:
        raise ConfigError("Z offset cannot be negative")
    final_min_z = bounds.min_z + z_offset_mm if preserve_coordinates else z_offset_mm
    final_max_z = bounds.max_z + z_offset_mm if preserve_coordinates else bounds.height + z_offset_mm
    if final_min_z < 0:
        raise ConfigError("mesh cannot extend below the build plate")
    if final_max_z > config.size_z_mm:
        raise ConfigError(
            f"mesh height {bounds.height:.2f}mm plus {z_offset_mm:.2f}mm lift exceeds "
            f"build height {config.size_z_mm:.2f}mm"
        )

    if preserve_coordinates:
        ox = xy_offset_mm[0]
        oy = xy_offset_mm[1]
    elif config.center_model:
        ox = (config.size_x_mm - bounds.width) / 2.0 - bounds.min_x + xy_offset_mm[0]
        oy = (config.size_y_mm - bounds.depth) / 2.0 - bounds.min_y + xy_offset_mm[1]
    else:
        ox = -bounds.min_x + xy_offset_mm[0]
        oy = -bounds.min_y + xy_offset_mm[1]
    oz = z_offset_mm if preserve_coordinates else z_offset_mm - bounds.min_z

    transformed = mesh.transformed((ox, oy, oz))
    total_height = final_max_z
    layer_count = config.layer_count_for_height(total_height)
    return PreparedMesh(transformed, layer_count, total_height, _build_layer_index(transformed, config, layer_count))


def render_model_layer(mesh: Mesh, config: PrintConfig, layer_index: int) -> LayerRaster:
    z = (layer_index + 0.5) * config.layer_height_mm
    segments = _slice_segments(mesh, z)
    return _rasterize_segments(segments, config)


def render_prepared_layer(prepared: PreparedMesh, config: PrintConfig, layer_index: int) -> LayerRaster:
    z = (layer_index + 0.5) * config.layer_height_mm
    segments = _slice_segments_from_indices(
        prepared.mesh.triangles,
        prepared.layer_triangles.indices_for_layer(layer_index),
        z,
    )
    return _rasterize_segments(segments, config)


def rasterize_segments(segments: list[Segment], config: PrintConfig) -> LayerRaster:
    return _rasterize_segments(segments, config)


def _build_layer_index(
    mesh: Mesh,
    config: PrintConfig,
    layer_count: int,
    block_size: int | None = None,
) -> LayerTriangleIndex:
    first_layers = array("i", [-1]) * len(mesh.triangles)
    last_layers = array("i", [-1]) * len(mesh.triangles)
    dense_reference_count = 0
    active_triangles = 0
    height = config.layer_height_mm
    for triangle_index, triangle in enumerate(mesh.triangles):
        min_z = min(point[2] for point in triangle)
        max_z = max(point[2] for point in triangle)
        if max_z <= min_z:
            continue
        first = max(0, int(ceil(min_z / height - 0.5)))
        last = min(layer_count - 1, int(ceil(max_z / height - 0.5)) - 1)
        if first > last:
            continue
        first_layers[triangle_index] = first
        last_layers[triangle_index] = last
        dense_reference_count += last - first + 1
        active_triangles += 1
    if block_size is None:
        # Fine miniature triangles commonly intersect only one or two planes.
        # A 64-layer block then repeats mostly-failing tests on every layer.
        # Use exact buckets only when bounded to 8 MiB of uint32 references and
        # at most eight references per active triangle on average. Tall CAD
        # faces keep the compact block index; never build an unbounded dense map.
        block_size = 1 if layer_count <= 8192 and dense_reference_count <= min(2_000_000, active_triangles * 8) else 64
    block_size = max(1, int(block_size))
    block_count = max(1, int(ceil(layer_count / block_size)))
    blocks = [array("I") for _ in range(block_count)]
    for triangle_index, (first, last) in enumerate(zip(first_layers, last_layers)):
        if first < 0:
            continue
        for block_index in range(first // block_size, last // block_size + 1):
            blocks[block_index].append(triangle_index)
    return LayerTriangleIndex(
        tuple(blocks),
        first_layers,
        last_layers,
        block_size,
        layer_count,
        dense_reference_count,
    )


def _slice_segments(mesh: Mesh, z: float) -> list[Segment]:
    return _slice_segments_from_triangles(mesh.triangles, z)


def _slice_segments_from_triangles(triangles: Iterable[Triangle], z: float) -> list[Segment]:
    # Each segment is oriented consistently with the triangle's winding (outward
    # normal), so that overlapping/unioned solids accumulate a nonzero winding
    # number in _rasterize_segments instead of cancelling out under even-odd fill.
    segments: list[Segment] = []
    for tri in triangles:
        below = [tri[0][2] <= z, tri[1][2] <= z, tri[2][2] <= z]
        below_count = sum(below)
        if below_count == 0 or below_count == 3:
            continue
        if below_count == 1:
            lone = below.index(True)
            lone_is_below = True
        else:
            lone = below.index(False)
            lone_is_below = False
        pred = (lone - 1) % 3
        succ = (lone + 1) % 3
        point_pred = _edge_intersection(tri[lone], tri[pred], z)
        point_succ = _edge_intersection(tri[lone], tri[succ], z)
        if point_pred is None or point_succ is None:
            continue
        segment = (point_pred, point_succ) if lone_is_below else (point_succ, point_pred)
        if _distance2(segment[0], segment[1]) > 1e-16:
            segments.append(segment)
    return segments


def _slice_segments_from_indices(
    triangles: Sequence[Triangle],
    indices: Iterable[int],
    z: float,
) -> list[Segment]:
    return _slice_segments_from_triangles(
        (triangles[int(index)] for index in indices),
        z,
    )


def _edge_intersection(a: Point3, b: Point3, z: float) -> tuple[float, float] | None:
    az = a[2]
    bz = b[2]
    if az == bz:
        return None
    if (az <= z < bz) or (bz <= z < az):
        t = (z - az) / (bz - az)
        return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
    return None


def _rasterize_segments(segments: list[Segment], config: PrintConfig) -> LayerRaster:
    width = config.resolution_x
    height = config.resolution_y
    pixel_x = config.pixel_size_x_mm
    pixel_y = config.pixel_size_y_mm
    raster = LayerRaster(width, height)
    if not segments:
        return raster

    # An active-edge table stores each edge once and advances its crossing X by
    # one scanline at a time.  The former row buckets duplicated long edges in
    # every intersected row and then recomputed each intersection from scratch.
    starts: dict[int, list[_ScanEdge]] = {}
    for order, ((x1, y1), (x2, y2)) in enumerate(segments):
        if y1 == y2:
            continue
        ymin = min(y1, y2)
        ymax = max(y1, y2)
        if ymax <= 0 or ymin >= config.size_y_mm:
            continue
        first = max(0, int(ceil(ymin / pixel_y - 0.5)))
        last = min(height - 1, int(ceil(ymax / pixel_y - 0.5)) - 1)
        if first > last:
            continue
        first_y_mm = (first + 0.5) * pixel_y
        slope = (x2 - x1) / (y2 - y1)
        first_x = x1 + (first_y_mm - y1) * slope
        starts.setdefault(first, []).append(
            _ScanEdge(last, first_x, slope * pixel_y, 1 if y2 > y1 else -1, order)
        )

    active: list[_ScanEdge] = []
    for y in range(height):
        active.extend(starts.get(y, ()))
        if not active:
            continue
        # Crossings carry a winding sign so overlapping/unioned solids fill their
        # union (nonzero winding number) rather than cancelling under even-odd pairing.
        crossings = [(edge.x, edge.winding, edge.order) for edge in active]
        crossings.sort(key=lambda crossing: (crossing[0], crossing[2]))

        winding = 0
        span_start: float | None = None
        for x, sign, _order in crossings:
            was_outside = winding == 0
            winding += sign
            if was_outside and winding != 0:
                span_start = x
            elif not was_outside and winding == 0 and span_start is not None:
                left = max(0.0, span_start)
                right = min(config.size_x_mm, x)
                span_start = None
                if right <= left:
                    continue
                x0 = int(ceil(left / pixel_x - 0.5))
                x1 = int(floor(right / pixel_x - 0.5))
                raster.set_span(y, x0, x1, 255)

        next_active: list[_ScanEdge] = []
        for edge in active:
            if y < edge.end_row:
                edge.x += edge.x_step
                next_active.append(edge)
        active = next_active
    return raster


def _distance2(a: tuple[float, float], b: tuple[float, float]) -> float:
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    return dx * dx + dy * dy
