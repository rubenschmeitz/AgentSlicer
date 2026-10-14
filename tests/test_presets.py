from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_slicer.presets import (
    PresetError,
    add_external_preset,
    load_catalog,
    recommend_settings,
    resolve_presets,
    validate_catalog_directory,
)


class PresetTests(unittest.TestCase):
    def test_resolves_mars_5_ultra_with_local_detail_profile(self) -> None:
        value = resolve_presets(
            process="resin",
            machine="elegoo-mars-5-ultra",
            material="sunlu-standard-mars-5-ultra",
            purpose="miniature-display-high-detail",
            quality="mars-5-ultra-0.05-high-detail",
            output_format="ctb",
        )
        self.assertEqual(value["machine"]["resolution"], [8520, 4320])
        self.assertEqual(value["machine"]["build_volume_mm"], [153.36, 77.76, 165.0])
        self.assertEqual(value["material"]["resin_slicer_profile"], "elegoo-mars-5-ultra-sunlu-standard")
        self.assertEqual(value["quality"]["layer_height_mm"], 0.05)
        self.assertTrue(value["purpose"]["geometry"]["hollow"])

    def test_resolves_machine_material_purpose_and_quality(self) -> None:
        value = resolve_presets(
            process="resin",
            machine="elegoo-mars-3-pro",
            material="ifun-if3121-toughness-grey",
            purpose="miniature-display-high-detail",
            quality="high-detail",
            output_format="ctb",
        )
        self.assertEqual(value["machine"]["resolution"], [4098, 2560])
        self.assertEqual(value["quality"]["layer_height_mm"], 0.03)
        self.assertTrue(value["purpose"]["geometry"]["hollow"])
        self.assertEqual(value["purpose"]["supports"]["max_feedback_contacts"], 360)
        simplification = value["purpose"]["geometry"]["simplification"]
        self.assertFalse(simplification["enabled"])
        self.assertEqual(value["purpose"]["geometry"]["repair_method"], "local-topology")
        self.assertEqual(simplification["target_triangles"], 350000)
        self.assertEqual(simplification["protected_component_triangles"], 5000)
        self.assertLessEqual(simplification["max_p95_deviation_mm"], 0.035)

    def test_rejects_unknown_output_format(self) -> None:
        with self.assertRaises(PresetError):
            resolve_presets(
                process="resin",
                machine="elegoo-mars-3-pro",
                material="ifun-if3121-toughness-grey",
                purpose="miniature-display-high-detail",
                quality="standard",
                output_format="goo",
            )

    def test_deep_merges_known_overrides_and_rejects_typos(self) -> None:
        value = resolve_presets(
            process="resin",
            machine="elegoo-mars-3-pro",
            material="anycubic-water-washable-2-grey",
            purpose="miniature-display-high-detail",
            quality="high-detail",
            output_format="ctb",
            overrides={"purpose": {"geometry": {"wall_mm": 2.0}}},
        )
        self.assertEqual(value["purpose"]["geometry"]["wall_mm"], 2.0)
        with self.assertRaisesRegex(PresetError, "unknown override path"):
            resolve_presets(
                process="resin",
                machine="elegoo-mars-3-pro",
                material="anycubic-water-washable-2-grey",
                purpose="miniature-display-high-detail",
                quality="high-detail",
                output_format="ctb",
                overrides={"purpose": {"geometry": {"wal_mm": 2.0}}},
            )

    def test_recommends_pinned_official_p2s_profile(self) -> None:
        value = recommend_settings(
            process="fdm",
            machine="bambu-lab-p2s-0.4",
            material="bambu-pla-basic-p2s",
            purpose="fdm-general",
            quality="p2s-0.20-standard",
            output_format="gcode.3mf",
            search_paths=[],
        )
        self.assertEqual(value["recommendation_status"], "evidence-backed-starting-point")
        self.assertEqual(value["production_status"], "calibration-required")
        self.assertEqual(value["settings"]["material"]["nozzle_temperature_c"], 220)
        self.assertEqual(value["settings"]["quality"]["outer_wall_line_width_mm"], 0.42)
        self.assertEqual(value["settings"]["quality"]["speeds_mm_s"]["outer_wall"], 200)
        self.assertEqual(
            value["upstream_profiles"]["machine"]["repository_commit"],
            "108e108635f2a223219dcf8f5e8fe3fce5ef40df",
        )
        self.assertTrue(value["calibration"]["required"])
        self.assertTrue(any("requires OrcaSlicer" in item for item in value["limitations"]))

    def test_rejects_cross_printer_material(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            add_external_preset(
                directory=temporary,
                catalog="machines",
                preset_id="other-fdm-printer",
                definition={
                    "process": "fdm",
                    "build_volume_mm": [100.0, 100.0, 100.0],
                    "output_formats": ["gcode"],
                    "default_output_format": "gcode",
                },
            )
            with self.assertRaisesRegex(PresetError, "not compatible"):
                resolve_presets(
                    process="fdm",
                    machine="other-fdm-printer",
                    material="bambu-pla-basic-p2s",
                    purpose="fdm-general",
                    quality="p2s-0.20-standard",
                    output_format="gcode",
                    search_paths=[temporary],
                )

    def test_rejects_resin_purpose_or_quality_for_p2s(self) -> None:
        for purpose, quality in (
            ("miniature-display-high-detail", "p2s-0.20-standard"),
            ("fdm-general", "standard"),
        ):
            with self.subTest(purpose=purpose, quality=quality):
                with self.assertRaisesRegex(PresetError, "is for 'resin'"):
                    resolve_presets(
                        process="fdm",
                        machine="bambu-lab-p2s-0.4",
                        material="bambu-pla-basic-p2s",
                        purpose=purpose,
                        quality=quality,
                        output_format="gcode.3mf",
                        search_paths=[],
                    )

    def test_external_catalog_add_validate_load_and_collision(self) -> None:
        definition = {
            "process": "fdm",
            "build_volume_mm": [100.0, 100.0, 100.0],
            "output_formats": ["gcode"],
            "default_output_format": "gcode",
        }
        with tempfile.TemporaryDirectory() as temporary:
            result = add_external_preset(
                directory=temporary,
                catalog="machines",
                preset_id="custom-printer",
                definition=definition,
            )
            self.assertEqual(result["status"], "added")
            catalog = load_catalog("machines", search_paths=[temporary])
            self.assertIn("custom-printer", catalog)
            validation = validate_catalog_directory(temporary)
            self.assertEqual(validation["status"], "passed")
            self.assertEqual(validation["entries"], 1)

            path = Path(temporary) / "machines.json"
            path.write_text(
                json.dumps({"elegoo-mars-3-pro": definition}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(PresetError, "collision"):
                load_catalog("machines", search_paths=[temporary])


if __name__ == "__main__":
    unittest.main()
