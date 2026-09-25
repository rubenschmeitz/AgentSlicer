import unittest

from resin_slicer.config import SupportConfig, profile
from resin_slicer.errors import ConfigError
from resin_slicer.mesh import cube_mesh
from resin_slicer.raster import LayerRaster
from resin_slicer.slicing import prepare_mesh
from resin_slicer.supports import (
    SupportAnchor, _PlacedPointIndex, apply_supports, attach_raft_masks,
    build_support_plan,
)


class SupportContactSafetyTests(unittest.TestCase):
    def test_contact_spacing_uses_physical_height(self):
        config = profile("small-test").with_overrides(layer_height_mm=0.05)
        index = _PlacedPointIndex(config)
        index.add(30, 30, 20)
        self.assertTrue(index.too_close(30, 30, 2.0, 21))
        self.assertFalse(index.too_close(30, 30, 2.0, 100))
        self.assertFalse(index.too_close(30, 30, 2.0, 500))

    def test_contact_overlap_is_small_bounded_and_not_added_to_trunks(self):
        config = profile("small-test").with_overrides(layer_height_mm=0.05)
        support = SupportConfig(tip_contact_depth_mm=0.1, bed_interface="none")
        for role in ("secondary", "manual-trunk"):
            anchor = SupportAnchor(20, 20, 40, joint_layer=20, role=role)
            plan = build_support_plan((anchor,), (), config, support)
            self.assertEqual(plan.contact_depth_layers, 2)
            for layer_index in (41, 42, 43):
                layer = LayerRaster(config.resolution_x, config.resolution_y)
                apply_supports(layer, layer_index, plan)
                expected = LayerRaster(config.resolution_x, config.resolution_y)
                if role != "manual-trunk" and layer_index <= 42:
                    expected.add_disk(20, 20, plan.tip_radius_px, 255)
                self.assertEqual(layer.pixels, expected.pixels)

    def test_raft_bottom_covers_offset_root_and_connecting_strip(self):
        config = profile("small-test")
        support = SupportConfig(foot_radius_mm=2.0, raft_margin_mm=1.0)
        prepared = prepare_mesh(cube_mesh(8).transformed((20, 20, 5)),
                                config, preserve_coordinates=True)
        anchor = SupportAnchor(23, 23, 20, base_x=10, base_y=23, joint_layer=15)
        plan = attach_raft_masks(build_support_plan((anchor,), (), config, support),
                                 prepared, config, support)
        layer = LayerRaster(config.resolution_x, config.resolution_y)
        apply_supports(layer, 0, plan)
        for x in range(10, 24):
            self.assertEqual(layer.pixels[23 * layer.width + x], 255)
        self.assertEqual(layer.pixels[24 * layer.width + 10], 255)

    def test_only_model_to_model_lower_contact_penetrates(self):
        config = profile("small-test").with_overrides(layer_height_mm=0.05)
        support = SupportConfig(tip_contact_depth_mm=0.1, bed_interface="none")
        for kind in ("enforcer", "branch", "bed"):
            anchor = SupportAnchor(30, 30, 60, base_x=20, base_y=20,
                                   base_layer=10, joint_layer=40, kind=kind)
            plan = build_support_plan((anchor,), (), config, support)
            for index in (7, 8, 9):
                actual = LayerRaster(config.resolution_x, config.resolution_y)
                expected = LayerRaster(config.resolution_x, config.resolution_y)
                actual.set_span(20,20,20)
                expected.set_span(20,20,20)
                apply_supports(actual, index, plan)
                if kind == "enforcer" and index >= 8:
                    expected.add_disk(20, 20, plan.tip_radius_px, 255)
                self.assertEqual(actual.pixels, expected.pixels)

    def test_lower_contact_cannot_start_a_floating_tip_below_model(self):
        config = profile("small-test").with_overrides(layer_height_mm=0.05)
        support = SupportConfig(tip_contact_depth_mm=0.1, bed_interface="none")
        anchor = SupportAnchor(30,30,60,base_x=20,base_y=20,base_layer=10,
                               joint_layer=40,kind="enforcer")
        plan = build_support_plan((anchor,), (), config, support)
        for index in (8,9):
            layer = LayerRaster(config.resolution_x, config.resolution_y)
            apply_supports(layer, index, plan)
            self.assertEqual(layer.count_on(), 0)
            self.assertEqual(layer.storage_mode, "sparse")

    def test_contact_depth_rejects_nonfinite_negative_and_excessive_values(self):
        for value in (-0.01, 0.51, float("nan"), float("inf")):
            with self.assertRaises(ConfigError):
                SupportConfig(tip_contact_depth_mm=value).validate()


if __name__ == "__main__":
    unittest.main()
