"""Opt-in real native Blender regression tests, never mocked as passed."""
import os
import json
import tempfile
import unittest
from pathlib import Path

from agent_slicer.blender_adapter import prepare_resin_model
from agent_slicer.benchmark import sha256
from tests.helpers import write_cube


@unittest.skipUnless(os.environ.get("AGENT_SLICER_TEST_BLENDER"), "set AGENT_SLICER_TEST_BLENDER to a real executable")
class NativeBlenderIntegrationTests(unittest.TestCase):
    def test_valid_convex_solid_preserves_geometry_and_can_be_hollowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "cube.stl"
            write_cube(source, 12)
            original = sha256(source)
            result = prepare_resin_model(source, root / "hollow.stl", root / "report.json",
                                         orient=False, blender_path=os.environ["AGENT_SLICER_TEST_BLENDER"], timeout=120)
            repair = result["stages"]["repair_and_orientation"]["stages"]["repair"]
            self.assertEqual(repair["method"], "local-topology-repair")
            self.assertFalse(repair["lossy"])
            self.assertEqual(repair["before"]["faces"], repair["after"]["faces"])
            hollow = result["stages"]["hollow_and_drain"]
            self.assertEqual(hollow["sealed_cavity_voxels"], 0)
            self.assertTrue(all(h["independently_connects_cavity"] for h in hollow["drain_holes"]))
            self.assertEqual(sha256(source), original)

    def test_inward_normals_are_fixed_without_remeshing(self):
        import trimesh
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mesh = trimesh.creation.box(extents=(12, 12, 12))
            mesh.invert()
            source = root / "inverted.stl"
            mesh.export(source)
            result = prepare_resin_model(source, root / "solid.stl", root / "report.json",
                                         orient=False, hollow=False, drain=False,
                                         blender_path=os.environ["AGENT_SLICER_TEST_BLENDER"], timeout=120)
            repair = result["stages"]["repair_and_orientation"]["stages"]["repair"]
            self.assertEqual(repair["method"], "local-topology-repair")
            self.assertFalse(repair["remeshed"])
            self.assertEqual(repair["before"]["faces"], repair["after"]["faces"])
            self.assertGreater(trimesh.load_mesh(root / "solid.stl").volume, 0)

    def _repair_mesh(self, mesh, root):
        source = root / "source.stl"
        mesh.export(source)
        original = sha256(source)
        result = prepare_resin_model(source, root / "fixed.stl", root / "report.json",
                                     orient=False, hollow=False, drain=False,
                                     blender_path=os.environ["AGENT_SLICER_TEST_BLENDER"], timeout=180)
        self.assertEqual(sha256(source), original)
        return result["stages"]["repair_and_orientation"]["stages"]["repair"]

    def test_detailed_curved_surface_is_retained_even_with_flipped_faces(self):
        import numpy as np
        import trimesh
        for inverted in (False, True):
            with self.subTest(inverted=inverted), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                mesh = trimesh.creation.icosphere(subdivisions=4, radius=5)
                if inverted:
                    mesh.faces[10] = mesh.faces[10][::-1]
                report = self._repair_mesh(mesh, root)
                result = trimesh.load_mesh(root / "fixed.stl")
                source = trimesh.load_mesh(root / "source.stl")
                self.assertEqual(len(result.faces), len(source.faces))
                self.assertTrue(result.is_watertight and result.is_winding_consistent)
                self.assertFalse(report["lossy"])
                a, b = np.unique(source.vertices, axis=0), np.unique(result.vertices, axis=0)
                np.testing.assert_array_equal(a, b)

    def test_small_missing_triangle_is_patched(self):
        import trimesh
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mesh = trimesh.creation.box(extents=(.1, .1, .1))
            mesh.update_faces(list(range(1, len(mesh.faces))))
            report = self._repair_mesh(mesh, root)
            result = trimesh.load_mesh(root / "fixed.stl")
            self.assertTrue(result.is_watertight)
            self.assertEqual(len(result.faces), 12)
            self.assertEqual(report["faces_added"], 1)

    def test_nested_cavity_winding_is_preserved(self):
        import trimesh
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outer = trimesh.creation.box(extents=(10, 10, 10))
            inner = trimesh.creation.box(extents=(6, 6, 6))
            inner.invert()
            report = self._repair_mesh(trimesh.util.concatenate([outer, inner]), root)
            result = trimesh.load_mesh(root / "fixed.stl")
            self.assertAlmostEqual(result.volume, 1000 - 216, places=4)
            self.assertEqual(report["normals"]["nested_cavity_shells"], 1)

    def test_large_hole_fails_without_reconstruction_or_output_replacement(self):
        import trimesh
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mesh = trimesh.creation.box(extents=(10, 10, 10))
            mesh.update_faces(list(range(1, len(mesh.faces))))
            existing = root / "fixed.stl"
            existing.write_bytes(b"existing user output")
            with self.assertRaisesRegex(RuntimeError, "needs review"):
                self._repair_mesh(mesh, root)
            self.assertEqual(existing.read_bytes(), b"existing user output")
            self.assertEqual(json.loads((root / "report.json").read_text())["status"], "failed")
