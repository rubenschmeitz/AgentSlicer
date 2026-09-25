import unittest

from resin_slicer.mesh import cube_mesh
from resin_slicer.errors import ConfigError
from resin_slicer.transform import MeshTransform, apply_transform


class TransformTests(unittest.TestCase):
    def test_nonfinite_transform_is_rejected(self) -> None:
        for transform in (
            MeshTransform(scale=float("nan")),
            MeshTransform(rotate_x_deg=float("inf")),
            MeshTransform(translate_z_mm=float("-inf")),
        ):
            with self.subTest(transform=transform):
                with self.assertRaises(ConfigError):
                    apply_transform(cube_mesh(10), transform)

    def test_rotate_cube_changes_bounds(self) -> None:
        mesh = cube_mesh(10)
        rotated = apply_transform(mesh, MeshTransform(rotate_x_deg=45))
        bounds = rotated.bounds()
        self.assertGreater(bounds.height, 10)
        self.assertAlmostEqual(bounds.width, 10)

    def test_scale_cube(self) -> None:
        scaled = apply_transform(cube_mesh(10), MeshTransform(scale=0.5))
        self.assertAlmostEqual(scaled.bounds().width, 5)

    def test_scale_and_translation_carry_exact_cached_bounds(self) -> None:
        scaled = apply_transform(
            cube_mesh(10),
            MeshTransform(scale=0.5, translate_x_mm=7, translate_z_mm=-2),
        )
        bounds = scaled.bounds()
        self.assertAlmostEqual(bounds.width, 5)
        self.assertAlmostEqual(bounds.min_x, 9.5)
        self.assertAlmostEqual(bounds.max_x, 14.5)
        self.assertAlmostEqual(bounds.min_z, 0.5)
        self.assertAlmostEqual(bounds.max_z, 5.5)


if __name__ == "__main__":
    unittest.main()
