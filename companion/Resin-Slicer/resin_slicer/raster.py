from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import ceil, floor, sqrt
from typing import Iterable

_BINARY_TRANSLATION = bytes(0 if value == 0 else 1 for value in range(256))


@dataclass(frozen=True)
class Run:
    length: int
    value: int


@dataclass(frozen=True)
class Component:
    pixels: tuple[int, ...]
    min_x: int
    min_y: int
    max_x: int
    max_y: int

    @property
    def area_px(self) -> int:
        return len(self.pixels)

    def centroid(self, width: int) -> tuple[float, float]:
        sx = 0
        sy = 0
        for index in self.pixels:
            sx += index % width
            sy += index // width
        n = max(1, len(self.pixels))
        return sx / n, sy / n


@dataclass(frozen=True)
class ComponentSummary:
    area_px: int
    min_x: int
    min_y: int
    max_x: int
    max_y: int


@dataclass(frozen=True)
class FootprintDeficitSummary:
    area_px: int
    unsupported_area_px: int
    min_x: int
    min_y: int
    max_x: int
    max_y: int


class LayerRaster:
    def __init__(self, width: int, height: int, pixels: bytearray | None = None) -> None:
        self.width = width
        self.height = height
        if pixels is not None and len(pixels) != width * height:
            raise ValueError("pixel buffer length does not match layer dimensions")
        # Sliced layers are usually a small collection of horizontal spans.  Keep
        # them in that form until a caller genuinely needs random pixel access;
        # this avoids allocating a full 10 MiB buffer for every Mars 3 layer.
        self._pixels: bytearray | None = pixels
        self._rows: dict[int, list[tuple[int, int]]] = {}

    @property
    def pixels(self) -> bytearray:
        """Return a mutable dense buffer, materializing sparse spans on demand."""

        if self._pixels is None:
            pixels = bytearray(self.width * self.height)
            for y, spans in self.nonzero_rows():
                row_start = y * self.width
                for x0, x1 in spans:
                    pixels[row_start + x0 : row_start + x1] = b"\xff" * (x1 - x0)
            self._pixels = pixels
            self._rows.clear()
        return self._pixels

    @pixels.setter
    def pixels(self, value: bytearray) -> None:
        if len(value) != self.width * self.height:
            raise ValueError("pixel buffer length does not match layer dimensions")
        self._pixels = value
        self._rows.clear()

    @property
    def storage_mode(self) -> str:
        return "dense" if self._pixels is not None else "sparse"

    @property
    def dense_buffer_bytes(self) -> int:
        """Bytes held by the dense payload (zero while the layer stays sparse)."""

        return 0 if self._pixels is None else len(self._pixels)

    def copy(self) -> "LayerRaster":
        if self._pixels is not None:
            return LayerRaster(self.width, self.height, bytearray(self._pixels))
        copied = LayerRaster(self.width, self.height)
        copied._rows = {y: list(spans) for y, spans in self._rows.items()}
        return copied

    def count_on(self) -> int:
        total = 0
        for start, end in self._nonzero_spans():
            total += end - start
        return total

    def set_span(self, y: int, x0: int, x1: int, value: int = 255) -> None:
        if y < 0 or y >= self.height:
            return
        x0 = max(0, x0)
        x1 = min(self.width - 1, x1)
        if x0 > x1:
            return
        if value < 0 or value > 255:
            raise ValueError("pixel value must be between 0 and 255")
        if self._pixels is not None:
            start = y * self.width + x0
            self._pixels[start : start + x1 - x0 + 1] = bytes([value]) * (x1 - x0 + 1)
            return
        if value not in (0, 255):
            start = y * self.width + x0
            self.pixels[start : start + x1 - x0 + 1] = bytes([value]) * (x1 - x0 + 1)
            return

        start = x0
        end = x1 + 1
        existing = self._rows.get(y, [])
        if value == 0:
            remaining: list[tuple[int, int]] = []
            for span_start, span_end in existing:
                if span_end <= start or span_start >= end:
                    remaining.append((span_start, span_end))
                    continue
                if span_start < start:
                    remaining.append((span_start, start))
                if span_end > end:
                    remaining.append((end, span_end))
            if remaining:
                self._rows[y] = remaining
            else:
                self._rows.pop(y, None)
            return

        merged: list[tuple[int, int]] = []
        inserted = False
        for span_start, span_end in existing:
            if span_end < start:
                merged.append((span_start, span_end))
            elif end < span_start:
                if not inserted:
                    merged.append((start, end))
                    inserted = True
                merged.append((span_start, span_end))
            else:
                start = min(start, span_start)
                end = max(end, span_end)
        if not inserted:
            merged.append((start, end))
        self._rows[y] = merged

    def add_disk(self, cx: int, cy: int, radius: int, value: int = 255) -> None:
        radius = max(0, radius)
        r2 = radius * radius
        for y in range(max(0, cy - radius), min(self.height, cy + radius + 1)):
            dy2 = (y - cy) * (y - cy)
            dx = int(floor(sqrt(max(0, r2 - dy2))))
            self.set_span(y, cx - dx, cx + dx, value)

    def add_capsule(self, x0: int, y0: int, x1: int, y1: int, radius: int, value: int = 255) -> None:
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        previous: tuple[int, int] | None = None
        for step in range(steps + 1):
            t = step / steps
            x = int(round(x0 + (x1 - x0) * t))
            y = int(round(y0 + (y1 - y0) * t))
            if previous == (x, y):
                continue
            self.add_disk(x, y, radius, value)
            previous = (x, y)

    def or_with(self, other: "LayerRaster") -> None:
        if self.width != other.width or self.height != other.height:
            raise ValueError("cannot merge rasters with different dimensions")
        if other._pixels is None:
            for y, spans in other.nonzero_rows():
                for x0, x1 in spans:
                    self.set_span(y, x0, x1 - 1)
            return
        pixels = self.pixels
        for i, value in enumerate(other._pixels):
            if value:
                pixels[i] = max(pixels[i], value)

    def runs(self) -> Iterable[Run]:
        total = self.width * self.height
        cursor = 0
        for start, end in self._nonzero_spans():
            if start > cursor:
                yield Run(start - cursor, 0)
            yield Run(end - start, 255)
            cursor = end
        if cursor < total:
            yield Run(total - cursor, 0)

    def nonzero_spans(self) -> Iterable[tuple[int, int]]:
        return self._nonzero_spans()

    def row_spans(self, y: int) -> tuple[tuple[int, int], ...]:
        """Return local, end-exclusive nonzero spans for one row."""

        if y < 0 or y >= self.height:
            return ()
        if self._pixels is None:
            return tuple(self._rows.get(y, ()))
        row_start = y * self.width
        row_end = row_start + self.width
        row = self._pixels[row_start:row_end]
        spans: list[tuple[int, int]] = []
        x = row.find(255)
        while x != -1:
            span_end = row.find(0, x)
            if span_end == -1:
                span_end = self.width
            spans.append((x, span_end))
            x = row.find(255, span_end)
        return tuple(spans)

    def nonzero_rows(self) -> Iterable[tuple[int, tuple[tuple[int, int], ...]]]:
        """Yield only rows containing nonzero, end-exclusive spans."""

        if self._pixels is None:
            for y in sorted(self._rows):
                spans = self._rows[y]
                if spans:
                    yield y, tuple(spans)
            return
        for y in range(self.height):
            spans = self.row_spans(y)
            if spans:
                yield y, spans

    def _nonzero_spans(self) -> Iterable[tuple[int, int]]:
        for y, spans in self.nonzero_rows():
            row_start = y * self.width
            for x0, x1 in spans:
                yield row_start + x0, row_start + x1

    def unsupported_mask(self, support_mask: bytearray) -> bytearray:
        if len(support_mask) != len(self.pixels):
            raise ValueError("support mask length mismatch")
        out = bytearray(len(self.pixels))
        for y in range(self.height):
            row_start = y * self.width
            row_end = row_start + self.width
            model_row = self.pixels[row_start:row_end]
            support_row = support_mask[row_start:row_end]
            x = model_row.find(255)
            while x != -1:
                span_end = model_row.find(0, x)
                if span_end == -1:
                    span_end = self.width

                cursor = x
                while cursor < span_end:
                    unsupported_start = support_row.find(0, cursor, span_end)
                    if unsupported_start == -1:
                        break
                    unsupported_end = support_row.find(1, unsupported_start, span_end)
                    if unsupported_end == -1:
                        unsupported_end = span_end
                    out[row_start + unsupported_start : row_start + unsupported_end] = b"\x01" * (
                        unsupported_end - unsupported_start
                    )
                    cursor = unsupported_end

                x = model_row.find(255, span_end)
        return out

    def connected_components(self, active_mask: bytearray, min_area_px: int) -> list[Component]:
        width = self.width
        height = self.height
        visited = bytearray(len(active_mask))
        components: list[Component] = []
        start = active_mask.find(1)
        while start != -1:
            if visited[start]:
                start = active_mask.find(1, start + 1)
                continue
            queue: deque[int] = deque([start])
            visited[start] = 1
            pixels: list[int] = []
            min_x = max_x = start % width
            min_y = max_y = start // width
            while queue:
                index = queue.popleft()
                pixels.append(index)
                x = index % width
                y = index // width
                min_x = min(min_x, x)
                max_x = max(max_x, x)
                min_y = min(min_y, y)
                max_y = max(max_y, y)
                for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                    if nx < 0 or ny < 0 or nx >= width or ny >= height:
                        continue
                    nindex = ny * width + nx
                    if active_mask[nindex] and not visited[nindex]:
                        visited[nindex] = 1
                        queue.append(nindex)
            if len(pixels) >= min_area_px:
                components.append(Component(tuple(pixels), min_x, min_y, max_x, max_y))
            start = active_mask.find(1, start + 1)
        return components


    def to_png_bytes(self, scale: int = 1) -> bytes:
        import struct, zlib

        scale = max(1, int(scale))
        out_w = max(1, ceil(self.width / scale))
        out_h = max(1, ceil(self.height / scale))
        raw = bytearray(out_h * (out_w + 1))
        for sy, spans in self.nonzero_rows():
            py = sy // scale
            out_row_start = py * (out_w + 1) + 1
            for span_start, span_end in spans:
                out_x0 = span_start // scale
                out_x1 = (span_end - 1) // scale
                raw[out_row_start + out_x0 : out_row_start + out_x1 + 1] = b"\xff" * (out_x1 - out_x0 + 1)
        compressed = zlib.compress(bytes(raw), 1)

        def chunk(tag: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", out_w, out_h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", compressed)
            + chunk(b"IEND", b"")
        )


