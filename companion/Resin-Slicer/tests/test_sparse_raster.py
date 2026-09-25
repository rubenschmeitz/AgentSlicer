import random
import unittest
from math import ceil, cos, floor, pi, sin

from resin_slicer.config import profile
from resin_slicer.raster import LayerRaster, dilate_mask, unsupported_components
from resin_slicer.slicing import rasterize_segments


def _reference_rasterize(segments, config):
    """Pre-active-edge implementation retained as a correctness oracle."""

    width = config.resolution_x
    height = config.resolution_y
    pixel_x = config.pixel_size_x_mm
    pixel_y = config.pixel_size_y_mm
    raster = LayerRaster(width, height, bytearray(width * height))
    buckets = [[] for _ in range(height)]
    for segment in segments:
        (_, y1), (_, y2) = segment
        ymin = min(y1, y2)
        ymax = max(y1, y2)
        if ymax <= 0 or ymin >= config.size_y_mm:
            continue
        first = max(0, int(floor(ymin / pixel_y - 0.5)))
        last = min(height - 1, int(ceil(ymax / pixel_y - 0.5)))
        for y in range(first, last + 1):
            y_mm = (y + 0.5) * pixel_y
            if ymin <= y_mm < ymax:
                buckets[y].append(segment)

    for y, row_segments in enumerate(buckets):
        if not row_segments:
            continue
        y_mm = (y + 0.5) * pixel_y
        crossings = []
        for (x1, y1), (x2, y2) in row_segments:
            if y1 == y2:
                continue
            ymin = min(y1, y2)
            ymax = max(y1, y2)
            if not (ymin <= y_mm < ymax):
                continue
            t = (y_mm - y1) / (y2 - y1)
            crossings.append((x1 + t * (x2 - x1), 1 if y2 > y1 else -1))
        crossings.sort(key=lambda crossing: crossing[0])
        winding = 0
        span_start = None
        for x, sign in crossings:
            was_outside = winding == 0
            winding += sign
            if was_outside and winding != 0:
                span_start = x
            elif not was_outside and winding == 0 and span_start is not None:
                left = max(0.0, span_start)
                right = min(config.size_x_mm, x)
                span_start = None
                if right > left:
                    raster.set_span(
                        y,
                        int(ceil(left / pixel_x - 0.5)),
                        int(floor(right / pixel_x - 0.5)),
                    )
    return raster


class SparseRasterTests(unittest.TestCase):
    def test_mars_sized_layer_stays_sparse_through_rle_and_preview(self) -> None:
        layer = LayerRaster(4098, 2560)
        layer.set_span(100, 50, 500)
        layer.set_span(100, 400, 700)
        layer.set_span(101, 60, 80)

        self.assertEqual(layer.storage_mode, "sparse")
        self.assertEqual(layer.dense_buffer_bytes, 0)
        self.assertEqual(layer.count_on(), 672)
        self.assertGreater(len(list(layer.runs())), 1)
        self.assertTrue(layer.to_png_bytes(scale=16).startswith(b"\x89PNG"))
        self.assertEqual(layer.storage_mode, "sparse")
        self.assertEqual(layer.dense_buffer_bytes, 0)

        self.assertEqual(len(layer.pixels), 4098 * 2560)
        self.assertEqual(layer.storage_mode, "dense")
        self.assertEqual(layer.dense_buffer_bytes, 4098 * 2560)

    def test_sparse_copy_clear_and_union_preserve_binary_geometry(self) -> None:
        left = LayerRaster(20, 5)
        left.set_span(2, 2, 10)
        left.set_span(2, 5, 7, 0)
        right = LayerRaster(20, 5)
        right.set_span(2, 5, 12)

        copied = left.copy()
        copied.or_with(right)

        self.assertEqual(left.row_spans(2), ((2, 5), (8, 11)))
        self.assertEqual(copied.row_spans(2), ((2, 13),))
        self.assertEqual(left.storage_mode, "sparse")
        self.assertEqual(copied.storage_mode, "sparse")

    def test_span_components_match_dense_mask_reference(self) -> None:
        current = LayerRaster(30, 20)
        below = LayerRaster(30, 20)
        current.set_span(4, 2, 8)
        current.set_span(5, 2, 9)
        current.set_span(6, 15, 20)
        current.set_span(7, 14, 21)
        current.set_span(12, 0, 1)
        below.set_span(3, 3, 6)
        below.set_span(6, 16, 18)

        for radius in (0, 1, 2):
            with self.subTest(radius=radius):
                legacy_layer = current.copy()
                mask = dilate_mask(below.copy(), radius)
                active = legacy_layer.unsupported_mask(mask)
                expected = legacy_layer.connected_components(active, 2)
                actual = unsupported_components(current, below, radius, 2)

                normalize = lambda components: sorted(
                    (
                        tuple(sorted(component.pixels)),
                        component.min_x,
                        component.min_y,
                        component.max_x,
                        component.max_y,
                    )
                    for component in components
                )
                self.assertEqual(normalize(actual), normalize(expected))
                self.assertEqual(current.storage_mode, "sparse")
                self.assertEqual(below.storage_mode, "sparse")

    def test_active_edge_rasterizer_matches_previous_scanline_algorithm(self) -> None:
        config = profile("small-test")
        rng = random.Random(93017)
        for case in range(30):
            segments = []
            for _shape in range(1 + case % 4):
                center_x = rng.uniform(-5.0, config.size_x_mm + 5.0)
                center_y = rng.uniform(-5.0, config.size_y_mm + 5.0)
                radius_x = rng.uniform(0.2, 12.0)
                radius_y = rng.uniform(0.2, 12.0)
                sides = rng.randrange(3, 8)
                angle = rng.uniform(0.0, 2.0 * pi)
                points = tuple(
                    (
                        center_x + radius_x * cos(angle + index * 2.0 * pi / sides),
                        center_y + radius_y * sin(angle + index * 2.0 * pi / sides),
                    )
                    for index in range(sides)
                )
                segments.extend(zip(points, points[1:] + points[:1]))

            with self.subTest(case=case):
                actual = rasterize_segments(list(segments), config)
                expected = _reference_rasterize(list(segments), config)
                self.assertEqual(actual.pixels, expected.pixels)


if __name__ == "__main__":
    unittest.main()
