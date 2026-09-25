from __future__ import annotations

import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import trimesh

from agent_slicer.voxel_hollow import _reconstruct_shell, hollow_and_drain_voxel, _hollow_memory_preflight
from tests.helpers import write_cube


class VoxelHollowTests(unittest.TestCase):
    def test_memory_budget_rejects_before_grid_allocation(self):
        with patch.dict(os.environ, {"AGENT_SLICER_MAX_HOLLOW_MEMORY_MB": "16"}):
            with self.assertRaisesRegex(RuntimeError, "memory estimate"):
                _hollow_memory_preflight([100, 100, 100], 0.1)
            self.assertLess(_hollow_memory_preflight([4, 4, 4], 0.5)["estimated_grid_bytes"], 16 * 1024**2)
        for invalid in (float("nan"), float("inf"), 0):
            with self.assertRaises(ValueError):
                _hollow_memory_preflight([10, 10, 10], invalid)

    def test_hollowing_creates_two_independent_drains_and_no_sealed_cavity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "cube.stl"
            output = root / "cube-hollow.stl"
            report = root / "cube-hollow.json"
            write_cube(source, 12.0)

            result = hollow_and_drain_voxel(
                source,
                output,
                report,
                wall_mm=2.0,
                voxel_mm=0.5,
                drain_diameter_mm=3.0,
                max_outer_p95_change_mm=None,
                max_outer_change_mm=None,
            )

            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["sealed_cavity_voxels"], 0)
            self.assertTrue(result["cavity_connected_to_exterior"])
            self.assertTrue(result["outer_surface_preserved"])
            self.assertLessEqual(result["outer_surface_change"]["max_mm"], 1e-5)
            self.assertTrue(all(h["mesh_independently_connects_cavity"] for h in result["drain_holes"]))
            self.assertEqual(len(result["drain_holes"]), 2)
            self.assertTrue(
                all(
                    hole["independently_connects_cavity"]
                    for hole in result["drain_holes"]
                )
            )
            self.assertTrue(output.is_file())
            self.assertTrue(report.is_file())
            mesh = trimesh.load_mesh(output, process=True)
            self.assertTrue(mesh.is_watertight)
            self.assertTrue(mesh.is_winding_consistent)

    def test_curved_outer_surface_survives_a_coarse_cavity_grid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sphere.stl"
            sphere = trimesh.creation.icosphere(subdivisions=3, radius=6)
            sphere.export(source)
            result = hollow_and_drain_voxel(source, root / "hollow.stl", root / "report.json",
                                           wall_mm=1.2, voxel_mm=.3, drain_diameter_mm=1.5)
            self.assertLessEqual(result["outer_surface_change"]["max_mm"], 1e-5)
            self.assertLessEqual(result["outer_bounds_expansion_mm"], 1e-5)
            bounds = np.asarray(result["mesh_validation"]["bounds_mm"])
            np.testing.assert_allclose(bounds[:, :2], sphere.bounds[:, :2], atol=1e-5)
            self.assertGreaterEqual(bounds[0, 2], sphere.bounds[0, 2])
            self.assertEqual(result["surface_reconstruction"]["scope"], "internal-cavity-only")

    def test_elevated_body_drains_do_not_cut_the_base_below_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            body = trimesh.creation.box(extents=(12, 12, 12))
            body.apply_translation((0, 0, 20))
            base = trimesh.creation.box(extents=(14, 14, 1))
            stem = trimesh.creation.cylinder(radius=1, height=14, sections=32)
            stem.apply_translation((0, 0, 7))
            mesh = trimesh.boolean.union([body, base, stem], engine="manifold")
            mesh.export(root / "raised.stl")
            result = hollow_and_drain_voxel(root / "raised.stl", root / "hollow.stl", root / "report.json",
                                           wall_mm=2, voxel_mm=.5, drain_diameter_mm=3)
            self.assertTrue(all(h["cutter_bottom_z_mm"] > 13 for h in result["drain_holes"]))
            output = trimesh.load_mesh(root / "hollow.stl")
            baseline_section = mesh.section(plane_origin=[0, 0, 0], plane_normal=[0, 0, 1])
            result_section = output.section(plane_origin=[0, 0, 0], plane_normal=[0, 0, 1])
            self.assertAlmostEqual(baseline_section.length, result_section.length, places=5)

    def test_hollowing_rejects_an_undersampled_wall(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "cube.stl"
            write_cube(source, 12.0)
            with self.assertRaisesRegex(ValueError, "at least three"):
                hollow_and_drain_voxel(
                    source,
                    root / "output.stl",
                    root / "report.json",
                    wall_mm=1.0,
                    voxel_mm=0.5,
                )

    def test_subvoxel_recess_is_not_erased_by_cavity_fill(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            body = trimesh.creation.box(extents=(20, 20, 20))
            # A narrow open slot gets sealed by voxel filling. Its source
            # surfaces must nevertheless retain a wall against the cavity.
            slot = trimesh.creation.box(extents=(0.2, 8, 12))
            slot.apply_translation((0.25, 0, 6))
            source = trimesh.boolean.difference([body, slot], engine="manifold")
            source.export(root / "slotted.stl")
            result = hollow_and_drain_voxel(
                root / "slotted.stl", root / "hollow.stl", root / "report.json",
                wall_mm=2, voxel_mm=0.5, drain_diameter_mm=3)
            self.assertTrue(result["outer_surface_preserved"])
            self.assertLessEqual(result["outer_surface_change"]["max_mm"], 1e-5)
            self.assertTrue(result["mesh_validation"]["watertight"])

    def test_reconstruction_disambiguates_a_nonmanifold_direct_result(self) -> None:
        shell = np.zeros((8, 8, 8), dtype=bool)
        shell[2:6, 2:6, 2:6] = True
        open_triangle = trimesh.Trimesh(
            vertices=np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=float),
            faces=np.array([[0, 1, 2]], dtype=np.int64),
            process=False,
        )
        with patch(
            "agent_slicer.voxel_hollow.trimesh.voxel.ops.matrix_to_marching_cubes",
            return_value=open_triangle,
        ):
            mesh, report = _reconstruct_shell(shell, pitch_mm=0.5)

        self.assertTrue(report["topology_disambiguation_applied"])
        self.assertEqual(report["gaussian_sigma_voxels"], 0.35)
        self.assertTrue(mesh.is_watertight)
        self.assertTrue(mesh.is_winding_consistent)


if __name__ == "__main__":
    unittest.main()