def unsupported_components(
    current: LayerRaster,
    below: LayerRaster,
    radius: int,
    min_area_px: int,
) -> list[Component]:
    """Find unsupported 4-connected regions without dense layer-sized masks.

    ``below`` is square-dilated by ``radius`` conceptually.  The subtraction
    and component labelling operate directly on row spans, so full-resolution
    support checks do not require three dense pixel buffers per layer.
    """

    if current.width != below.width or current.height != below.height:
        raise ValueError("cannot compare rasters with different dimensions")
    if radius < 0:
        raise ValueError("support radius cannot be negative")
    min_area_px = max(1, int(min_area_px))

    below_rows = {y: spans for y, spans in below.nonzero_rows()}
    unsupported_rows: list[tuple[int, tuple[tuple[int, int], ...]]] = []
    for y, current_spans in current.nonzero_rows():
        supported: list[tuple[int, int]] = []
        for source_y in range(max(0, y - radius), min(current.height, y + radius + 1)):
            for x0, x1 in below_rows.get(source_y, ()):
                supported.append((max(0, x0 - radius), min(current.width, x1 + radius)))
        unsupported = _subtract_spans(current_spans, _merge_spans(supported))
        if unsupported:
            unsupported_rows.append((y, unsupported))

    if not unsupported_rows:
        return []

    retained: list[tuple[int, Component]] = []
    width = current.width
    for spans in _component_span_groups(unsupported_rows):
        area = sum(x1 - x0 for _y, x0, x1 in spans)
        if area < min_area_px:
            continue
        min_x = min(x0 for _y, x0, _x1 in spans)
        min_y = min(y for y, _x0, _x1 in spans)
        max_x = max(x1 - 1 for _y, _x0, x1 in spans)
        max_y = max(y for y, _x0, _x1 in spans)
        pixels = tuple(
            y * width + x
            for y, x0, x1 in spans
            for x in range(x0, x1)
        )
        retained.append((pixels[0], Component(pixels, min_x, min_y, max_x, max_y)))
    retained.sort(key=lambda item: item[0])
    return [component for _first, component in retained]


