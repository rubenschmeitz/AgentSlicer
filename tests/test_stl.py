from __future__ import annotations

import tempfile
import struct
import unittest
from pathlib import Path

from agent_slicer.models import MeshInspection, Placement, SourceModel
from agent_slicer.project import sha256_file
from agent_slicer.stl import inspect_stl, pack_shelf, write_combined_binary_stl
from tests.helpers import write_cube


class StlTests(unittest.TestCase):
    def test_inspects_binary_cube(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cube.stl"
            write_cube(path, 10.0)
            inspection = inspect_stl(path)
            self.assertEqual(inspection.format, "binary-stl")
            self.assertEqual(inspection.triangle_count, 12)
            self.assertEqual(inspection.degenerate_triangles, 0)
            self.assertEqual(inspection.dimensions, (10.0, 10.0, 10.0))
            self.assertAlmostEqual(inspection.signed_volume_mm3, 1000.0)
            self.assertFalse(inspection.units_ambiguous)

    def test_scales_packs_and_combines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "cube.stl"
            write_cube(source, 10.0)
            inspection = inspect_stl(source)
            model = SourceModel(
                id="cube",
                source="cube.stl",
                sha256=sha256_file(source),
                target_height_mm=20.0,
            )
            placements = pack_shelf(
                root=root,
                models=[model],
                inspections=[inspection],
                plate_size_mm=(80.0, 45.0, 80.0),
                clearance_mm=2.0,
                margin_mm=2.0,
            )
            self.assertEqual(placements[0].scale, 2.0)
            output = root / "plate.stl"
            stats = write_combined_binary_stl(output, placements)
            self.assertEqual(stats["triangle_count"], 12)
            self.assertEqual(inspect_stl(output).dimensions, (20.0, 20.0, 20.0))

    def test_drops_triangle_that_collapses_during_float32_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "thin.stl"
            with source.open("wb") as handle:
                handle.write(b"quantization fixture".ljust(80, b"\0"))
                handle.write(struct.pack("<I", 1))
                handle.write(
                    struct.pack(
                        "<12fH",
                        0.0,
                        0.0,
                        1.0,
                        0.0,
                        0.0,
                        0.0,
                        5e-7,
                        0.0,
                        0.0,
                        0.0,
                        1.0,
                        0.0,
                        0,
                    )
                )
            output = root / "plate.stl"
            result = write_combined_binary_stl(
                output,
                [
                    Placement(
                        model_id="thin",
                        source=str(source),
                        scale=1.0,
                        translate=(60.0, 0.0, 0.0),
                        dimensions_mm=(5e-7, 1.0, 0.0),
                    )
                ],
            )
            self.assertEqual(result["triangle_count"], 0)
            self.assertEqual(result["removed_degenerate_triangles"], 1)

    def test_support_aware_pack_reserves_footprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "cube.stl"
            write_cube(source, 10.0)
            inspection = inspect_stl(source)
            model = SourceModel(
                id="cube",
                source="cube.stl",
                sha256=sha256_file(source),
            )
            placements = pack_shelf(
                root=root,
                models=[model],
                inspections=[inspection],
                plate_size_mm=(40.0, 40.0, 40.0),
                clearance_mm=2.0,
                margin_mm=2.0,
                support_footprint_margin_mm=3.0,
            )
            self.assertEqual(placements[0].translate[:2], (5.0, 5.0))

    def test_rotation_aware_pack_finds_feasible_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            models = [
                SourceModel(id="wide", source="wide.stl", sha256="a" * 64),
                SourceModel(id="deep", source="deep.stl", sha256="b" * 64),
            ]
            inspections = [
                MeshInspection(
                    path="wide.stl",
                    format="binary-stl",
                    triangle_count=1,
                    degenerate_triangles=0,
                    bounds_min=(0.0, 0.0, 0.0),
                    bounds_max=(30.0, 20.0, 10.0),
                    dimensions=(30.0, 20.0, 10.0),
                    signed_volume_mm3=1.0,
                    units_ambiguous=False,
                    sha256="a" * 64,
                ),
                MeshInspection(
                    path="deep.stl",
                    format="binary-stl",
                    triangle_count=1,
                    degenerate_triangles=0,
                    bounds_min=(0.0, 0.0, 0.0),
                    bounds_max=(20.0, 25.0, 10.0),
                    dimensions=(20.0, 25.0, 10.0),
                    signed_volume_mm3=1.0,
                    units_ambiguous=False,
                    sha256="b" * 64,
                ),
            ]
            placements = pack_shelf(
                root=root,
                models=models,
                inspections=inspections,
                plate_size_mm=(54.0, 38.0, 50.0),
                clearance_mm=2.0,
                margin_mm=2.0,
            )
            self.assertEqual(placements[0].rotate_z_deg, 90.0)
            self.assertEqual(placements[0].dimensions_mm[:2], (20.0, 30.0))
            self.assertEqual(placements[1].rotate_z_deg, 0.0)


if __name__ == "__main__":
    unittest.main()
