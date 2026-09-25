import json
import tempfile
import unittest
from pathlib import Path

from agent_slicer.support_audit import audit_planned_drains, audit_encoded_drain


class DrainScreenTests(unittest.TestCase):
    def fixtures(self, root, blocked=False, lift=5):
        plan = {"layer_height_mm": .05, "pixel_size_x_mm": .1, "pixel_size_y_mm": .1,
                "post_radius_px": 50, "tree_trunk_radius_px": 50, "tip_radius_px": 1,
                "raft_layers": 8, "pedestal_height_layers": 40, "anchors": [], "braces": []}
        if blocked:
            plan["anchors"].append({"x": 100, "y": 100, "base_x": None, "base_y": None,
                "joint_x": None, "joint_y": None, "joint_layer": 200, "top_layer": 240,
                "base_layer": 0, "kind": "bed", "load_count": 1})
        cache = {"plan": plan, "identity": {"print_config": {"center_model": True,
                 "size_x_mm": 20, "size_y_mm": 20}, "support_config": {"model_lift_mm": lift}}}
        geometry = {"status": "passed", "export_validation": {"status": "passed",
                    "bounds_mm": [[-4,-4,0], [4,4,20]]}, "stages": {"hollow_and_drain":
                    {"drain_holes": [{"index": 1, "center_world_xy_mm": [0,0],
                    "diameter_mm": 4, "cutter_bottom_z_mm": .1, "cutter_top_z_mm": 2}]}}}
        cache_path, geometry_path = root / "cache.json", root / "geometry.json"
        cache_path.write_text(json.dumps(cache))
        geometry_path.write_text(json.dumps(geometry))
        return cache_path, geometry_path

    def test_clear_bore_has_finite_width_continuous_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = audit_planned_drains(*self.fixtures(Path(tmp)))
            self.assertEqual(result["status"], "screen-passed")
            self.assertEqual(result["holes"][0]["continuous_channel_diameter_mm"], 1)

    def test_support_filling_bore_is_not_certified_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = audit_planned_drains(*self.fixtures(Path(tmp), blocked=True))
            self.assertEqual(result["status"], "needs-review")
            self.assertEqual(result["holes"][0]["min_free_cross_section_mm2"], 0)

    def test_raft_area_requires_actual_layer_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "raft"):
                audit_planned_drains(*self.fixtures(Path(tmp), lift=.2))


class EncodedDrainTests(unittest.TestCase):
    def fixture(self, root, blocked=False):
        from PIL import Image
        paths = []
        for index in range(3):
            path = root / f"{index}.png"
            Image.new("L", (60,60), 255 if blocked and index == 1 else 0).save(path)
            paths.append(str(path))
        evidence = root / "evidence.json"
        evidence.write_text(json.dumps({"schema": "agent-slicer-layer-evidence/v1",
            "scale": 1, "crop_pixels": [0,0,60,60], "indices_zero_based": [0,1,2],
            "files": paths, "source_sha256": "test"}))
        return evidence

    def audit(self, path, **kwargs):
        return audit_encoded_drain(path, center_mm=(3,3), diameter_mm=4,
                                   first_layer=0, last_layer=2, pixel_mm=.1, **kwargs)

    def test_empty_bore_is_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.audit(self.fixture(Path(tmp)))
            self.assertTrue(result["continuous_channel"])
            self.assertGreater(result["straight_channel_diameter_lower_bound_mm"], 3)

    def test_single_blocked_layer_prevents_clearance(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(self.audit(self.fixture(Path(tmp), True))["continuous_channel"])

    def test_incomplete_or_scaled_evidence_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.fixture(Path(tmp))
            data = json.loads(path.read_text())
            data["scale"] = 2
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "unscaled"):
                self.audit(path)
            data["scale"] = 1
            data["indices_zero_based"].pop()
            data["files"].pop()
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "Missing"):
                self.audit(path)