def detached_component_summaries(
    current: LayerRaster,
    below: LayerRaster,
    radius: int = 0,
    min_area_px: int = 1,
) -> list[ComponentSummary]:
    """Return current-layer components with no footprint on the layer below.

    Unlike overhang detection, a component is detached only when none of its
    pixels overlap the optionally dilated preceding layer.  This is suitable
    for a streaming final-layer island audit and does not allocate pixel lists.
    """

    detached, _deficits = layer_support_component_summaries(
        current,
        below,
        radius=radius,
        min_detached_area_px=min_area_px,
    )
    return detached


def footprint_deficit_component_summaries(
    current: LayerRaster,
    below: LayerRaster,
    radius: int = 0,
    min_unsupported_area_px: int = 1,
) -> list[FootprintDeficitSummary]:
    """Return whole components containing unsupported footprint pixels.

    This complements the stricter detached-component metric. A component can
    touch a few support tips yet still introduce a large unsupported base on
    its first layer; issue detectors commonly report the whole current-layer
    component in that situation.
    """

    _detached, deficits = layer_support_component_summaries(
        current,
        below,
        radius=radius,
        min_unsupported_area_px=min_unsupported_area_px,
    )
    return deficits


def layer_support_component_summaries(
    current: LayerRaster,
    below: LayerRaster,
    radius: int = 0,
    min_detached_area_px: int = 1,
    min_unsupported_area_px: int = 1,
) -> tuple[list[ComponentSummary], list[FootprintDeficitSummary]]:
    """Compute detached and footprint-deficit summaries in one span pass."""

    if current.width != below.width or current.height != below.height:
        raise ValueError("cannot compare rasters with different dimensions")
    if radius < 0:
        raise ValueError("support radius cannot be negative")
    min_detached_area_px = max(1, int(min_detached_area_px))
    min_unsupported_area_px = max(1, int(min_unsupported_area_px))
    below_rows = {y: spans for y, spans in below.nonzero_rows()}
    supported_cache: dict[int, tuple[tuple[int, int], ...]] = {}

    def supported_spans(y: int) -> tuple[tuple[int, int], ...]:
        cached = supported_cache.get(y)
        if cached is not None:
            return cached
        spans: list[tuple[int, int]] = []
        for source_y in range(max(0, y - radius), min(current.height, y + radius + 1)):
            for x0, x1 in below_rows.get(source_y, ()):
                spans.append((max(0, x0 - radius), min(current.width, x1 + radius)))
        merged = _merge_spans(spans)
        supported_cache[y] = merged
        return merged

    detached: list[tuple[int, ComponentSummary]] = []
    deficits: list[tuple[int, FootprintDeficitSummary]] = []
    for spans in _component_span_groups(list(current.nonzero_rows())):
        area = sum(x1 - x0 for _y, x0, x1 in spans)
        unsupported_area = 0
        for y, x0, x1 in spans:
            unsupported_area += sum(
                end - start
                for start, end in _subtract_spans(((x0, x1),), supported_spans(y))
            )
        min_x = min(x0 for _y, x0, _x1 in spans)
        min_y = min(y for y, _x0, _x1 in spans)
        max_x = max(x1 - 1 for _y, _x0, x1 in spans)
        max_y = max(y for y, _x0, _x1 in spans)
        first = min(y * current.width + x0 for y, x0, _x1 in spans)
        if unsupported_area == area and area >= min_detached_area_px:
            detached.append(
                (first, ComponentSummary(area, min_x, min_y, max_x, max_y))
            )
        if unsupported_area >= min_unsupported_area_px:
            deficits.append(
                (
                    first,
                    FootprintDeficitSummary(
                        area,
                        unsupported_area,
                        min_x,
                        min_y,
                        max_x,
                        max_y,
                    ),
                )
            )
    detached.sort(key=lambda item: item[0])
    deficits.sort(key=lambda item: item[0])
    return (
        [summary for _first, summary in detached],
        [summary for _first, summary in deficits],
    )


