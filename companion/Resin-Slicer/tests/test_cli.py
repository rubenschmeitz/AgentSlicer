from __future__ import annotations

import json
import io
import tempfile
import unittest
from dataclasses import replace
from contextlib import redirect_stdout
from pathlib import Path

from resin_slicer.cli import _build_parser, _write_report_json
from resin_slicer.pipeline import SliceResult
from resin_slicer.validation import (
    FootprintDeficitIssue,
    LayerStackValidation,
    LayerValidationParity,
    PostEncodeValidation,
)


class CliTests(unittest.TestCase):
    def test_version_flag_is_available_without_slice_arguments(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            _build_parser().parse_args(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertRegex(output.getvalue(), r"(?:\d+\.\d+\.\d+|source)\n$")

    def test_repeated_manual_support_points_are_parsed(self) -> None:
        args = _build_parser().parse_args(
            [
                "model.stl",
                "--output",
                "model.ctb",
                "--manual-support",
                "10.5x20x5.25",
                "--manual-support",
                "11x21x6",
                "--require-manual-supports",
                "--require-auto-supports",
                "--no-tree-branching",
                "--tree-merge-distance",
                "3.5",
                "--tree-max-tips",
                "9",
                "--tree-trunk-radius",
                "0.7",
                "--pedestal-height",
                "1.8",
                "--support-plan-cache-dir",
                "/tmp/resin-support-cache",
            ]
        )
        self.assertEqual(
            args.manual_support,
            [(10.5, 20.0, 5.25), (11.0, 21.0, 6.0)],
        )
        self.assertTrue(args.require_manual_supports)
        self.assertTrue(args.require_auto_supports)
        self.assertTrue(args.no_tree_branching)
        self.assertEqual(args.tree_merge_distance, 3.5)
        self.assertEqual(args.tree_max_tips, 9)
        self.assertEqual(args.tree_trunk_radius, 0.7)
        self.assertEqual(args.pedestal_height, 1.8)
        self.assertEqual(args.support_plan_cache_dir, "/tmp/resin-support-cache")

    def test_slice_report_lists_exact_unrouted_manual_points(self) -> None:
        result = SliceResult(
            layer_count=100,
            support_count=12,
            material_ml=3.25,
            output_path=Path("model.ctb"),
            manual_supports_requested=2,
            manual_supports_routed=1,
            manual_supports_routed_points=((1.0, 2.0, 3.0),),
            manual_supports_actual_contact_points=((1.1, 2.0, 3.0),),
            manual_supports_unrouted_points=((4.0, 5.0, 6.0),),
            auto_components_detected=5,
            auto_components_supported=4,
            auto_components_unrouted=1,
            auto_candidates_attempted=8,
            auto_candidates_routed=7,
            auto_candidates_unrouted=1,
            auto_unrouted_area_mm2=0.125,
            tree_root_count=3,
            tree_branch_count=9,
            max_tree_load=7,
            encoded_layer_bytes=1234,
            max_encoded_layer_bytes=42,
            output_storage_mode="disk-spooled-atomic",
            layer_validation=LayerStackValidation(
                mode="pre-encode-native-raster-v1",
                layers_observed=100,
                nonempty_layers=99,
                first_nonempty_layer=0,
                last_nonempty_layer=99,
                leading_empty_layers=0,
                internal_empty_layers=1,
                trailing_empty_layers=0,
                detached_component_count=2,
                detached_pixel_count=7,
                largest_detached_component_px=5,
                layers_with_detached_components=(20, 70),
                footprint_deficit_component_count=3,
                footprint_deficit_component_pixels=20,
                unsupported_footprint_pixels=12,
                largest_footprint_deficit_component_px=10,
                layers_with_footprint_deficit_components=(20, 50, 70),
                footprint_issue_record_limit=1000,
                footprint_issue_records_truncated=0,
                footprint_deficit_issues=(
                    FootprintDeficitIssue(20, 10, 5, 1, 2, 5, 6),
                    FootprintDeficitIssue(50, 6, 4, 7, 8, 9, 10),
                    FootprintDeficitIssue(70, 4, 3, 2, 3, 3, 4),
                ),
            ),
        )
        result = replace(
            result,
            post_encode_validation=PostEncodeValidation(
                format="ctb",
                format_version="5",
                resolution_x=4098,
                resolution_y=2560,
                declared_layer_count=100,
                file_size=12345,
                decoded_layer_bytes=1234,
                integrity_checks=("encrypted-signature", "trailer"),
                layer_validation=replace(
                    result.layer_validation,
                    mode="post-encode-native-decoded-ctb-v1",
                ),
            ),
            encode_validation_parity=LayerValidationParity(True, ()),
        )
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "receipt.json"
            _write_report_json(report, result, "ctb")
            payload = json.loads(report.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema"], "resin-slicer-slice-report-v1")
        self.assertEqual(payload["manual_supports"]["routed_points_mm"], [[1.0, 2.0, 3.0]])
        self.assertEqual(payload["manual_supports"]["actual_contact_points_mm"], [[1.1, 2.0, 3.0]])
        self.assertEqual(payload["manual_supports"]["unrouted_points_mm"], [[4.0, 5.0, 6.0]])
        self.assertEqual(payload["automatic_supports"]["components_unrouted"], 1)
        self.assertEqual(payload["automatic_supports"]["unrouted_area_mm2"], 0.125)
        self.assertEqual(
            payload["support_tree"],
            {"roots": 3, "branches": 9, "max_tip_load": 7},
        )
        self.assertEqual(payload["output_writer"]["mode"], "disk-spooled-atomic")
        self.assertEqual(payload["support_plan_cache"]["status"], "disabled")
        self.assertEqual(payload["performance"]["pipeline_total_seconds"], 0.0)
        self.assertEqual(payload["internal_layer_validation"]["detached_component_count"], 2)
        self.assertEqual(
            payload["internal_layer_validation"]["layers_with_detached_components_zero_based"],
            [20, 70],
        )
        self.assertEqual(
            payload["internal_layer_validation"]["footprint_deficit_component_count"],
            3,
        )
        self.assertEqual(
            payload["internal_layer_validation"]["footprint_deficit_issues"][0],
            {
                "layer_index_zero_based": 20,
                "component_area_px": 10,
                "unsupported_area_px": 5,
                "bounds_px": {"min_x": 1, "min_y": 2, "max_x": 5, "max_y": 6},
            },
        )
        self.assertEqual(payload["post_encode_validation"]["format_version"], "5")
        self.assertEqual(
            payload["post_encode_validation"]["layer_validation"]["mode"],
            "post-encode-native-decoded-ctb-v1",
        )
        self.assertEqual(
            payload["encode_validation_parity"],
            {"matches": True, "differing_fields": []},
        )


if __name__ == "__main__":
    unittest.main()
