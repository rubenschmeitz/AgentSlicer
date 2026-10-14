import json
import tempfile
import unittest
from pathlib import Path

from agent_slicer.bambu_presets import BambuProfiles, definition
from agent_slicer.presets import PresetError, resolve_presets, validate_catalog_directory


class BambuImportTests(unittest.TestCase):
    def test_parent_include_child_vectors_and_privacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "filament").mkdir()
            profiles = {
                "base": {"filament_flow_ratio": ["0.95"], "pressure_advance": ["0.02"]},
                "template": {"filament_flow_ratio": ["0.96", "0.98"]},
                "child": {"inherits": "base", "include": ["template"],
                          "pressure_advance": ["0.03"], "filament_notes": "PRIVATE",
                          "filament_start_gcode": "PRIVATE"},
            }
            for name, data in profiles.items():
                (root / "filament" / (name + ".json")).write_text(json.dumps(data))
            importer = BambuProfiles(root)
            user = root / "user.json"
            user.write_text(json.dumps({"inherits": "child", "pressure_advance": ["0.04"],
                "access_code": "PRIVATE", "unknown_setting": "PRIVATE"}))
            settings, sources, excluded = importer.resolve("filament", "user", path=user)
            self.assertEqual(settings["filament_flow_ratio"], ["0.96", "0.98"])
            self.assertEqual(settings["pressure_advance"], ["0.04"])
            self.assertEqual([x["file"] for x in sources], ["base.json", "template.json", "child.json", "user.json"])
            self.assertEqual(set(excluded), {"access_code", "unknown_setting", "filament_start_gcode", "filament_notes"})
            self.assertNotIn("PRIVATE", json.dumps(settings))
            value = definition("filament", "user", settings, sources, excluded, "test", "1")
            self.assertIn("filament_flow_ratio", value["upstream_profile"]["unmapped_keys"])
            self.assertEqual(value["settings"]["pressure_advance"], 0.04)

    def test_cycle_and_missing_parent_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "process").mkdir()
            path = root / "process/a.json"
            path.write_text(json.dumps({"inherits": "a"}))
            with self.assertRaisesRegex(PresetError, "cycle"):
                BambuProfiles(root).resolve("process", "a")
            path.write_text(json.dumps({"inherits": "missing"}))
            with self.assertRaisesRegex(PresetError, "missing"):
                BambuProfiles(root).resolve("process", "a")

    def test_collected_catalog_saved_values_and_overrides(self):
        root = Path(__file__).resolve().parents[1] / "presets/bambu-work-pc-20261014"
        validate_catalog_directory(root)
        p = json.loads((root / "provenance.json").read_text())
        s = p["selected_ids"]
        config = resolve_presets(process="fdm", machine=s["machine"], material=s["materials"][0],
            purpose=s["purpose"], quality=s["quality"], output_format="gcode.3mf",
            search_paths=[root], overrides={"material": {"settings": {"flow_ratio": 0.97}}})
        self.assertEqual(config["material"]["settings"]["flow_ratio"], 0.97)
        self.assertEqual(config["material"]["upstream_profile"]["resolved_settings"]["filament_flow_ratio"], ["0.95", "0.95", "0.95"])
        self.assertEqual(config["material"]["settings"]["nozzle_temperature_c"], 250)
        self.assertEqual(config["quality"]["layer_height_mm"], 0.2)
        self.assertEqual(config["quality"]["upstream_profile"]["name"], "0.20mm High Quality @BBL P2S")
        qualities = json.loads((root / "qualities.json").read_text())
        custom_id = next(k for k, v in qualities.items() if "MAX DETAIL" in v["display_name"])
        self.assertEqual(qualities[custom_id]["layer_height_mm"], 0.06)
        self.assertIn("support_top_z_distance", qualities[custom_id]["upstream_profile"]["resolved_settings"])
        self.assertIn("tree_support_branch_distance", qualities[custom_id]["upstream_profile"]["resolved_settings"])
        materials = json.loads((root / "materials.json").read_text())
        overture = next(v for v in materials.values() if "OVERTURE" in v["display_name"])
        self.assertEqual(overture["settings"]["pressure_advance"], 0.02)
        self.assertEqual(len([v for v in materials.values() if v["upstream_profile"]["sources"][-1]["scope"] == "user"]), 7)