def _merge_spans(spans: Iterable[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    ordered = sorted(spans)
    if not ordered:
        return ()
    merged: list[tuple[int, int]] = []
    start, end = ordered[0]
    for span_start, span_end in ordered[1:]:
        if span_start <= end:
            end = max(end, span_end)
        else:
            merged.append((start, end))
            start, end = span_start, span_end
    merged.append((start, end))
    return tuple(merged)


def _subtract_spans(
    spans: Iterable[tuple[int, int]],
    covered: Iterable[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    covered_spans = tuple(covered)
    result: list[tuple[int, int]] = []
    covered_cursor = 0
    for start, end in spans:
        cursor = start
        while covered_cursor < len(covered_spans) and covered_spans[covered_cursor][1] <= cursor:
            covered_cursor += 1
        index = covered_cursor
        while index < len(covered_spans):
            cover_start, cover_end = covered_spans[index]
            if cover_start >= end:
                break
            if cover_start > cursor:
                result.append((cursor, min(end, cover_start)))
            cursor = max(cursor, cover_end)
            if cursor >= end:
                break
            index += 1
        if cursor < end:
            result.append((cursor, end))
    return tuple(result)


def _component_span_groups(
    rows: Iterable[tuple[int, Iterable[tuple[int, int]]]],
) -> list[list[tuple[int, int, int]]]:
    """Group horizontal runs with 4-connectivity using a compact union-find."""

    nodes: list[tuple[int, int, int]] = []
    parents: list[int] = []

    def find(node: int) -> int:
        root = node
        while parents[root] != root:
            root = parents[root]
        while parents[node] != node:
            parent = parents[node]
            parents[node] = root
            node = parent
        return root

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    previous_y = -2
    previous_nodes: list[int] = []
    for y, spans in rows:
        if y != previous_y + 1:
            previous_nodes = []
        row_nodes: list[int] = []
        previous_cursor = 0
        for x0, x1 in spans:
            node = len(nodes)
            nodes.append((y, x0, x1))
            parents.append(node)
            row_nodes.append(node)
            while (
                previous_cursor < len(previous_nodes)
                and nodes[previous_nodes[previous_cursor]][2] <= x0
            ):
                previous_cursor += 1
            cursor = previous_cursor
            while cursor < len(previous_nodes):
                _previous_y, previous_x0, previous_x1 = nodes[previous_nodes[cursor]]
                if previous_x0 >= x1:
                    break
                if previous_x1 > x0:
                    union(node, previous_nodes[cursor])
                cursor += 1
        previous_y = y
        previous_nodes = row_nodes

    grouped: dict[int, list[tuple[int, int, int]]] = {}
    for node, span in enumerate(nodes):
        grouped.setdefault(find(node), []).append(span)
    return list(grouped.values())


def dilate_mask(layer: LayerRaster, radius: int) -> bytearray:
    width = layer.width
    height = layer.height
    if radius <= 0:
        out = bytearray(width * height)
        for start, end in layer.nonzero_spans():
            out[start:end] = b"\x01" * (end - start)
        return out

    out = bytearray(width * height)
    for y, spans in layer.nonzero_rows():
        for span_start, span_end in spans:
            x0 = max(0, span_start - radius)
            x1 = min(width, span_end + radius)
            fill = b"\x01" * (x1 - x0)
            for yy in range(max(0, y - radius), min(height, y + radius + 1)):
                base = yy * width
                out[base + x0 : base + x1] = fill
    return out


def dilate_mask_dense(layer: LayerRaster, radius: int) -> bytearray:
    width = layer.width
    height = layer.height
    if radius <= 0:
        return layer.pixels.translate(_BINARY_TRANSLATION)

    horizontal = bytearray(width * height)
    for y in range(height):
        row_start = y * width
        prefix = [0] * (width + 1)
        for x in range(width):
            prefix[x + 1] = prefix[x] + (1 if layer.pixels[row_start + x] else 0)
        for x in range(width):
            left = max(0, x - radius)
            right = min(width, x + radius + 1)
            if prefix[right] - prefix[left] > 0:
                horizontal[row_start + x] = 1

    out = bytearray(width * height)
    counts = [0] * width
    for y in range(height):
        add_y = y + radius
        if add_y < height:
            base = add_y * width
            for x in range(width):
                counts[x] += horizontal[base + x]
        remove_y = y - radius - 1
        if remove_y >= 0:
            base = remove_y * width
            for x in range(width):
                counts[x] -= horizontal[base + x]
        base = y * width
        for x in range(width):
            if counts[x] > 0:
                out[base + x] = 1
    return out


def mm_to_px(mm: float, pixel_mm: float) -> int:
    return max(1, int(ceil(mm / pixel_mm)))
