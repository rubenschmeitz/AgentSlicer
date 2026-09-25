from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import trimesh

from agent_slicer.mesh_simplify import simplify_mesh_measured
from agent_slicer.project import sha256_file


class MeshSimplificationTests(unittest.TestCase):
    def test_small_mesh_is_copied_without_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.stl"
            output = root / "output.stl"
            report = root / "report.json"
            trimesh.creation.box(extents=(10, 10, 10)).export(source)

            result = simplify_mesh_measured(
                source,
                output,
                report,
                minimum_input_triangles=100,
                target_triangles=50,
            )

            self.assertFalse(result["applied"])
            self.assertEqual(result["decision"], "below-threshold-original-retained")
            self.assertEqual(sha256_file(source), sha256_file(output))
            self.assertEqual(json.loads(report.read_text())["status"], "passed")

    def test_quadric_reduction_preserves_closed_sphere_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.stl"
            output = root / "output.stl"
            report = root / "report.json"
            trimesh.creation.icosphere(subdivisions=4, radius=20).export(source)

            result = simplify_mesh_measured(
                source,
                output,
                report,
                minimum_input_triangles=1_000,
                target_triangles=700,
                max_p95_deviation_mm=0.30,
                max_deviation_mm=0.60,
                max_volume_change_ratio=0.02,
                sample_points=2_000,
                aggression=5,
            )

            self.assertTrue(result["applied"])
            self.assertGreater(result["reduction_ratio"], 0.5)
            accepted = next(item for item in result["attempts"] if item["status"] == "passed")
            self.assertTrue(all(accepted["checks"].values()))
            mesh = trimesh.load_mesh(output, process=True)
            self.assertTrue(mesh.is_watertight)
            self.assertTrue(mesh.is_winding_consistent)

    def test_unsafe_reduction_retains_original(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.stl"
            output = root / "output.stl"
            report = root / "report.json"
            trimesh.creation.icosphere(subdivisions=3, radius=20).export(source)

            result = simplify_mesh_measured(
                source,
                output,
                report,
                minimum_input_triangles=500,
                target_triangles=100,
                max_p95_deviation_mm=1e-9,
                max_deviation_mm=2e-9,
                max_volume_change_ratio=1e-9,
                sample_points=1_000,
                aggression=5,
            )

            self.assertFalse(result["applied"])
            self.assertEqual(result["decision"], "no-safe-candidate-original-retained")
            self.assertEqual(sha256_file(source), sha256_file(output))

    def test_small_connected_bodies_are_preserved_during_reduction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.stl"
            output = root / "output.stl"
            report = root / "report.json"
            large = trimesh.creation.icosphere(subdivisions=4, radius=20)
            small_a = trimesh.creation.icosphere(subdivisions=1, radius=1)
            small_a.apply_translation((30, 0, 0))
            small_b = trimesh.creation.icosphere(subdivisions=1, radius=0.5)
            small_b.apply_translation((-30, 0, 0))
            trimesh.util.concatenate((large, small_a, small_b)).export(source)

            result = simplify_mesh_measured(
                source,
                output,
                report,
                minimum_input_triangles=1_000,
                target_triangles=800,
                max_p95_deviation_mm=0.30,
                max_deviation_mm=0.60,
                max_volume_change_ratio=0.02,
                sample_points=2_000,
                protected_component_triangles=100,
            )

            self.assertTrue(result["applied"])
            accepted = next(item for item in result["attempts"] if item["status"] == "passed")
            self.assertEqual(accepted["component_result"]["protected_components"], 2)
            mesh = trimesh.load_mesh(output, process=True)
            self.assertEqual(mesh.body_count, 3)
            self.assertTrue(mesh.is_watertight)


if __name__ == "__main__":
    unittest.main()
