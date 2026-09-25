from __future__ import annotations

import tempfile
import unittest
import subprocess
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_slicer.backends import ToolInfo
from agent_slicer.pipeline import (
    _commit_simplification_results,
    _expected_preparation_request,
    _find_cached_preparation,
    _find_cached_simplification,
    _embedded_analysis_for_sha,
    _resume_targeted_support_points,
    _resin_slicer_implementation_identity,
    _resin_slice_timeout_seconds,
    _slice_resin,
    _support_variants,
    _targeted_support_points,
    _validate_automatic_support_receipt,
    _validate_internal_layer_validation_receipt,
    _validate_post_encode_validation_receipt,
    _validate_slice_performance_receipt,
    _validate_support_tree_receipt,
    _validated_receipt_points,
    _write_markdown_report,
    build_project,
    compile_plan,
)
from agent_slicer.models import SourceModel
from agent_slicer.presets import resolve_presets
from agent_slicer.project import add_models, init_project, sha256_file
from agent_slicer.uvtools_contract import DETECTION_CONTRACT, SCAN_FLAGS
from tests.helpers import write_cube


class PipelineTests(unittest.TestCase):
    def test_internal_layer_validation_receipt_must_be_self_consistent(self) -> None:
        valid = {
            "mode": "pre-encode-native-raster-v1",
            "scope": "pre-encode raster stream",
            "attachment_radius_px": 2,
            "footprint_min_unsupported_area_px": 16,
            "ignored_initial_layers": 2,
            "layers_observed": 10,
            "nonempty_layers": 8,
            "first_nonempty_layer_zero_based": 1,
            "last_nonempty_layer_zero_based": 8,
            "leading_empty_layers": 1,
            "internal_empty_layers": 0,
            "trailing_empty_layers": 1,
            "detached_component_count": 2,
            "detached_pixel_count": 9,
            "largest_detached_component_px": 6,
            "layers_with_detached_components_zero_based": [3, 7],
            "footprint_deficit_component_count": 3,
            "footprint_deficit_component_pixels": 20,
            "unsupported_footprint_pixels": 12,
            "largest_footprint_deficit_component_px": 10,
            "layers_with_footprint_deficit_components_zero_based": [3, 5, 7],
            "footprint_issue_record_limit": 1000,
            "footprint_issue_records_truncated": 0,
            "footprint_deficit_issues": [
                {
                    "layer_index_zero_based": 3,
                    "component_area_px": 10,
                    "unsupported_area_px": 6,
                    "bounds_px": {"min_x": 1, "min_y": 2, "max_x": 5, "max_y": 6},
                },
                {
                    "layer_index_zero_based": 5,
                    "component_area_px": 6,
                    "unsupported_area_px": 4,
                    "bounds_px": {"min_x": 7, "min_y": 8, "max_x": 9, "max_y": 10},
                },
                {
                    "layer_index_zero_based": 7,
                    "component_area_px": 4,
                    "unsupported_area_px": 2,
                    "bounds_px": {"min_x": 2, "min_y": 3, "max_x": 3, "max_y": 4},
                },
            ],
        }
        self.assertEqual(_validate_internal_layer_validation_receipt(valid), valid)
        self.assertEqual(
            _validate_internal_layer_validation_receipt(None),
            {"status": "unavailable"},
        )

        with self.assertRaisesRegex(RuntimeError, "layer counts"):
            _validate_internal_layer_validation_receipt(
                {**valid, "trailing_empty_layers": 2}
            )
        with self.assertRaisesRegex(RuntimeError, "detached-layer"):
            _validate_internal_layer_validation_receipt(
                {**valid, "layers_with_detached_components_zero_based": [7, 3]}
            )
        with self.assertRaisesRegex(RuntimeError, "footprint-deficit"):
            _validate_internal_layer_validation_receipt(
                {**valid, "unsupported_footprint_pixels": 21}
            )

        decoded = {
            **valid,
            "mode": "post-encode-native-decoded-ctb-v1",
            "scope": "decoded saved-file layer stream",
        }
        post = {
            "format": "ctb",
            "format_version": "5",
            "resolution": [4098, 2560],
            "declared_layer_count": 10,
            "file_size_bytes": 12345,
            "decoded_layer_bytes": 2345,
            "integrity_checks": ["bounded-sections", "encrypted-signature"],
            "layer_validation": decoded,
        }
        verified = _validate_post_encode_validation_receipt(
            post,
            {"matches": True, "differing_fields": []},
            valid,
        )
        self.assertEqual(verified["status"], "verified-parity")
        self.assertFalse(verified["production_authority"])
        with self.assertRaisesRegex(RuntimeError, "structural validation differs"):
            _validate_post_encode_validation_receipt(
                post,
                {"matches": False, "differing_fields": ["nonempty_layers"]},
                valid,
            )

    def test_resin_slice_timeout_has_production_headroom_and_bounded_override(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_resin_slice_timeout_seconds(), 3 * 60 * 60)
        with patch.dict(
            "os.environ",
            {"AGENT_SLICER_RESIN_SLICE_TIMEOUT_SECONDS": "7200"},
            clear=True,
        ):
            self.assertEqual(_resin_slice_timeout_seconds(), 7200)
        for value in ("invalid", "59", "86401"):
            with self.subTest(value=value), patch.dict(
                "os.environ",
                {"AGENT_SLICER_RESIN_SLICE_TIMEOUT_SECONDS": value},
                clear=True,
            ), self.assertRaises(ValueError):
                _resin_slice_timeout_seconds()

    def test_automatic_support_receipt_must_balance(self) -> None:
        valid = {
            "components_detected": 4,
            "components_supported": 3,
            "components_excluded": 1,
            "components_unrouted": 0,
            "candidates_attempted": 7,
            "candidates_routed": 5,
            "candidates_unrouted": 2,
            "unrouted_area_mm2": 0.0,
        }
        _validate_automatic_support_receipt(valid)

        invalid = dict(valid, components_supported=2)
        with self.assertRaisesRegex(RuntimeError, "component accounting"):
            _validate_automatic_support_receipt(invalid)

        invalid = dict(valid, candidates_unrouted=1)
        with self.assertRaisesRegex(RuntimeError, "candidate accounting"):
            _validate_automatic_support_receipt(invalid)

    def test_support_tree_receipt_must_balance_and_respect_load_cap(self) -> None:
        valid = {"roots": 2, "branches": 4, "max_tip_load": 3}
        _validate_support_tree_receipt(
            valid,
            support_count=6,
            maximum_tip_load=12,
        )
        for invalid in (
            {"roots": 2, "branches": 3, "max_tip_load": 3},
            {"roots": 2, "branches": 4, "max_tip_load": 13},
            {"roots": True, "branches": 4, "max_tip_load": 3},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                _validate_support_tree_receipt(
                    invalid,
                    support_count=6,
                    maximum_tip_load=12,
                )

    def test_support_receipt_coordinates_must_be_finite_xyz_points(self) -> None:
        self.assertEqual(
            _validated_receipt_points([[1, 2.5, 3]], "routed"),
            [[1.0, 2.5, 3.0]],
        )
        for invalid in ([[1, 2]], [[1, 2, float("nan")]], "not-a-list"):
            with self.subTest(payload=invalid):
                with self.assertRaisesRegex(RuntimeError, "coordinates"):
                    _validated_receipt_points(invalid, "routed")

    def test_slice_phase_timings_must_fit_inside_total(self) -> None:
        valid = {
            "preparation_seconds": 1.0,
            "support_planning_seconds": 2.0,
            "output_writing_seconds": 3.0,
            "pipeline_total_seconds": 6.1,
        }
        self.assertEqual(
            _validate_slice_performance_receipt(valid, required=True),
            valid,
        )
        invalid = dict(valid, pipeline_total_seconds=5.5)
        with self.assertRaisesRegex(RuntimeError, "phase timings"):
            _validate_slice_performance_receipt(invalid, required=True)

    def test_build_report_creates_missing_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "missing" / "build-report.md"
            plan = SimpleNamespace(
                project=str(Path(temporary) / "project"),
                resolved_config={
                    "purpose_id": "test-smoke",
                    "machine_id": "test-small",
                    "material_id": "standard-test",
                },
            )
            _write_markdown_report(
                destination,
                {"status": "prepared", "plate": "plate.stl"},
                plan,
            )
            self.assertTrue(destination.is_file())

    def test_resin_backend_identity_hashes_installed_package_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "bin" / "resin-slicer"
            package = root / "lib" / "python3.12" / "site-packages" / "resin_slicer"
            executable.parent.mkdir(parents=True)
            package.mkdir(parents=True)
            executable.write_text("entrypoint", encoding="utf-8")
            source = package / "pipeline.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")

            first = _resin_slicer_implementation_identity(executable)
            assert first is not None
            source.write_text("VALUE = 2\n", encoding="utf-8")
            second = _resin_slicer_implementation_identity(executable)
            assert second is not None

        self.assertEqual(first["file_count"], 1)
        self.assertNotEqual(first["sha256"], second["sha256"])

    def test_resin_backend_identity_resolves_editable_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "bin" / "resin-slicer"
            interpreter = root / "bin" / "python"
            package = root / "checkout" / "resin_slicer"
            executable.parent.mkdir(parents=True)
            package.mkdir(parents=True)
            executable.write_text("entrypoint", encoding="utf-8")
            interpreter.write_text("interpreter", encoding="utf-8")
            (package / "pipeline.py").write_text("VALUE = 1\n", encoding="utf-8")
            located = subprocess.CompletedProcess(
                [str(interpreter)], 0, stdout=f"{package}\n", stderr=""
            )
            with patch("agent_slicer.pipeline.subprocess.run", return_value=located):
                identity = _resin_slicer_implementation_identity(executable)

        assert identity is not None
        self.assertEqual(identity["package_path"], str(package.resolve()))
        self.assertEqual(identity["file_count"], 1)

    def test_cached_preparation_requires_exact_hash_settings_and_topology(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.stl"
            write_cube(source)
            source_hash = sha256_file(source)
            cache = root / "artifacts" / "prepared"
            cache.mkdir(parents=True)
            mesh = cache / "demo-cached.stl"
            write_cube(mesh)
            arguments = {
                "target_height_mm": 50.0,
                "voxel_mm": 0.12,
                "max_p95_deviation_mm": 0.2,
                "max_deviation_mm": 1.0,
                "wall_mm": 1.8,
                "hollow_voxel_mm": 0.15,
                "hollow_max_p95_change_mm": 0.2,
                "hollow_max_change_mm": 0.6,
                "drain_diameter_mm": 3.5,
                "drain_count": 2,
                "max_tilt_deg": 20.0,
                "orient": True,
                "hollow": True,
                "drain": True,
            }
            report = cache / "demo-cached.json"
            report.write_text(
                json.dumps(
                    {
                        "schema": "agent-slicer-resin-preparation/v3",
                        "status": "passed",
                        "export_validation": {"status": "passed"},
                        "source_sha256": source_hash,
                        "output_sha256": sha256_file(mesh),
                        "requested": _expected_preparation_request(arguments),
                        "stages": {
                            "repair_and_orientation": {"status": "passed"},
                            "hollow_and_drain": {
                                "status": "passed",
                                "outer_surface_preserved": True,
                                "sealed_cavity_voxels": 0,
                                "drain_holes": [
                                    {"independently_connects_cavity": True, "mesh_independently_connects_cavity": True},
                                    {"independently_connects_cavity": True, "mesh_independently_connects_cavity": True},
                                ],
                                "mesh_validation": {
                                    "watertight": True,
                                    "winding_consistent": True,
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            model = SourceModel(
                id="demo",
                source="source.stl",
                sha256=source_hash,
                target_height_mm=50.0,
            )
            self.assertIsNotNone(_find_cached_preparation(root, model, arguments))
            current = json.loads(report.read_text(encoding="utf-8"))
            old = dict(current, schema="agent-slicer-resin-preparation/v2")
            report.write_text(json.dumps(old), encoding="utf-8")
            self.assertIsNone(_find_cached_preparation(root, model, arguments))
            report.write_text(json.dumps(current), encoding="utf-8")
            changed = dict(arguments, wall_mm=2.0)
            self.assertIsNone(_find_cached_preparation(root, model, changed))

    def test_committed_simplification_report_points_to_reusable_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.stl"
            simplified = root / "simplified.stl"
            transient_report = root / "transient.json"
            write_cube(source, 10.0)
            write_cube(simplified, 9.0)
            source_hash = sha256_file(source)
            output_hash = sha256_file(simplified)
            settings = {
                "minimum_input_triangles": 500_000,
                "target_triangles": 350_000,
                "max_p95_deviation_mm": 0.035,
                "max_deviation_mm": 0.1,
                "max_volume_change_ratio": 0.005,
                "sample_points": 20_000,
                "aggression": 5,
                "protected_component_triangles": 5_000,
            }
            transient_report.write_text(
                json.dumps(
                    {
                        "schema": "agent-slicer-mesh-simplification/v1",
                        "status": "passed",
                        "applied": True,
                        "source_sha256": source_hash,
                        "output_sha256": output_hash,
                        "output": str(root / "deleted-transaction.stl"),
                        "requested": settings,
                    }
                ),
                encoding="utf-8",
            )
            committed = _commit_simplification_results(
                root / "artifacts",
                [
                    {
                        "model_id": "demo",
                        "source": str(source),
                        "source_sha256": source_hash,
                        "simplified": str(simplified),
                        "simplified_sha256": output_hash,
                        "report": str(transient_report),
                        "result": {"reduction_ratio": 0.5},
                        "applied": True,
                    }
                ],
            )

            saved_report = Path(committed[0]["report"])
            payload = json.loads(saved_report.read_text(encoding="utf-8"))
            self.assertEqual(payload["output"], committed[0]["simplified"])
            model = SourceModel(
                id="demo",
                source=str(source),
                sha256=source_hash,
                target_height_mm=10.0,
            )
            self.assertIsNotNone(_find_cached_simplification(root, model, settings))

            retained_source = root / "retained-source.stl"
            retained_report = root / "retained-transient.json"
            write_cube(retained_source, 8.0)
            retained_hash = sha256_file(retained_source)
            retained_report.write_text(
                json.dumps(
                    {
                        "schema": "agent-slicer-mesh-simplification/v1",
                        "status": "passed",
                        "applied": False,
                        "decision": "no-safe-candidate-original-retained",
                        "source_sha256": retained_hash,
                        "output_sha256": retained_hash,
                        "requested": settings,
                    }
                ),
                encoding="utf-8",
            )
            _commit_simplification_results(
                root / "artifacts",
                [
                    {
                        "model_id": "retained",
                        "source": str(retained_source),
                        "source_sha256": retained_hash,
                        "simplified": None,
                        "simplified_sha256": None,
                        "report": str(retained_report),
                        "result": {"reduction_ratio": 0.0},
                        "applied": False,
                    }
                ],
            )
            retained_model = SourceModel(
                id="retained",
                source=str(retained_source),
                sha256=retained_hash,
                target_height_mm=8.0,
            )
            retained_cache = _find_cached_simplification(
                root,
                retained_model,
                settings,
            )
            self.assertIsNotNone(retained_cache)
            assert retained_cache is not None
            self.assertIsNone(retained_cache[0])

    def test_support_variants_reserve_final_feedback_passes(self) -> None:
        variants = _support_variants(
            {
                "spacing_mm": 2.0,
                "feedback_passes": 2,
                "feedback_contact_spacing_mm": 0.24,
                "refinement_passes": [{"spacing_mm": 1.0}],
            }
        )
        self.assertEqual([item["spacing_mm"] for item in variants], [2.0, 1.0, 1.0, 1.0])
        self.assertTrue(
            all(
                "feedback_passes" not in item
                and "feedback_contact_spacing_mm" not in item
                and "refinement_passes" not in item
                for item in variants
            )
        )

    def test_slice_adapter_forwards_production_support_controls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "resin-slicer"
            executable.write_text("test executable", encoding="utf-8")
            tools = {
                "resin-slicer": ToolInfo(
                    id="resin-slicer",
                    path=str(executable),
                    discovered=True,
                    implemented_capabilities=("resin.slice",),
                    planned_capabilities=(),
                )
            }
            resolved = resolve_presets(
                process="resin",
                machine="elegoo-mars-3-pro",
                material="anycubic-water-washable-2-grey",
                purpose="miniature-display-high-detail",
                quality="high-detail",
                output_format="ctb",
            )
            def completed_with_receipt(command, **_kwargs):
                receipt_path = Path(command[command.index("--report-json") + 1])
                receipt_path.write_text(
                    json.dumps(
                        {
                            "schema": "resin-slicer-slice-report-v1",
                            "support_count": 6,
                            "automatic_supports": {
                                "components_detected": 5,
                                "components_supported": 4,
                                "components_excluded": 0,
                                "components_unrouted": 1,
                                "candidates_attempted": 8,
                                "candidates_routed": 6,
                                "candidates_unrouted": 2,
                                "unrouted_area_mm2": 0.1,
                            },
                            "support_tree": {
                                "roots": 2,
                                "branches": 4,
                                "max_tip_load": 3,
                            },
                            "support_plan_cache": {"status": "miss"},
                            "performance": {
                                "preparation_seconds": 1.0,
                                "support_planning_seconds": 2.0,
                                "output_writing_seconds": 3.0,
                                "pipeline_total_seconds": 6.0,
                            },
                            "manual_supports": {
                                "requested": 1,
                                "routed": 1,
                                "routed_points_mm": [[70.0, 44.0, 5.0]],
                                "actual_contact_points_mm": [[70.1, 44.0, 5.0]],
                                "unrouted_points_mm": [],
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(
                    args=command, returncode=0, stdout="wrote test.ctb", stderr=""
                )
            with (
                patch("agent_slicer.pipeline.discover_tools", return_value=tools),
                patch("agent_slicer.pipeline.run_backend", side_effect=completed_with_receipt),
            ):
                result = _slice_resin(
                    root / "plate.stl",
                    root / "output.ctb",
                    resolved,
                    manual_support_points=[(70.0, 44.0, 5.0)],
                    require_manual_supports=True,
                    support_plan_cache_dir=root / "support-cache",
                )
            command = result["command"]
            for flag in (
                "--primary-supports",
                "--tip-type",
                "--raft-chamfer-width",
                "--brace-radius",
                "--collision-clearance",
                "--max-support-angle",
                "--tree-merge-distance",
                "--tree-max-tips",
                "--tree-trunk-radius",
                "--pedestal-height",
                "--enforcers",
            ):
                self.assertIn(flag, command)
            self.assertIn("--manual-support", command)
            self.assertIn("--report-json", command)
            self.assertIn("--require-manual-supports", command)
            self.assertIn("--support-plan-cache-dir", command)
            self.assertNotIn("--require-auto-supports", command)
            self.assertIn("70.000000x44.000000x5.000000", command)
            self.assertEqual(result["manual_supports_routed_points"], [[70.0, 44.0, 5.0]])
            self.assertEqual(
                result["manual_supports_actual_contact_points"],
                [[70.1, 44.0, 5.0]],
            )
            self.assertEqual(result["manual_supports_unrouted_points"], [])
            self.assertEqual(
                result["automatic_supports"]["components_unrouted"], 1
            )
            self.assertTrue(result["automatic_supports_need_encoded_validation"])
            self.assertEqual(
                result["slice_report_payload"]["support_tree"],
                {"roots": 2, "branches": 4, "max_tip_load": 3},
            )
            self.assertEqual(result["support_tree"]["branches"], 4)
            self.assertEqual(result["support_plan_cache"]["status"], "miss")
            self.assertEqual(result["performance"]["pipeline_total_seconds"], 6.0)

    def test_targeted_support_points_expand_large_actionable_island(self) -> None:
        resolved = resolve_presets(
            process="resin",
            machine="elegoo-mars-3-pro",
            material="anycubic-water-washable-2-grey",
            purpose="miniature-display-high-detail",
            quality="high-detail",
            output_format="ctb",
        )
        validation = {
            "checks": [
                {
                    "id": "uvtools.issue.islands",
                    "passed": False,
                    "largest_actionable_records": [
                        {
                            "first_layer": 100,
                            "area_mm2": 1.0,
                            "bounds_pixels": {
                                "x": 2000,
                                "y": 1200,
                                "width": 100,
                                "height": 80,
                            },
                        }
                    ],
                }
            ]
        }
        points = _targeted_support_points(validation, resolved)
        self.assertEqual(len(points), 5)
        pixel_x = 143.43 / 4098
        pixel_y = 89.6 / 2560
        self.assertAlmostEqual(points[0][0], (2050.5) * pixel_x)
        # UVtools reports the decoded CTB raster row. Resin-Slicer serializes
        # that row order verbatim, so applying an additional image-origin flip
        # would mirror the requested support onto the other side of the plate.
        self.assertAlmostEqual(points[0][1], 1240.5 * pixel_y)
        for point in points:
            self.assertAlmostEqual(point[2], 3.015)

    def test_embedded_resume_analysis_requires_current_contract_and_exact_ctb_hash(self) -> None:
        analysis = {
            "schema": "agent-slicer-uvtools-analysis/v4",
            "detection_contract": DETECTION_CONTRACT,
            "scan_flags": SCAN_FLAGS,
            "source_sha256": "abc123",
            "status": "completed",
        }
        manifest = {"production_validation": {"analysis": analysis}}
        self.assertIs(_embedded_analysis_for_sha(manifest, "abc123"), analysis)
        self.assertIsNone(_embedded_analysis_for_sha(manifest, "different"))
        for change in (
            {"schema": "agent-slicer-uvtools-analysis/v3"},
            {"detection_contract": None},
            {"scan_flags": {**SCAN_FLAGS, "suction_cups": ["--suction-cups"]}},
        ):
            with self.subTest(change=change):
                self.assertIsNone(_embedded_analysis_for_sha(
                    {"production_validation": {"analysis": {**analysis, **change}}},
                    "abc123",
                ))
        self.assertIsNone(
            _embedded_analysis_for_sha(
                {
                    "production_validation": {
                        "analysis": {**analysis, "schema": "agent-slicer-uvtools-analysis/v2"}
                    }
                },
                "abc123",
            )
        )

    def test_resume_carries_only_verified_native_raster_contacts(self) -> None:
        resolved = resolve_presets(
            process="resin",
            machine="elegoo-mars-3-pro",
            material="anycubic-water-washable-2-grey",
            purpose="miniature-display-high-detail",
            quality="high-detail",
            output_format="ctb",
        )
        validation = {
            "checks": [
                {
                    "id": "uvtools.issue.islands",
                    "passed": False,
                    "largest_actionable_records": [
                        {
                            "first_layer": 100,
                            "area_mm2": 0.03,
                            "bounds_pixels": {"x": 20, "y": 30, "width": 2, "height": 2},
                            "suggested_contact_pixels": [[20, 30]],
                        }
                    ],
                }
            ]
        }
        expected = _targeted_support_points(validation, resolved)
        inferred = {
            "slice_attempts": [
                {"targeted_support_points": [], "production_validation": validation},
                {"targeted_support_points": expected},
            ]
        }
        self.assertEqual(
            _resume_targeted_support_points(inferred, resolved),
            expected,
        )
        mirrored = [[x, 89.6 - y, z] for x, y, z in expected]
        inferred["slice_attempts"][1]["targeted_support_points"] = mirrored
        self.assertEqual(_resume_targeted_support_points(inferred, resolved), [])

        explicit = {
            "feedback_coordinate_system": "ctb-raster-native-v1",
            "slice_attempts": [{"targeted_support_points": expected}],
        }
        self.assertEqual(
            _resume_targeted_support_points(explicit, resolved),
            expected,
        )

    def test_native_prepare_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source.stl"
            write_cube(source, 10.0)
            project = init_project(
                base / "project",
                name="smoke",
                process="resin",
                machine="test-small",
                material="standard-test",
                purpose="test-smoke",
            )
            add_models(project, [source])
            _, plan = compile_plan(project)
            self.assertEqual(plan.blockers, [])
            result = build_project(project, skip_slice=True)
            self.assertEqual(result["status"], "prepared")
            self.assertTrue(Path(result["plate"]).is_file())

    def test_high_detail_preset_is_ready_when_production_capabilities_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source.stl"
            write_cube(source, 10.0)
            project = init_project(
                base / "project",
                name="professional",
                process="resin",
                machine="elegoo-mars-3-pro",
                material="ifun-if3121-toughness-grey",
                purpose="miniature-display-high-detail",
                quality="high-detail",
            )
            add_models(project, [source])
            capabilities = {
                "mesh.inspect", "mesh.clean.degenerate", "mesh.scale",
                "mesh.simplify.measured",
                "mesh.repair.safe", "mesh.orient.resin", "mesh.hollow",
                "mesh.drain.auto", "mesh.cavity.validate", "plate.pack.shelf",
                "plate.pack.support-aware", "mesh.combine", "resin.support.generate",
                "resin.slice", "resin.validate.basic", "resin.validate.production",
            }
            with patch("agent_slicer.pipeline.available_capabilities", return_value=capabilities):
                _, plan = compile_plan(project)
            self.assertEqual(plan.blockers, [])
            required = set(plan.required_capabilities)
            self.assertIn("mesh.hollow", required)
            self.assertNotIn("mesh.simplify.measured", required)
            self.assertIn("mesh.cavity.validate", required)
            self.assertIn("resin.validate.production", required)

    def test_high_detail_preset_still_fails_closed_when_backend_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source.stl"
            write_cube(source, 10.0)
            project = init_project(
                base / "project",
                name="professional",
                process="resin",
                machine="elegoo-mars-3-pro",
                material="ifun-if3121-toughness-grey",
                purpose="miniature-display-high-detail",
                quality="high-detail",
            )
            add_models(project, [source])
            with patch("agent_slicer.pipeline.available_capabilities", return_value=set()):
                _, plan = compile_plan(project)
            missing = {
                blocker.get("capability")
                for blocker in plan.blockers
                if blocker["code"] == "CAPABILITY_UNAVAILABLE"
            }
            self.assertIn("mesh.hollow", missing)
            self.assertIn("mesh.drain.auto", missing)
            self.assertIn("resin.validate.production", missing)

    def test_p2s_plan_requests_fdm_capabilities_without_resin_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source.stl"
            write_cube(source, 10.0)
            project = init_project(
                base / "project",
                name="p2s-profile-check",
                process="fdm",
                machine="bambu-lab-p2s-0.4",
                material="bambu-pla-basic-p2s",
                purpose="fdm-general",
                quality="p2s-0.20-standard",
                output_format="gcode.3mf",
            )
            add_models(project, [source])
            with patch("agent_slicer.pipeline.available_capabilities", return_value=set()):
                _, plan = compile_plan(project)
            required = set(plan.required_capabilities)
            self.assertIn("fdm.slice", required)
            self.assertIn("fdm.validate.basic", required)
            self.assertNotIn("resin.slice", required)
            self.assertNotIn("resin.validate.basic", required)


if __name__ == "__main__":
    unittest.main()
