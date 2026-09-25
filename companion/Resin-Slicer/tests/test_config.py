import unittest

from resin_slicer.config import SupportConfig, profile
from resin_slicer.errors import ConfigError


class ConfigTests(unittest.TestCase):
    def test_nonfinite_print_and_support_values_are_rejected(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(print_value=value):
                with self.assertRaises(ConfigError):
                    profile("small-test").with_overrides(layer_height_mm=value)
            with self.subTest(support_value=value):
                with self.assertRaises(ConfigError):
                    SupportConfig(support_spacing_mm=value).validate()

    def test_integer_safety_fields_reject_bools_and_floats(self) -> None:
        with self.assertRaisesRegex(ConfigError, "resolution_x must be an integer"):
            profile("small-test").with_overrides(resolution_x=True)
        with self.assertRaisesRegex(
            ConfigError, "max_supports_per_island must be an integer"
        ):
            SupportConfig(max_supports_per_island=12.5).validate()

    def test_resin_tree_limits_are_fail_closed(self) -> None:
        with self.assertRaisesRegex(ConfigError, "tree_max_tips must be positive"):
            SupportConfig(tree_max_tips=0).validate()
        with self.assertRaisesRegex(
            ConfigError, "tree_trunk_radius_mm cannot be smaller"
        ):
            SupportConfig(post_radius_mm=0.4, tree_trunk_radius_mm=0.3).validate()

    def test_two_stage_motion_requires_a_speed_for_each_used_segment(self) -> None:
        with self.assertRaisesRegex(ConfigError, "lift_speed2_mm_min"):
            profile("small-test").with_overrides(
                lift_distance2_mm=2.0,
                lift_speed2_mm_min=0.0,
            )
        with self.assertRaisesRegex(ConfigError, "retract_speed2_mm_min"):
            profile("small-test").with_overrides(
                retract_distance2_mm=2.0,
                retract_speed2_mm_min=0.0,
            )

    def test_mars_3_pro_resin_profiles_match_supplied_configs(self) -> None:
        expected = {
            "elegoo-mars-3-pro-anycubic-water-washable-2-grey": (
                "ANYCUBIC Water-Washable Resin 2.0 Grey", 2.5, 30.0, 90.0
            ),
            "elegoo-mars-3-pro-photocentric-uv-lcd-hard-black": (
                "Photocentric UV LCD Hard Black", 4.3, 38.0, 90.0
            ),
            "elegoo-mars-3-pro-ifun-if3121-toughness-grey": (
                "iFun iF3121 LCD/DLP Toughness Grey", 2.5, 32.0, 100.0
            ),
        }
        for name, (resin_name, exposure, bottom_exposure, lift_speed) in expected.items():
            with self.subTest(profile=name):
                config = profile(name)
                self.assertEqual((config.resolution_x, config.resolution_y), (4098, 2560))
                self.assertEqual((config.size_x_mm, config.size_y_mm, config.size_z_mm), (143.43, 89.6, 175.0))
                self.assertEqual(config.machine_name, "ELEGOO Mars 3 Pro")
                self.assertEqual(config.resin_name, resin_name)
                self.assertEqual(config.layer_height_mm, 0.05)
                self.assertEqual(config.exposure_time_s, exposure)
                self.assertEqual(config.bottom_exposure_time_s, bottom_exposure)
                self.assertEqual(config.bottom_layers, 5)
                self.assertEqual(config.transition_layers, 5)
                self.assertEqual(config.lift_distance_mm, 5.0)
                self.assertEqual(config.lift_speed_mm_min, lift_speed)
                self.assertEqual(config.bottom_lift_speed_mm_min, 70.0)
                self.assertEqual(config.retract_speed_mm_min, 150.0)
                self.assertEqual(config.wait_after_retract_s, 0.8)
                self.assertEqual(config.rest_before_lift_s, 0.5)

    def test_mars_5_ultra_profiles_match_local_machine_and_start_settings(self) -> None:
        expected = {
            "elegoo-mars-5-ultra": ("Standard (Mars 5 Ultra)", 2.5, 1.1),
            "elegoo-mars-5-ultra-sunlu-standard": ("SUNLU Standard", 2.5, 1.1),
            "elegoo-mars-5-ultra-sunlu-high-tough-toughness": (
                "SUNLU High-Tough / Toughness", 2.8, 1.1
            ),
        }
        for name, (resin_name, exposure, density) in expected.items():
            with self.subTest(profile=name):
                config = profile(name)
                self.assertEqual((config.resolution_x, config.resolution_y), (8520, 4320))
                self.assertEqual((config.size_x_mm, config.size_y_mm, config.size_z_mm), (153.36, 77.76, 165.0))
                self.assertEqual(config.machine_name, "ELEGOO Mars 5 Ultra")
                self.assertEqual(config.resin_name, resin_name)
                self.assertEqual(config.layer_height_mm, 0.05)
                self.assertEqual(config.exposure_time_s, exposure)
                self.assertEqual(config.resin_density_g_ml, density)
                self.assertEqual(config.bottom_exposure_time_s, 30.0)
                self.assertEqual(config.bottom_layers, 4)
                self.assertEqual(config.transition_layers, 5)
                self.assertEqual(config.lift_distance_mm, 0.0)
                self.assertEqual(config.retract_distance_mm, 0.0)
                self.assertEqual(config.wait_after_retract_s, 0.5)

    def test_jupiter_2_16k_profile_validates(self) -> None:
        config = profile("elegoo-jupiter-2-16k")

        self.assertEqual(config.resolution_x, 15120)
        self.assertEqual(config.resolution_y, 6230)
        self.assertEqual(config.resolution_x * config.resolution_y, 94_197_600)
        self.assertGreaterEqual(config.max_pixels_per_layer, config.resolution_x * config.resolution_y)

    def test_default_limit_allows_jupiter_2_16k_resolution_override(self) -> None:
        config = profile("generic-2k").with_overrides(
            resolution_x=15120,
            resolution_y=6230,
            size_x_mm=302.40,
            size_y_mm=161.98,
            size_z_mm=300.0,
        )

        self.assertEqual(config.max_pixels_per_layer, 100_000_000)


if __name__ == "__main__":
    unittest.main()
