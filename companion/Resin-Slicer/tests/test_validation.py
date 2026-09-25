import unittest

from resin_slicer.raster import (
    LayerRaster,
    detached_component_summaries,
    footprint_deficit_component_summaries,
)
from resin_slicer.validation import LayerStackValidator


class LayerValidationTests(unittest.TestCase):
    def test_detached_components_exclude_regions_attached_to_previous_layer(self) -> None:
        below = LayerRaster(20, 10)
        below.set_span(3, 2, 6)
        current = LayerRaster(20, 10)
        current.set_span(3, 3, 7)
        current.set_span(7, 12, 14)

        detached = detached_component_summaries(current, below)

        self.assertEqual(len(detached), 1)
        self.assertEqual(detached[0].area_px, 3)
        self.assertEqual(
            (detached[0].min_x, detached[0].min_y, detached[0].max_x, detached[0].max_y),
            (12, 7, 14, 7),
        )
        self.assertEqual(current.storage_mode, "sparse")
        self.assertEqual(below.storage_mode, "sparse")

        deficits = footprint_deficit_component_summaries(current, below)
        self.assertEqual([component.area_px for component in deficits], [5, 3])
        self.assertEqual(
            [component.unsupported_area_px for component in deficits],
            [1, 3],
        )

    def test_attachment_radius_can_accept_one_pixel_layer_shift(self) -> None:
        below = LayerRaster(10, 10)
        below.set_span(4, 4, 4)
        current = LayerRaster(10, 10)
        current.set_span(5, 5, 5)

        self.assertEqual(len(detached_component_summaries(current, below, radius=0)), 1)
        self.assertEqual(detached_component_summaries(current, below, radius=1), [])

    def test_streaming_audit_records_islands_and_empty_layer_gaps(self) -> None:
        layers = [LayerRaster(20, 10) for _ in range(5)]
        layers[0].set_span(3, 2, 6)
        layers[1].set_span(3, 3, 7)
        layers[1].set_span(7, 12, 13)
        # Layer 2 is an internal gap. Layer 3 therefore starts detached.
        layers[3].set_span(1, 1, 3)
        # Layer 4 is trailing empty space.
        validator = LayerStackValidator()
        for index, layer in enumerate(layers):
            validator.observe(index, layer)

        result = validator.result()

        self.assertEqual(result.layers_observed, 5)
        self.assertEqual(result.nonempty_layers, 3)
        self.assertEqual(result.first_nonempty_layer, 0)
        self.assertEqual(result.last_nonempty_layer, 3)
        self.assertEqual(result.leading_empty_layers, 0)
        self.assertEqual(result.internal_empty_layers, 1)
        self.assertEqual(result.trailing_empty_layers, 1)
        self.assertEqual(result.detached_component_count, 2)
        self.assertEqual(result.detached_pixel_count, 5)
        self.assertEqual(result.largest_detached_component_px, 3)
        self.assertEqual(result.layers_with_detached_components, (1, 3))
        self.assertEqual(result.footprint_deficit_component_count, 3)
        self.assertEqual(result.footprint_deficit_component_pixels, 10)
        self.assertEqual(result.unsupported_footprint_pixels, 6)
        self.assertEqual(result.largest_footprint_deficit_component_px, 5)
        self.assertEqual(result.layers_with_footprint_deficit_components, (1, 3))
        self.assertEqual(result.footprint_issue_record_limit, 1000)
        self.assertEqual(result.footprint_issue_records_truncated, 0)
        self.assertEqual(
            [
                (issue.layer_index, issue.component_area_px, issue.unsupported_area_px)
                for issue in result.footprint_deficit_issues
            ],
            [(1, 5, 1), (1, 2, 2), (3, 3, 3)],
        )

        bounded = LayerStackValidator(max_footprint_issue_records=1)
        for index, layer in enumerate(layers):
            bounded.observe(index, layer)
        bounded_result = bounded.result()
        self.assertEqual(len(bounded_result.footprint_deficit_issues), 1)
        self.assertEqual(bounded_result.footprint_issue_records_truncated, 2)

    def test_streaming_audit_rejects_nonconsecutive_indices(self) -> None:
        validator = LayerStackValidator()
        with self.assertRaisesRegex(ValueError, "consecutive indices"):
            validator.observe(1, LayerRaster(2, 2))


if __name__ == "__main__":
    unittest.main()
